import asyncio
from datetime import UTC, datetime

import click
from rich.console import Console
from rich.table import Table

from .agent import DEFAULT_TASK, run_agent
from .db import (
    delete_schedule,
    get_project,
    init_db,
    insert_project,
    insert_schedule,
    list_active_agents,
    list_jobs,
    list_projects,
    list_schedules,
)
from .models import JobStatus, Project, Schedule
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
@click.option("--project", default=None, help="Project context for this run.")
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
    project: str | None,
    skill: str | None,
    allowed_tools: str | None,
) -> None:
    """Run a one-shot agent task and log the result to SQLite."""
    init_db()

    # Resolve skill content if provided
    if skill:
        s = get_skill(skill, project)
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

    cwd = None
    if project:
        proj = get_project(project)
        if proj:
            cwd = proj["path"]

    console.print(f"[bold cyan]Agent:[/bold cyan] {agent_name}")
    if project:
        console.print(f"[bold cyan]Project:[/bold cyan] {project}")
    console.print(f"[bold cyan]Task:[/bold cyan] {task[:100]}{'...' if len(task) > 100 else ''}")
    console.print()

    tools_list = allowed_tools.split(",") if allowed_tools else None

    with console.status("[bold green]Running agent...[/bold green]"):
        job = asyncio.run(
            run_agent(
                agent_name=agent_name,
                task=task,
                max_attempts=max_attempts,
                backoff_seconds=backoff,
                project_name=project,
                cwd=cwd,
                allowed_tools=tools_list,
            )
        )

    if job.status == JobStatus.SUCCESS:
        console.print("[bold green]Success[/bold green]")
        if job.output:
            console.print()
            console.print(job.output)
    else:
        console.print("[bold red]Failed[/bold red]")
        if job.error:
            console.print(f"[red]{job.error}[/red]")

    console.print()
    console.print(f"[dim]Job ID: {job.id}[/dim]")


# --- Jobs ---


@cli.command()
@click.option("--limit", default=20, help="Number of recent jobs to display.")
@click.option("--project", default=None, help="Filter by project.")
def jobs(limit: int, project: str | None) -> None:
    """Show recent job history from SQLite."""
    init_db()

    rows = list_jobs(limit=limit, project_name=project)
    if not rows:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    table = Table(title="Recent Jobs")
    table.add_column("Agent", style="cyan")
    table.add_column("Project", style="magenta")
    table.add_column("Status", style="bold")
    table.add_column("Attempt")
    table.add_column("Started")
    table.add_column("Duration")
    table.add_column("Task")

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

        task_text = row["task"] or ""
        task_preview = task_text[:50] + ("..." if len(task_text) > 50 else "")

        table.add_row(
            row["agent_name"],
            row.get("project_name") or "-",
            f"[{color}]{status}[/{color}]",
            str(row["attempt"]),
            (row["started_at"] or "")[:19],
            duration,
            task_preview,
        )

    console.print(table)


# --- Projects ---


@cli.group()
def project() -> None:
    """Manage projects."""


@project.command("add")
@click.argument("name")
@click.argument("path")
@click.option("--description", "-d", default="", help="Project description.")
def project_add(name: str, path: str, description: str) -> None:
    """Register a new project."""
    init_db()
    from pathlib import Path as P

    resolved = str(P(path).resolve())
    p = Project(name=name, path=resolved, description=description)
    try:
        insert_project(p)
        console.print(f"[green]Project '{name}' registered at {resolved}[/green]")
    except Exception as e:
        console.print(f"[red]Failed to add project: {e}[/red]")


