"""Tests for MCP memory tools."""

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory import add_memory
from careful_claude_claw.memory_tools import (
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
)
from careful_claude_claw.models import Job, Memory, MemoryType


def _h(sdk_tool):
    """Extract handler from SdkMcpTool for direct testing."""
    return sdk_tool.handler


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


# --- Memory Tools ---


@pytest.mark.asyncio
async def test_memory_write():
    result = await _h(memory_write_tool)(
        {"content": "User prefers dark mode", "type": "preference", "importance": 0.8}
    )
    assert "stored" in result["content"]

    from careful_claude_claw.memory import list_memories

    mems = list_memories()
    assert len(mems) == 1
    assert mems[0].content == "User prefers dark mode"
    assert mems[0].importance == 0.8


@pytest.mark.asyncio
async def test_memory_search():
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Uses PostgreSQL for backend"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Likes hiking"))

    result = await _h(memory_search_tool)({"query": "PostgreSQL"})
    assert "PostgreSQL" in result["content"]


@pytest.mark.asyncio
async def test_memory_search_no_results():
    result = await _h(memory_search_tool)({"query": "nonexistent"})
    assert "0 memories" in result["content"]


@pytest.mark.asyncio
async def test_memory_update():
    mem = Memory(memory_type=MemoryType.PREFERENCE, content="Likes vim")
    add_memory(mem)

    result = await _h(memory_update_tool)({"id": mem.id, "content": "Likes neovim"})
    assert "updated" in result["content"]

    from careful_claude_claw.memory import get_memory

    fetched = get_memory(mem.id)
    assert fetched.content == "Likes neovim"


@pytest.mark.asyncio
async def test_memory_update_not_found():
    result = await _h(memory_update_tool)({"id": 99999, "content": "new content"})
    assert "not found" in result["content"]


@pytest.mark.asyncio
async def test_memory_delete():
    mem = Memory(memory_type=MemoryType.OBSERVATION, content="temp fact")
    add_memory(mem)

    result = await _h(memory_delete_tool)({"id": mem.id})
    assert "deleted" in result["content"]

    from careful_claude_claw.memory import get_memory

    assert get_memory(mem.id) is None


@pytest.mark.asyncio
async def test_memory_list():
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Obs 1"))

    result = await _h(memory_list_tool)({})
    assert "2 memories" in result["content"]


@pytest.mark.asyncio
async def test_memory_list_with_type():
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Obs 1"))

    result = await _h(memory_list_tool)({"type": "preference"})
    assert "1 memories" in result["content"]
    assert "Pref 1" in result["content"]


@pytest.mark.asyncio
async def test_memory_list_empty():
    result = await _h(memory_list_tool)({})
    assert "No memories" in result["content"]


# --- Sub-agent Tools ---


@pytest.mark.asyncio
async def test_list_agents_empty():
    result = await _h(list_agents_tool)({})
    assert "No active agents" in result["content"]


@pytest.mark.asyncio
async def test_kill_agent_not_found():
    result = await _h(kill_agent_tool)({"name": "nope"})
    assert "No active agent" in result["content"]


@pytest.mark.asyncio
async def test_send_to_agent_not_found():
    result = await _h(send_to_agent_tool)({"name": "nope", "message": "hello"})
    assert "No active agent" in result["content"]


# --- System Tools ---


@pytest.mark.asyncio
async def test_list_jobs_empty():
    result = await _h(list_jobs_tool)({})
    assert "No jobs" in result["content"]


@pytest.mark.asyncio
async def test_list_jobs_with_data():
    db_module.insert_job(Job(name="my-job", task="do stuff", cron_expr="0 9 * * *"))
    result = await _h(list_jobs_tool)({})
    assert "my-job" in result["content"]
    assert "0 9 * * *" in result["content"]


@pytest.mark.asyncio
async def test_list_skills(monkeypatch):
    from pathlib import Path

    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))
    result = await _h(list_skills_tool)({})
    assert "No skills" in result["content"]
