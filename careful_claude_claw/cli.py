import asyncio
from datetime import UTC, datetime

import click
from rich.console import Console
from rich.table import Table

from .agent import DEFAULT_TASK, run_agent
from .db import (
    delete_job,
    init_db,
    insert_job,
    list_active_agents,
    list_executions,
    list_jobs,
)
from .models import Job, JobStatus
from .skills import discover_skills, get_skill

console = Console()


@click.group()
def cli() -> None:
    """CarefulClaudeClaw — security-first Claude Code orchestrator."""


# --- Run ---


@cli.command()
@click.option("--task", default=DEFAULT_TASK, help="Task for the agent to execute.")
@click.option("--agent-name", default="demo", help="Name to identify this agent.")
@click.option("--max-attempts", default=2, help="Maximum retry attempts on failure.")
@click.option("--backoff", default=5, help="Seconds to wait between retry attempts.")
@click.option("--cwd", default=None, help="Working directory for the agent.")
@click.option("--skill", default=None, help="Skill to run instead of a raw task.")
@click.option(
    "--allowed-tools",
    default=None,
    help="Comma-separated list of tools the agent can use.",
)
def run(
    task: str,
    agent_name: str,
    max_attempts: int,
    backoff: int,
    cwd: str | None,
    skill: str | None,
    allowed_tools: str | None,
) -> None:
    """Run a one-shot agent task and log the result."""
    init_db()

    # Resolve skill content if provided
    if skill:
        s = get_skill(skill)
        if not s:
            console.print(f"[red]Skill not found: {skill}[/red]")
            return
        from pathlib import Path

        try:
            task = Path(s.file_path).read_text()
        except OSError:
            console.print(f"[red]Cannot read skill file: {s.file_path}[/red]")
            return
        agent_name = f"skill-{skill}"

    console.print(f"[bold cyan]Agent:[/bold cyan] {agent_name}")
    if cwd:
        console.print(f"[bold cyan]Working dir:[/bold cyan] {cwd}")
    console.print(f"[bold cyan]Task:[/bold cyan] {task[:100]}{'...' if len(task) > 100 else ''}")
    console.print()

    tools_list = allowed_tools.split(",") if allowed_tools else None

    with console.status("[bold green]Running agent...[/bold green]"):
        execution = asyncio.run(
            run_agent(
                agent_name=agent_name,
                task=task,
                max_attempts=max_attempts,
                backoff_seconds=backoff,
                cwd=cwd,
                allowed_tools=tools_list,
            )
        )

    if execution.status == JobStatus.SUCCESS:
        console.print("[bold green]Success[/bold green]")
        if execution.output:
            console.print()
            console.print(execution.output)
    else:
        console.print("[bold red]Failed[/bold red]")
        if execution.error:
            console.print(f"[red]{execution.error}[/red]")

    console.print()
    console.print(f"[dim]Execution ID: {execution.id}[/dim]")


# --- Jobs ---


@cli.group("jobs")
def jobs_group() -> None:
    """Manage job definitions."""


@jobs_group.command("list")
def jobs_list() -> None:
    """List all job definitions."""
    init_db()

    rows = list_jobs()
    if not rows:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    table = Table(title="Jobs")
    table.add_column("Name", style="cyan")
    table.add_column("Task/Skill")
    table.add_column("Cron", style="bold")
    table.add_column("Working Dir")
    table.add_column("Enabled")

    for row in rows:
        task_or_skill = row["skill_name"] or (row["task"] or "")[:50]
        cron = row["cron_expr"] or "-"
        enabled = "[green]yes[/green]" if row["enabled"] else "[red]no[/red]"
        table.add_row(
            row["name"],
            task_or_skill,
            cron,
            row["cwd"] or "-",
            enabled,
        )

    console.print(table)


