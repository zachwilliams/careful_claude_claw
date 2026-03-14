"""Tests for the Telegram bot listener."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.models import Job, JobStatus
from careful_claude_claw.telegram import (
    CommandRouter,
    TelegramBot,
    _split_message,
    load_telegram_config,
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=TelegramBot)
    bot.send_message = AsyncMock()
    bot.chat_id = 12345
    return bot


@pytest.fixture
def router(mock_bot):
    return CommandRouter(mock_bot)


# --- load_telegram_config ---


def test_load_config_valid(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mcp-telegram"
    config_dir.mkdir()
    config = {"botToken": "123:ABC", "chatId": 99}
    (config_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = load_telegram_config()
    assert result == ("123:ABC", 99)


def test_load_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert load_telegram_config() is None


def test_load_config_invalid_json(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mcp-telegram"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("not json")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert load_telegram_config() is None


def test_load_config_missing_fields(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mcp-telegram"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"botToken": "abc"}))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert load_telegram_config() is None


def test_load_config_alt_keys(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mcp-telegram"
    config_dir.mkdir()
    config = {"token": "123:ABC", "chat_id": 42}
    (config_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = load_telegram_config()
    assert result == ("123:ABC", 42)


def test_load_config_nested_bot(tmp_path, monkeypatch):
    config_dir = tmp_path / ".mcp-telegram"
    config_dir.mkdir()
    config = {"bot": {"token": "123:ABC", "chat_id": "99"}}
    (config_dir / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = load_telegram_config()
    assert result == ("123:ABC", 99)


# --- _split_message ---


def test_split_short_message():
    assert _split_message("hello") == ["hello"]


def test_split_long_message():
    text = "a" * 5000
    chunks = _split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert "".join(chunks) == text


def test_split_at_newline():
    text = "a" * 4000 + "\n" + "b" * 200
    chunks = _split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 4000
    assert chunks[1] == "b" * 200


# --- CommandRouter ---


@pytest.mark.asyncio
async def test_help(router, mock_bot):
    await router.handle_message("/help")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "CarefulClaw Commands" in msg
    assert "/status" in msg


@pytest.mark.asyncio
async def test_status_no_agents(router, mock_bot):
    await router.handle_message("/status")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "No active agents" in msg


@pytest.mark.asyncio
async def test_status_with_jobs(router, mock_bot):
    job = Job(
        agent_name="test",
        task="test task",
        status=JobStatus.SUCCESS,
        started_at=datetime.now(UTC),
    )
    db_module.insert_job(job)

    await router.handle_message("/status")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Recent Jobs" in msg
    assert "test" in msg


@pytest.mark.asyncio
async def test_jobs_empty(router, mock_bot):
    await router.handle_message("/jobs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No jobs found" in msg


@pytest.mark.asyncio
async def test_jobs_with_data(router, mock_bot):
    for i in range(3):
        db_module.insert_job(
            Job(agent_name=f"agent-{i}", task=f"task {i}", started_at=datetime.now(UTC))
        )

    await router.handle_message("/jobs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Recent Jobs" in msg
    assert "agent-0" in msg


@pytest.mark.asyncio
async def test_projects_empty(router, mock_bot):
    await router.handle_message("/projects")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No projects" in msg


@pytest.mark.asyncio
async def test_skills(router, mock_bot, monkeypatch):
    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))
    await router.handle_message("/skills")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No skills" in msg


@pytest.mark.asyncio
async def test_schedules_empty(router, mock_bot):
    await router.handle_message("/schedules")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No schedules" in msg


@pytest.mark.asyncio
async def test_run_no_skill(router, mock_bot):
    await router.handle_message("/run")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


@pytest.mark.asyncio
async def test_run_skill_not_found(router, mock_bot):
    await router.handle_message("/run nonexistent")
    msg = mock_bot.send_message.call_args[0][0]
    assert "not found" in msg


@pytest.mark.asyncio
async def test_free_text_spawns_agent(router, mock_bot):
    with patch("careful_claude_claw.telegram._run_and_reply", new_callable=AsyncMock) as mock_run:
        await router.handle_message("what time is it?")
        mock_bot.send_message.assert_called_with("Starting task...")
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["task"] == "what time is it?"


@pytest.mark.asyncio
async def test_command_with_bot_mention(router, mock_bot):
    await router.handle_message("/help@CarefulClawBot")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "CarefulClaw Commands" in msg


@pytest.mark.asyncio
async def test_empty_message(router, mock_bot):
    await router.handle_message("")
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_empty_whitespace_message(router, mock_bot):
    await router.handle_message("   ")
    mock_bot.send_message.assert_not_called()
