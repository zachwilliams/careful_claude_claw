import sqlite3
from pathlib import Path

from .models import Job, Project, Schedule

DB_PATH = Path("claw.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                project_name TEXT,
                started_at TEXT,
                ended_at TEXT,
                output TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                name TEXT PRIMARY KEY,
                cron_expr TEXT NOT NULL,
                task TEXT DEFAULT '',
                skill_name TEXT,
                project_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                allowed_tools TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_agents (
                job_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                project_name TEXT,
                task TEXT NOT NULL,
                started_at TEXT NOT NULL
            )
        """)


# --- Jobs ---


def insert_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO jobs (id, agent_name, task, status, attempt, project_name, "
            "started_at, ended_at, output, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.agent_name,
                job.task,
                job.status,
                job.attempt,
                job.project_name,
                job.started_at.isoformat() if job.started_at else None,
                job.ended_at.isoformat() if job.ended_at else None,
                job.output,
                job.error,
            ),
        )


def update_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, attempt=?, started_at=?, ended_at=?, output=?, error=? "
            "WHERE id=?",
            (
                job.status,
                job.attempt,
                job.started_at.isoformat() if job.started_at else None,
                job.ended_at.isoformat() if job.ended_at else None,
                job.output,
                job.error,
                job.id,
            ),
        )


def update_job_status(job_id: str, status: str) -> None:
    """Update just the status of a job by ID."""
    with get_connection() as conn:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


def list_jobs(limit: int = 20, project_name: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if project_name:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_name=? ORDER BY started_at DESC LIMIT ?",
                (project_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


# --- Projects ---


def insert_project(project: Project) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (name, path, status, description, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project.name,
                project.path,
                project.status,
                project.description,
                project.created_at.isoformat(),
            ),
        )


def update_project(project: Project) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE projects SET path=?, status=?, description=? WHERE name=?",
            (project.path, project.status, project.description, project.name),
        )


def get_project(name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def list_projects() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


# --- Schedules ---


def insert_schedule(schedule: Schedule) -> None:
    import json

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO schedules "
            "(name, cron_expr, task, skill_name, project_name, enabled, allowed_tools) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                schedule.name,
                schedule.cron_expr,
                schedule.task,
                schedule.skill_name,
                schedule.project_name,
                1 if schedule.enabled else 0,
                json.dumps(schedule.allowed_tools) if schedule.allowed_tools else None,
            ),
        )


def delete_schedule(name: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM schedules WHERE name=?", (name,))


def list_schedules() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return [dict(row) for row in rows]


# --- Active Agents ---


def register_active_agent(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO active_agents "
            "(job_id, agent_name, project_name, task, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                job.id,
                job.agent_name,
                job.project_name,
                job.task,
                job.started_at.isoformat() if job.started_at else None,
            ),
        )


def unregister_active_agent(job_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM active_agents WHERE job_id=?", (job_id,))


def list_active_agents() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM active_agents ORDER BY started_at").fetchall()
        return [dict(row) for row in rows]
