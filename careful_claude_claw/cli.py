import asyncio
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from .agent import DEFAULT_TASK, run_agent
from .db import init_db, list_jobs
from .models import JobStatus

console = Console()


@click.group()
def cli() -> None:
    """CarefulClaudeClaw — security-first Claude Code orchestrator."""


@cli.command()
@click.option("--task", default=DEFAULT_TASK, help="Task for the agent to execute.")
@click.option("--agent-name", default="demo", help="Name to identify this agent.")
@click.option("--cwd", default=None, help="Working directory for the agent.")
@click.option("--max-attempts", default=2, help="Maximum retry attempts on failure.")
@click.option("--backoff", default=5, help="Seconds to wait between retry attempts.")
def run(task: str, agent_name: str, cwd: str | None, max_attempts: int, backoff: int) -> None:
    """Run a one-shot agent task and log the result to SQLite."""
    init_db()
    console.print(f"[bold cyan]Agent:[/bold cyan] {agent_name}")
    console.print(f"[bold cyan]Task:[/bold cyan] {task}")
    console.print()

    with console.status("[bold green]Running agent...[/bold green]"):
        job = asyncio.run(
            run_agent(
                agent_name=agent_name,
                task=task,
                max_attempts=max_attempts,
                backoff_seconds=backoff,
                cwd=cwd,
            )
        )

    if job.status == JobStatus.SUCCESS:
        console.print("[bold green]✓ Success[/bold green]")
        if job.output:
            console.print()
            console.print(job.output)
    else:
        console.print("[bold red]✗ Failed[/bold red]")
        if job.error:
            console.print(f"[red]{job.error}[/red]")

    console.print()
    console.print(f"[dim]Job ID: {job.id}[/dim]")


@cli.command()
@click.option("--limit", default=20, help="Number of recent jobs to display.")
def jobs(limit: int) -> None:
    """Show recent job history from SQLite."""
    init_db()

    rows = list_jobs(limit=limit)
    if not rows:
        console.print("[yellow]No jobs found.[/yellow]")
        return

    table = Table(title="Recent Jobs")
    table.add_column("Agent", style="cyan")
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
        task_preview = task_text[:50] + ("…" if len(task_text) > 50 else "")

        table.add_row(
            row["agent_name"],
            f"[{color}]{status}[/{color}]",
            str(row["attempt"]),
            (row["started_at"] or "")[:19],
            duration,
            task_preview,
        )

    console.print(table)


if __name__ == "__main__":
    cli()
