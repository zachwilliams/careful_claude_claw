import asyncio
from datetime import UTC, datetime

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from .db import insert_job, update_job
from .models import Job, JobStatus

DEFAULT_TASK = (
    "List all Python source files in the current directory and briefly describe "
    "each file's purpose based on its name and content."
)


async def run_agent(
    agent_name: str,
    task: str,
    max_attempts: int = 2,
    backoff_seconds: int = 30,
    cwd: str | None = None,
) -> Job:
    """Spawn a Claude agent for the given task, with retry on failure."""
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
