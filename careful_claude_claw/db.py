import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Execution, Job, Memory

DB_PATH = Path("claw.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                source_id TEXT,
                tags TEXT,
                category TEXT,
                metadata TEXT,
                embedding BLOB,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        # FTS5 virtual table for full-text search
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, tags, category,
                content='memories', content_rowid='rowid'
            )
        """)
        # Triggers to keep FTS in sync
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags, category)
                VALUES (NEW.rowid, NEW.content, NEW.tags, NEW.category);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags, category)
                VALUES ('delete', OLD.rowid, OLD.content, OLD.tags, OLD.category);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags, category)
                VALUES ('delete', OLD.rowid, OLD.content, OLD.tags, OLD.category);
                INSERT INTO memories_fts(rowid, content, tags, category)
                VALUES (NEW.rowid, NEW.content, NEW.tags, NEW.category);
            END
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


# --- Memories ---


def insert_memory(memory: Memory) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (id, memory_type, content, source, source_id, "
            "tags, category, metadata, embedding, created_at, updated_at, "
            "expires_at, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory.id,
                memory.memory_type,
                memory.content,
                memory.source,
                memory.source_id,
                json.dumps(memory.tags),
                memory.category,
                json.dumps(memory.metadata) if memory.metadata else None,
                memory.embedding,
                memory.created_at.isoformat(),
                memory.updated_at.isoformat(),
                memory.expires_at.isoformat() if memory.expires_at else None,
                1 if memory.is_active else 0,
            ),
        )


def get_memory(memory_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM memories WHERE id=? AND is_active=1", (memory_id,)
        ).fetchone()
        return dict(row) if row else None


def update_memory(memory: Memory) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE memories SET memory_type=?, content=?, source=?, source_id=?, "
            "tags=?, category=?, metadata=?, embedding=?, updated_at=?, "
            "expires_at=?, is_active=? WHERE id=?",
            (
                memory.memory_type,
                memory.content,
                memory.source,
                memory.source_id,
                json.dumps(memory.tags),
                memory.category,
                json.dumps(memory.metadata) if memory.metadata else None,
                memory.embedding,
                memory.updated_at.isoformat(),
                memory.expires_at.isoformat() if memory.expires_at else None,
                1 if memory.is_active else 0,
                memory.id,
            ),
        )


def delete_memory(memory_id: str) -> None:
    """Soft delete — sets is_active=0."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE memories SET is_active=0, updated_at=? WHERE id=?",
            (datetime.now().isoformat(), memory_id),
        )


def list_memories(memory_type: str | None = None, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        if memory_type:
            rows = conn.execute(
                "SELECT * FROM memories WHERE is_active=1 AND memory_type=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (memory_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE is_active=1 ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def _fts_query(query: str) -> str:
    """Convert a plain text query into FTS5 prefix-match terms.

    Strips special characters, appends '*' to each word so 'database'
    matches 'databases'. Uses OR so any matching term returns results.
    """
    import re

    # Strip FTS5 special characters
    cleaned = re.sub(r"[^\w\s]", "", query)
    words = cleaned.strip().split()
    if not words:
        return ""
    return " OR ".join(f'"{w}"*' for w in words if w)


def search_memories_fts(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across memories using FTS5."""
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT m.* FROM memories m "
            "JOIN memories_fts f ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH ? AND m.is_active=1 "
            "ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def search_memories(
    query: str | None = None,
    memory_type: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search memories with optional FTS5 query, type, category, and tag filters."""
    with get_connection() as conn:
        if query:
            # FTS5 path
            fts_q = _fts_query(query)
            sql = (
                "SELECT m.* FROM memories m "
                "JOIN memories_fts f ON m.rowid = f.rowid "
                "WHERE memories_fts MATCH ? AND m.is_active=1"
            )
            params: list = [fts_q]
        else:
            sql = "SELECT * FROM memories m WHERE m.is_active=1"
            params = []

        if memory_type:
            sql += " AND m.memory_type=?"
            params.append(memory_type)

        if category:
            sql += " AND m.category=?"
            params.append(category)

        if tags:
            for tag in tags:
                sql += " AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value=?)"
                params.append(tag)

        sql += " ORDER BY m.updated_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def cleanup_expired_memories() -> int:
    """Soft-delete memories past their expires_at. Returns count affected."""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE memories SET is_active=0, updated_at=? "
            "WHERE is_active=1 AND expires_at IS NOT NULL AND expires_at < ?",
            (now, now),
        )
        return cursor.rowcount
