"""
PTY subprocess lifecycle manager.

Implements PTYManagerProtocol from interfaces.py.  One background asyncio read
task is created per session; it drains the PTY master fd, maintains the ring
buffer on PTYSession.output_buffer, and fans output chunks into per-consumer
queues so that output_stream() callers each receive every chunk independently.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
from datetime import UTC, datetime
from typing import AsyncIterator

import ptyprocess

from .interfaces import PTYManagerProtocol, PTYSession, SessionID

# Sentinel placed in per-consumer queues when the PTY process exits.
_EOF = None

# How many bytes to read from the PTY fd in a single non-blocking pass.
_READ_CHUNK = 4096


class PTYManager:
    """Concrete implementation of PTYManagerProtocol."""

    def __init__(self) -> None:
        # session_id → PTYSession (metadata + ring buffer)
        self._sessions: dict[SessionID, PTYSession] = {}
        # session_id → live PtyProcess handle (kept separate from the dataclass)
        self._procs: dict[SessionID, ptyprocess.PtyProcess] = {}
        # session_id → running background read task
        self._read_tasks: dict[SessionID, asyncio.Task[None]] = {}
        # session_id → list of per-consumer queues (fan-out)
        self._consumer_queues: dict[SessionID, list[asyncio.Queue[bytes | None]]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise internal state. No background work until spawn() is called."""
        # Nothing to do — state already initialised in __init__.
        # Kept as an explicit no-op so callers satisfy the protocol contract.
        pass

    async def close(self) -> None:
        """Cancel all read tasks and close open PTY fds."""
        for session_id in list(self._sessions):
            await self._teardown_session(session_id, close_fd=True)

    async def __aenter__(self) -> PTYManagerProtocol:
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    def spawn(
        self,
        cmd: list[str],
        session_id: SessionID,
        name: str,
        task: str,
        cwd: str | None = None,
    ) -> PTYSession:
        """Spawn a PTY subprocess and start its background read task."""
        if session_id in self._sessions:
            raise ValueError(f"Session already exists: {session_id!r}")

        proc = ptyprocess.PtyProcess.spawn(cmd, cwd=cwd)

        session = PTYSession(
            session_id=session_id,
            pid=proc.pid,
            fd=proc.fd,
            # ptyprocess exposes the slave name via .name
            pty_name=proc.name if hasattr(proc, "name") else "",
            cmd=cmd,
            started_at=datetime.now(UTC),
            cwd=cwd or "",
        )

        self._sessions[session_id] = session
        self._procs[session_id] = proc
        self._consumer_queues[session_id] = []

        # Start the background read task.
        # spawn() is always called from within a running event loop (Textual on_mount).
        loop = asyncio.get_running_loop()
        task_obj = loop.create_task(
            self._read_loop(session_id, proc),
            name=f"pty-read-{session_id}",
        )
        self._read_tasks[session_id] = task_obj

        return session

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, session_id: SessionID, data: bytes) -> None:
        """Write bytes to the PTY's stdin."""
        if session_id not in self._sessions:
            raise KeyError(session_id)
        self._procs[session_id].write(data)

    async def output_stream(self, session_id: SessionID) -> AsyncIterator[bytes]:
        """Async generator yielding byte chunks as they arrive from the PTY fd."""
        # Register a new per-consumer queue before we start iterating so we
        # don't miss any chunks that arrive between now and the first await.
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        queues = self._consumer_queues.get(session_id)
        if queues is None:
            # Session doesn't exist (or was already removed).
            return
        queues.append(q)
        try:
            while True:
                chunk = await q.get()
                if chunk is _EOF:
                    break
                yield chunk
        finally:
            # Clean up even if the consumer is cancelled.
            try:
                queues.remove(q)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def resize(self, session_id: SessionID, rows: int, cols: int) -> None:
        """Propagate a terminal resize to the PTY process."""
        self._procs[session_id].setwinsize(rows, cols)

    def kill(self, session_id: SessionID, *, hard: bool = False) -> None:
        """Send SIGTERM (hard=False) or SIGKILL (hard=True) and unregister the session."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        sig = signal.SIGKILL if hard else signal.SIGTERM
        try:
            os.kill(session.pid, sig)
        except ProcessLookupError:
            pass  # process already exited
        # Remove from registry; the read task will notice the fd is closed and exit.
        self._sessions.pop(session_id, None)
        self._procs.pop(session_id, None)
        self._consumer_queues.pop(session_id, None)
        task = self._read_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_session(self, session_id: SessionID) -> PTYSession | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[PTYSession]:
        return list(self._sessions.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_loop(self, session_id: SessionID, proc: ptyprocess.PtyProcess) -> None:
        """
        Background task: drain the PTY master fd until the process exits.

        The fd is set non-blocking; we use loop.add_reader to get an asyncio
        notification when data is available, avoiding any busy-spin or thread.
        """
        loop = asyncio.get_running_loop()
        fd = proc.fd

        # Non-blocking so os.read() returns immediately with BlockingIOError
        # when there's nothing to read yet.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        try:
            while True:
                # Wait for the fd to become readable before attempting a read.
                fut: asyncio.Future[None] = loop.create_future()
                loop.add_reader(fd, fut.set_result, None)
                try:
                    await fut
                finally:
                    loop.remove_reader(fd)

                # Drain all data currently available on the fd.
                while True:
                    try:
                        data = os.read(fd, _READ_CHUNK)
                    except BlockingIOError:
                        # Buffer drained; go back to waiting for the next readable event.
                        break
                    except OSError:
                        # EIO or similar — PTY hangup (process exited).
                        return

                    if not data:
                        return

                    self._deliver(session_id, data)
        finally:
            # Notify all waiting consumers that the stream is finished.
            self._broadcast_eof(session_id)
            # Clean up session state (without closing the fd — caller may still need it).
            await self._teardown_session(session_id, close_fd=False)

    def _deliver(self, session_id: SessionID, data: bytes) -> None:
        """Append data to the ring buffer and push to all consumer queues."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        # Ring-buffer trimming: keep the tail, drop the oldest bytes.
        buf = session.output_buffer
        max_size = session.output_buffer_max
        overflow = len(buf) + len(data) - max_size
        if overflow > 0:
            del buf[:overflow]
        buf.extend(data)

        # Fan out to all currently registered consumer queues.
        for q in list(self._consumer_queues.get(session_id, [])):
            q.put_nowait(data)

    def _broadcast_eof(self, session_id: SessionID) -> None:
        """Push the EOF sentinel to every consumer queue for this session."""
        for q in list(self._consumer_queues.get(session_id, [])):
            q.put_nowait(_EOF)

    async def _teardown_session(self, session_id: SessionID, *, close_fd: bool) -> None:
        """Cancel the read task and optionally close the PTY fd."""
        task = self._read_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if close_fd:
            session = self._sessions.get(session_id)
            if session is not None:
                try:
                    os.close(session.fd)
                except OSError:
                    pass

        self._sessions.pop(session_id, None)
        self._procs.pop(session_id, None)
        self._consumer_queues.pop(session_id, None)
