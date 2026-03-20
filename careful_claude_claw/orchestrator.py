"""Orchestrator — routes requests, injects memory context, triggers extraction.

The orchestrator is a Python routing layer (not a persistent Claude session).
Claude is only called for memory extraction/consolidation — routing is deterministic.
"""

import logging
import re
from collections.abc import Awaitable, Callable

from .memory import (
    add_memory,
    get_relevant_memories,
    list_memories,
    search_memories,
)
from .memory_extraction import extract_and_store, summarize_session
from .models import (
    Execution,
    Memory,
    MemorySource,
    MemoryType,
    OrchestratorResult,
    RequestType,
)

logger = logging.getLogger(__name__)

MessageCallback = Callable[[str], Awaitable[None]]

MEMORY_CONTEXT_HEADER = """
--- Memory Context ---
The following is relevant context from previous interactions:

{memories}
--- End Memory Context ---
"""


class Orchestrator:
    """Routes requests, enriches context with memories, triggers post-task extraction."""

    def classify_request(self, text: str) -> RequestType:
        """Classify a request deterministically based on patterns."""
        text_stripped = text.strip()

        # /command → COMMAND
        if text_stripped.startswith("/"):
            return RequestType.COMMAND

        # @name → FOLLOW_UP
        if text_stripped.startswith("@"):
            return RequestType.FOLLOW_UP

        # "remember that..." patterns
        remember_patterns = [
            r"^remember\s+that\b",
            r"^remember:\s*",
            r"^save\s+(?:this|that)\s+(?:memory|preference|fact)\b",
            r"^note\s+that\b",
        ]
        text_lower = text_stripped.lower()
        for pattern in remember_patterns:
            if re.match(pattern, text_lower):
                return RequestType.MEMORY_ADD

        # "what do you know/remember about..." patterns
        query_patterns = [
            r"^what\s+do\s+you\s+(?:know|remember)\s+about\b",
            r"^do\s+you\s+(?:know|remember)\b",
            r"^recall\b",
            r"^what\s+(?:are\s+)?my\s+preferences?\b",
            r"^search\s+(?:your\s+)?memor",
        ]
        for pattern in query_patterns:
            if re.match(pattern, text_lower):
                return RequestType.MEMORY_QUERY

        # Everything else → TASK
        return RequestType.TASK

    def retrieve_context(self, task: str) -> str:
        """Retrieve relevant memories and format them as context."""
        memories = get_relevant_memories(task, limit=10)
        if not memories:
            return ""

        lines = []
        for m in memories:
            prefix = f"[{m.memory_type}]"
            if m.category:
                prefix += f" ({m.category})"
            lines.append(f"{prefix}: {m.content}")

        return MEMORY_CONTEXT_HEADER.format(memories="\n".join(lines))

    def build_system_prompt(self, base_prompt: str | None, memory_context: str) -> str | None:
        """Append memory context to the base system prompt."""
        if not memory_context:
            return base_prompt
        if base_prompt:
            return f"{base_prompt}\n\n{memory_context}"
        return memory_context

    async def handle_memory_add(self, text: str) -> str:
        """Parse and store an explicit memory from user text."""
        # Strip common prefixes
        content = text.strip()
        for prefix in [
            "remember that ",
            "remember: ",
            "save this memory: ",
            "save this preference: ",
            "save this fact: ",
            "note that ",
        ]:
            if content.lower().startswith(prefix):
                content = content[len(prefix) :]
                break

        # Detect type from keywords
        memory_type = MemoryType.FACT
        content_lower = content.lower()
        if any(w in content_lower for w in ["prefer", "like to", "always", "never", "style"]):
            memory_type = MemoryType.PREFERENCE

        memory = Memory(
            memory_type=memory_type,
            content=content,
            source=MemorySource.USER,
        )
        add_memory(memory)
        return f"Remembered: {content}"

    async def handle_memory_query(self, query: str) -> str:
        """Search memories and return formatted results."""
        # Strip common prefixes
        search_text = query.strip()
        for prefix in [
            "what do you know about ",
            "what do you remember about ",
            "do you know ",
            "do you remember ",
            "recall ",
            "what are my preferences",
            "search memory for ",
            "search memories for ",
        ]:
            if search_text.lower().startswith(prefix):
                search_text = search_text[len(prefix) :]
                break

        if not search_text.strip():
            # No specific query — list all
            memories = list_memories(limit=20)
        else:
            memories = search_memories(query=search_text, limit=20)

        if not memories:
            return "No memories found."

        lines = [f"Found {len(memories)} memory(ies):"]
        for m in memories:
            prefix = f"[{m.memory_type}]"
            if m.category:
                prefix += f" ({m.category})"
            lines.append(f"  {prefix}: {m.content}")

        return "\n".join(lines)

    async def handle_request(
        self,
        text: str,
        on_message: MessageCallback | None = None,
    ) -> OrchestratorResult:
        """Main entry point: classify, enrich, delegate.

        For COMMAND and FOLLOW_UP types, returns immediately with the
        request type — the caller handles routing to CommandRouter / send_to_agent.

        For MEMORY_ADD and MEMORY_QUERY, handles directly and returns response.

        For TASK, retrieves context but does NOT delegate — returns the
        enriched system prompt in the response field for the caller to use.
        """
        request_type = self.classify_request(text)

        if request_type == RequestType.COMMAND:
            return OrchestratorResult(request_type=request_type)

        if request_type == RequestType.FOLLOW_UP:
            return OrchestratorResult(request_type=request_type)

        if request_type == RequestType.MEMORY_ADD:
            response = await self.handle_memory_add(text)
            if on_message:
                await on_message(response)
            return OrchestratorResult(request_type=request_type, response=response)

        if request_type == RequestType.MEMORY_QUERY:
            response = await self.handle_memory_query(text)
            if on_message:
                await on_message(response)
            return OrchestratorResult(request_type=request_type, response=response)

        # TASK — retrieve context
        memory_context = self.retrieve_context(text)
        return OrchestratorResult(
            request_type=request_type,
            response=memory_context,
        )

    async def on_task_complete(
        self,
        execution: Execution,
        output: str | None = None,
    ) -> None:
        """Post-task hook: extract memories from task output in the background.

        Should be called via asyncio.create_task() to avoid blocking.
        """
        text = output or execution.output
        if not text:
            return

        try:
            await extract_and_store(text, source_id=execution.id)
        except Exception:
            logger.exception("Post-task memory extraction failed for %s", execution.id)

    async def on_session_complete(
        self,
        session_name: str,
        output: str,
        execution: Execution | None = None,
    ) -> None:
        """Post-session hook: summarize session and extract memories."""
        try:
            summary = await summarize_session(session_name, output)
            if summary:
                add_memory(summary)

            source_id = execution.id if execution else None
            await extract_and_store(output, source_id=source_id)
        except Exception:
            logger.exception("Post-session extraction failed for %s", session_name)
