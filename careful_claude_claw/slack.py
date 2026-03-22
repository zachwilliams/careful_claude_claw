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
        # Thread timestamp for the current message; bot replies into this thread.
        self.reply_thread_ts: str = ""
        # (channel_id, thread_ts) pairs where the bot is participating.
        self.active_threads: set[tuple[str, str]] = set()
        # Channel/ts of the thinking reaction to remove on first reply.
        self._thinking_channel: str = ""
        self._thinking_ts: str = ""

    @property
    def platform(self) -> str:
        return "slack"

    async def close(self) -> None:
        await self._http.aclose()

    async def _clear_thinking_reaction(self) -> None:
        """Remove the thinking reaction from the triggering message, if set."""
        if not self._thinking_ts:
            return
        channel, ts = self._thinking_channel, self._thinking_ts
        self._thinking_channel = ""
        self._thinking_ts = ""
        try:
            await self._app.client.reactions_remove(channel=channel, timestamp=ts, name="thinking_face")
        except Exception:
            logger.debug("Could not remove thinking reaction")

    def get_reply_fn(self) -> Callable[[str], Awaitable[None]]:
        """Snapshot the current reply context so background agents reply to the right place."""
        channel = self.reply_channel
        thread_ts = self.reply_thread_ts
        app = self._app
        bot = self

        async def _send(text: str) -> None:
            if not channel:
                logger.warning("get_reply_fn: no channel captured")
                return
            await bot._clear_thinking_reaction()
            chunks = split_message(text, max_len=SLACK_MESSAGE_MAX_LEN)
            for chunk in chunks:
                try:
                    kwargs: dict = {"channel": channel, "text": chunk, "mrkdwn": True}
                    if thread_ts:
                        kwargs["thread_ts"] = thread_ts
                    await app.client.chat_postMessage(**kwargs)
                except Exception:
                    logger.exception("Failed to send Slack message")

        return _send

    async def send_message(self, text: str) -> None:
        """Send a message to the current reply channel, auto-splitting if needed."""
        if not self.reply_channel:
            logger.warning("send_message called with no reply_channel set")
            return
        await self._clear_thinking_reaction()
        chunks = split_message(text, max_len=SLACK_MESSAGE_MAX_LEN)
        for chunk in chunks:
            try:
                kwargs: dict = {
                    "channel": self.reply_channel,
                    "text": chunk,
                    "mrkdwn": True,
                }
                if self.reply_thread_ts:
                    kwargs["thread_ts"] = self.reply_thread_ts
                await self._app.client.chat_postMessage(**kwargs)
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

    # Start persistent orchestrator (loads memories, connects Claude session)
    from .persistent_orchestrator import PersistentOrchestrator

    persistent_orch = PersistentOrchestrator()
    try:
        logger.info("Starting persistent orchestrator...")
        await asyncio.wait_for(persistent_orch.wake(), timeout=60)
        logger.info("Persistent orchestrator is awake and ready.")
    except TimeoutError:
        logger.warning("Persistent orchestrator timed out, falling back to stateless")
        persistent_orch = None
    except Exception as exc:
        logger.warning("Failed to start persistent orchestrator: %s", exc)
        persistent_orch = None

    router = CommandRouter(slack_bot, persistent_orchestrator=persistent_orch)

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
        # Reply in the same thread; if the message is already in a thread use
        # its thread_ts, otherwise use the message's own ts to start one.
        msg_ts = event.get("ts", "")
        slack_bot.reply_thread_ts = event.get("thread_ts") or msg_ts
        # Remember this thread so we can pick up future replies without @mention.
        if slack_bot.reply_thread_ts:
            slack_bot.active_threads.add((channel, slack_bot.reply_thread_ts))

        text = (event.get("text") or "").strip()
        # Strip bot @mention prefix (present in app_mention events)
        text = _strip_bot_mention(text, bot_user_id)

        # Handle file shares
        files = event.get("files", [])
        if files:
            try:
                await app.client.reactions_add(channel=channel, timestamp=msg_ts, name="thinking_face")
            except Exception:
                logger.debug("Could not add thinking reaction")
            slack_bot._thinking_channel = channel
            slack_bot._thinking_ts = msg_ts
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
            await app.client.reactions_add(channel=channel, timestamp=msg_ts, name="thinking_face")
        except Exception:
            logger.debug("Could not add thinking reaction")
        slack_bot._thinking_channel = channel
        slack_bot._thinking_ts = msg_ts
        try:
            await router.handle_message(text)
        except Exception:
            logger.exception("Error handling Slack message: %s", text[:100])
            await slack_bot.send_message("Error processing your message.")

    @app.event("app_mention")
    async def handle_mention(event: dict, say: object) -> None:  # noqa: ARG001
        """Handle @mentions of the bot in any channel."""
        await _route_event(event)

    @app.event("message")
    async def handle_channel_thread_reply(event: dict, say: object) -> None:  # noqa: ARG001
        """Handle thread replies in channels where the bot is already participating."""
        thread_ts = event.get("thread_ts")
        channel = event.get("channel", "")
        logger.debug(
            "message event: channel=%s thread_ts=%s active_threads=%s",
            channel, thread_ts, slack_bot.active_threads,
        )
        if not thread_ts:
            return
        if (channel, thread_ts) not in slack_bot.active_threads:
            logger.debug("Thread (%s, %s) not in active_threads — ignoring", channel, thread_ts)
            return
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
