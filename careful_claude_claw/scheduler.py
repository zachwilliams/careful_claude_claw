import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .agent import run_agent
from .db import init_db, list_schedules
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
    schedule_name: str,
    task: str,
    skill_name: str | None,
    project_name: str | None,
) -> None:
    """Execute a scheduled task by running an agent."""
    agent_name = f"sched-{schedule_name}"

    # If a skill is referenced, read its content as the task prompt
    effective_task = task
    if skill_name:
        skill = get_skill(skill_name, project_name)
        if skill and skill.file_path:
            try:
                from pathlib import Path

                effective_task = Path(skill.file_path).read_text()
            except OSError:
                logger.warning("Could not read skill file %s, using task string", skill.file_path)

    if not effective_task:
        logger.warning("Schedule %s has no task or skill content, skipping", schedule_name)
        return

    cwd = None
    if project_name:
        from .db import get_project

        proj = get_project(project_name)
        if proj:
            cwd = proj["path"]

    logger.info("Running scheduled task: %s", schedule_name)
    try:
        job = await run_agent(
            agent_name=agent_name,
            task=effective_task,
            project_name=project_name,
            cwd=cwd,
        )
        logger.info("Schedule %s completed: %s", schedule_name, job.status)
    except Exception:
        logger.exception("Schedule %s failed", schedule_name)


def build_scheduler() -> AsyncIOScheduler:
    """Create a scheduler with all enabled schedules from the database."""
    init_db()
    scheduler = AsyncIOScheduler()

    for row in list_schedules():
        if not row["enabled"]:
            continue
        try:
            trigger = _parse_cron(row["cron_expr"])
        except ValueError:
            logger.error("Invalid cron for schedule %s: %s", row["name"], row["cron_expr"])
            continue

        scheduler.add_job(
            _run_scheduled_task,
            trigger=trigger,
            args=[row["name"], row["task"], row["skill_name"], row["project_name"]],
            id=row["name"],
            name=row["name"],
            replace_existing=True,
        )
        logger.info("Loaded schedule: %s (%s)", row["name"], row["cron_expr"])

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
