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

from .conftest import AsyncIterator


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
        client.receive_messages = MagicMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client
        yield client


async def _cleanup_orch(orch):
    """Cancel pump task for clean test teardown."""
    if orch._pump_task and not orch._pump_task.done():
        orch._pump_task.cancel()
        try:
            await orch._pump_task
        except (asyncio.CancelledError, Exception):
            pass


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
    assert "dark mode" not in briefing


def test_build_core_briefing_respects_token_limit():
    """Many memories should be truncated to MAX_BRIEFING_TOKENS."""
    for i in range(50):
        add_memory(
            Memory(
                memory_type=MemoryType.OBSERVATION,
                content=f"Memory number {i} with some extra content to take up space " * 3,
                importance=0.5,
            )
        )

    orch = PersistentOrchestrator()
    from careful_claude_claw.persistent_orchestrator import MAX_BRIEFING_TOKENS

    briefing = orch._build_core_briefing()
    assert len(briefing) <= MAX_BRIEFING_TOKENS + 200  # small buffer for last line


def test_build_core_briefing_prefers_high_scored():
    """Higher importance memories should appear first in briefing."""
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Low importance fact",
            importance=0.1,
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="High importance preference",
            importance=0.9,
        )
    )

    orch = PersistentOrchestrator()
    briefing = orch._build_core_briefing()
    lines = briefing.strip().split("\n")
    # High importance should come first
    assert "High importance" in lines[0]


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
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_wake_is_idempotent(mock_sdk_client):
    orch = PersistentOrchestrator()
    await orch.wake()
    await orch.wake()  # should not connect twice
    mock_sdk_client.connect.assert_awaited_once()
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_sleep_disconnects(mock_sdk_client):
    orch = PersistentOrchestrator()
    await orch.wake()
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([]))
    await orch.sleep()
    assert not orch.is_awake
    mock_sdk_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_auto_wakes(mock_sdk_client):
    orch = PersistentOrchestrator()
    assert not orch.is_awake

    callback = AsyncMock()

    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "Hello!"
    result_msg.session_id = None
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    await orch.submit("hello", "test", callback)
    assert orch.is_awake

    await asyncio.sleep(0.2)
    await _cleanup_orch(orch)


# --- Message pump e2e ---


@pytest.mark.asyncio
async def test_pump_processes_message_and_calls_callback(mock_sdk_client):
    """Submit a message, verify callback receives ResultMessage text."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "Response from agent"
    result_msg.session_id = None
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()

    callback = AsyncMock()
    await orch.submit("test message", "test", callback)
    await asyncio.sleep(0.2)

    callback.assert_awaited_with("Response from agent")
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_pump_streams_assistant_messages(mock_sdk_client):
    """Mock AssistantMessage, verify callback gets text."""
    from claude_agent_sdk import AssistantMessage

    assistant_msg = MagicMock(spec=AssistantMessage)
    assistant_msg.content = "Streaming text"

    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([assistant_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()

    callback = AsyncMock()
    await orch.submit("test", "test", callback)
    await asyncio.sleep(0.2)

    callback.assert_awaited_with("Streaming text")
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_pump_captures_session_id(mock_sdk_client):
    """Mock ResultMessage with session_id, verify state persisted."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "ok"
    result_msg.session_id = "sess-abc-123"
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()

    callback = AsyncMock()
    await orch.submit("test", "test", callback)
    await asyncio.sleep(0.2)

    assert orch._state.session_id == "sess-abc-123"
    # Verify persisted to DB
    state = db_module.get_orchestrator_state()
    assert state.session_id == "sess-abc-123"
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_pump_increments_message_count(mock_sdk_client):
    """Verify counter increments after processing."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "ok"
    result_msg.session_id = None
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()
    initial_count = orch._state.total_messages_handled

    callback = AsyncMock()
    await orch.submit("test", "test", callback)
    await asyncio.sleep(0.2)

    assert orch._state.total_messages_handled == initial_count + 1
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_pump_injects_memory_context(mock_sdk_client):
    """Add a memory, submit a message, verify query includes memory content."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "ok"
    result_msg.session_id = None
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()

    # Add memory AFTER wake (which calls init_db and drops tables)
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="User prefers Python for scripts",
            importance=0.9,
        )
    )

    callback = AsyncMock()
    await orch.submit("write a script", "test", callback)
    await asyncio.sleep(0.2)

    # Verify the query call included memory context
    query_call = mock_sdk_client.query.call_args
    query_text = query_call[0][0]
    assert "Memory Context" in query_text
    assert "Python for scripts" in query_text
    assert "write a script" in query_text
    await _cleanup_orch(orch)


