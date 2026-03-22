"""Telegram bot listener for CarefulClaudeClaw.

Long-polls the Telegram Bot API for messages, routes commands to
direct handlers or background agent tasks.
"""

import asyncio
import logging
from pathlib import Path

import httpx

from .bot import BotClient, CommandRouter, split_message
from .config import settings
from .db import init_db

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramBot(BotClient):
    """Thin async wrapper around the Telegram Bot API."""

    def __init__(self, token: str, chat_id: int) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = f"{TELEGRAM_API}/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    @property
    def platform(self) -> str:
        return "telegram"

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict:
        resp = await self._client.get(f"{self._base}/getMe")
        resp.raise_for_status()
        return resp.json()["result"]

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = await self._client.get(
            f"{self._base}/getUpdates",
            params=params,
            timeout=httpx.Timeout(timeout + 10.0),
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    async def get_file(self, file_id: str) -> dict:
        """Get file metadata from Telegram (including file_path for download)."""
        resp = await self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        return resp.json()["result"]

    async def download_file(self, file_id: str, destination: Path) -> Path:
        """Download a file from Telegram servers to a local path.

        Accepts either a file_id (fetches metadata first) or a raw file_path.
        """
        # If file_id looks like a Telegram file_path (contains '/'), use it directly
        if "/" in file_id:
            file_path = file_id
        else:
            tg_file = await self.get_file(file_id)
            file_path = tg_file.get("file_path", "")
            if not file_path:
                raise ValueError(f"Could not retrieve file_path for file_id={file_id}")

        url = f"{TELEGRAM_API}/file/bot{self.token}/{file_path}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(resp.content)
        return destination

    async def send_and_get_id(self, text: str) -> int | None:
        """Send a message and return its message_id."""
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            resp = await self._client.post(f"{self._base}/sendMessage", json=payload)
            if resp.is_success:
                return resp.json().get("result", {}).get("message_id")
        except httpx.HTTPError:
            pass
        return None

    async def delete_message(self, message_id: int) -> bool:
        """Delete a single message. Returns True if successful."""
        try:
            resp = await self._client.post(
                f"{self._base}/deleteMessage",
                json={"chat_id": self.chat_id, "message_id": message_id},
            )
            return resp.is_success
        except httpx.HTTPError:
            return False

    async def delete_all_messages(self, latest_message_id: int) -> int:
        """Delete messages counting down from latest_message_id.

        Stops after 10 consecutive failures (past bot's reachable history).
        """
        deleted = 0
        consecutive_failures = 0
        msg_id = latest_message_id

        while consecutive_failures < 10 and msg_id > 0:
            if await self.delete_message(msg_id):
                deleted += 1
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            msg_id -= 1
            await asyncio.sleep(0.05)  # rate-limit safety

        return deleted

    async def send_message(self, text: str, parse_mode: str | None = "Markdown") -> None:
        """Send a message, auto-splitting if over 4096 chars."""
        chunks = split_message(text, max_len=4096)
        for chunk in chunks:
            payload: dict = {"chat_id": self.chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                resp = await self._client.post(f"{self._base}/sendMessage", json=payload)
                if not resp.is_success and parse_mode:
                    # Retry without parse_mode if formatting fails
                    payload.pop("parse_mode")
                    await self._client.post(f"{self._base}/sendMessage", json=payload)
            except httpx.HTTPError:
                logger.exception("Failed to send Telegram message")


def _extract_file_info(msg: dict) -> tuple[str, str, str] | None:
    """Extract (file_id, filename, media_type) from a Telegram message.

    Supports document, photo, video, audio, voice, video_note.
    Returns None if no attachment is present.
    """
    if "document" in msg:
        doc = msg["document"]
        return doc["file_id"], doc.get("file_name", "document"), "document"
    if "photo" in msg:
        # photos come as array of sizes; take the largest
        photo = msg["photo"][-1]
        return photo["file_id"], "photo.jpg", "photo"
    if "video" in msg:
        vid = msg["video"]
        return vid["file_id"], vid.get("file_name", "video.mp4"), "video"
    if "audio" in msg:
        aud = msg["audio"]
        return aud["file_id"], aud.get("file_name", "audio.mp3"), "audio"
    if "voice" in msg:
        voice = msg["voice"]
        return voice["file_id"], "voice.ogg", "voice"
    if "video_note" in msg:
        vn = msg["video_note"]
        return vn["file_id"], "video_note.mp4", "video_note"
    return None


async def run_telegram_listener() -> None:
    """Main loop: long-poll Telegram and route messages."""
    if not settings.telegram_configured:
        logger.error("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return

    token, chat_id = settings.telegram_bot_token, settings.telegram_chat_id
    init_db()

    bot = TelegramBot(token, chat_id)
    router = CommandRouter(bot)

    try:
        me = await bot.get_me()
        logger.info("Telegram bot connected: @%s", me.get("username", "?"))
    except Exception:
        logger.exception("Failed to connect to Telegram")
        await bot.close()
        return

    offset: int | None = None
    try:
        while True:
            try:
                updates = await bot.get_updates(offset=offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    msg_chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text")

                    # Security: only process messages from configured chat_id
                    if msg_chat_id != chat_id:
                        continue

                    # Check for file attachments before text-only guard
                    file_info = _extract_file_info(msg)
                    if file_info:
                        file_id, file_name, media_type = file_info
                        caption = (msg.get("caption") or "").strip()
                        logger.info("Received file: %s (%s)", file_name, media_type)
                        try:
                            await router.handle_file_message(
                                file_id, file_name, media_type, caption
                            )
                        except Exception:
                            logger.exception("Error handling file message")
                            await bot.send_message("Error processing your file.")
                        continue

                    if not text:
                        continue

                    logger.info("Received: %s", text[:100])
                    try:
                        await router.handle_message(text)
                    except Exception:
                        logger.exception("Error handling message: %s", text[:100])
                        await bot.send_message("Error processing your message.")

            except (httpx.HTTPError, httpx.TimeoutException):
                logger.warning("Telegram poll error, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception:
                logger.exception("Unexpected error in Telegram listener")
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        logger.info("Telegram listener cancelled.")
    finally:
        await bot.close()
        logger.info("Telegram listener stopped.")
