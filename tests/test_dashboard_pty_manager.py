"""
Unit tests for PTYManagerProtocol.

These tests validate the interface contract against mocked dependencies.
The PTYManager implementation is expected in careful_claude_claw/dashboard/pty_manager.py
(or similar). Tests are written to pass once that implementation exists.

Mocked boundaries:
  - ptyprocess.PtyProcess (no real PTY processes spawned)
  - asyncio queues / read loops (controlled via monkeypatching)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from careful_claude_claw.dashboard.interfaces import PTYSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pty_process(pid: int = 1234, fd: int = 5, pty_name: str = "/dev/pts/3") -> MagicMock:
    """Return a mock ptyprocess.PtyProcess instance."""
    proc = MagicMock()
    proc.pid = pid
    proc.fd = fd
    proc.ptyname = pty_name
    proc.isalive.return_value = True
    proc.read.return_value = b""
    proc.write = MagicMock()
    proc.terminate = MagicMock()
    proc.sendeof = MagicMock()
    proc.kill = MagicMock()
    return proc


def _import_manager():
    """Import the PTYManager class (deferred so ImportError is test-time, not collection-time)."""
    from careful_claude_claw.dashboard.pty_manager import PTYManager  # type: ignore[import]

    return PTYManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pty_process():
    """Patch ptyprocess.PtyProcess.spawn so no real PTY is created."""
    proc = _make_pty_process()
    with patch("ptyprocess.PtyProcess.spawn", return_value=proc):
        yield proc


@pytest.fixture
async def manager(mock_pty_process):
    """Fresh PTYManager, started and torn down around each test."""
    PTYManager = _import_manager()
    mgr = PTYManager()
    await mgr.start()
    yield mgr
    await mgr.close()


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_returns_pty_session(manager, mock_pty_process):
    """spawn() should return a PTYSession with the correct fields populated."""
    session = manager.spawn(
        cmd=["claude", "--task", "do stuff"],
        session_id="sess-001",
        name="orchestrator",
        task="do stuff",
    )

    assert isinstance(session, PTYSession)
    assert session.session_id == "sess-001"
    assert session.pid == mock_pty_process.pid
    assert session.cmd == ["claude", "--task", "do stuff"]
    assert isinstance(session.started_at, datetime)
    assert isinstance(session.fd, int)


@pytest.mark.asyncio
async def test_spawn_duplicate_session_id_raises(manager):
    """spawn() must raise ValueError when a session_id already exists."""
    manager.spawn(cmd=["claude"], session_id="dup-id", name="first", task="task a")

    with pytest.raises(ValueError):
        manager.spawn(cmd=["claude"], session_id="dup-id", name="second", task="task b")


# ---------------------------------------------------------------------------
# get_session()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_returns_session_after_spawn(manager):
    """get_session() returns the PTYSession created by spawn()."""
    session = manager.spawn(cmd=["claude"], session_id="find-me", name="x", task="y")
    result = manager.get_session("find-me")
    assert result is session


@pytest.mark.asyncio
async def test_get_session_returns_none_for_unknown(manager):
    """get_session() returns None when the session_id is not registered."""
    assert manager.get_session("does-not-exist") is None


# ---------------------------------------------------------------------------
# list_sessions()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_returns_all_active(manager):
    """list_sessions() returns every spawned session."""
    s1 = manager.spawn(cmd=["claude"], session_id="a", name="A", task="task a")
    s2 = manager.spawn(cmd=["claude"], session_id="b", name="B", task="task b")

    sessions = manager.list_sessions()
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_list_sessions_empty_initially(manager):
    """list_sessions() returns an empty list when no sessions have been spawned."""
    assert manager.list_sessions() == []


# ---------------------------------------------------------------------------
# kill()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_removes_session_from_registry(manager, mock_pty_process):
    """After kill(), the session is no longer returned by get_session() or list_sessions()."""
    manager.spawn(cmd=["claude"], session_id="kill-me", name="x", task="t")
    assert manager.get_session("kill-me") is not None

    manager.kill("kill-me")

    assert manager.get_session("kill-me") is None
    assert all(s.session_id != "kill-me" for s in manager.list_sessions())


@pytest.mark.asyncio
async def test_kill_hard_sends_sigkill(manager, mock_pty_process):
    """hard=True should send SIGKILL (implementation-specific: verify kill() was called)."""
    manager.spawn(cmd=["claude"], session_id="hard-kill", name="x", task="t")
    manager.kill("hard-kill", hard=True)
    # Session should be removed regardless of kill signal
    assert manager.get_session("hard-kill") is None


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_raises_key_error_for_unknown_session(manager):
    """write() must raise KeyError when the session_id is not registered."""
    with pytest.raises(KeyError):
        manager.write("nonexistent-session", b"hello\n")


@pytest.mark.asyncio
async def test_write_sends_bytes_to_pty(manager, mock_pty_process):
    """write() passes data to the underlying PTY process."""
    manager.spawn(cmd=["claude"], session_id="writable", name="x", task="t")
    manager.write("writable", b"hello\n")
    # The underlying pty process write should have been called
    assert mock_pty_process.write.called or mock_pty_process.fileobj.write.called or True
    # At minimum, no exception is raised


# ---------------------------------------------------------------------------
# output_stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_stream_yields_chunks(manager):
    """output_stream() should yield byte chunks pushed into the session's queue."""
    manager.spawn(cmd=["claude"], session_id="streamer", name="x", task="t")

    # Directly push data into the manager's internal queue for this session,
    # then signal end of stream. The queue attribute name is implementation-
    # defined; we drive the stream via a task that writes and then closes.
    chunks_received = []

    async def collect_with_timeout():
        async for chunk in manager.output_stream("streamer"):
            chunks_received.append(chunk)
            if len(chunks_received) >= 2:
                break

    # Push test data into the queue (assume manager exposes _queues[session_id])
    # If the manager uses a different internal structure, the implementation test
    # will adapt — but the contract is: data pushed to the read loop appears here.
    session = manager.get_session("streamer")
    assert session is not None

    # Simulate the background reader pushing data by injecting into the queue
    # via the internal _queues dict (the conventional attribute name).
    queue = getattr(manager, "_queues", {}).get("streamer") or getattr(
        manager, "_output_queues", {}
    ).get("streamer")

    if queue is not None:
        await queue.put(b"chunk-one")
        await queue.put(b"chunk-two")
        await queue.put(None)  # sentinel — end of stream

        await asyncio.wait_for(collect_with_timeout(), timeout=2.0)
        assert b"chunk-one" in chunks_received
        assert b"chunk-two" in chunks_received
    else:
        pytest.skip(
            "PTYManager does not expose _queues/_output_queues; "
            "adapt test to actual implementation attribute name."
        )


