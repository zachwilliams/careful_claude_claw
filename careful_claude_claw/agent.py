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

from .db import insert_execution, register_active_agent, unregister_active_agent, update_execution
from .models import Execution, JobStatus
from .paths import JOBS_DIR

DEFAULT_TASK = "List all the active MCP connections you have."

DEFAULT_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


async def run_agent(
    agent_name: str,
    task: str,
    job_name: str = "ad-hoc",
    max_attempts: int = 2,
    backoff_seconds: int = 30,
    cwd: str | None = None,
    system_prompt: str | dict | None = None,
    setting_sources: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> Execution:
    """Spawn a Claude agent for the given task, with retry on failure."""
    workspace = Path(cwd) if cwd else JOBS_DIR / job_name
    workspace.mkdir(parents=True, exist_ok=True)
    is_temp = False  # job workspaces are persistent; history carries between runs
    cwd = str(workspace)

    execution = Execution(
        job_name=job_name,
        agent_name=agent_name,
        started_at=datetime.now(UTC),
        status=JobStatus.PENDING,
    )
    insert_execution(execution)

    try:
        for attempt in range(1, max_attempts + 1):
            execution.attempt = attempt
            execution.status = JobStatus.RUNNING
            update_execution(execution)
            register_active_agent(execution)

            try:
                result_text: str | None = None
                tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
                opts = ClaudeAgentOptions(
                    cwd=cwd,
                    allowed_tools=tools,
                    max_turns=10,
                    setting_sources=setting_sources or ["user"],
                )
                if system_prompt is not None:
                    opts.system_prompt = system_prompt

                async for message in query(
                    prompt=task,
                    options=opts,
                ):
                    if isinstance(message, ResultMessage):
                        result_text = message.result

                execution.status = JobStatus.SUCCESS
                execution.output = result_text or ""
                execution.ended_at = datetime.now(UTC)
                update_execution(execution)
                unregister_active_agent(execution.id)
                return execution

            except (CLINotFoundError, CLIConnectionError, Exception) as exc:
                execution.error = str(exc)
                if attempt < max_attempts:
                    await asyncio.sleep(backoff_seconds)
                else:
                    execution.status = JobStatus.FAILED
                    execution.ended_at = datetime.now(UTC)
                    update_execution(execution)
                    unregister_active_agent(execution.id)
    finally:
        if is_temp and workspace.exists():
            try:
                shutil.rmtree(workspace)
            except OSError:
                pass

    return execution
