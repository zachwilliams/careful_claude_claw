"""Tests for the persistent orchestrator.

Uses mocked ClaudeSDKClient to test orchestrator lifecycle without live Claude.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory import add_memory
from careful_claude_claw.models import Memory, MemoryType
from careful_claude_claw.persistent_orchestrator import PersistentOrchestrator


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def mock_sdk_client():
    """Create a mock ClaudeSDKClient."""
    with patch("careful_claude_claw.persistent_orchestrator.ClaudeSDKClient") as mock_cls:
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_messages = AsyncMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client
        yield client


class AsyncIterator:
    """Helper for mocking async iterators."""

    def __init__(self, items):
        self.items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration


# --- Core Briefing ---


def test_build_core_briefing_empty():
    orch = PersistentOrchestrator()
    briefing = orch._build_core_briefing()
    assert briefing == ""


def test_build_core_briefing_from_memories():
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="User prefers dark mode",
            importance=0.9,
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Project uses FastAPI",
            importance=0.7,
        )
    )

    orch = PersistentOrchestrator()
    briefing = orch._build_core_briefing()
    assert "dark mode" in briefing
    assert "FastAPI" in briefing


def test_build_core_briefing_prefers_procedure():
    add_memory(
        Memory(
            memory_type=MemoryType.PROCEDURE,
            content="Core briefing: User is a senior engineer working on CarefulClaw.",
            category="core_briefing",
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="Likes dark mode",
        )
    )

    orch = PersistentOrchestrator()
    briefing = orch._build_core_briefing()
    assert "senior engineer" in briefing
    # Should use the explicit core_briefing, not assemble from all memories
    assert "dark mode" not in briefing


# --- Orchestrator State ---


def test_orchestrator_state_persistence():
    state = db_module.get_orchestrator_state()
    assert state.is_awake is False

    state.is_awake = True
    state.session_id = "test-session"
    state.total_messages_handled = 5
    db_module.upsert_orchestrator_state(state)

    loaded = db_module.get_orchestrator_state()
    assert loaded.is_awake is True
    assert loaded.session_id == "test-session"
    assert loaded.total_messages_handled == 5


# --- Wake/Sleep lifecycle (mocked) ---


@pytest.mark.asyncio
async def test_wake_creates_client(mock_sdk_client):
    orch = PersistentOrchestrator()
    await orch.wake()
    assert orch.is_awake
    mock_sdk_client.connect.assert_awaited_once()

    # Cleanup
    if orch._pump_task:
        orch._pump_task.cancel()
        try:
            await orch._pump_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_wake_is_idempotent(mock_sdk_client):
    orch = PersistentOrchestrator()
    await orch.wake()
    await orch.wake()  # should not connect twice
    mock_sdk_client.connect.assert_awaited_once()

    if orch._pump_task:
        orch._pump_task.cancel()
        try:
            await orch._pump_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_sleep_disconnects(mock_sdk_client):
    orch = PersistentOrchestrator()
    await orch.wake()

    # Mock receive_messages to return empty
    mock_sdk_client.receive_messages = AsyncMock(return_value=AsyncIterator([]))

    await orch.sleep()
    assert not orch.is_awake
    mock_sdk_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_auto_wakes(mock_sdk_client):
    orch = PersistentOrchestrator()
    assert not orch.is_awake

    callback = AsyncMock()

    # Mock receive_messages to return a result
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "Hello!"
    result_msg.session_id = None
    mock_sdk_client.receive_messages = AsyncMock(return_value=AsyncIterator([result_msg]))

    await orch.submit("hello", "test", callback)
    assert orch.is_awake

    # Give pump a moment to process
    import asyncio

    await asyncio.sleep(0.2)

    if orch._pump_task:
        orch._pump_task.cancel()
        try:
            await orch._pump_task
        except Exception:
            pass
