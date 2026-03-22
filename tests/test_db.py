from datetime import UTC, datetime

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.models import Execution, Job, JobStatus


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def test_init_db_creates_tables():
    with db_module.get_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [t["name"] for t in tables]
    assert "jobs" in names
    assert "executions" in names
    assert "active_agents" in names
    assert "memories" in names
    assert "orchestrator_state" in names


# --- Jobs ---


def test_insert_and_list_jobs():
    job = Job(name="test-job", task="test task")
    db_module.insert_job(job)

    rows = db_module.list_jobs()
    assert len(rows) == 1
    assert rows[0]["name"] == "test-job"
    assert rows[0]["task"] == "test task"


def test_get_job():
    job = Job(name="my-job", task="do stuff")
    db_module.insert_job(job)

    result = db_module.get_job("my-job")
    assert result is not None
    assert result["name"] == "my-job"

    assert db_module.get_job("nonexistent") is None


def test_update_job():
    job = Job(name="my-job", task="old task")
    db_module.insert_job(job)

    job.task = "new task"
    job.cron_expr = "0 9 * * *"
    db_module.update_job(job)

    result = db_module.get_job("my-job")
    assert result["task"] == "new task"
    assert result["cron_expr"] == "0 9 * * *"


def test_delete_job():
    job = Job(name="del-job", task="bye")
    db_module.insert_job(job)
    assert len(db_module.list_jobs()) == 1

    db_module.delete_job("del-job")
    assert len(db_module.list_jobs()) == 0


def test_list_jobs_cron_only():
    db_module.insert_job(Job(name="plain", task="no cron"))
    db_module.insert_job(Job(name="cron", task="with cron", cron_expr="0 9 * * *"))

    all_jobs = db_module.list_jobs()
    assert len(all_jobs) == 2

    cron_jobs = db_module.list_jobs(cron_only=True)
    assert len(cron_jobs) == 1
    assert cron_jobs[0]["name"] == "cron"


def test_list_jobs_empty():
    assert db_module.list_jobs() == []


# --- Executions ---


def test_insert_and_list_executions():
    ex = Execution(
        job_name="test-job",
        agent_name="test-agent",
        status=JobStatus.SUCCESS,
        started_at=datetime.now(UTC),
        output="done",
    )
    returned_id = db_module.insert_execution(ex)
    assert isinstance(returned_id, int)
    assert ex.id == returned_id

    rows = db_module.list_executions()
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "test-agent"
    assert rows[0]["job_name"] == "test-job"
    assert rows[0]["status"] == "success"
    assert rows[0]["output"] == "done"


def test_update_execution():
    ex = Execution(job_name="test-job", agent_name="test-agent")
    db_module.insert_execution(ex)

    ex.status = JobStatus.SUCCESS
    ex.output = "finished"
    ex.ended_at = datetime.now(UTC)
    db_module.update_execution(ex)

    rows = db_module.list_executions()
    assert rows[0]["status"] == "success"
    assert rows[0]["output"] == "finished"
    assert rows[0]["ended_at"] is not None


def test_list_executions_limit():
    for i in range(5):
        db_module.insert_execution(
            Execution(job_name="job", agent_name="agent", started_at=datetime.now(UTC))
        )

    assert len(db_module.list_executions(limit=3)) == 3
    assert len(db_module.list_executions(limit=10)) == 5


def test_list_executions_by_job():
    db_module.insert_execution(
        Execution(job_name="job-a", agent_name="a", started_at=datetime.now(UTC))
    )
    db_module.insert_execution(
        Execution(job_name="job-b", agent_name="b", started_at=datetime.now(UTC))
    )

    rows = db_module.list_executions(job_name="job-a")
    assert len(rows) == 1
    assert rows[0]["job_name"] == "job-a"


def test_list_executions_empty():
    assert db_module.list_executions() == []


def test_insert_returns_autoincrement_id():
    ex = Execution(job_name="job", agent_name="agent")
    assert ex.id is None
    db_module.insert_execution(ex)
    assert isinstance(ex.id, int)
    assert ex.id >= 1


# --- Active Agents ---


def test_active_agents_crud():
    ex = Execution(
        job_name="test-job",
        agent_name="test-agent",
        started_at=datetime.now(UTC),
        status=JobStatus.RUNNING,
    )
    db_module.insert_execution(ex)

    db_module.register_active_agent(ex)
    agents = db_module.list_active_agents()
    assert len(agents) == 1
    assert agents[0]["agent_name"] == "test-agent"
    assert agents[0]["job_name"] == "test-job"

    db_module.unregister_active_agent(ex.id)
    assert len(db_module.list_active_agents()) == 0


# --- Orchestrator State ---


def test_orchestrator_state_default():
    state = db_module.get_orchestrator_state()
    assert state.is_awake is False
    assert state.session_id is None
    assert state.total_messages_handled == 0


def test_upsert_orchestrator_state():
    state = db_module.get_orchestrator_state()
    state.is_awake = True
    state.session_id = "sess-123"
    state.core_briefing = "User prefers concise output"
    state.total_messages_handled = 42
    state.last_wake_at = datetime.now(UTC)
    db_module.upsert_orchestrator_state(state)

    loaded = db_module.get_orchestrator_state()
    assert loaded.is_awake is True
    assert loaded.session_id == "sess-123"
    assert loaded.core_briefing == "User prefers concise output"
    assert loaded.total_messages_handled == 42
    assert loaded.last_wake_at is not None


def test_increment_message_count():
    db_module.increment_message_count()
    db_module.increment_message_count()
    db_module.increment_message_count()
    state = db_module.get_orchestrator_state()
    assert state.total_messages_handled == 3


# --- Memory Access Tracking ---


def test_record_memory_access():
    from careful_claude_claw.models import Memory, MemoryType

    mem = Memory(memory_type=MemoryType.OBSERVATION, content="test fact")
    db_module.insert_memory(mem)

    db_module.record_memory_access(mem.id)
    db_module.record_memory_access(mem.id)

    row = db_module.get_memory(mem.id)
    assert row["access_count"] == 2
    assert row["last_accessed_at"] is not None
