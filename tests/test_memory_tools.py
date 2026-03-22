"""Tests for MCP memory tools."""

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory import add_memory
from careful_claude_claw.memory_tools import (
    ALL_TOOLS,
    kill_agent_tool,
    list_agents_tool,
    list_jobs_tool,
    list_skills_tool,
    memory_delete_tool,
    memory_list_tool,
    memory_search_tool,
    memory_update_tool,
    memory_write_tool,
    send_to_agent_tool,
    spawn_agent_tool,
)
from careful_claude_claw.models import Job, Memory, MemoryType


def _h(sdk_tool):
    """Extract handler from SdkMcpTool for direct testing."""
    return sdk_tool.handler


def _text(result: dict) -> str:
    """Extract text from MCP tool result content blocks."""
    content = result["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block.get("type") == "text")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


# --- ALL_TOOLS count (hybrid architecture: only write/update + agent/system tools) ---


def test_all_tools_count():
    """ALL_TOOLS should contain all 11 orchestrator tools."""
    assert len(ALL_TOOLS) == 11
    tool_names = [t.name for t in ALL_TOOLS]
    assert "memory_write" in tool_names
    assert "memory_update" in tool_names
    assert "memory_search" in tool_names
    assert "memory_list" in tool_names
    assert "memory_delete" in tool_names


# --- Memory Tools ---


@pytest.mark.asyncio
async def test_memory_write():
    result = await _h(memory_write_tool)(
        {"content": "User prefers dark mode", "type": "preference", "importance": 0.8}
    )
    assert "stored" in _text(result)

    from careful_claude_claw.memory import list_memories

    mems = list_memories()
    assert len(mems) == 1
    assert mems[0].content == "User prefers dark mode"
    assert mems[0].importance == 0.8


@pytest.mark.asyncio
async def test_memory_write_invalid_type():
    """Invalid memory type should return error, not crash."""
    result = await _h(memory_write_tool)({"content": "test", "type": "invalid_type"})
    assert "Error" in _text(result)


@pytest.mark.asyncio
async def test_memory_search():
    """memory_search_tool function still works even though removed from ALL_TOOLS."""
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Uses PostgreSQL for backend"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Likes hiking"))

    result = await _h(memory_search_tool)({"query": "PostgreSQL"})
    assert "PostgreSQL" in _text(result)


@pytest.mark.asyncio
async def test_memory_search_no_results():
    result = await _h(memory_search_tool)({"query": "nonexistent"})
    assert "0 memories" in _text(result)


@pytest.mark.asyncio
async def test_memory_update():
    mem = Memory(memory_type=MemoryType.PREFERENCE, content="Likes vim")
    add_memory(mem)

    result = await _h(memory_update_tool)({"id": mem.id, "content": "Likes neovim"})
    assert "updated" in _text(result)

    from careful_claude_claw.memory import get_memory

    fetched = get_memory(mem.id)
    assert fetched.content == "Likes neovim"


@pytest.mark.asyncio
async def test_memory_update_not_found():
    result = await _h(memory_update_tool)({"id": 99999, "content": "new content"})
    assert "not found" in _text(result)


@pytest.mark.asyncio
async def test_memory_delete():
    """memory_delete_tool function still works even though removed from ALL_TOOLS."""
    mem = Memory(memory_type=MemoryType.OBSERVATION, content="temp fact")
    add_memory(mem)

    result = await _h(memory_delete_tool)({"id": mem.id})
    assert "deleted" in _text(result)

    from careful_claude_claw.memory import get_memory

    assert get_memory(mem.id) is None


@pytest.mark.asyncio
async def test_memory_list():
    """memory_list_tool function still works even though removed from ALL_TOOLS."""
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Obs 1"))

    result = await _h(memory_list_tool)({})
    assert "2 memories" in _text(result)


@pytest.mark.asyncio
async def test_memory_list_with_type():
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Obs 1"))

    result = await _h(memory_list_tool)({"type": "preference"})
    assert "1 memories" in _text(result)
    assert "Pref 1" in _text(result)


@pytest.mark.asyncio
async def test_memory_list_empty():
    result = await _h(memory_list_tool)({})
    assert "No memories" in _text(result)


# --- Sub-agent Tools ---


@pytest.mark.asyncio
async def test_list_agents_empty():
    result = await _h(list_agents_tool)({})
    assert "No active agents" in _text(result)


@pytest.mark.asyncio
async def test_kill_agent_not_found():
    result = await _h(kill_agent_tool)({"name": "nope"})
    assert "No active agent" in _text(result)


@pytest.mark.asyncio
async def test_send_to_agent_not_found():
    result = await _h(send_to_agent_tool)({"name": "nope", "message": "hello"})
    assert "No active agent" in _text(result)


@pytest.mark.asyncio
async def test_spawn_agent_skill_not_found(monkeypatch):
    """Nonexistent skill should return error message."""
    from pathlib import Path

    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))

    result = await _h(spawn_agent_tool)({"task": "do something", "skill": "nonexistent-skill"})
    assert "not found" in _text(result).lower()


@pytest.mark.asyncio
async def test_list_agents_with_sessions():
    """Register a mock session, verify it shows in list_agents."""
    from unittest.mock import MagicMock

    from careful_claude_claw.agent_session import (
        AGENT_SESSIONS,
        AgentSession,
        register_session,
    )

    AGENT_SESSIONS.clear()
    try:
        client = MagicMock()
        session = AgentSession(name="test-agent", execution_id=42, client=client)
        register_session(session)

        result = await _h(list_agents_tool)({})
        assert "test-agent" in _text(result)
        assert "1" in _text(result)
    finally:
        AGENT_SESSIONS.clear()


@pytest.mark.asyncio
async def test_send_to_agent_with_session():
    """Register a mock session, verify message is sent."""
    from unittest.mock import AsyncMock, MagicMock

    from careful_claude_claw.agent_session import (
        AGENT_SESSIONS,
        AgentSession,
        register_session,
    )

    AGENT_SESSIONS.clear()
    try:
        client = MagicMock()
        client.query = AsyncMock()
        session = AgentSession(name="responder", execution_id=1, client=client)
        register_session(session)

        result = await _h(send_to_agent_tool)({"name": "responder", "message": "hello there"})
        assert "sent" in _text(result).lower()
        client.query.assert_awaited_once_with("hello there")
    finally:
        AGENT_SESSIONS.clear()


# --- System Tools ---


@pytest.mark.asyncio
async def test_list_jobs_empty():
    result = await _h(list_jobs_tool)({})
    assert "No jobs" in _text(result)


@pytest.mark.asyncio
async def test_list_jobs_with_data():
    db_module.insert_job(Job(name="my-job", task="do stuff", cron_expr="0 9 * * *"))
    result = await _h(list_jobs_tool)({})
    assert "my-job" in _text(result)
    assert "0 9 * * *" in _text(result)


@pytest.mark.asyncio
async def test_list_skills(monkeypatch):
    from pathlib import Path

    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))
    result = await _h(list_skills_tool)({})
    assert "No skills" in _text(result)
