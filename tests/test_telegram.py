"""Tests for Telegram-specific bot code and the shared CommandRouter."""

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
from careful_claude_claw.bot import CommandRouter, split_message
from careful_claude_claw.config import Settings
from careful_claude_claw.models import Execution, JobStatus
from careful_claude_claw.telegram import (
    TelegramBot,
    _extract_file_info,
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
    bot.download_file = AsyncMock()
    bot.platform = "telegram"
    bot.chat_id = 12345
    return bot


@pytest.fixture
def router(mock_bot):
    return CommandRouter(mock_bot)


# --- Settings (Telegram) ---


def test_telegram_configured_when_env_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    s = Settings()
    assert s.telegram_configured
    assert s.telegram_bot_token == "123:ABC"
    assert s.telegram_chat_id == 99


def test_telegram_not_configured_when_env_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    s = Settings()
    assert not s.telegram_configured


def test_telegram_not_configured_when_only_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    s = Settings()
    assert not s.telegram_configured


# --- split_message ---


def test_split_short_message():
    assert split_message("hello") == ["hello"]


def test_split_long_message():
    text = "a" * 5000
    chunks = split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert "".join(chunks) == text


def test_split_at_newline():
    text = "a" * 4000 + "\n" + "b" * 200
    chunks = split_message(text, max_len=4096)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 4000
    assert chunks[1] == "b" * 200


# --- CommandRouter ---


@pytest.mark.asyncio
async def test_help(router, mock_bot):
    await router.handle_message("/help")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "littleflame Commands" in msg
    assert "/status" in msg


@pytest.mark.asyncio
async def test_status_no_agents(router, mock_bot):
    await router.handle_message("/status")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "No active tasks" in msg


@pytest.mark.asyncio
async def test_status_with_executions(router, mock_bot):
    ex = Execution(
        job_name="test-job",
        agent_name="test",
        status=JobStatus.SUCCESS,
        started_at=datetime.now(UTC),
    )
    db_module.insert_execution(ex)

    await router.handle_message("/status")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Recent Executions" in msg
    assert "test" in msg


@pytest.mark.asyncio
async def test_jobs_empty(router, mock_bot):
    await router.handle_message("/jobs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No jobs" in msg


@pytest.mark.asyncio
async def test_jobs_with_data(router, mock_bot):
    from careful_claude_claw.models import Job

    db_module.insert_job(Job(name="my-job", task="do stuff"))

    await router.handle_message("/jobs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Jobs" in msg
    assert "my-job" in msg


@pytest.mark.asyncio
async def test_runs_empty(router, mock_bot):
    await router.handle_message("/runs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No executions" in msg


@pytest.mark.asyncio
async def test_runs_with_data(router, mock_bot):
    for i in range(3):
        db_module.insert_execution(
            Execution(
                job_name=f"job-{i}",
                agent_name=f"agent-{i}",
                started_at=datetime.now(UTC),
            )
        )

    await router.handle_message("/runs")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Recent Executions" in msg
    assert "agent-0" in msg


@pytest.mark.asyncio
async def test_skills(router, mock_bot, monkeypatch):
    import careful_claude_claw.skills as skills_module

    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", Path("/nonexistent"))
    await router.handle_message("/skills")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No skills" in msg


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
async def test_unrecognized_slash_command_errors(router, mock_bot):
    await router.handle_message("/stauts")
    msg = mock_bot.send_message.call_args[0][0]
    assert "not a valid command" in msg
    assert "/help" in msg


@pytest.mark.asyncio
async def test_unrecognized_slash_command_does_not_spawn_agent(router, mock_bot):
    with patch(
        "careful_claude_claw.bot.run_interactive_agent", new_callable=AsyncMock
    ) as mock_run:
        await router.handle_message("/blah something")
        mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_free_text_spawns_agent(router, mock_bot):
    with patch(
        "careful_claude_claw.bot.run_interactive_agent", new_callable=AsyncMock
    ) as mock_run:
        await router.handle_message("what time is it?")
        mock_bot.send_message.assert_called_with("@T1: Starting...")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["task"] == "what time is it?"
        assert mock_run.call_args.kwargs["name"] == "T1"


@pytest.mark.asyncio
async def test_command_with_bot_mention(router, mock_bot):
    await router.handle_message("/help@littleflameBot")
    mock_bot.send_message.assert_called_once()
    msg = mock_bot.send_message.call_args[0][0]
    assert "littleflame Commands" in msg


@pytest.mark.asyncio
async def test_empty_message(router, mock_bot):
    await router.handle_message("")
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_empty_whitespace_message(router, mock_bot):
    await router.handle_message("   ")
    mock_bot.send_message.assert_not_called()


# --- /tasks ---


@pytest.mark.asyncio
async def test_tasks_empty(router, mock_bot):
    await router.handle_message("/tasks")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No active tasks" in msg


@pytest.mark.asyncio
async def test_tasks_with_sessions(router, mock_bot):
    client = MagicMock()
    session = AgentSession(name="task-1", execution_id="e1", client=client)
    register_session(session)

    await router.handle_message("/tasks")
    msg = mock_bot.send_message.call_args[0][0]
    assert "task-1" in msg
    assert "Active Tasks (1)" in msg


# --- /kill ---


@pytest.mark.asyncio
async def test_kill_no_args(router, mock_bot):
    await router.handle_message("/kill")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


@pytest.mark.asyncio
async def test_kill_named_agent(router, mock_bot):
    with patch("careful_claude_claw.bot.kill_session", new_callable=AsyncMock) as mock_kill:
        mock_kill.return_value = True
        await router.handle_message("/kill task-1")
        mock_kill.assert_awaited_once_with("task-1")
        msg = mock_bot.send_message.call_args[0][0]
        assert "Killed" in msg
        assert "task-1" in msg


@pytest.mark.asyncio
async def test_kill_not_found(router, mock_bot):
    with patch("careful_claude_claw.bot.kill_session", new_callable=AsyncMock) as mock_kill:
        mock_kill.return_value = False
        await router.handle_message("/kill nope")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active task" in msg


@pytest.mark.asyncio
async def test_kill_all(router, mock_bot):
    with patch(
        "careful_claude_claw.bot.kill_all_sessions", new_callable=AsyncMock
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
    with patch("careful_claude_claw.bot.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await router.handle_message("/reply task-1 check the tests")
        mock_send.assert_awaited_once_with("task-1", "check the tests")
        # No error message sent
        mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_reply_agent_not_found(router, mock_bot):
    with patch("careful_claude_claw.bot.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = False
        await router.handle_message("/reply nope hello")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active task" in msg


# --- @name routing ---


@pytest.mark.asyncio
async def test_at_reply_sends_to_agent(router, mock_bot):
    with patch("careful_claude_claw.bot.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await router.handle_message("@task-1 what about failing tests?")
        mock_send.assert_awaited_once_with("task-1", "what about failing tests?")


@pytest.mark.asyncio
async def test_at_reply_agent_not_found(router, mock_bot):
    with patch("careful_claude_claw.bot.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = False
        await router.handle_message("@nope hello")
        msg = mock_bot.send_message.call_args[0][0]
        assert "No active task" in msg


@pytest.mark.asyncio
async def test_at_reply_no_message(router, mock_bot):
    await router.handle_message("@task-1")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Usage" in msg


# --- /status with sessions ---


@pytest.mark.asyncio
async def test_status_with_sessions(router, mock_bot):
    client = MagicMock()
    session = AgentSession(name="task-1", execution_id="e1234567-rest", client=client)
    register_session(session)

    await router.handle_message("/status")
    msg = mock_bot.send_message.call_args[0][0]
    assert "Active Tasks" in msg
    assert "task-1" in msg


# --- help includes new commands ---


@pytest.mark.asyncio
async def test_help_includes_new_commands(router, mock_bot):
    await router.handle_message("/help")
    msg = mock_bot.send_message.call_args[0][0]
    assert "/kill" in msg
    assert "/reply" in msg
    assert "/tasks" in msg
    assert "@<name>" in msg
    assert "/new" in msg


# --- _extract_file_info ---


def test_extract_file_info_document():
    msg = {"document": {"file_id": "abc123", "file_name": "report.pdf"}}
    result = _extract_file_info(msg)
    assert result == ("abc123", "report.pdf", "document")


def test_extract_file_info_photo():
    msg = {
        "photo": [
            {"file_id": "small", "width": 90},
            {"file_id": "large", "width": 800},
        ]
    }
    result = _extract_file_info(msg)
    assert result == ("large", "photo.jpg", "photo")


def test_extract_file_info_none():
    msg = {"text": "just text"}
    assert _extract_file_info(msg) is None


def test_extract_file_info_voice():
    msg = {"voice": {"file_id": "voice123", "duration": 5}}
    result = _extract_file_info(msg)
    assert result == ("voice123", "voice.ogg", "voice")


# --- handle_file_message ---


@pytest.mark.asyncio
async def test_handle_file_with_target(router, mock_bot, tmp_path):
    """File with @name caption routes to the named agent."""
    client = MagicMock()
    client.query = AsyncMock()
    session = AgentSession(
        name="T1",
        execution_id="e1",
        client=client,
        cwd=tmp_path,
        is_temp_workspace=False,
    )
    register_session(session)

    mock_bot.download_file = AsyncMock(return_value=tmp_path / "report.pdf")

    with patch("careful_claude_claw.bot.send_to_agent", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await router.handle_file_message("abc", "report.pdf", "document", "@T1 please review this")
        mock_bot.download_file.assert_awaited_once()
        mock_send.assert_awaited_once()
        sent_msg = mock_send.call_args[0][1]
        assert "report.pdf" in sent_msg


@pytest.mark.asyncio
async def test_handle_file_asks_which_agent(router, mock_bot):
    """Multiple active agents + no @name → asks user which agent."""
    client = MagicMock()
    s1 = AgentSession(name="T1", execution_id="e1", client=client)
    s2 = AgentSession(name="T2", execution_id="e2", client=client)
    register_session(s1)
    register_session(s2)

    await router.handle_file_message("abc", "data.csv", "document", "")
    msg_text = mock_bot.send_message.call_args[0][0]
    assert "Which task" in msg_text
    assert "@T1" in msg_text
    assert "@T2" in msg_text
    assert "/new" in msg_text
    assert router._pending_file is not None
    assert router._pending_file["file_id"] == "abc"


@pytest.mark.asyncio
async def test_handle_file_no_agents_spawns(router, mock_bot):
    """No active agents → spawns a new agent with the file."""
    with patch(
        "careful_claude_claw.bot.run_interactive_agent", new_callable=AsyncMock
    ) as mock_run:
        mock_bot.download_file = AsyncMock()
        await router.handle_file_message("abc", "readme.pdf", "document", "summarize this")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["task"] == "summarize this"


# --- /new command ---


@pytest.mark.asyncio
async def test_new_no_pending(router, mock_bot):
    await router.handle_message("/new")
    msg = mock_bot.send_message.call_args[0][0]
    assert "No pending file" in msg


@pytest.mark.asyncio
async def test_new_with_pending_file(router, mock_bot):
    router._pending_file = {
        "file_id": "abc",
        "file_name": "data.csv",
        "media_type": "document",
        "caption": "analyze this",
    }
    with patch(
        "careful_claude_claw.bot.run_interactive_agent", new_callable=AsyncMock
    ) as mock_run:
        mock_bot.download_file = AsyncMock()
        await router.handle_message("/new")
        mock_run.assert_called_once()
        assert router._pending_file is None
