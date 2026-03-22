import math

from careful_claude_claw.models import (
    Execution,
    Job,
    JobStatus,
    Memory,
    MemoryType,
    OrchestratorState,
    score_memory,
)


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
    assert ex.id is None  # None before insert


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


def test_job_status_enum_values():
    assert JobStatus.PENDING == "pending"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.SUCCESS == "success"
    assert JobStatus.FAILED == "failed"
    assert JobStatus.CANCELLED == "cancelled"


def test_memory_type_enum():
    assert MemoryType.PREFERENCE == "preference"
    assert MemoryType.DECISION == "decision"
    assert MemoryType.OBSERVATION == "observation"
    assert MemoryType.PROCEDURE == "procedure"


def test_memory_defaults():
    mem = Memory(memory_type=MemoryType.OBSERVATION, content="test")
    assert mem.id is None
    assert mem.importance == 0.5
    assert mem.decay_rate == 0.0
    assert mem.access_count == 0
    assert mem.last_accessed_at is None


def test_score_memory_permanent():
    mem = Memory(memory_type=MemoryType.PREFERENCE, content="test", importance=1.0, decay_rate=0.0)
    score = score_memory(mem)
    # No decay, importance 1.0, type weight 1.2
    assert abs(score - 1.2) < 0.01


def test_score_memory_with_decay():
    from datetime import datetime, timedelta

    mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="test",
        importance=1.0,
        decay_rate=0.3,
        updated_at=datetime.now() - timedelta(days=7),
    )
    score = score_memory(mem)
    expected = math.exp(-0.3 * 7) * 1.0 * 0.8
    assert abs(score - expected) < 0.01


def test_orchestrator_state_defaults():
    state = OrchestratorState()
    assert state.session_id is None
    assert state.is_awake is False
    assert state.core_briefing == ""
    assert state.total_messages_handled == 0
