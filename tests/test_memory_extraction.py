"""Tests for LLM-powered memory extraction and consolidation.

Uses mocked Claude responses to test extraction logic without API calls.
"""

from unittest.mock import MagicMock, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory_extraction import (
    _parse_json_response,
    consolidate_memories,
    extract_and_store,
    extract_memories,
    summarize_session,
)
from careful_claude_claw.models import Memory, MemorySource, MemoryType


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def _mock_response(text: str) -> MagicMock:
    """Create a mock Anthropic message response."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


# --- JSON Parsing ---


def test_parse_json_response_direct():
    result = _parse_json_response('[{"key": "value"}]')
    assert result == [{"key": "value"}]


def test_parse_json_response_code_block():
    text = '```json\n[{"key": "value"}]\n```'
    result = _parse_json_response(text)
    assert result == [{"key": "value"}]


def test_parse_json_response_invalid():
    assert _parse_json_response("not json at all") is None


def test_parse_json_response_dict():
    result = _parse_json_response('{"action": "add"}')
    assert result == {"action": "add"}


# --- Extract Memories ---


@pytest.mark.asyncio
async def test_extract_memories_success():
    mock_resp = _mock_response(
        '[{"memory_type": "preference", "content": "Likes Python",'
        ' "category": "language", "tags": ["python"]}]'
    )

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        memories = await extract_memories("User said they like Python")

    assert len(memories) == 1
    assert memories[0].memory_type == MemoryType.PREFERENCE
    assert memories[0].content == "Likes Python"
    assert memories[0].source == MemorySource.EXTRACTION
    assert "python" in memories[0].tags


@pytest.mark.asyncio
async def test_extract_memories_empty():
    mock_resp = _mock_response("[]")

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        memories = await extract_memories("Nothing interesting here")

    assert memories == []


@pytest.mark.asyncio
async def test_extract_memories_api_error():
    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.side_effect = Exception("API Error")
        memories = await extract_memories("Some text")

    assert memories == []


@pytest.mark.asyncio
async def test_extract_memories_malformed_response():
    mock_resp = _mock_response('[{"bad_key": "no memory_type"}]')

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        memories = await extract_memories("Some text")

    assert memories == []


# --- Consolidate Memories ---


@pytest.mark.asyncio
async def test_consolidate_add():
    mock_resp = _mock_response('{"action": "add", "reason": "Novel memory"}')
    new_mem = Memory(memory_type=MemoryType.FACT, content="New fact")
    existing = [Memory(memory_type=MemoryType.FACT, content="Old fact")]

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        result = await consolidate_memories(new_mem, existing)

    assert result is not None
    assert result.content == "New fact"


@pytest.mark.asyncio
async def test_consolidate_discard():
    mock_resp = _mock_response('{"action": "discard", "reason": "Duplicate"}')
    new_mem = Memory(memory_type=MemoryType.FACT, content="Duplicate fact")
    existing = [Memory(memory_type=MemoryType.FACT, content="Same fact")]

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        result = await consolidate_memories(new_mem, existing)

    assert result is None


@pytest.mark.asyncio
async def test_consolidate_merge():
    existing_mem = Memory(memory_type=MemoryType.FACT, content="Knows Python")
    mock_resp = _mock_response(
        f'{{"action": "merge", "merge_target_id": "{existing_mem.id}", '
        f'"merged_content": "Knows Python and Rust", "reason": "Merging language facts"}}'
    )
    new_mem = Memory(memory_type=MemoryType.FACT, content="Knows Rust")

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        result = await consolidate_memories(new_mem, [existing_mem])

    assert result is not None
    assert result.content == "Knows Python and Rust"
    assert result.id == existing_mem.id


@pytest.mark.asyncio
async def test_consolidate_no_existing():
    new_mem = Memory(memory_type=MemoryType.FACT, content="Brand new fact")
    result = await consolidate_memories(new_mem, [])
    assert result is not None
    assert result.content == "Brand new fact"


# --- Summarize Session ---


@pytest.mark.asyncio
async def test_summarize_session():
    mock_resp = _mock_response("Session completed a database migration task.")

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_resp
        memory = await summarize_session("T1", "Ran migration, updated schema")

    assert memory is not None
    assert memory.memory_type == MemoryType.SUMMARY
    assert "migration" in memory.content.lower()
    assert "T1" in memory.tags


@pytest.mark.asyncio
async def test_summarize_session_api_error():
    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.side_effect = Exception("fail")
        memory = await summarize_session("T1", "output")

    assert memory is None


# --- Extract and Store ---


@pytest.mark.asyncio
async def test_extract_and_store():
    extract_resp = _mock_response(
        '[{"memory_type": "fact", "content": "Uses pytest",'
        ' "category": "testing", "tags": ["testing"]}]'
    )
    consolidate_resp = _mock_response('{"action": "add", "reason": "Novel"}')

    with patch("careful_claude_claw.memory_extraction._get_client") as mock_client:
        mock_client.return_value.messages.create.side_effect = [
            extract_resp,
            consolidate_resp,
        ]
        stored = await extract_and_store("The project uses pytest for testing")

    assert len(stored) == 1
    assert stored[0].content == "Uses pytest"

    # Verify it was persisted
    from careful_claude_claw.memory import get_memory

    fetched = get_memory(stored[0].id)
    assert fetched is not None
