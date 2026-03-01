from datetime import UTC, datetime

import careful_claude_claw.db as db_module
import pytest

from careful_claude_claw.models import Job, JobStatus


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def test_init_db_creates_jobs_table():
    with db_module.get_connection() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = [t["name"] for t in tables]
    assert "jobs" in names


def test_insert_and_list_jobs():
    job = Job(
        agent_name="test-agent",
        task="test task",
        status=JobStatus.SUCCESS,
        started_at=datetime.now(UTC),
        output="done",
    )
    db_module.insert_job(job)

    rows = db_module.list_jobs()
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "test-agent"
    assert rows[0]["task"] == "test task"
    assert rows[0]["status"] == "success"
    assert rows[0]["output"] == "done"


def test_update_job():
    job = Job(agent_name="test-agent", task="test task")
    db_module.insert_job(job)

    job.status = JobStatus.SUCCESS
    job.output = "finished"
    job.ended_at = datetime.now(UTC)
    db_module.update_job(job)

    rows = db_module.list_jobs()
    assert rows[0]["status"] == "success"
    assert rows[0]["output"] == "finished"
    assert rows[0]["ended_at"] is not None


def test_list_jobs_limit():
    for i in range(5):
        db_module.insert_job(Job(agent_name="agent", task=f"task {i}"))

    assert len(db_module.list_jobs(limit=3)) == 3
    assert len(db_module.list_jobs(limit=10)) == 5


def test_list_jobs_empty():
    assert db_module.list_jobs() == []


def test_insert_preserves_id():
    job = Job(agent_name="agent", task="task")
    original_id = job.id
    db_module.insert_job(job)
    rows = db_module.list_jobs()
    assert rows[0]["id"] == original_id
