"""Tests for agent session registry and lifecycle."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.agent_session import (
    AGENT_SESSIONS,
    AgentSession,
    _cleanup_workspace,
    generate_name,
    get_session,
    kill_all_sessions,
    kill_session,
    list_sessions,
    register_session,
    send_to_agent,
    unregister_session,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure the session registry is clean before and after each test."""
    AGENT_SESSIONS.clear()
    yield
    AGENT_SESSIONS.clear()


@pytest.fixture(autouse=True)
def reset_counter():
    """Reset the name counter between tests."""
    import careful_claude_claw.agent_session as mod

    mod._name_counter = 0
    yield
    mod._name_counter = 0


def _make_session(name: str = "task-1") -> AgentSession:
    client = MagicMock()
    client.interrupt = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    return AgentSession(name=name, execution_id="test-exec-id", client=client)


# --- generate_name ---


def test_generate_name_increments():
    n1 = generate_name()
    n2 = generate_name()
    assert n1 == "T1"
    assert n2 == "T2"


def test_generate_name_custom_prefix():
    n = generate_name("S")
    assert n == "S1"


def test_generate_name_skips_collision():
    session = _make_session("T1")
    register_session(session)
    n = generate_name()
    assert n == "T2"


# --- register / unregister / get / list ---


def test_register_and_get():
    session = _make_session("foo")
    register_session(session)
    assert get_session("foo") is session


def test_get_missing_returns_none():
    assert get_session("nonexistent") is None


def test_unregister():
    session = _make_session("bar")
    register_session(session)
    unregister_session("bar")
    assert get_session("bar") is None


def test_unregister_missing_is_noop():
    unregister_session("nope")  # should not raise


def test_list_sessions():
    s1 = _make_session("a")
    s2 = _make_session("b")
    register_session(s1)
    register_session(s2)
    sessions = list_sessions()
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"a", "b"}


# --- case-insensitive lookup ---


def test_get_session_case_insensitive():
    session = _make_session("T1")
    register_session(session)
    assert get_session("t1") is session
    assert get_session("T1") is session


@pytest.mark.asyncio
async def test_kill_session_case_insensitive():
    session = _make_session("T2")
    session.client.interrupt = AsyncMock()
    session.client.disconnect = AsyncMock()
    register_session(session)
    result = await kill_session("t2")
    assert result is True
    assert get_session("T2") is None


@pytest.mark.asyncio
async def test_send_to_agent_case_insensitive():
    session = _make_session("T3")
    session.client.query = AsyncMock()
    register_session(session)
    result = await send_to_agent("t3", "hello")
    assert result is True
    session.client.query.assert_awaited_once_with("hello")


# --- kill_session ---


@pytest.mark.asyncio
async def test_kill_session_success():
    from careful_claude_claw.models import Execution

    session = _make_session("kill-me")
    ex = Execution(
        id="test-exec-id",
        job_name="test-job",
        agent_name="test",
        started_at=datetime.now(UTC),
    )
    db_module.insert_execution(ex)
    db_module.register_active_agent(ex)

    session.task = MagicMock()
    session.task.done.return_value = False
    session.task.cancel = MagicMock()
    register_session(session)

    result = await kill_session("kill-me")
    assert result is True
    assert get_session("kill-me") is None
    session.client.interrupt.assert_awaited_once()
    session.task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_kill_session_not_found():
    result = await kill_session("nope")
    assert result is False


# --- kill_all_sessions ---


@pytest.mark.asyncio
async def test_kill_all_sessions():
    from careful_claude_claw.models import Execution

    for i in range(3):
        exec_id = f"exec-{i}"
        ex = Execution(
            id=exec_id,
            job_name="test-job",
            agent_name="test",
            started_at=datetime.now(UTC),
        )
        db_module.insert_execution(ex)
        db_module.register_active_agent(ex)
        session = _make_session(f"agent-{i}")
        session.execution_id = exec_id
        register_session(session)

    count = await kill_all_sessions()
    assert count == 3
    assert len(AGENT_SESSIONS) == 0


# --- send_to_agent ---


@pytest.mark.asyncio
async def test_send_to_agent_success():
    session = _make_session("responder")
    register_session(session)

    result = await send_to_agent("responder", "hello")
    assert result is True
    session.client.query.assert_awaited_once_with("hello")


@pytest.mark.asyncio
async def test_send_to_agent_not_found():
    result = await send_to_agent("nope", "hello")
    assert result is False


@pytest.mark.asyncio
async def test_send_to_agent_error():
    session = _make_session("error-agent")
    session.client.query = AsyncMock(side_effect=Exception("boom"))
    register_session(session)

    result = await send_to_agent("error-agent", "hello")
    assert result is False


# --- workspace cleanup ---


def test_cleanup_workspace_temp(tmp_path):
    """Temp workspace should be deleted on cleanup."""
    workspace = tmp_path / "workspace" / "T1"
    workspace.mkdir(parents=True)
    (workspace / "somefile.txt").write_text("data")

    session = _make_session("T1")
    session.cwd = workspace
    session.is_temp_workspace = True

    _cleanup_workspace(session)
    assert not workspace.exists()


def test_cleanup_workspace_project(tmp_path):
    """Project workspace (is_temp_workspace=False) should NOT be deleted."""
    workspace = tmp_path / "my_project"
    workspace.mkdir(parents=True)
    (workspace / "code.py").write_text("print('hi')")

    session = _make_session("S1")
    session.cwd = workspace
    session.is_temp_workspace = False

    _cleanup_workspace(session)
    assert workspace.exists()
    assert (workspace / "code.py").exists()


@pytest.mark.asyncio
async def test_kill_session_cleans_workspace(tmp_path):
    """Kill should trigger workspace cleanup for temp workspaces."""
    from careful_claude_claw.models import Execution

    workspace = tmp_path / "workspace" / "K1"
    workspace.mkdir(parents=True)
    (workspace / "temp.txt").write_text("temp data")

    exec_id = "kill-cleanup-exec"
    ex = Execution(
        id=exec_id,
        job_name="test-job",
        agent_name="test",
        started_at=datetime.now(UTC),
    )
    db_module.insert_execution(ex)
    db_module.register_active_agent(ex)

    session = _make_session("K1")
    session.execution_id = exec_id
    session.cwd = workspace
    session.is_temp_workspace = True
    session.task = MagicMock()
    session.task.done.return_value = False
    session.task.cancel = MagicMock()
    register_session(session)

    result = await kill_session("K1")
    assert result is True
    assert not workspace.exists()
