import sqlite3
from pathlib import Path

from .models import Job

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
                started_at TEXT,
                ended_at TEXT,
                output TEXT,
                error TEXT
            )
        """)


def insert_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO jobs (id, agent_name, task, status, attempt, started_at, ended_at, output, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.agent_name,
                job.task,
                job.status,
                job.attempt,
                job.started_at.isoformat() if job.started_at else None,
                job.ended_at.isoformat() if job.ended_at else None,
                job.output,
                job.error,
            ),
        )


def update_job(job: Job) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, attempt=?, started_at=?, ended_at=?, output=?, error=? WHERE id=?",
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


def list_jobs(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