@jobs_group.command("add")
@click.argument("name")
@click.option("--task", default="", help="Task prompt.")
@click.option("--skill", default=None, help="Skill name to run.")
@click.option("--cron", default=None, help="Cron expression for recurring jobs.")
@click.option("--cwd", default=None, help="Working directory.")
@click.option(
    "--allowed-tools",
    default=None,
    help="Comma-separated list of tools the agent can use.",
)
def jobs_add(
    name: str,
    task: str,
    skill: str | None,
    cron: str | None,
    cwd: str | None,
    allowed_tools: str | None,
) -> None:
    """Create a new job definition."""
    init_db()
    if not task and not skill:
        console.print("[red]Provide --task or --skill[/red]")
        return

    tools_list = allowed_tools.split(",") if allowed_tools else None
    job = Job(
        name=name,
        task=task,
        skill_name=skill,
        cron_expr=cron,
        cwd=cwd,
        allowed_tools=tools_list,
    )
    try:
        insert_job(job)
        if cron:
            console.print(f"[green]Cron job '{name}' added: {cron}[/green]")
        else:
            console.print(f"[green]Job '{name}' added.[/green]")
    except Exception as e:
        console.print(f"[red]Failed to add job: {e}[/red]")


@jobs_group.command("remove")
@click.argument("name")
def jobs_remove(name: str) -> None:
    """Remove a job definition."""
    init_db()
    delete_job(name)
    console.print(f"[green]Job '{name}' removed.[/green]")


@jobs_group.command("runs")
@click.option("--job", default=None, help="Filter by job name.")
@click.option("--limit", default=20, help="Number of recent executions to display.")
def jobs_runs(job: str | None, limit: int) -> None:
    """Show execution history."""
    init_db()

    rows = list_executions(limit=limit, job_name=job)
    if not rows:
        console.print("[yellow]No executions found.[/yellow]")
        return

    table = Table(title="Executions")
    table.add_column("Agent", style="cyan")
    table.add_column("Job")
    table.add_column("Status", style="bold")
    table.add_column("Attempt")
    table.add_column("Started")
    table.add_column("Duration")

    status_colors = {
        "success": "green",
        "failed": "red",
        "running": "yellow",
        "pending": "blue",
    }

    for row in rows:
        status = row["status"]
        color = status_colors.get(status, "white")

        duration = ""
        if row.get("started_at") and row.get("ended_at"):
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["ended_at"])
            duration = f"{(end - start).total_seconds():.1f}s"

        table.add_row(
            row["agent_name"],
            row["job_name"],
            f"[{color}]{status}[/{color}]",
            str(row["attempt"]),
            (row["started_at"] or "")[:19],
            duration,
        )

    console.print(table)


# --- Skills ---


@cli.command()
def skills() -> None:
    """List available skills."""
    found = discover_skills()
    if not found:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for s in found:
        table.add_row(
            s.name,
            s.description[:80] if s.description else "-",
        )

    console.print(table)


# --- Status ---


@cli.command()
def status() -> None:
    """Show active tasks and recent activity."""
    init_db()

    agents = list_active_agents()
    if agents:
        table = Table(title="Active Tasks")
        table.add_column("Agent", style="cyan")
        table.add_column("Job")
        table.add_column("Running Since")
        table.add_column("Duration")

        now = datetime.now(UTC)
        for a in agents:
            started = a.get("started_at", "")
            duration = ""
            if started:
                start_dt = datetime.fromisoformat(started)
                delta = now - start_dt
                duration = f"{delta.total_seconds():.0f}s"
            table.add_row(
                a["agent_name"],
                a.get("job_name") or "-",
                started[:19] if started else "?",
                duration,
            )
        console.print(table)
    else:
        console.print("[dim]No active tasks.[/dim]")

    console.print()

    # Show cron jobs
    cron_jobs = list_jobs(cron_only=True)
    if cron_jobs:
        console.print("[bold]Cron Jobs:[/bold]")
        for j in cron_jobs:
            enabled = "[green]on[/green]" if j["enabled"] else "[red]off[/red]"
            console.print(f"  {j['name']} [{enabled}] `{j['cron_expr']}`")
        console.print()

    recent = list_executions(limit=5)
    if recent:
        console.print("[bold]Recent executions:[/bold]")
        for e in recent:
            status_colors = {"success": "green", "failed": "red", "running": "yellow"}
            c = status_colors.get(e["status"], "white")
            agent = e["agent_name"]
            job = e.get("job_name") or ""
            job_str = f" ({job})" if job else ""
            console.print(f"  [{c}]{e['status']}[/{c}] {agent}{job_str}")


