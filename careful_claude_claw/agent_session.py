"""Agent session registry for interactive, long-lived agent connections.

Uses ClaudeSDKClient for bidirectional communication with agents,
and maintains an in-memory registry of active sessions.
"""

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
)

from .db import insert_execution, register_active_agent, unregister_active_agent, update_execution
from .models import Execution, JobStatus

logger = logging.getLogger(__name__)

# Type alias for the callback that sends messages to Telegram
MessageCallback = Callable[[str], Awaitable[None]]


IDLE_TIMEOUT_SECONDS = 600  # 10 minutes


@dataclass
class AgentSession:
    name: str
    execution_id: int
    client: ClaudeSDKClient
    task: asyncio.Task | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    cwd: Path | None = None
    is_temp_workspace: bool = False


# Module-level registry
AGENT_SESSIONS: dict[str, AgentSession] = {}
_name_counter: int = 0


def generate_name(prefix: str = "T") -> str:
    """Generate a unique short name like T1, T2, S3, etc."""
    global _name_counter
    _name_counter += 1
    name = f"{prefix}{_name_counter}"
    # Avoid collisions with existing sessions
    while name in AGENT_SESSIONS:
        _name_counter += 1
        name = f"{prefix}{_name_counter}"
    return name


def register_session(session: AgentSession) -> None:
    AGENT_SESSIONS[session.name] = session


def unregister_session(name: str) -> None:
    AGENT_SESSIONS.pop(name, None)


def _resolve_name(name: str) -> str | None:
    """Resolve a session name case-insensitively. Returns the canonical name or None."""
    if name in AGENT_SESSIONS:
        return name
    name_lower = name.lower()
    for key in AGENT_SESSIONS:
        if key.lower() == name_lower:
            return key
    return None


def get_session(name: str) -> AgentSession | None:
    resolved = _resolve_name(name)
    return AGENT_SESSIONS.get(resolved) if resolved else None


def list_sessions() -> list[AgentSession]:
    return list(AGENT_SESSIONS.values())


def _cleanup_workspace(session: AgentSession) -> None:
    """Remove temp workspace directory if applicable."""
    if session.is_temp_workspace and session.cwd and session.cwd.exists():
        try:
            shutil.rmtree(session.cwd)
            logger.info("Cleaned up temp workspace: %s", session.cwd)
        except OSError:
            logger.warning("Failed to clean up workspace: %s", session.cwd)


async def kill_session(name: str) -> bool:
    """Kill an agent session by name (case-insensitive). Returns True if found and killed."""
    resolved = _resolve_name(name)
    if resolved is None:
        return False
    name = resolved
    session = AGENT_SESSIONS[name]
    if session is None:
        return False

    try:
        await asyncio.wait_for(session.client.interrupt(), timeout=5)
    except (TimeoutError, Exception):
        logger.debug("interrupt() failed/timed out for %s, proceeding with cleanup", name)

    if session.task and not session.task.done():
        session.task.cancel()

    try:
        await asyncio.wait_for(session.client.disconnect(), timeout=5)
    except (TimeoutError, Exception):
        logger.debug("disconnect() failed/timed out for %s", name)

    # Update execution status
    from .db import update_execution_status

    update_execution_status(session.execution_id, JobStatus.CANCELLED)
    unregister_active_agent(session.execution_id)
    _cleanup_workspace(session)
    unregister_session(name)
    return True


async def kill_all_sessions() -> int:
    """Kill all active sessions. Returns number killed."""
    names = list(AGENT_SESSIONS.keys())
    count = 0
    for name in names:
        if await kill_session(name):
            count += 1
    return count


async def send_to_agent(name: str, message: str) -> bool:
    """Send a follow-up message to a running agent (case-insensitive). Returns True if sent."""
    resolved = _resolve_name(name)
    if resolved is None:
        return False
    session = AGENT_SESSIONS[resolved]

    try:
        session.last_activity = datetime.now(UTC)
        await session.client.query(message)
        return True
    except Exception:
        logger.exception("Failed to send message to agent %s", name)
        return False


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


async def run_interactive_agent(
    name: str,
    task: str,
    on_message: MessageCallback,
    agent_name: str = "tg-task",
    cwd: str | None = None,
    system_prompt: str | None = None,
    job_name: str = "interactive",
    allowed_tools: list[str] | None = None,
    max_turns: int = 10,
) -> Execution:
    """Spawn an interactive agent using ClaudeSDKClient.

    Creates a persistent client session that supports follow-up messages
    and interrupts. Sends results to Telegram via on_message callback.
    The session stays alive after the first result to allow follow-ups,
    and auto-closes after IDLE_TIMEOUT_SECONDS of inactivity.
    """
    from .agent import DEFAULT_ALLOWED_TOOLS

    is_temp = cwd is None
    workspace = Path(cwd) if cwd else Path.cwd() / "workspace" / name
    workspace.mkdir(parents=True, exist_ok=True)

    execution = Execution(
        job_name=job_name,
        agent_name=agent_name,
        started_at=datetime.now(UTC),
        status=JobStatus.RUNNING,
    )
    insert_execution(execution)
    register_active_agent(execution)

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    opts = ClaudeAgentOptions(
        cwd=str(workspace),
        allowed_tools=tools,
        max_turns=max_turns,
        setting_sources=["user"],
    )
    if system_prompt is not None:
        opts.system_prompt = system_prompt

    client = ClaudeSDKClient(options=opts)
    session = AgentSession(
        name=name,
        execution_id=execution.id,
        client=client,
        cwd=workspace,
        is_temp_workspace=is_temp,
    )
    register_session(session)

    try:
        await client.connect()
        await client.query(task)

        result_text: str | None = None
        while True:
            got_result = False
            async for msg in client.receive_messages():
                session.last_activity = datetime.now(UTC)
                if isinstance(msg, ResultMessage):
                    result_text = msg.result
                    if result_text:
                        await on_message(f"@{name}: {result_text}")
                    got_result = True
                elif isinstance(msg, AssistantMessage):
                    text = _extract_assistant_text(msg)
                    if text:
                        await on_message(f"@{name}: {text}")

            # Iterator exhausted. If we got a result, wait for follow-up
            if got_result:
                while True:
                    await asyncio.sleep(1)
                    idle = (datetime.now(UTC) - session.last_activity).total_seconds()
                    if idle >= IDLE_TIMEOUT_SECONDS:
                        logger.info("Agent %s idle timeout after %ds", name, idle)
                        await on_message(f"@{name}: Session closed (idle timeout).")
                        break
                    if name not in AGENT_SESSIONS:
                        return execution
                    if idle < 1.5:
                        break
                else:
                    break
            else:
                break

        execution.status = JobStatus.SUCCESS
        execution.output = result_text or ""
        execution.ended_at = datetime.now(UTC)
        update_execution(execution)

        if not result_text:
            await on_message(f"@{name}: Done.")

    except asyncio.CancelledError:
        execution.status = JobStatus.CANCELLED
        execution.ended_at = datetime.now(UTC)
        update_execution(execution)
        await on_message(f"@{name}: Cancelled.")
    except (CLINotFoundError, CLIConnectionError, Exception) as exc:
        logger.exception("Interactive agent %s failed", name)
        execution.status = JobStatus.FAILED
        execution.error = str(exc)
        execution.ended_at = datetime.now(UTC)
        update_execution(execution)
        await on_message(f"@{name}: Failed: {exc}")
    finally:
        unregister_active_agent(execution.id)
        if name in AGENT_SESSIONS:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5)
            except (TimeoutError, Exception):
                pass
            _cleanup_workspace(session)
            unregister_session(name)

    return execution
