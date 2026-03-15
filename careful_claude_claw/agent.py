import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ResultMessage,
    query,
)

from .db import insert_job, register_active_agent, unregister_active_agent, update_job
from .models import Job, JobStatus

DEFAULT_TASK = "List all the active MCP connections you have."

DEFAULT_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


async def run_agent(
    agent_name: str,
    task: str,
    max_attempts: int = 2,
    backoff_seconds: int = 30,
    cwd: str | None = None,
    system_prompt: str | dict | None = None,
    setting_sources: list[str] | None = None,
    project_name: str | None = None,
    allowed_tools: list[str] | None = None,
) -> Job:
    """Spawn a Claude agent for the given task, with retry on failure."""
    is_temp = cwd is None
    workspace = Path(cwd) if cwd else Path.cwd() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    cwd = str(workspace)

    job = Job(
        agent_name=agent_name,
        task=task,
        project_name=project_name,
        started_at=datetime.now(UTC),
        status=JobStatus.PENDING,
    )
    insert_job(job)

    try:
        for attempt in range(1, max_attempts + 1):
            job.attempt = attempt
            job.status = JobStatus.RUNNING
            update_job(job)
            register_active_agent(job)

            try:
                result_text: str | None = None
                tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
                opts = ClaudeAgentOptions(
                    cwd=cwd,
                    allowed_tools=tools,
                    max_turns=10,
                )
                if system_prompt is not None:
                    opts.system_prompt = system_prompt
                if setting_sources is not None:
                    opts.setting_sources = setting_sources

                async for message in query(
                    prompt=task,
                    options=opts,
                ):
                    if isinstance(message, ResultMessage):
                        result_text = message.result

                job.status = JobStatus.SUCCESS
                job.output = result_text or ""
                job.ended_at = datetime.now(UTC)
                update_job(job)
                unregister_active_agent(job.id)
                return job

            except (CLINotFoundError, CLIConnectionError, Exception) as exc:
                job.error = str(exc)
                if attempt < max_attempts:
                    await asyncio.sleep(backoff_seconds)
                else:
                    job.status = JobStatus.FAILED
                    job.ended_at = datetime.now(UTC)
                    update_job(job)
                    unregister_active_agent(job.id)
    finally:
        if is_temp and workspace.exists():
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass

    return job
