"""Agent session registry for interactive, long-lived agent connections.

Uses ClaudeSDKClient for bidirectional communication with agents,
and maintains an in-memory registry of active sessions.
"""

import asyncio
import logging
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

from .db import insert_job, register_active_agent, unregister_active_agent, update_job
from .models import Job, JobStatus

logger = logging.getLogger(__name__)

# Type alias for the callback that sends messages to Telegram
MessageCallback = Callable[[str], Awaitable[None]]


@dataclass
class AgentSession:
    name: str
    job_id: str
    client: ClaudeSDKClient
    task: asyncio.Task | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# Module-level registry
AGENT_SESSIONS: dict[str, AgentSession] = {}
_name_counter: int = 0


def generate_name(prefix: str = "task") -> str:
    """Generate a unique friendly name like task-1, task-2, etc."""
    global _name_counter
    _name_counter += 1
    name = f"{prefix}-{_name_counter}"
    # Avoid collisions with existing sessions
    while name in AGENT_SESSIONS:
        _name_counter += 1
        name = f"{prefix}-{_name_counter}"
    return name


def register_session(session: AgentSession) -> None:
    AGENT_SESSIONS[session.name] = session


def unregister_session(name: str) -> None:
    AGENT_SESSIONS.pop(name, None)


def get_session(name: str) -> AgentSession | None:
    return AGENT_SESSIONS.get(name)


def list_sessions() -> list[AgentSession]:
    return list(AGENT_SESSIONS.values())


async def kill_session(name: str) -> bool:
    """Kill an agent session by name. Returns True if found and killed."""
    session = AGENT_SESSIONS.get(name)
    if session is None:
        return False

    try:
        await session.client.interrupt()
    except Exception:
        logger.debug("interrupt() failed for %s, proceeding with cleanup", name)

    if session.task and not session.task.done():
        session.task.cancel()

    try:
        await session.client.disconnect()
    except Exception:
        logger.debug("disconnect() failed for %s", name)

    # Update job status
    from .db import update_job_status

    update_job_status(session.job_id, JobStatus.CANCELLED)
    unregister_active_agent(session.job_id)
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
    """Send a follow-up message to a running agent. Returns True if sent."""
    session = AGENT_SESSIONS.get(name)
    if session is None:
        return False

    try:
        await session.client.query(message)
        return True
    except Exception:
        logger.exception("Failed to send message to agent %s", name)
        return False


async def run_interactive_agent(
    name: str,
    task: str,
    on_message: MessageCallback,
    agent_name: str = "tg-task",
    cwd: str | None = None,
    system_prompt: str | None = None,
    project_name: str | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 10,
) -> Job:
    """Spawn an interactive agent using ClaudeSDKClient.

    Creates a persistent client session that supports follow-up messages
    and interrupts. Sends results to Telegram via on_message callback.
    """
    from .agent import DEFAULT_ALLOWED_TOOLS

    workspace = Path(cwd) if cwd else Path.cwd() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    job = Job(
        agent_name=agent_name,
        task=task,
        project_name=project_name,
        started_at=datetime.now(UTC),
        status=JobStatus.RUNNING,
    )
    insert_job(job)
    register_active_agent(job)

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    opts = ClaudeAgentOptions(
        cwd=str(workspace),
        allowed_tools=tools,
        max_turns=max_turns,
    )
    if system_prompt is not None:
        opts.system_prompt = system_prompt

    client = ClaudeSDKClient(options=opts)
    session = AgentSession(name=name, job_id=job.id, client=client)
    register_session(session)

    try:
        await client.connect(prompt=task)

        result_text: str | None = None
        async for msg in client.receive_messages():
            if isinstance(msg, ResultMessage):
                result_text = msg.result
                break
            elif isinstance(msg, AssistantMessage):
                # Extract text from content blocks (not streamed to TG,
                # but kept for potential future use)
                pass

        job.status = JobStatus.SUCCESS
        job.output = result_text or ""
        job.ended_at = datetime.now(UTC)
        update_job(job)

        if result_text:
            await on_message(f"[{name}] {result_text}")
        else:
            await on_message(f"[{name}] Done.")

    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        job.ended_at = datetime.now(UTC)
        update_job(job)
        await on_message(f"[{name}] Cancelled.")
    except (CLINotFoundError, CLIConnectionError, Exception) as exc:
        logger.exception("Interactive agent %s failed", name)
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.ended_at = datetime.now(UTC)
        update_job(job)
        await on_message(f"[{name}] Failed: {exc}")
    finally:
        unregister_active_agent(job.id)
        # Only unregister session if it wasn't already killed
        if name in AGENT_SESSIONS:
            try:
                await client.disconnect()
            except Exception:
                pass
            unregister_session(name)

    return job
