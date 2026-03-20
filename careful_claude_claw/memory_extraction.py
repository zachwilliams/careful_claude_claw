"""LLM-powered memory extraction and consolidation.

Uses Claude (via the Anthropic SDK) to extract structured memories
from conversation text and consolidate them with existing memories.
"""

import json
import logging
from datetime import datetime

import anthropic

from .memory import add_memory, search_memories
from .models import Memory, MemorySource, MemoryType

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
Analyze the following text and extract any persistent memories worth saving.
Memories should be things that would be useful in future conversations.

Categories of memories to extract:
- **preference**: User preferences, working style, tool choices
- **fact**: Facts about the user, their projects, or their environment
- **summary**: High-level summaries of what was accomplished
- **task_context**: Context about ongoing work that may be referenced later

For each memory, provide:
- memory_type: one of "preference", "summary", "fact", "task_context"
- content: the memory text (concise but complete)
- category: a short category label (e.g. "coding_style", "project_pref", "tool_choice")
- tags: list of relevant keyword tags

Return a JSON array of memories. If nothing is worth remembering, return [].

Text to analyze:
{text}
"""

CONSOLIDATION_PROMPT = """\
You are deciding how to handle a new memory in relation to existing memories.

New memory:
{new_memory}

Existing related memories:
{existing_memories}

Decide one of:
- "add": The new memory is novel, keep it as-is
- "merge": The new memory should be merged with an existing one
  (specify which by id and provide merged content)
- "discard": The new memory is a duplicate of an existing one

Return JSON:
{{"action": "add"|"merge"|"discard",
  "merge_target_id": "..." (if merge),
  "merged_content": "..." (if merge),
  "reason": "..."}}
"""

SESSION_SUMMARY_PROMPT = """\
Summarize the following agent session output into a concise memory.
Focus on: what was accomplished, key decisions made, and any important context for future reference.

Session name: {session_name}
Output:
{messages}

Return a single concise summary paragraph.
"""


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


async def extract_memories(
    text: str,
    source_id: str | None = None,
) -> list[Memory]:
    """Extract structured memories from text using Claude.

    Returns a list of Memory objects ready to be saved.
    """
    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(text=text)}],
        )

        # Parse the response
        response_text = response.content[0].text
        # Try to extract JSON from the response
        memories_data = _parse_json_response(response_text)
        if not memories_data:
            return []

        memories = []
        for item in memories_data:
            try:
                memory = Memory(
                    memory_type=MemoryType(item["memory_type"]),
                    content=item["content"],
                    source=MemorySource.EXTRACTION,
                    source_id=source_id,
                    tags=item.get("tags", []),
                    category=item.get("category"),
                )
                memories.append(memory)
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed extracted memory: %s", e)

        return memories

    except Exception:
        logger.exception("Memory extraction failed")
        return []


async def consolidate_memories(
    new_memory: Memory,
    existing_memories: list[Memory],
) -> Memory | None:
    """Decide whether to add, merge, or discard a new memory.

    Returns the memory to save (possibly merged), or None if discarded.
    """
    if not existing_memories:
        return new_memory

    try:
        client = _get_client()
        existing_str = "\n".join(
            f"- [{m.id}] ({m.memory_type}): {m.content}" for m in existing_memories
        )
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": CONSOLIDATION_PROMPT.format(
                        new_memory=f"({new_memory.memory_type}): {new_memory.content}",
                        existing_memories=existing_str,
                    ),
                }
            ],
        )

        result = _parse_json_response(response.content[0].text)
        if not result or not isinstance(result, dict):
            return new_memory

        action = result.get("action", "add")
        if action == "discard":
            logger.info("Discarding duplicate memory: %s", new_memory.content[:50])
            return None
        elif action == "merge":
            target_id = result.get("merge_target_id")
            merged_content = result.get("merged_content", new_memory.content)
            if target_id:
                # Update the existing memory with merged content
                for m in existing_memories:
                    if m.id == target_id:
                        m.content = merged_content
                        m.updated_at = datetime.now()
                        return m
            # Fallback: return new memory with merged content
            new_memory.content = merged_content
            return new_memory
        else:
            return new_memory

    except Exception:
        logger.exception("Memory consolidation failed")
        return new_memory


async def summarize_session(
    session_name: str,
    messages: str,
) -> Memory | None:
    """Generate a summary memory from session output."""
    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": SESSION_SUMMARY_PROMPT.format(
                        session_name=session_name,
                        messages=messages[:4000],  # Truncate to fit context
                    ),
                }
            ],
        )

        summary_text = response.content[0].text.strip()
        return Memory(
            memory_type=MemoryType.SUMMARY,
            content=summary_text,
            source=MemorySource.EXTRACTION,
            tags=["session_summary", session_name],
            category="session",
        )

    except Exception:
        logger.exception("Session summarization failed")
        return None


async def extract_and_store(
    text: str,
    source_id: str | None = None,
) -> list[Memory]:
    """Extract memories from text and store them, with consolidation.

    This is the main entry point for post-task memory extraction.
    Runs extraction, then consolidates each memory against existing ones.
    """
    extracted = await extract_memories(text, source_id)
    stored: list[Memory] = []

    for memory in extracted:
        # Search for related existing memories
        existing = search_memories(
            query=memory.content,
            memory_type=memory.memory_type,
            limit=5,
        )
        # Consolidate
        result = await consolidate_memories(memory, existing)
        if result:
            if result.id in {m.id for m in existing}:
                # It's a merged existing memory — update it
                from .memory import update_memory

                update_memory(result)
            else:
                add_memory(result)
            stored.append(result)

    return stored


def _parse_json_response(text: str) -> list | dict | None:
    """Extract JSON from an LLM response, handling markdown code blocks."""
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    if "```" in text:
        parts = text.split("```")
        for part in parts[1::2]:  # odd-indexed parts are inside code blocks
            # Strip optional language tag
            lines = part.strip().split("\n", 1)
            content = lines[1] if len(lines) > 1 else lines[0]
            try:
                return json.loads(content.strip())
            except json.JSONDecodeError:
                continue

    return None
