"""Tests for the orchestrator — routing, context assembly, memory operations."""

from unittest.mock import AsyncMock, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory import add_memory
from careful_claude_claw.models import (
    Execution,
    JobStatus,
    Memory,
    MemorySource,
    MemoryType,
    RequestType,
)
from careful_claude_claw.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def orchestrator():
    return Orchestrator()


# --- Classification ---


class TestClassifyRequest:
    def test_command(self, orchestrator):
        assert orchestrator.classify_request("/status") == RequestType.COMMAND
        assert orchestrator.classify_request("/help") == RequestType.COMMAND
        assert orchestrator.classify_request("/run my-skill") == RequestType.COMMAND
        assert orchestrator.classify_request("/kill all") == RequestType.COMMAND

    def test_follow_up(self, orchestrator):
        assert orchestrator.classify_request("@T1 do something") == RequestType.FOLLOW_UP
        assert orchestrator.classify_request("@agent-name hello") == RequestType.FOLLOW_UP

    def test_memory_add(self, orchestrator):
        classify = orchestrator.classify_request
        assert classify("remember that I like Python") == RequestType.MEMORY_ADD
        assert classify("Remember: dark mode") == RequestType.MEMORY_ADD
        assert classify("note that the API uses v2") == RequestType.MEMORY_ADD

    def test_memory_query(self, orchestrator):
        classify = orchestrator.classify_request
        assert classify("what do you know about Python") == RequestType.MEMORY_QUERY
        assert classify("what do you remember about my preferences") == RequestType.MEMORY_QUERY
        assert classify("recall the project setup") == RequestType.MEMORY_QUERY
        assert classify("what are my preferences") == RequestType.MEMORY_QUERY
        assert classify("search memory for coding") == RequestType.MEMORY_QUERY

    def test_task(self, orchestrator):
        assert orchestrator.classify_request("fix the login bug") == RequestType.TASK
        assert orchestrator.classify_request("write a function to parse CSV") == RequestType.TASK
        assert orchestrator.classify_request("hello") == RequestType.TASK


# --- Context Retrieval ---


class TestRetrieveContext:
    def test_no_memories(self, orchestrator):
        context = orchestrator.retrieve_context("some task")
        assert context == ""

    def test_with_preferences(self, orchestrator):
        add_memory(
            Memory(
                memory_type=MemoryType.PREFERENCE,
                content="Prefers concise code",
                source=MemorySource.USER,
            )
        )
        context = orchestrator.retrieve_context("write some code")
        assert "concise code" in context
        assert "Memory Context" in context

    def test_with_relevant_facts(self, orchestrator):
        add_memory(
            Memory(
                memory_type=MemoryType.FACT,
                content="Project uses FastAPI for the API layer",
            )
        )
        add_memory(
            Memory(
                memory_type=MemoryType.FACT,
                content="User has a pet hamster",
            )
        )
        context = orchestrator.retrieve_context("FastAPI endpoint")
        assert "FastAPI" in context


# --- System Prompt Building ---


class TestBuildSystemPrompt:
    def test_no_context(self, orchestrator):
        result = orchestrator.build_system_prompt("base prompt", "")
        assert result == "base prompt"

    def test_no_base(self, orchestrator):
        result = orchestrator.build_system_prompt(None, "memory context")
        assert result == "memory context"

    def test_both(self, orchestrator):
        result = orchestrator.build_system_prompt("base", "context")
        assert "base" in result
        assert "context" in result

    def test_no_either(self, orchestrator):
        result = orchestrator.build_system_prompt(None, "")
        assert result is None


# --- Memory Add ---


