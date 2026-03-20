import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.models import Job
from careful_claude_claw.scheduler import _parse_cron


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def test_insert_and_list_cron_jobs():
    job = Job(name="morning", task="Check emails", cron_expr="0 9 * * *")
    db_module.insert_job(job)

    rows = db_module.list_jobs(cron_only=True)
    assert len(rows) == 1
    assert rows[0]["name"] == "morning"
    assert rows[0]["cron_expr"] == "0 9 * * *"
    assert rows[0]["task"] == "Check emails"
    assert rows[0]["enabled"]


def test_delete_cron_job():
    job = Job(name="test", task="ping", cron_expr="*/5 * * * *")
    db_module.insert_job(job)
    assert len(db_module.list_jobs(cron_only=True)) == 1

    db_module.delete_job("test")
    assert len(db_module.list_jobs(cron_only=True)) == 0


def test_cron_job_with_skill():
    job = Job(
        name="review",
        cron_expr="0 10 * * 1-5",
        skill_name="code-review",
        cwd="/home/user/project",
    )
    db_module.insert_job(job)

    rows = db_module.list_jobs(cron_only=True)
    assert rows[0]["skill_name"] == "code-review"
    assert rows[0]["cwd"] == "/home/user/project"


def test_parse_cron_valid():
    trigger = _parse_cron("0 9 * * *")
    assert trigger is not None


def test_parse_cron_invalid():
    with pytest.raises(ValueError):
        _parse_cron("invalid")


def test_parse_cron_wrong_field_count():
    with pytest.raises(ValueError):
        _parse_cron("0 9 *")


def test_active_agents_crud():
    from datetime import UTC, datetime

    from careful_claude_claw.models import Execution, JobStatus

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