# --- Start (scheduler daemon) ---


@cli.command()
@click.option(
    "--telegram/--no-telegram",
    default=None,
    help="Enable/disable Telegram listener (auto-detects from config).",
)
@click.option(
    "--slack/--no-slack",
    default=None,
    help="Enable/disable Slack listener (auto-detects from config).",
)
def start(telegram: bool | None, slack: bool | None) -> None:
    """Start the scheduler daemon (optionally with Telegram/Slack listeners)."""
    init_db()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .config import settings
    from .scheduler import run_scheduler
    from .slack import run_slack_listener
    from .telegram import run_telegram_listener

    # Auto-detect listeners if not explicitly set
    tg_enabled = telegram if telegram is not None else settings.telegram_configured
    slack_enabled = slack if slack is not None else settings.slack_configured

    async def _run_all() -> None:
        tasks = [run_scheduler()]
        if tg_enabled:
            tasks.append(run_telegram_listener())
        if slack_enabled:
            tasks.append(run_slack_listener())
        await asyncio.gather(*tasks)

    listeners = []
    if tg_enabled:
        listeners.append("Telegram")
    if slack_enabled:
        listeners.append("Slack")

    if listeners:
        joined = " + ".join(listeners)
        console.print(f"[bold cyan]Starting scheduler + {joined} listener(s)...[/bold cyan]")
    else:
        console.print("[bold cyan]Starting scheduler...[/bold cyan]")

    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# --- Telegram ---


@cli.command()
def telegram() -> None:
    """Start the Telegram listener (standalone, for dev/testing)."""
    init_db()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .config import settings
    from .telegram import run_telegram_listener

    if not settings.telegram_configured:
        console.print("[red]Telegram not configured.[/red]")
        console.print("[dim]Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env[/dim]")
        return

    console.print("[bold cyan]Starting Telegram listener...[/bold cyan]")
    try:
        asyncio.run(run_telegram_listener())
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# --- Slack ---


@cli.command()
def slack() -> None:
    """Start the Slack listener (standalone, for dev/testing)."""
    init_db()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .config import settings
    from .slack import run_slack_listener

    if not settings.slack_configured:
        console.print("[red]Slack not configured.[/red]")
        console.print("[dim]Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env[/dim]")
        return

    console.print("[bold cyan]Starting Slack listener...[/bold cyan]")
    try:
        asyncio.run(run_slack_listener())
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


# --- Reset (dev) ---


async def _clear_telegram(token: str, chat_id: int) -> int:
    """Clear all reachable messages in the Telegram chat."""
    from .telegram import TelegramBot

    bot = TelegramBot(token, chat_id)
    try:
        probe_id = await bot.send_and_get_id("Resetting...")
        if not probe_id:
            return 0
        return await bot.delete_all_messages(probe_id)
    finally:
        await bot.close()


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def reset(yes: bool) -> None:
    """[Dev] Wipe all data: kill agents, clear SQLite DB, and delete Telegram messages."""
    if not yes:
        click.confirm(
            "This will DELETE all database data and Telegram messages. Continue?",
            abort=True,
        )

    # Kill active agent sessions
    from .agent_session import kill_all_sessions

    killed = asyncio.run(kill_all_sessions())
    if killed:
        console.print(f"[yellow]Killed {killed} active agent(s).[/yellow]")

    # Wipe database
    init_db()
    console.print("[green]Database reset.[/green]")

    # Clear Telegram messages
    from .config import settings

    if not settings.telegram_configured:
        console.print("[yellow]Telegram not configured, skipping message cleanup.[/yellow]")
    else:
        deleted = asyncio.run(_clear_telegram(settings.telegram_bot_token, settings.telegram_chat_id))
        console.print(f"[green]Deleted {deleted} Telegram message(s).[/green]")

    console.print("[bold green]Reset complete.[/bold green]")


if __name__ == "__main__":
    cli()