class TestHandleMemoryAdd:
    @pytest.mark.asyncio
    async def test_add_fact(self, orchestrator):
        response = await orchestrator.handle_memory_add("remember that the API key is in .env")
        assert "Remembered" in response
        assert "API key" in response

        from careful_claude_claw.memory import list_memories

        mems = list_memories()
        assert len(mems) == 1
        assert mems[0].memory_type == MemoryType.FACT
        assert mems[0].source == MemorySource.USER

    @pytest.mark.asyncio
    async def test_add_preference(self, orchestrator):
        response = await orchestrator.handle_memory_add("remember that I prefer dark mode")
        assert "Remembered" in response

        from careful_claude_claw.memory import list_memories

        mems = list_memories()
        assert len(mems) == 1
        assert mems[0].memory_type == MemoryType.PREFERENCE

    @pytest.mark.asyncio
    async def test_prefix_stripping(self, orchestrator):
        await orchestrator.handle_memory_add("note that Python 3.12 is required")
        from careful_claude_claw.memory import list_memories

        mems = list_memories()
        assert mems[0].content == "Python 3.12 is required"


# --- Memory Query ---


class TestHandleMemoryQuery:
    @pytest.mark.asyncio
    async def test_query_with_results(self, orchestrator):
        add_memory(
            Memory(
                memory_type=MemoryType.FACT,
                content="Project uses PostgreSQL database",
            )
        )
        response = await orchestrator.handle_memory_query("what do you know about PostgreSQL")
        assert "PostgreSQL" in response
        assert "1 memory" in response

    @pytest.mark.asyncio
    async def test_query_no_results(self, orchestrator):
        response = await orchestrator.handle_memory_query("what do you know about Haskell")
        assert "No memories found" in response

    @pytest.mark.asyncio
    async def test_query_list_all(self, orchestrator):
        add_memory(Memory(memory_type=MemoryType.FACT, content="Fact 1"))
        add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
        response = await orchestrator.handle_memory_query("what are my preferences")
        assert "2 memory" in response


# --- Handle Request ---


class TestHandleRequest:
    @pytest.mark.asyncio
    async def test_command_passthrough(self, orchestrator):
        result = await orchestrator.handle_request("/status")
        assert result.request_type == RequestType.COMMAND

    @pytest.mark.asyncio
    async def test_follow_up_passthrough(self, orchestrator):
        result = await orchestrator.handle_request("@T1 hello")
        assert result.request_type == RequestType.FOLLOW_UP

    @pytest.mark.asyncio
    async def test_memory_add(self, orchestrator):
        on_message = AsyncMock()
        result = await orchestrator.handle_request("remember that I use vim", on_message=on_message)
        assert result.request_type == RequestType.MEMORY_ADD
        assert "Remembered" in result.response
        on_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_query(self, orchestrator):
        add_memory(Memory(memory_type=MemoryType.FACT, content="Uses vim editor"))
        on_message = AsyncMock()
        result = await orchestrator.handle_request(
            "what do you know about vim", on_message=on_message
        )
        assert result.request_type == RequestType.MEMORY_QUERY
        assert "vim" in result.response
        on_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_task_with_context(self, orchestrator):
        add_memory(
            Memory(
                memory_type=MemoryType.PREFERENCE,
                content="Prefers type hints in Python",
            )
        )
        result = await orchestrator.handle_request("write a Python function")
        assert result.request_type == RequestType.TASK
        assert "type hints" in result.response


# --- Post-task Hook ---


class TestOnTaskComplete:
    @pytest.mark.asyncio
    async def test_extraction_called(self, orchestrator):
        execution = Execution(
            job_name="test",
            agent_name="agent",
            status=JobStatus.SUCCESS,
            output="User prefers dark mode and vim keybindings",
        )
        with patch(
            "careful_claude_claw.orchestrator.extract_and_store",
            new_callable=AsyncMock,
        ) as mock_extract:
            mock_extract.return_value = []
            await orchestrator.on_task_complete(execution)
            mock_extract.assert_called_once_with(
                "User prefers dark mode and vim keybindings",
                source_id=execution.id,
            )

    @pytest.mark.asyncio
    async def test_no_output_skips_extraction(self, orchestrator):
        execution = Execution(
            job_name="test",
            agent_name="agent",
            status=JobStatus.SUCCESS,
            output=None,
        )
        with patch(
            "careful_claude_claw.orchestrator.extract_and_store",
            new_callable=AsyncMock,
        ) as mock_extract:
            await orchestrator.on_task_complete(execution)
            mock_extract.assert_not_called()
