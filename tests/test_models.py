from careful_claude_claw.models import Execution, Job, JobStatus


def test_job_defaults():
    job = Job(name="test-job", task="do something")
    assert job.name == "test-job"
    assert job.enabled is True
    assert job.cron_expr is None
    assert job.cwd is None
    assert job.created_at is not None


def test_job_with_cron():
    job = Job(name="cron-job", task="check emails", cron_expr="0 9 * * *")
    assert job.cron_expr == "0 9 * * *"


def test_job_with_cwd():
    job = Job(name="proj-job", task="build", cwd="/home/user/project")
    assert job.cwd == "/home/user/project"


def test_execution_default_id():
    ex = Execution(job_name="test", agent_name="agent")
    assert ex.id
    assert len(ex.id) == 36  # UUID4 format


def test_execution_default_status():
    ex = Execution(job_name="test", agent_name="agent")
    assert ex.status == JobStatus.PENDING


def test_execution_default_attempt():
    ex = Execution(job_name="test", agent_name="agent")
    assert ex.attempt == 1


def test_execution_status_transitions():
    ex = Execution(job_name="test", agent_name="agent")
    ex.status = JobStatus.RUNNING
    assert ex.status == JobStatus.RUNNING
    ex.status = JobStatus.SUCCESS
    assert ex.status == JobStatus.SUCCESS


def test_execution_unique_ids():
    ex1 = Execution(job_name="test", agent_name="agent")
    ex2 = Execution(job_name="test", agent_name="agent")
    assert ex1.id != ex2.id


def test_job_status_enum_values():
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.SUCCESS == "success"
    assert JobStatus.FAILED == "failed"
    assert JobStatus.CANCELLED == "cancelled"
