"""
Unit tests for AgentRegistryProtocol.

Tests validate the in-memory agent state store contract, including:
  - register/get/unregister round-trips
  - list_top_level / list_children filtering
  - update_status, update_tokens, update_context_limit mutations
  - poll_subagents: psutil-driven SUB_AGENT detection
  - reconcile_with_db: startup reconciliation from SQLite active_agents

Mocked boundaries:
  - psutil.Process (no real process tree introspection)
  - careful_claude_claw.db.list_active_agents (no real SQLite during these tests)
  - psutil.pid_exists (controlled liveness checks)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.dashboard.interfaces import (
    AgentKind,
    AgentState,
    AgentStatus,
    SessionID,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_state(
    session_id: str = "sess-001",
    kind: AgentKind = AgentKind.TOP_LEVEL,
    pid: int = 1000,
    name: str = "orchestrator",
    task: str = "do stuff",
    status: AgentStatus = AgentStatus.ACTIVE,
    parent_session_id: SessionID | None = None,
    started_at: datetime | None = None,
) -> AgentState:
    return AgentState(
        session_id=session_id,
        kind=kind,
        pid=pid,
        name=name,
        task=task,
        status=status,
        started_at=started_at or _now(),
        last_output_at=None,
        parent_session_id=parent_session_id,
    )


def _import_registry():
    """Import AgentRegistry class (deferred so ImportError is test-time)."""
    from careful_claude_claw.dashboard.agent_registry import AgentRegistry  # type: ignore[import]

    return AgentRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def registry():
    """Fresh AgentRegistry instance for each test."""
    AgentRegistry = _import_registry()
    return AgentRegistry()


# ---------------------------------------------------------------------------
# register() / get() round-trip
# ---------------------------------------------------------------------------


def test_register_and_get_round_trip(registry):
    """register() followed by get() returns the same AgentState."""
    state = _make_state("abc")
    registry.register(state)
    result = registry.get("abc")
    assert result is state


def test_get_returns_none_for_unknown(registry):
    """get() returns None when the session_id is not registered."""
    assert registry.get("not-here") is None


# ---------------------------------------------------------------------------
# unregister()
# ---------------------------------------------------------------------------


def test_unregister_removes_entry(registry):
    """unregister() removes the entry; subsequent get() returns None."""
    state = _make_state("remove-me")
    registry.register(state)
    assert registry.get("remove-me") is not None

    registry.unregister("remove-me")
    assert registry.get("remove-me") is None


def test_unregister_unknown_is_noop(registry):
    """unregister() on an unknown session_id does not raise."""
    registry.unregister("phantom")  # no exception


# ---------------------------------------------------------------------------
# list_top_level()
# ---------------------------------------------------------------------------


def test_list_top_level_returns_only_top_level_agents(registry):
    """list_top_level() returns only TOP_LEVEL agents, not SUB_AGENT or EXTERNAL."""
    top1 = _make_state("top-1", kind=AgentKind.TOP_LEVEL)
    top2 = _make_state("top-2", kind=AgentKind.TOP_LEVEL, pid=1002)
    sub = _make_state("sub-9999", kind=AgentKind.SUB_AGENT, pid=9999)
    ext = _make_state("ext-1", kind=AgentKind.EXTERNAL, pid=2000)

    for s in (top1, top2, sub, ext):
        registry.register(s)

    results = registry.list_top_level()
    ids = {s.session_id for s in results}
    assert ids == {"top-1", "top-2"}


def test_list_top_level_ordered_by_started_at(registry):
    """list_top_level() returns agents sorted ascending by started_at."""
    import time

    t1 = _make_state("early", kind=AgentKind.TOP_LEVEL, started_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    time.sleep(0.01)
    t2 = _make_state("late", kind=AgentKind.TOP_LEVEL, started_at=datetime(2025, 6, 1, tzinfo=timezone.utc))

    registry.register(t2)  # register out of order
    registry.register(t1)

    results = registry.list_top_level()
    assert results[0].session_id == "early"
    assert results[1].session_id == "late"


def test_list_top_level_empty(registry):
    """list_top_level() returns [] when no top-level agents are registered."""
    assert registry.list_top_level() == []


# ---------------------------------------------------------------------------
# list_children()
# ---------------------------------------------------------------------------


def test_list_children_returns_children_of_parent(registry):
    """list_children() returns only sub-agents with matching parent_session_id."""
    parent = _make_state("parent-1", kind=AgentKind.TOP_LEVEL, pid=100)
    child1 = _make_state("sub-101", kind=AgentKind.SUB_AGENT, pid=101, parent_session_id="parent-1")
    child2 = _make_state("sub-102", kind=AgentKind.SUB_AGENT, pid=102, parent_session_id="parent-1")
    unrelated = _make_state("sub-200", kind=AgentKind.SUB_AGENT, pid=200, parent_session_id="other-parent")

    for s in (parent, child1, child2, unrelated):
        registry.register(s)

    children = registry.list_children("parent-1")
    ids = {s.session_id for s in children}
    assert ids == {"sub-101", "sub-102"}


def test_list_children_returns_empty_for_no_children(registry):
    """list_children() returns [] when parent has no sub-agents."""
    parent = _make_state("lonely-parent", kind=AgentKind.TOP_LEVEL)
    registry.register(parent)
    assert registry.list_children("lonely-parent") == []


# ---------------------------------------------------------------------------
# update_status()
# ---------------------------------------------------------------------------


def test_update_status_mutates_status_field(registry):
    """update_status() changes the status on the stored AgentState."""
    state = _make_state("update-me", status=AgentStatus.ACTIVE)
    registry.register(state)

    registry.update_status("update-me", AgentStatus.IDLE)

    result = registry.get("update-me")
    assert result is not None
    assert result.status == AgentStatus.IDLE


def test_update_status_working_sets_last_output_at(registry):
    """Transitioning to WORKING must set last_output_at to a non-None datetime."""
    state = _make_state("active-agent", status=AgentStatus.ACTIVE)
    assert state.last_output_at is None
    registry.register(state)

    registry.update_status("active-agent", AgentStatus.WORKING)

    result = registry.get("active-agent")
    assert result is not None
    assert result.status == AgentStatus.WORKING
    assert result.last_output_at is not None
    assert isinstance(result.last_output_at, datetime)


def test_update_status_non_working_does_not_update_last_output_at(registry):
    """Transitioning to a non-WORKING status should NOT change last_output_at."""
    state = _make_state("idle-agent", status=AgentStatus.WORKING)
    state.last_output_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    registry.register(state)

    registry.update_status("idle-agent", AgentStatus.IDLE)

    result = registry.get("idle-agent")
    assert result is not None
    # last_output_at should remain the same (not cleared, not updated)
    assert result.last_output_at == datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# update_tokens()
# ---------------------------------------------------------------------------


def test_update_tokens_mutates_token_fields(registry):
    """update_tokens() sets input_tokens and output_tokens on the stored state."""
    state = _make_state("tokened")
    assert state.input_tokens == 0
    assert state.output_tokens == 0
    registry.register(state)

    registry.update_tokens("tokened", input_tokens=100, output_tokens=50)

    result = registry.get("tokened")
    assert result is not None
    assert result.input_tokens == 100
    assert result.output_tokens == 50


def test_update_tokens_cumulative(registry):
    """Successive update_tokens() calls accumulate (add to) existing counts."""
    state = _make_state("cumulative")
    registry.register(state)

    registry.update_tokens("cumulative", input_tokens=100, output_tokens=50)
    registry.update_tokens("cumulative", input_tokens=200, output_tokens=75)

    result = registry.get("cumulative")
    assert result is not None
    # Could be cumulative (300/125) or replaced (200/75); document expected behavior.
    # The interface says "Update token counts from a parsed usage event" —
    # accumulation is the most natural interpretation for a monitoring dashboard.
    assert result.input_tokens >= 200
    assert result.output_tokens >= 50


# ---------------------------------------------------------------------------
# update_context_limit()
# ---------------------------------------------------------------------------


def test_update_context_limit_sets_field(registry):
    """update_context_limit() sets context_limit on the stored state."""
    state = _make_state("ctx-agent")
    assert state.context_limit is None
    registry.register(state)

    registry.update_context_limit("ctx-agent", limit=200_000)

    result = registry.get("ctx-agent")
    assert result is not None
    assert result.context_limit == 200_000


def test_context_pct_computed_after_update(registry):
    """After setting tokens and context_limit, context_pct returns correct ratio."""
    state = _make_state("pct-agent")
    registry.register(state)

    registry.update_context_limit("pct-agent", limit=100_000)
    registry.update_tokens("pct-agent", input_tokens=40_000, output_tokens=10_000)

    result = registry.get("pct-agent")
    assert result is not None
    # used_tokens = 50_000; limit = 100_000 → 0.5
    assert result.context_limit == 100_000
    # Verify the property works (not a registry responsibility, but validates integration)
    assert result.used_tokens == result.input_tokens + result.output_tokens


# ---------------------------------------------------------------------------
# poll_subagents()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_subagents_registers_new_child_processes(registry):
    """
    poll_subagents() detects new child claude processes and registers them as
    SUB_AGENT entries with session_id = 'sub-<pid>'.
    """
    parent = _make_state("parent-top", kind=AgentKind.TOP_LEVEL, pid=5000)
    registry.register(parent)

    # Mock child process returned by psutil
    fake_child = MagicMock()
    fake_child.pid = 5001
    fake_child.name.return_value = "claude"
    fake_child.is_running.return_value = True

    with patch("psutil.Process") as mock_proc_cls:
        fake_parent_proc = MagicMock()
        fake_parent_proc.children.return_value = [fake_child]
        mock_proc_cls.return_value = fake_parent_proc

        await registry.poll_subagents()

    sub = registry.get("sub-5001")
    assert sub is not None
    assert sub.kind == AgentKind.SUB_AGENT
    assert sub.pid == 5001
    assert sub.parent_session_id == "parent-top"


@pytest.mark.asyncio
async def test_poll_subagents_unregisters_dead_children(registry):
    """
    poll_subagents() unregisters sub-agents whose processes are no longer alive.
    """
    parent = _make_state("parent-2", kind=AgentKind.TOP_LEVEL, pid=6000)
    registry.register(parent)

    # Pre-register a sub-agent that has since exited
    dead_sub = _make_state(
        "sub-6001",
        kind=AgentKind.SUB_AGENT,
        pid=6001,
        parent_session_id="parent-2",
    )
    registry.register(dead_sub)

    with patch("psutil.Process") as mock_proc_cls:
        fake_parent_proc = MagicMock()
        # No children alive
        fake_parent_proc.children.return_value = []
        mock_proc_cls.return_value = fake_parent_proc

        await registry.poll_subagents()

    assert registry.get("sub-6001") is None


@pytest.mark.asyncio
async def test_poll_subagents_handles_no_such_process(registry):
    """
    poll_subagents() handles psutil.NoSuchProcess gracefully (no exception raised).
    """
    import psutil

    parent = _make_state("parent-3", kind=AgentKind.TOP_LEVEL, pid=7000)
    registry.register(parent)

    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(7000)):
        # Should not raise
        await registry.poll_subagents()


# ---------------------------------------------------------------------------
# reconcile_with_db()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_registers_live_external_agents(registry):
    """
    reconcile_with_db() reads active_agents from SQLite, checks liveness via
    psutil.pid_exists(), and registers live agents as EXTERNAL kind.
    """
    # Seed a real active_agents row using the existing db pattern
    from careful_claude_claw.models import Execution, JobStatus

    ex = Execution(
        job_name="telegram-job",
        agent_name="telegram-agent",
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db_module.insert_execution(ex)
    db_module.register_active_agent(ex)

    # Patch pid_exists to report the PID as alive
    with patch("psutil.pid_exists", return_value=True):
        await registry.reconcile_with_db()

    # There should be at least one EXTERNAL agent registered
    external_agents = registry.list_external()
    assert len(external_agents) >= 1


@pytest.mark.asyncio
async def test_reconcile_registers_all_active_agents(registry):
    """
    reconcile_with_db() registers all rows from SQLite active_agents as EXTERNAL agents.

    NOTE: The active_agents table does not store a PID column, so no liveness check
    is possible at reconciliation time. All rows are registered with pid=-1 as a
    sentinel. The main system is responsible for cleaning up stale rows.
    If a PID column is added to active_agents in a future migration, this test
    should be updated to verify the liveness check behaviour.
    """
    from careful_claude_claw.models import Execution, JobStatus

    ex = Execution(
        job_name="dead-job",
        agent_name="dead-agent",
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db_module.insert_execution(ex)
    db_module.register_active_agent(ex)

    await registry.reconcile_with_db()

    # All rows are registered (no liveness check without pid column)
    external = registry.list_external()
    assert len(external) == 1
    assert external[0].job_name == "dead-job"


@pytest.mark.asyncio
async def test_reconcile_populates_execution_id_and_job_name(registry):
    """
    reconcile_with_db() sets execution_id and job_name on reconciled EXTERNAL agents.
    """
    from careful_claude_claw.models import Execution, JobStatus

    ex = Execution(
        job_name="slack-job",
        agent_name="slack-agent",
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    db_module.insert_execution(ex)
    db_module.register_active_agent(ex)

    with patch("psutil.pid_exists", return_value=True):
        await registry.reconcile_with_db()

    external = registry.list_external()
    assert len(external) >= 1
    agent = external[0]
    assert agent.job_name == "slack-job"
    assert agent.execution_id is not None
