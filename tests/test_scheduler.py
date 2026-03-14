import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.models import Schedule
from careful_claude_claw.scheduler import _parse_cron


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def test_insert_and_list_schedules():
    s = Schedule(name="morning", cron_expr="0 9 * * *", task="Check emails")
    db_module.insert_schedule(s)

    rows = db_module.list_schedules()
    assert len(rows) == 1
    assert rows[0]["name"] == "morning"
    assert rows[0]["cron_expr"] == "0 9 * * *"
    assert rows[0]["task"] == "Check emails"
    assert rows[0]["enabled"]


def test_delete_schedule():
    s = Schedule(name="test", cron_expr="*/5 * * * *", task="ping")
    db_module.insert_schedule(s)
    assert len(db_module.list_schedules()) == 1

    db_module.delete_schedule("test")
    assert len(db_module.list_schedules()) == 0


def test_schedule_with_skill():
    s = Schedule(
        name="review",
        cron_expr="0 10 * * 1-5",
        skill_name="code-review",
        project_name="myproj",
    )
    db_module.insert_schedule(s)

    rows = db_module.list_schedules()
    assert rows[0]["skill_name"] == "code-review"
    assert rows[0]["project_name"] == "myproj"


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

    from careful_claude_claw.models import Job, JobStatus

    job = Job(
        agent_name="test-agent",
        task="test task",
        project_name="proj1",
        started_at=datetime.now(UTC),
        status=JobStatus.RUNNING,
    )

    db_module.register_active_agent(job)
    agents = db_module.list_active_agents()
    assert len(agents) == 1
    assert agents[0]["agent_name"] == "test-agent"
    assert agents[0]["project_name"] == "proj1"

    db_module.unregister_active_agent(job.id)
    assert len(db_module.list_active_agents()) == 0