# --- Session resume ---


@pytest.mark.asyncio
async def test_wake_with_existing_session_id(mock_sdk_client):
    """Set session_id in DB, wake, verify opts.resume was set."""
    state = db_module.get_orchestrator_state()
    state.session_id = "existing-session"
    db_module.upsert_orchestrator_state(state)

    # Patch init_db to no-op so wake() doesn't wipe the state we just set
    with (
        patch("careful_claude_claw.persistent_orchestrator.init_db"),
        patch("careful_claude_claw.persistent_orchestrator.ClaudeSDKClient") as mock_cls,
    ):
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_messages = MagicMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client

        orch = PersistentOrchestrator()
        await orch.wake()

        # Check that ClaudeAgentOptions had resume set
        call_args = mock_cls.call_args
        opts = call_args.kwargs.get("options") or call_args.args[0]
        assert opts.resume == "existing-session"
        await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_wake_resume_failure_starts_fresh(mock_sdk_client):
    """Mock connect() to raise on first call, succeed on second."""
    state = db_module.get_orchestrator_state()
    state.session_id = "stale-session"
    db_module.upsert_orchestrator_state(state)

    call_count = 0

    async def connect_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Session expired")

    with (
        patch("careful_claude_claw.persistent_orchestrator.init_db"),
        patch("careful_claude_claw.persistent_orchestrator.ClaudeSDKClient") as mock_cls,
    ):
        client = MagicMock()
        client.connect = AsyncMock(side_effect=connect_side_effect)
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_messages = MagicMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client

        orch = PersistentOrchestrator()
        await orch.wake()

        assert orch.is_awake
        assert orch._state.session_id is None  # cleared after failure
        await _cleanup_orch(orch)


# --- Sleep consolidation ---


@pytest.mark.asyncio
async def test_sleep_sends_consolidation_prompt(mock_sdk_client):
    """Verify client.query(CONSOLIDATION_PROMPT) called during sleep."""
    from claude_agent_sdk import ResultMessage

    from careful_claude_claw.persistent_orchestrator import CONSOLIDATION_PROMPT

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "Consolidation complete"
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()
    await orch.sleep()

    # Query should have been called with consolidation prompt
    mock_sdk_client.query.assert_awaited_with(CONSOLIDATION_PROMPT)


@pytest.mark.asyncio
async def test_sleep_consolidation_failure_still_disconnects(mock_sdk_client):
    """Mock query to raise, verify disconnect still happens."""
    mock_sdk_client.query = AsyncMock(side_effect=Exception("consolidation error"))

    orch = PersistentOrchestrator()
    await orch.wake()
    await orch.sleep()

    assert not orch.is_awake
    mock_sdk_client.disconnect.assert_awaited_once()


# --- Error handling ---


@pytest.mark.asyncio
async def test_pump_error_sends_error_message(mock_sdk_client):
    """Mock client.query() to raise, verify callback gets error message."""
    mock_sdk_client.query = AsyncMock(side_effect=Exception("boom"))

    orch = PersistentOrchestrator()
    await orch.wake()

    callback = AsyncMock()
    await orch.submit("test", "test", callback)
    await asyncio.sleep(0.2)

    callback.assert_awaited_with("Sorry, I encountered an internal error.")
    await _cleanup_orch(orch)


@pytest.mark.asyncio
async def test_pump_continues_after_error(mock_sdk_client):
    """Two messages: first raises, second succeeds."""
    from claude_agent_sdk import ResultMessage

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "success"
    result_msg.session_id = None

    call_count = 0

    async def query_side_effect(text):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("first fails")

    mock_sdk_client.query = AsyncMock(side_effect=query_side_effect)
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))

    orch = PersistentOrchestrator()
    await orch.wake()

    callback1 = AsyncMock()
    callback2 = AsyncMock()

    await orch.submit("msg1", "test", callback1)
    await asyncio.sleep(0.2)
    callback1.assert_awaited_with("Sorry, I encountered an internal error.")

    # Second message should succeed — need to reset receive_messages
    mock_sdk_client.receive_messages = MagicMock(return_value=AsyncIterator([result_msg]))
    await orch.submit("msg2", "test", callback2)
    await asyncio.sleep(0.2)

    callback2.assert_awaited_with("success")
    await _cleanup_orch(orch)
