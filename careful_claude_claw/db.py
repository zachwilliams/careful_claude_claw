import json
import sqlite3
from pathlib import Path

from .models import Execution, Job

DB_PATH = Path("claw.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        # This is just in early stages. 
        # We should add migration management here eventually
        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.execute("DROP TABLE IF EXISTS executions")
        conn.execute("DROP TABLE IF EXISTS active_agents")
        conn.execute("DROP TABLE IF EXISTS projects")
        conn.execute("DROP TABLE IF EXISTS schedules")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                name TEXT PRIMARY KEY,
                task TEXT DEFAULT '',
                skill_name TEXT,
                cron_expr TEXT,
                cwd TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                allowed_tools TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                job_name TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                started_at TEXT,
                ended_at TEXT,
                output TEXT,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_agents (
                execution_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                job_name TEXT,
                task TEXT NOT NULL,
                started_at TEXT NOT NULL
            )
        """)


# --- Jobs ---


def insert_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO jobs (name, task, skill_name, cron_expr, cwd, "
            "enabled, allowed_tools, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.name,
                job.task,
                job.skill_name,
                job.cron_expr,
                job.cwd,
                1 if job.enabled else 0,
                json.dumps(job.allowed_tools) if job.allowed_tools else None,
                job.created_at.isoformat(),
            ),
        )


def update_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET task=?, skill_name=?, cron_expr=?, cwd=?, enabled=?, allowed_tools=? "
            "WHERE name=?",
            (
                job.task,
                job.skill_name,
                job.cron_expr,
                job.cwd,
                1 if job.enabled else 0,
                json.dumps(job.allowed_tools) if job.allowed_tools else None,
                job.name,
            ),
        )


def get_job(name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def delete_job(name: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM jobs WHERE name=?", (name,))


def list_jobs(cron_only: bool = False) -> list[dict]:
    with get_connection() as conn:
        if cron_only:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE cron_expr IS NOT NULL ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]


# --- Executions ---


def insert_execution(execution: Execution) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO executions (id, job_name, agent_name, status, attempt, "
            "started_at, ended_at, output, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution.id,
                execution.job_name,
                execution.agent_name,
                execution.status,
                execution.attempt,
                execution.started_at.isoformat() if execution.started_at else None,
                execution.ended_at.isoformat() if execution.ended_at else None,
                execution.output,
                execution.error,
            ),
        )


def update_execution(execution: Execution) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE executions SET status=?, attempt=?, started_at=?, ended_at=?, "
            "output=?, error=? WHERE id=?",
            (
                execution.status,
                execution.attempt,
                execution.started_at.isoformat() if execution.started_at else None,
                execution.ended_at.isoformat() if execution.ended_at else None,
                execution.output,
                execution.error,
                execution.id,
            ),
        )


def update_execution_status(execution_id: str, status: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE executions SET status=? WHERE id=?", (status, execution_id))


def list_executions(limit: int = 20, job_name: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if job_name:
            rows = conn.execute(
                "SELECT * FROM executions WHERE job_name=? ORDER BY started_at DESC LIMIT ?",
                (job_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


# --- Active Agents ---


def register_active_agent(execution: Execution) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO active_agents "
            "(execution_id, agent_name, job_name, task, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                execution.id,
                execution.agent_name,
                execution.job_name,
                "",
                execution.started_at.isoformat() if execution.started_at else None,
            ),
        )


def unregister_active_agent(execution_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM active_agents WHERE execution_id=?", (execution_id,))


def list_active_agents() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM active_agents ORDER BY started_at").fetchall()
        return [dict(row) for row in rows]
