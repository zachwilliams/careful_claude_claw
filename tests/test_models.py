from datetime import UTC, datetime

from careful_claude_claw.models import Job, JobStatus


def test_job_default_id():
    job = Job(agent_name="test", task="do something")
    assert job.id
    assert len(job.id) == 36  # UUID4 format


def test_job_default_status():
    job = Job(agent_name="test", task="do something")
    assert job.status == JobStatus.PENDING


def test_job_default_attempt():
    job = Job(agent_name="test", task="do something")
    assert job.attempt == 1


def test_job_status_transitions():
    job = Job(agent_name="test", task="do something")
    job.status = JobStatus.RUNNING
    assert job.status == JobStatus.RUNNING
    job.status = JobStatus.SUCCESS
    assert job.status == JobStatus.SUCCESS


def test_job_with_timestamps():
    now = datetime.now(UTC)
    job = Job(agent_name="test", task="do something", started_at=now)
    assert job.started_at == now
    assert job.ended_at is None


def test_job_unique_ids():
    job1 = Job(agent_name="test", task="task1")
    job2 = Job(agent_name="test", task="task2")
    assert job1.id != job2.id


def test_job_status_enum_values():
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.SUCCESS == "success"
    assert JobStatus.FAILED == "failed"
