"""Tests for CLI commands using Click's CliRunner."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import careful_claude_claw.db as db_module
from careful_claude_claw.cli import cli
from careful_claude_claw.memory import add_memory
from careful_claude_claw.models import Job, Memory, MemorySource, MemoryType


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def runner():
    return CliRunner()


# Note: Each CLI command calls init_db() which drops/recreates tables.
# So we insert data _after_ invoking the command that calls init_db(),
# or we test single-command operations.


# --- Memory CLI ---


def test_memory_list_empty(runner):
    result = runner.invoke(cli, ["memory", "list"])
    assert result.exit_code == 0
    assert "No memories" in result.output


def test_memory_add(runner):
    result = runner.invoke(cli, ["memory", "add", "User likes dark mode", "--type", "preference"])
    assert result.exit_code == 0
    assert "added" in result.output.lower()


def test_memory_list_with_data(runner):
    # init_db is called by the CLI, so insert data via DB after that
    # Use a patch to prevent init_db from wiping data
    with patch("careful_claude_claw.cli.init_db"):
        add_memory(
            Memory(
                memory_type=MemoryType.PREFERENCE,
                content="User likes dark mode",
                source=MemorySource.USER,
            )
        )
        result = runner.invoke(cli, ["memory", "list"])
    assert result.exit_code == 0
    assert "dark mode" in result.output


def test_memory_search(runner):
    with patch("careful_claude_claw.cli.init_db"):
        add_memory(
            Memory(
                memory_type=MemoryType.OBSERVATION,
                content="User works with PostgreSQL",
                source=MemorySource.USER,
            )
        )
        result = runner.invoke(cli, ["memory", "search", "PostgreSQL"])
    assert result.exit_code == 0
    assert "PostgreSQL" in result.output


def test_memory_search_no_results(runner):
    result = runner.invoke(cli, ["memory", "search", "nonexistent"])
    assert result.exit_code == 0
    assert "No matching" in result.output


def test_memory_delete(runner):
    with patch("careful_claude_claw.cli.init_db"):
        mem = Memory(
            memory_type=MemoryType.OBSERVATION,
            content="To be deleted",
            source=MemorySource.USER,
        )
        add_memory(mem)
        result = runner.invoke(cli, ["memory", "delete", str(mem.id)])
    assert result.exit_code == 0
    assert "deleted" in result.output.lower()


# --- Status ---


def test_status_no_agents(runner):
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "No active tasks" in result.output


# --- Jobs CLI ---


def test_jobs_add(runner):
    result = runner.invoke(
        cli, ["jobs", "add", "test-job", "--task", "do stuff", "--cron", "0 9 * * *"]
    )
    assert result.exit_code == 0
    assert "added" in result.output.lower()


def test_jobs_list_with_data(runner):
    with patch("careful_claude_claw.cli.init_db"):
        db_module.insert_job(Job(name="test-job", task="do stuff", cron_expr="0 9 * * *"))
        result = runner.invoke(cli, ["jobs", "list"])
    assert result.exit_code == 0
    assert "test-job" in result.output


def test_jobs_remove(runner):
    with patch("careful_claude_claw.cli.init_db"):
        db_module.insert_job(Job(name="rm-job", task="delete me"))
        result = runner.invoke(cli, ["jobs", "remove", "rm-job"])
    assert result.exit_code == 0
    assert "removed" in result.output.lower()


def test_jobs_add_no_task_or_skill(runner):
    result = runner.invoke(cli, ["jobs", "add", "empty-job"])
    assert result.exit_code == 0
    assert "task" in result.output.lower() or "skill" in result.output.lower()


# --- Sleep/Wake ---


def test_sleep_when_already_asleep(runner):
    result = runner.invoke(cli, ["sleep"])
    assert result.exit_code == 0
    assert "already asleep" in result.output.lower()


def test_wake_command(runner):
    with patch("careful_claude_claw.persistent_orchestrator.ClaudeSDKClient") as mock_cls:
        from tests.conftest import AsyncIterator

        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_messages = MagicMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client

        result = runner.invoke(cli, ["wake"])
        assert result.exit_code == 0
        assert "awake" in result.output.lower()


# --- Skills ---


def test_skills_empty(runner, monkeypatch):
    from pathlib import Path

    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))

    result = runner.invoke(cli, ["skills"])
    assert result.exit_code == 0
    assert "No skills" in result.output
