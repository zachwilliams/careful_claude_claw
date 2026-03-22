"""Slack bot listener for CarefulClaudeClaw.

Uses slack-bolt with Socket Mode for receiving messages and routing
commands to direct handlers or background agent tasks.

Accepts DMs to the bot and @mentions in any channel. Replies are sent
back to whichever channel the message came from. If SLACK_ALLOWED_USER_ID
is set, messages from any other user are silently ignored.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .bot import BotClient, CommandRouter, split_message
from .config import settings
from .db import init_db

logger = logging.getLogger(__name__)

SLACK_MESSAGE_MAX_LEN = 4000


class SlackBot(BotClient):
    """Async Slack bot client using slack-bolt."""

    def __init__(self, app: AsyncApp, bot_token: str) -> None:
        self._app = app
        self._bot_token = bot_token
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        # Set before each message is routed; replies go back to this channel.
        self.reply_channel: str = ""

    @property
    def platform(self) -> str:
        return "slack"

    async def close(self) -> None:
        await self._http.aclose()

    def get_reply_fn(self) -> Callable[[str], Awaitable[None]]:
        """Snapshot the current reply_channel so background agents reply to the right place."""
        channel = self.reply_channel
        app = self._app

        async def _send(text: str) -> None:
            if not channel:
                logger.warning("get_reply_fn: no channel captured")
                return
            chunks = split_message(text, max_len=SLACK_MESSAGE_MAX_LEN)
            for chunk in chunks:
                try:
                    await app.client.chat_postMessage(
                        channel=channel,
                        text=chunk,
                        mrkdwn=True,
                    )
                except Exception:
                    logger.exception("Failed to send Slack message")

        return _send

    async def send_message(self, text: str) -> None:
        """Send a message to the current reply channel, auto-splitting if needed."""
        if not self.reply_channel:
            logger.warning("send_message called with no reply_channel set")
            return
        chunks = split_message(text, max_len=SLACK_MESSAGE_MAX_LEN)
        for chunk in chunks:
            try:
                await self._app.client.chat_postMessage(
                    channel=self.reply_channel,
                    text=chunk,
                    mrkdwn=True,
                )
            except Exception:
                logger.exception("Failed to send Slack message")

    async def download_file(self, file_id: str, destination: Path) -> Path:
        """Download a file from Slack using the files.info API."""
        try:
            info = await self._app.client.files_info(file=file_id)
            file_data = info["file"]
            url = file_data.get("url_private_download") or file_data.get("url_private")
            if not url:
                raise ValueError(f"No download URL for Slack file {file_id}")

            resp = await self._http.get(
                url,
                headers={"Authorization": f"Bearer {self._bot_token}"},
            )
            resp.raise_for_status()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(resp.content)
            return destination
        except Exception:
            logger.exception("Failed to download Slack file %s", file_id)
            raise


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove leading <@BOT_USER_ID> mention from text."""
    return re.sub(rf"^\s*<@{re.escape(bot_user_id)}>\s*", "", text).strip()


async def run_slack_listener() -> None:
    """Main loop: connect via Socket Mode and route Slack messages."""
    if not settings.slack_configured:
        logger.error(
            "Slack not configured. Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env"
        )
        return

    bot_token = settings.slack_bot_token
    app_token = settings.slack_app_token
    allowed_user_id = settings.slack_allowed_user_id
    init_db()

    app = AsyncApp(token=bot_token)
    slack_bot = SlackBot(app=app, bot_token=bot_token)
    router = CommandRouter(slack_bot)

    # Discover the bot's own user ID so we can strip mentions from text.
    try:
        auth = await app.client.auth_test()
        bot_user_id: str = auth["user_id"]
        logger.info("Slack bot connected: @%s (%s)", auth.get("user"), bot_user_id)
    except Exception:
        logger.exception("Failed to call auth.test — cannot start Slack listener")
        return

    if allowed_user_id:
        logger.info("Slack: only accepting messages from user %s", allowed_user_id)
    else:
        logger.warning(
            "Slack: SLACK_ALLOWED_USER_ID not set — accepting messages from any user"
        )

    async def _route_event(event: dict) -> None:
        """Common handler: security checks, set reply channel, route."""
        # Ignore bot messages and edits
        if event.get("bot_id") or event.get("subtype"):
            return

        user_id = event.get("user", "")
        if allowed_user_id and user_id != allowed_user_id:
            channel = event.get("channel", "")
            if channel:
                slack_bot.reply_channel = channel
                await slack_bot.send_message("Sorry I am not allowed to talk to you :(")
            return

        channel = event.get("channel", "")
        if not channel:
            return

        slack_bot.reply_channel = channel

        text = (event.get("text") or "").strip()
        # Strip bot @mention prefix (present in app_mention events)
        text = _strip_bot_mention(text, bot_user_id)

        # Handle file shares
        files = event.get("files", [])
        if files:
            for f in files:
                file_id = f.get("id", "")
                file_name = f.get("name", "file")
                media_type = f.get("filetype", "document")
                logger.info("Received Slack file: %s (%s)", file_name, media_type)
                try:
                    await router.handle_file_message(file_id, file_name, media_type, text)
                except Exception:
                    logger.exception("Error handling Slack file message")
                    await slack_bot.send_message("Error processing your file.")
            return

        if not text:
            return

        logger.info("Received Slack message from %s: %s", user_id, text[:100])
        try:
            await router.handle_message(text)
        except Exception:
            logger.exception("Error handling Slack message: %s", text[:100])
            await slack_bot.send_message("Error processing your message.")

    @app.event("message")
    async def handle_dm(event: dict, say: object) -> None:  # noqa: ARG001
        """Handle direct messages to the bot."""
        # Only process DMs (im = instant message / direct message channel type)
        if event.get("channel_type") != "im":
            return
        await _route_event(event)

    @app.event("app_mention")
    async def handle_mention(event: dict, say: object) -> None:  # noqa: ARG001
        """Handle @mentions of the bot in any channel."""
        await _route_event(event)

    handler = AsyncSocketModeHandler(app, app_token)
    try:
        logger.info("Connecting to Slack via Socket Mode...")
        await handler.start_async()
    except asyncio.CancelledError:
        logger.info("Slack listener cancelled.")
    finally:
        await handler.close_async()
        await slack_bot.close()
        logger.info("Slack listener stopped.")