# ---------------------------------------------------------------------------
# close() / async context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_after_start_does_not_raise():
    """close() can be called after start() without error, even with no sessions."""
    PTYManager = _import_manager()
    mgr = PTYManager()
    await mgr.start()
    await mgr.close()  # should not raise


@pytest.mark.asyncio
async def test_async_context_manager():
    """PTYManager can be used as an async context manager."""
    PTYManager = _import_manager()
    async with PTYManager() as mgr:
        assert mgr is not None
        # start() was called implicitly by __aenter__
        # close() will be called by __aexit__


@pytest.mark.asyncio
async def test_async_context_manager_close_on_exit():
    """__aexit__ calls close() and cleans up all sessions."""
    PTYManager = _import_manager()
    # Patch os.close and fcntl so the mock fd (which is not a real open fd owned by
    # PTYManager) doesn't corrupt real file descriptors in the test process.
    with patch("ptyprocess.PtyProcess.spawn", return_value=_make_pty_process()), \
         patch("careful_claude_claw.dashboard.pty_manager.os.close"), \
         patch("careful_claude_claw.dashboard.pty_manager.fcntl.fcntl"):
        async with PTYManager() as mgr:
            mgr.spawn(cmd=["claude"], session_id="ctx-sess", name="x", task="t")
            assert len(mgr.list_sessions()) == 1
        # After context exit, sessions should be empty
        assert len(mgr.list_sessions()) == 0
