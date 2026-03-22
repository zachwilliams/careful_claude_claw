"""Persistent Orchestrator — a long-lived Claude agent session with memory MCP tools.

Replaces the stateless Orchestrator for interactive use cases.
Messages from all interfaces feed into a single queue, and the orchestrator
processes them one at a time through its persistent ClaudeSDKClient session.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from .db import (
    get_orchestrator_state,
    increment_message_count,
    init_db,
    upsert_orchestrator_state,
)
from .memory import get_relevant_memories, list_memories
from .memory_tools import create_orchestrator_mcp_server, set_message_callback
from .models import MemoryType, OrchestratorState, score_memory

logger = logging.getLogger(__name__)

MessageCallback = Callable[[str], Awaitable[None]]

MAX_BRIEFING_TOKENS = 2000  # approximate char limit for core briefing

MEMORY_CONTEXT_HEADER = """
--- Memory Context ---
{memories}
--- End Memory Context ---
"""

SYSTEM_PROMPT = """\
You are 小火苗 (Miao), a persistent AI assistant with memory and sub-agent capabilities.

## Memory
Relevant memories are auto-injected into each message. Use `memory_write` to store
new memories and `memory_update` to revise existing ones.
- `preference`: permanent, high weight — user preferences
- `decision`: slow decay — key decisions
- `observation`: decays over weeks — facts about user/project
- `procedure`: permanent — workflows and processes
- Set importance 0.0-1.0; use categories and tags for organization

## Sub-agents
Use `spawn_agent` for coding/file tasks. Manage with `list_agents`, `kill_agent`, `send_to_agent`.

## Style
Be concise. Spawn sub-agents for coding tasks. Ask when uncertain.

## Core Briefing
{core_briefing}
"""

CONSOLIDATION_PROMPT = """\
Review your recent interactions and consolidate your memories:

1. Search for redundant or overlapping memories and merge them
2. Update any memories that are now outdated based on recent interactions
3. Write a fresh core briefing as a `procedure` memory with category `core_briefing`
   - Include key user preferences, active projects, and important context
   - Keep it under 2000 characters