@project.command("list")
def project_list() -> None:
    """List all registered projects."""
    init_db()
    rows = list_projects()
    if not rows:
        console.print("[yellow]No projects registered.[/yellow]")
        return

    table = Table(title="Projects")
    table.add_column("Name", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Path")
    table.add_column("Description")

    status_colors = {"active": "green", "paused": "yellow", "archived": "dim"}

    for row in rows:
        color = status_colors.get(row["status"], "white")
        table.add_row(
            row["name"],
            f"[{color}]{row['status']}[/{color}]",
            row["path"],
            row["description"] or "-",
        )

    console.print(table)


@project.command("status")
@click.argument("name")
def project_status(name: str) -> None:
    """Show status of a specific project."""
    init_db()
    proj = get_project(name)
    if not proj:
        console.print(f"[red]Project not found: {name}[/red]")
        return

    console.print(f"[bold cyan]{proj['name']}[/bold cyan] — {proj['status']}")
    console.print(f"  Path: {proj['path']}")
    if proj["description"]:
        console.print(f"  Description: {proj['description']}")

    # Active tasks for this project
    agents = [a for a in list_active_agents() if a.get("project_name") == name]
    if agents:
        console.print(f"\n[bold]Active tasks ({len(agents)}):[/bold]")
        for a in agents:
            started = a["started_at"][:19] if a.get("started_at") else "?"
            console.print(f"  - {a['agent_name']} (since {started})")

    # Recent jobs
    recent = list_jobs(limit=5, project_name=name)
    if recent:
        console.print("\n[bold]Recent jobs:[/bold]")
        for j in recent:
            status_colors = {"success": "green", "failed": "red", "running": "yellow"}
            c = status_colors.get(j["status"], "white")
            task_preview = (j["task"] or "")[:60]
            console.print(f"  [{c}]{j['status']}[/{c}] {j['agent_name']}: {task_preview}")


# --- Skills ---


@cli.command()
@click.option("--project", default=None, help="Filter skills by project.")
def skills(project: str | None) -> None:
    """List available skills (global + per-project)."""
    init_db()
    found = discover_skills(project)
    if not found:
        console.print("[yellow]No skills found.[/yellow]")
        return

    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Scope", style="bold")
    table.add_column("Project")
    table.add_column("Description")

    for s in found:
        table.add_row(
            s.name,
            s.scope.value,
            s.project_name or "-",
            s.description[:80] if s.description else "-",
        )

    console.print(table)


# --- Schedules ---


@cli.group()
def schedule() -> None:
    """Manage scheduled tasks."""


@schedule.command("add")
@click.argument("name")
@click.argument("cron_expr")
@click.option("--task", default="", help="Task prompt to run.")
@click.option("--skill", default=None, help="Skill name to run.")
@click.option("--project", default=None, help="Project context.")
@click.option(
    "--allowed-tools",
    default=None,
    help="Comma-separated list of tools the agent can use.",
)
def schedule_add(
    name: str,
    cron_expr: str,
    task: str,
    skill: str | None,
    project: str | None,
    allowed_tools: str | None,
) -> None:
    """Add a scheduled task. CRON_EXPR is a 5-field cron expression (e.g. '0 9 * * *')."""
    init_db()
    if not task and not skill:
        console.print("[red]Provide --task or --skill[/red]")
        return

    tools_list = allowed_tools.split(",") if allowed_tools else None
    s = Schedule(
        name=name,
        cron_expr=cron_expr,
        task=task,
        skill_name=skill,
        project_name=project,
        allowed_tools=tools_list,
    )
    try:
        insert_schedule(s)
        console.print(f"[green]Schedule '{name}' added: {cron_expr}[/green]")
    except Exception as e:
        console.print(f"[red]Failed to add schedule: {e}[/red]")


@schedule.command("list")
def schedule_list() -> None:
    """List all scheduled tasks."""
    init_db()
    rows = list_schedules()
    if not rows:
        console.print("[yellow]No schedules configured.[/yellow]")
        return

    table = Table(title="Schedules")
    table.add_column("Name", style="cyan")
    table.add_column("Cron", style="bold")
    table.add_column("Task/Skill")
    table.add_column("Project")
    table.add_column("Enabled")

    for row in rows:
        task_or_skill = row["skill_name"] or (row["task"] or "")[:50]
        table.add_row(
            row["name"],
            row["cron_expr"],
            task_or_skill,
            row["project_name"] or "-",
            "[green]yes[/green]" if row["enabled"] else "[red]no[/red]",
        )

    console.print(table)


@schedule.command("remove")
@click.argument("name")
def schedule_remove(name: str) -> None:
    """Remove a scheduled task."""
    init_db()
    delete_schedule(name)
    console.print(f"[green]Schedule '{name}' removed.[/green]")


# --- Status ---


@cli.command()
def status() -> None:
    """Show active tasks and recent activity."""
    init_db()

    agents = list_active_agents()
    if agents:
        table = Table(title="Active Tasks")
        table.add_column("Agent", style="cyan")
        table.add_column("Project", style="magenta")
        table.add_column("Task")
        table.add_column("Running Since")
        table.add_column("Duration")

        now = datetime.now(UTC)
        for a in agents:
            task_preview = (a["task"] or "")[:50]
            started = a.get("started_at", "")
            duration = ""
            if started:
                start_dt = datetime.fromisoformat(started)
                delta = now - start_dt
                duration = f"{delta.total_seconds():.0f}s"
            table.add_row(
                a["agent_name"],
                a.get("project_name") or "-",
                task_preview,
                started[:19] if started else "?",
                duration,
            )
        console.print(table)
    else:
        console.print("[dim]No active tasks.[/dim]")

    console.print()

    recent = list_jobs(limit=5)
    if recent:
        console.print("[bold]Recent jobs:[/bold]")
        for j in recent:
            status_colors = {"success": "green", "failed": "red", "running": "yellow"}
            c = status_colors.get(j["status"], "white")
            agent = j["agent_name"]
            proj = j.get("project_name") or ""
            proj_str = f" ({proj})" if proj else ""
            console.print(f"  [{c}]{j['status']}[/{c}] {agent}{proj_str}")


# --- Start (scheduler daemon) ---


@cli.command()
@click.option(
    "--telegram/--no-telegram",
    default=None,
    help="Enable/disable Telegram listener (auto-detects from config).",
)
def start(telegram: bool | None) -> None:
    """Start the scheduler daemon (optionally with Telegram listener)."""
    init_db()

    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .scheduler import run_scheduler
    from .telegram import load_telegram_config, run_telegram_listener

    # Auto-detect Telegram if not explicitly set
    tg_enabled = telegram if telegram is not None else load_telegram_config() is not None

    async def _run_all() -> None:
        tasks = [run_scheduler()]
        if tg_enabled:
            tasks.append(run_telegram_listener())
        await asyncio.gather(*tasks)

    if tg_enabled:
        console.print("[bold cyan]Starting scheduler + Telegram listener...[/bold cyan]")
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

    from .telegram import load_telegram_config, run_telegram_listener

    if not load_telegram_config():
        console.print("[red]Telegram not configured.[/red]")
        console.print("[dim]Add botToken and chatId to ~/.mcp-telegram/config.json[/dim]")
        return

    console.print("[bold cyan]Starting Telegram listener...[/bold cyan]")
    try:
        asyncio.run(run_telegram_listener())
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


if __name__ == "__main__":
    cli()
