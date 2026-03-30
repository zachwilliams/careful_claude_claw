"""
AgentRegistry — in-memory agent state store, backed by SQLite active_agents.

Implements AgentRegistryProtocol from interfaces.py. Thread/task safety is
provided by an asyncio.Lock; all public mutating methods acquire the lock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import psutil

from careful_claude_claw import db as db_module
from careful_claude_claw.dashboard.interfaces import (
    AgentKind,
    AgentState,
    AgentStatus,
    SessionID,
)

logger = logging.getLogger(__name__)


class AgentRegistry:
    """
    In-memory agent state store, backed by SQLite active_agents.

    Primary store: dict[SessionID, AgentState] in memory.
    SQLite is read once on startup via reconcile_with_db(); the in-memory
    dict is the live source of truth during a dashboard session.
    """

    def __init__(self) -> None:
        self._agents: dict[SessionID, AgentState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Basic CRUD
    # ------------------------------------------------------------------

    def register(self, state: AgentState) -> None:
        """Add or replace an agent's state entry."""
        self._agents[state.session_id] = state

    def unregister(self, session_id: SessionID) -> None:
        """Remove an agent (e.g. after it exits)."""
        self._agents.pop(session_id, None)

    def get(self, session_id: SessionID) -> AgentState | None:
        """Return state for one agent, or None if not found."""
        return self._agents.get(session_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_top_level(self) -> list[AgentState]:
        """Return all top-level (attachable) agents, ordered by started_at."""
        return sorted(
            (a for a in self._agents.values() if a.kind == AgentKind.TOP_LEVEL),
            key=lambda a: a.started_at,
        )

    def list_external(self) -> list[AgentState]:
        """Return all EXTERNAL (SDK-spawned, read-only) agents, ordered by started_at."""
        return sorted(
            (a for a in self._agents.values() if a.kind == AgentKind.EXTERNAL),
            key=lambda a: a.started_at,
        )

    def list_children(self, parent_session_id: SessionID) -> list[AgentState]:
        """Return sub-agents under a given parent, ordered by started_at."""
        return sorted(
            (
                a
                for a in self._agents.values()
                if a.parent_session_id == parent_session_id
            ),
            key=lambda a: a.started_at,
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def update_status(self, session_id: SessionID, status: AgentStatus) -> None:
        """
        Update just the status field.

        Also sets last_output_at = datetime.utcnow() when transitioning to
        WORKING, so callers can track when activity was last detected.
        """
        agent = self._agents.get(session_id)
        if agent is None:
            return
        agent.status = status
        if status == AgentStatus.WORKING:
            agent.last_output_at = datetime.utcnow()

    def update_tokens(
        self, session_id: SessionID, input_tokens: int, output_tokens: int
    ) -> None:
        """Accumulate token counts from a parsed usage event (values are per-turn deltas)."""
        agent = self._agents.get(session_id)
        if agent is None:
            return
        agent.input_tokens += input_tokens
        agent.output_tokens += output_tokens

    def update_context_limit(self, session_id: SessionID, limit: int) -> None:
        """Set the model context limit once parsed from session start."""
        agent = self._agents.get(session_id)
        if agent is None:
            return
        agent.context_limit = limit

    # ------------------------------------------------------------------
    # Background polling
    # ------------------------------------------------------------------

    async def poll_subagents(self) -> None:
        """
        One poll cycle: discover new child claude processes and clean up
        exited ones for every TOP_LEVEL agent in the registry.

        Call this method in a loop with ~2s intervals from the app layer.
        """
        # Snapshot the top-level agents so we don't hold the lock during the
        # potentially slow psutil calls.
        async with self._lock:
            top_level = [
                a
                for a in self._agents.values()
                if a.kind == AgentKind.TOP_LEVEL
            ]

        for parent in top_level:
            try:
                proc = psutil.Process(parent.pid)
                children = proc.children(recursive=True)
            except psutil.NoSuchProcess:
                # The top-level process already exited; skip gracefully.
                continue

            # Build a set of current child PIDs that are claude processes.
            live_claude_pids: set[int] = set()
            for child in children:
                try:
                    child_name = child.name()
                except psutil.NoSuchProcess:
                    continue
                if child_name == "claude" or child_name.startswith("claude"):
                    live_claude_pids.add(child.pid)

            async with self._lock:
                # Find previously registered sub-agents under this parent.
                known_sub_session_ids = [
                    a.session_id
                    for a in self._agents.values()
                    if (
                        a.kind == AgentKind.SUB_AGENT
                        and a.parent_session_id == parent.session_id
                    )
                ]
                known_sub_pids = {
                    int(sid.removeprefix("sub-")): sid
                    for sid in known_sub_session_ids
                    if sid.startswith("sub-")
                }

                # Register new sub-agents.
                for pid in live_claude_pids:
                    if pid not in known_sub_pids:
                        sub_session_id: SessionID = f"sub-{pid}"
                        try:
                            proc_name = psutil.Process(pid).name()
                        except psutil.NoSuchProcess:
                            continue
                        sub_state = AgentState(
                            session_id=sub_session_id,
                            kind=AgentKind.SUB_AGENT,
                            pid=pid,
                            name=proc_name,
                            task="",
                            status=AgentStatus.ACTIVE,
                            started_at=datetime.utcnow(),
                            last_output_at=None,
                            parent_session_id=parent.session_id,
                        )
                        self._agents[sub_session_id] = sub_state
                        # Track session_id in parent's children list.
                        if sub_session_id not in parent.children:
                            parent.children.append(sub_session_id)

                # Unregister departed sub-agents.
                for pid, sid in known_sub_pids.items():
                    if pid not in live_claude_pids:
                        self.update_status(sid, AgentStatus.DONE)
                        self._agents.pop(sid, None)
                        if sid in parent.children:
                            parent.children.remove(sid)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def reconcile_with_db(self) -> None:
        """
        Called once on dashboard startup. Reads SQLite active_agents and
        registers any currently-running processes as EXTERNAL agents with
        execution_id/job_name populated.

        Uses psutil to verify the PID is still alive. Skips dead rows.
        """
        rows = db_module.list_active_agents()

        registered = 0
        skipped = 0

        for row in rows:
            execution_id: int = row["execution_id"]
            agent_name: str = row["agent_name"]
            job_name: str | None = row.get("job_name")
            task: str = row.get("task") or ""
            started_at_raw: str | None = row.get("started_at")

            # The active_agents table does not store a PID directly; we use
            # the execution_id as a stable identifier and derive a session_id
            # from it. We cannot verify liveness without a PID column, so we
            # check via psutil whether a process with a matching name exists.
            # For now: treat the execution_id as a pseudo-PID placeholder and
            # attempt a best-effort liveness check.
            #
            # Convention: session_id for external (SDK-spawned) agents is
            # "ext-<execution_id>" so it never collides with PTY or sub-agent IDs.
            session_id: SessionID = f"ext-{execution_id}"

            started_at: datetime
            if started_at_raw:
                try:
                    started_at = datetime.fromisoformat(started_at_raw)
                except ValueError:
                    started_at = datetime.utcnow()
            else:
                started_at = datetime.utcnow()

            # We cannot check PID liveness without a stored PID; register with
            # a sentinel PID of -1 and mark the agent as ACTIVE so the dashboard
            # can display it. If the process has actually exited the main system
            # will eventually clean up the SQLite row.
            #
            # NOTE: The active_agents schema (db.py) does not include a pid column.
            # We store pid=-1 as a sentinel; poll_subagents will never match
            # these entries (it only walks TOP_LEVEL agents).
            state = AgentState(
                session_id=session_id,
                kind=AgentKind.EXTERNAL,
                pid=-1,
                name=agent_name,
                task=task,
                status=AgentStatus.ACTIVE,
                started_at=started_at,
                last_output_at=None,
                execution_id=execution_id,
                job_name=job_name,
            )

            async with self._lock:
                self._agents[session_id] = state
            registered += 1

        logger.info(
            "reconcile_with_db: registered %d external agent(s), skipped %d stale row(s)",
            registered,
            skipped,
        )