After consolidation, respond with a brief summary of what you changed.
"""


@dataclass
class PendingMessage:
    text: str
    source: str  # "telegram", "cli", "scheduler"
    callback: MessageCallback


class PersistentOrchestrator:
    """A persistent ClaudeSDKClient session with in-process MCP tools for memory and sub-agents."""

    def __init__(self) -> None:
        self._client: ClaudeSDKClient | None = None
        self._queue: asyncio.Queue[PendingMessage] = asyncio.Queue()
        self._state: OrchestratorState = OrchestratorState()
        self._pump_task: asyncio.Task | None = None
        self._mcp_server = create_orchestrator_mcp_server()
        self._processing: bool = False

    @property
    def is_awake(self) -> bool:
        return self._state.is_awake

    async def wake(self) -> None:
        """Connect (or resume) the orchestrator session."""
        if self._state.is_awake and self._client:
            return

        init_db()
        self._state = get_orchestrator_state()

        # Build core briefing from scored memories
        self._state.core_briefing = self._build_core_briefing()

        # Create client with MCP tools
        briefing = self._state.core_briefing or "No memories yet."
        system_prompt = SYSTEM_PROMPT.format(core_briefing=briefing)

        opts = ClaudeAgentOptions(
            allowed_tools=["Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "mcp__*"],
            max_turns=25,
            permission_mode="bypassPermissions",
            setting_sources=[],
            system_prompt=system_prompt,
            mcp_servers={"claw_orchestrator": self._mcp_server},
        )

        # Try to resume existing session
        if self._state.session_id:
            opts.resume = self._state.session_id

        self._client = ClaudeSDKClient(options=opts)

        try:
            await self._client.connect()
        except Exception:
            # Resume failed — start fresh
            logger.warning("Session resume failed, starting fresh")
            self._state.session_id = None
            opts.resume = None
            self._client = ClaudeSDKClient(options=opts)
            await self._client.connect()

        self._state.is_awake = True
        self._state.last_wake_at = datetime.now(UTC)
        upsert_orchestrator_state(self._state)

        # Start message pump
        self._pump_task = asyncio.create_task(self._message_pump())
        logger.info("Orchestrator awake (session=%s)", self._state.session_id or "new")

    async def sleep(self) -> None:
        """Run consolidation, then disconnect."""
        if not self._state.is_awake or not self._client:
            return

        # Cancel pump
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass

        # Send consolidation prompt
        try:
            await self._client.query(CONSOLIDATION_PROMPT)
            async for msg in self._client.receive_messages():
                if isinstance(msg, ResultMessage):
                    logger.info("Consolidation result: %s", (msg.result or "")[:200])
                    break
        except Exception:
            logger.exception("Consolidation failed")

        # Disconnect
        try:
            await asyncio.wait_for(self._client.disconnect(), timeout=10)
        except (TimeoutError, Exception):
            logger.warning("Disconnect timed out")

        self._client = None
        self._state.is_awake = False
        self._state.last_sleep_at = datetime.now(UTC)
        upsert_orchestrator_state(self._state)
        logger.info("Orchestrator asleep")

    async def submit(self, text: str, source: str, callback: MessageCallback) -> None:
        """Entry point for all interfaces. Auto-wakes if asleep."""
        if not self._state.is_awake:
            await self.wake()
        if self._processing:
            await callback("hmmmmm...")
        await self._queue.put(PendingMessage(text=text, source=source, callback=callback))

    async def _message_pump(self) -> None:
        """Async loop: dequeue messages, query the agent, send responses via callback."""
        while True:
            try:
                pending = await self._queue.get()
                logger.info("Pump: dequeued message from %s: %s", pending.source, pending.text[:100])
                set_message_callback(pending.callback)

                self._processing = True
                try:
                    # Auto-inject relevant memories into context
                    query_text = pending.text
                    memories = get_relevant_memories(query_text, limit=10)
                    if memories:
                        lines = []
                        for m in memories:
                            prefix = f"[{m.memory_type}]"
                            if m.category:
                                prefix += f" ({m.category})"
                            lines.append(f"{prefix}: {m.content}")
                        memory_context = MEMORY_CONTEXT_HEADER.format(memories="\n".join(lines))
                        query_text = f"{memory_context}\n\n{pending.text}"
                        logger.info("Pump: injected %d memories", len(memories))

                    logger.info("Pump: sending query to agent...")
                    await self._client.query(query_text)
                    logger.info("Pump: query sent, waiting for messages...")

                    msg_count = 0
                    async for msg in self._client.receive_messages():
                        msg_count += 1
                        logger.info("Pump: received message #%d: %s", msg_count, type(msg).__name__)
                        if isinstance(msg, ResultMessage):
                            logger.info("Pump: ResultMessage result=%s", (msg.result or "")[:200])
                            if msg.result:
                                await pending.callback(msg.result)
                            # Capture session_id for resume
                            if hasattr(msg, "session_id") and msg.session_id:
                                self._state.session_id = msg.session_id
                                upsert_orchestrator_state(self._state)
                            break
                        elif isinstance(msg, AssistantMessage):
                            text = _extract_assistant_text(msg)
                            logger.info("Pump: AssistantMessage text=%s", (text or "")[:200])
                            if text:
                                await pending.callback(text)

                    logger.info("Pump: finished after %d messages", msg_count)
                    increment_message_count()
                    self._state.total_messages_handled += 1

                except Exception:
                    logger.exception("Pump: error processing message")
                    try:
                        await pending.callback("Sorry, I encountered an internal error.")
                    except Exception:
                        pass
                finally:
                    self._processing = False
                    set_message_callback(None)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Pump error")

    def _build_core_briefing(self) -> str:
        """Build core briefing from scored top memories."""
        # Check for explicit core_briefing procedure
        briefings = list_memories(memory_type=MemoryType.PROCEDURE)
        for mem in briefings:
            if mem.category == "core_briefing":
                return mem.content[:MAX_BRIEFING_TOKENS]

        # Fallback: assemble from top-scored memories
        all_mems = list_memories(limit=100)
        if not all_mems:
            return ""

        scored = [(m, score_memory(m)) for m in all_mems]
        scored.sort(key=lambda x: x[1], reverse=True)

        lines = []
        total = 0
        for mem, sc in scored:
            line = f"- [{mem.memory_type}] {mem.content}"
            if total + len(line) > MAX_BRIEFING_TOKENS:
                break
            lines.append(line)
            total += len(line) + 1

        return "\n".join(lines)


def _extract_assistant_text(msg: AssistantMessage) -> str | None:
    """Extract text content from an AssistantMessage's content blocks."""
    if not hasattr(msg, "content"):
        return None
    content = msg.content
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif hasattr(block, "type") and block.type == "text":
                parts.append(getattr(block, "text", ""))
        text = "\n".join(parts).strip()
        return text or None
    return None
