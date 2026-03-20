import asyncio
import json
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .agent import run_agent
from .db import init_db, list_jobs
from .orchestrator import Orchestrator
from .skills import get_skill

logger = logging.getLogger(__name__)


def _parse_cron(expr: str) -> CronTrigger:
    """Parse a 5-field cron expression into an APScheduler CronTrigger."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {expr!r}")
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week)


async def _run_scheduled_task(
    job_name: str,
    task: str,
    skill_name: str | None,
    cwd: str | None,
    allowed_tools: list[str] | None = None,
) -> None:
    """Execute a scheduled task by running an agent."""
    agent_name = f"sched-{job_name}"

    # If a skill is referenced, read its content as the task prompt
    effective_task = task
    if skill_name:
        skill = get_skill(skill_name)
        if skill and skill.file_path:
            try:
                from pathlib import Path

                effective_task = Path(skill.file_path).read_text()
            except OSError:
                logger.warning("Could not read skill file %s, using task string", skill.file_path)

    if not effective_task:
        logger.warning("Job %s has no task or skill content, skipping", job_name)
        return

    # Enrich with memory context
    orchestrator = Orchestrator()
    memory_context = orchestrator.retrieve_context(effective_task)
    system_prompt = orchestrator.build_system_prompt(None, memory_context)

    logger.info("Running scheduled task: %s", job_name)
    try:
        execution = await run_agent(
            agent_name=agent_name,
            task=effective_task,
            job_name=job_name,
            cwd=cwd,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
        )
        logger.info("Job %s completed: %s", job_name, execution.status)
        # Async memory extraction
        asyncio.create_task(orchestrator.on_task_complete(execution))
    except Exception:
        logger.exception("Job %s failed", job_name)


def build_scheduler() -> AsyncIOScheduler:
    """Create a scheduler with all enabled cron jobs from the database."""
    init_db()
    scheduler = AsyncIOScheduler()

    for row in list_jobs(cron_only=True):
        if not row["enabled"]:
            continue
        try:
            trigger = _parse_cron(row["cron_expr"])
        except ValueError:
            logger.error("Invalid cron for job %s: %s", row["name"], row["cron_expr"])
            continue

        raw_tools = row.get("allowed_tools")
        tools = json.loads(raw_tools) if raw_tools else None

        scheduler.add_job(
            _run_scheduled_task,
            trigger=trigger,
            args=[row["name"], row["task"], row["skill_name"], row["cwd"], tools],
            id=row["name"],
            name=row["name"],
            replace_existing=True,
        )
        logger.info("Loaded cron job: %s (%s)", row["name"], row["cron_expr"])

    return scheduler


async def run_scheduler() -> None:
    """Start the scheduler and run until interrupted."""
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("Scheduler started. Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        scheduler.shutdown()
        logger.info("Scheduler stopped.")
