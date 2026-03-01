import asyncio
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from .db import insert_job, update_job
from .models import Job, JobStatus

DEFAULT_TASK = "List all the active MCP connections you have."


def prepare_workspace(cwd: str|None, claude_md: str | None = None) -> Path:
    """Create workspace directory and optionally seed it with a CLAUDE.md file."""
    workspace = Path(cwd) if cwd else Path.cwd().joinpath("/workspace")

    workspace.mkdir(parents=True, exist_ok=True)
    if claude_md is not None:
        (workspace / "CLAUDE.md").write_text(claude_md)
    return workspace


async def run_agent(
    agent_name: str,
    task: str,
    max_attempts: int = 2,
    backoff_seconds: int = 30,
    cwd: str | None = None,
    claude_md: str | None = None,
) -> Job:
    """Spawn a Claude agent for the given task, with retry on failure."""
    cwd = str(prepare_workspace(cwd, claude_md))

    job = Job(
        agent_name=agent_name,
        task=task,
        started_at=datetime.now(UTC),
        status=JobStatus.PENDING,
    )
    insert_job(job)

    for attempt in range(1, max_attempts + 1):
        job.attempt = attempt
        job.status = JobStatus.RUNNING
        update_job(job)

        try:
            result_text: str | None = None
            async for message in query(
                prompt=task,
                options=ClaudeAgentOptions(
                    cwd=cwd,
                    allowed_tools=["Read", "Glob", "Grep"],
                    max_turns=10,
                ),
            ):
                if isinstance(message, ResultMessage):
                    result_text = message.result

            job.status = JobStatus.SUCCESS
            job.output = result_text or ""
            job.ended_at = datetime.now(UTC)
            update_job(job)
            return job

        except (CLINotFoundError, CLIConnectionError, Exception) as exc:
            job.error = str(exc)
            if attempt < max_attempts:
                await asyncio.sleep(backoff_seconds)
            else:
                job.status = JobStatus.FAILED
                job.ended_at = datetime.now(UTC)
                update_job(job)

    return job
