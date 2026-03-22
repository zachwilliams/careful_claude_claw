"""Tests for Slack-specific bot code."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.agent_session as agent_session_module
import careful_claude_claw.db as db_module
from careful_claude_claw.agent_session import AGENT_SESSIONS
from careful_claude_claw.config import Settings
from careful_claude_claw.slack import SlackBot, _strip_bot_mention


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


# --- Settings (Slack) ---


def test_slack_configured_when_env_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-123")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-456")
    s = Settings()
    assert s.slack_configured
    assert s.slack_bot_token == "xoxb-123"
    assert s.slack_app_token == "xapp-456"


def test_slack_not_configured_when_env_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    s = Settings()
    assert not s.slack_configured


def test_slack_not_configured_when_partial(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-123")
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    s = Settings()
    assert not s.slack_configured


def test_slack_allowed_user_id_optional(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-123")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-456")
    monkeypatch.delenv("SLACK_ALLOWED_USER_ID", raising=False)
    s = Settings()
    assert s.slack_configured
    assert s.slack_allowed_user_id == ""


# --- _strip_bot_mention ---


def test_strip_bot_mention_removes_prefix():
    assert _strip_bot_mention("<@U123ABC> hello", "U123ABC") == "hello"


def test_strip_bot_mention_with_extra_spaces():
    assert _strip_bot_mention("  <@U123ABC>   do a thing", "U123ABC") == "do a thing"


def test_strip_bot_mention_no_mention():
    assert _strip_bot_mention("hello world", "U123ABC") == "hello world"


def test_strip_bot_mention_different_user():
    assert _strip_bot_mention("<@UOTHER> hello", "U123ABC") == "<@UOTHER> hello"


# --- SlackBot ---


@pytest.fixture
def mock_app():
    app = MagicMock()
    app.client = MagicMock()
    app.client.chat_postMessage = AsyncMock()
    app.client.files_info = AsyncMock()
    return app


@pytest.fixture
def slack_bot(mock_app):
    bot = SlackBot(app=mock_app, bot_token="xoxb-123")
    bot.reply_channel = "C0123456789"
    return bot


def test_slack_bot_platform(slack_bot):
    assert slack_bot.platform == "slack"


@pytest.mark.asyncio
async def test_send_message_short(slack_bot, mock_app):
    await slack_bot.send_message("hello world")
    mock_app.client.chat_postMessage.assert_awaited_once_with(
        channel="C0123456789",
        text="hello world",
        mrkdwn=True,
    )


@pytest.mark.asyncio
async def test_send_message_splits_long(slack_bot, mock_app):
    long_text = "a" * 5000
    await slack_bot.send_message(long_text)
    assert mock_app.client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_send_message_no_reply_channel(mock_app):
    bot = SlackBot(app=mock_app, bot_token="xoxb-123")
    # reply_channel is empty — should not call chat_postMessage
    await bot.send_message("hello")
    mock_app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_file(slack_bot, mock_app, tmp_path):
    mock_app.client.files_info.return_value = {
        "file": {
            "url_private_download": "https://files.slack.com/files/test.pdf",
        }
    }

    dest = tmp_path / "test.pdf"
    fake_content = b"PDF content"

    with patch.object(slack_bot._http, "get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.content = fake_content
        mock_get.return_value = mock_response

        result = await slack_bot.download_file("F123456", dest)

    assert result == dest
    assert dest.read_bytes() == fake_content
    mock_app.client.files_info.assert_awaited_once_with(file="F123456")
    mock_get.assert_awaited_once()
    call_kwargs = mock_get.call_args[1]
    assert "Authorization" in call_kwargs.get("headers", {})
    assert "xoxb-123" in call_kwargs["headers"]["Authorization"]
