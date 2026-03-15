"""Tests for the Telegram bot listener."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.agent_session as agent_session_module
import careful_claude_claw.db as db_module
from careful_claude_claw.agent_session import (
    AGENT_SESSIONS,
    AgentSession,
    register_session,
)
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


@pytest.fixture(autouse=True)
def clean_sessions():
    AGENT_SESSIONS.clear()
    agent_session_module._name_counter = 0
    yield
    AGENT_SESSIONS.clear()
    agent_session_module._name_counter = 0


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
    with patch(
        "careful_claude_claw.telegram.run_interactive_agent", new_callable=AsyncMock
    ) as mock_run:
        await router.handle_message("what time is it?")
        mock_bot.send_message.assert_called_with("[T1] Starting...")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["task"] == "what time is it?"
        assert mock_run.call_args.kwargs["name"] == "T1"


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


# --- /agents ---


@pytest.mark.asyncio
async def test_agents_empty(router, mock_bot):
    await router.handle_message("/agents")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No active agent sessions" in msg


@pytest.mark.asyncio
async def test_agents_with_sessions(router, mock_bot):
    client = MagicMock()
    session = AgentSession(name="task-1", job_id="j1", client=client)
    register_session(session)

    await router.handle_message("/agents")
    msg = mock_bot.send_message.call_args[0][0]
    assert "task-1" in msg
    assert "Active Agent Sessions (1)" in msg


# --- /kill ---


@pytest.mark.asyncio
async def test_kill_no_args(router, mock_bot):
    await router.handle_message("/kill")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


@pytest.mark.asyncio
async def test_kill_named_agent(router, mock_bot):
    with patch("careful_claude_claw.telegram.kill_session", new_callable=AsyncMock) as mock_kill:
        mock_kill.return_value = True
        await router.handle_message("/kill task-1")
        mock_kill.assert_awaited_once_with("task-1")
        msg = mock_bot.send_message.call_args[0][0]
        assert "Killed" in msg
        assert "task-1" in msg


@pytest.mark.asyncio
async def test_kill_not_found(router, mock_bot):
    with patch("careful_claude_claw.telegram.kill_session", new_callable=AsyncMock) as mock_kill:
        mock_kill.return_value = False
        await router.handle_message("/kill nope")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active agent" in msg


@pytest.mark.asyncio
async def test_kill_all(router, mock_bot):
    with patch(
        "careful_claude_claw.telegram.kill_all_sessions", new_callable=AsyncMock
    ) as mock_kill_all:
        mock_kill_all.return_value = 3
        await router.handle_message("/kill all")
        mock_kill_all.assert_awaited_once()
        msg = mock_bot.send_message.call_args[0][0]
        assert "3" in msg


# --- /reply ---


@pytest.mark.asyncio
async def test_reply_no_args(router, mock_bot):
    await router.handle_message("/reply")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


@pytest.mark.asyncio
async def test_reply_missing_message(router, mock_bot):
    await router.handle_message("/reply task-1")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


@pytest.mark.asyncio
async def test_reply_sends_to_agent(router, mock_bot):
    with patch("careful_claude_claw.telegram.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await router.handle_message("/reply task-1 check the tests")
        mock_send.assert_awaited_once_with("task-1", "check the tests")
        # No error message sent
        mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_reply_agent_not_found(router, mock_bot):
    with patch("careful_claude_claw.telegram.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = False
        await router.handle_message("/reply nope hello")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active agent" in msg


# --- @name routing ---


@pytest.mark.asyncio
async def test_at_reply_sends_to_agent(router, mock_bot):
    with patch("careful_claude_claw.telegram.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await router.handle_message("@task-1 what about failing tests?")
        mock_send.assert_awaited_once_with("task-1", "what about failing tests?")


@pytest.mark.asyncio
async def test_at_reply_agent_not_found(router, mock_bot):
    with patch("careful_claude_claw.telegram.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = False
        await router.handle_message("@nope hello")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active agent" in msg


@pytest.mark.asyncio
async def test_at_reply_no_message(router, mock_bot):
    await router.handle_message("@task-1")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


# --- /status with sessions ---


@pytest.mark.asyncio
async def test_status_with_sessions(router, mock_bot):
    client = MagicMock()
    session = AgentSession(name="task-1", job_id="j1234567-rest", client=client)
    register_session(session)

    await router.handle_message("/status")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Active Agents" in msg
    assert "task-1" in msg


# --- help includes new commands ---


@pytest.mark.asyncio
async def test_help_includes_new_commands(router, mock_bot):
    await router.handle_message("/help")
    msg = mock_bot.send_message.call_args[0][0]
    assert "/kill" in msg
    assert "/reply" in msg
    assert "/agents" in msg
    assert "@<name>" in msg
