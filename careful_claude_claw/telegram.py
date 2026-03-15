"""Telegram bot listener for CarefulClaudeClaw.

Long-polls the Telegram Bot API for messages, routes commands to
direct handlers or background agent tasks.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .agent import run_agent
from .agent_session import (
    AgentSession,
    generate_name,
    get_session,
    kill_all_sessions,
    kill_session,
    list_sessions,
    run_interactive_agent,
    send_to_agent,
)
from .db import init_db, list_executions, list_jobs
from .skills import discover_skills, get_skill

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_SYSTEM_PROMPT = (
    "You are CarefulClaw, a personal AI assistant responding via Telegram. Respond concisely."
)
TELEGRAM_CONVERSATIONAL_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "WebSearch",
    "WebFetch",
]


def load_telegram_config() -> tuple[str, int] | None:
    """Read bot token and chat_id from ~/.mcp-telegram/config.json.

    Returns (token, chat_id) or None if not configured.
    """
    config_path = Path.home() / ".mcp-telegram" / "config.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
        # Support both flat keys and nested {"bot": {...}} structure
        bot_section = data.get("bot", {})
        token = data.get("botToken") or data.get("token") or bot_section.get("token")
        chat_id = data.get("chatId") or data.get("chat_id") or bot_section.get("chat_id")
        if not token or not chat_id:
            return None
        return token, int(chat_id)
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


class TelegramBot:
    """Thin async wrapper around the Telegram Bot API."""

    def __init__(self, token: str, chat_id: int) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = f"{TELEGRAM_API}/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

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

    async def download_file(self, file_path: str, destination: Path) -> Path:
        """Download a file from Telegram servers to a local path."""
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
        chunks = _split_message(text)
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


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    """Split a message into chunks of at most max_len characters."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


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


class CommandRouter:
    """Routes incoming Telegram messages to handlers."""

    def __init__(self, bot: TelegramBot) -> None:
        self.bot = bot
        self._pending_file: dict | None = None  # stored file info awaiting routing
        self._commands: dict[str, callable] = {
            "/help": self._handle_help,
            "/status": self._handle_status,
            "/jobs": self._handle_jobs,
            "/runs": self._handle_runs,
            "/skills": self._handle_skills,
            "/tasks": self._handle_tasks,
        }

    async def handle_message(self, text: str) -> None:
        """Route a message to the appropriate handler."""
        text = text.strip()
        if not text:
            return

        # Check for @name routing (e.g. "@task-1 do something")
        if text.startswith("@"):
            await self._handle_at_reply(text)
            return

        # Check for exact commands or /run
        cmd = text.split()[0].lower()
        # Strip bot mention suffix (e.g. /status@CarefulClawBot)
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        if cmd in self._commands:
            await self._commands[cmd]()
        elif cmd == "/run":
            await self._handle_run(text)
        elif cmd == "/kill":
            await self._handle_kill(text)
        elif cmd == "/reply":
            await self._handle_reply(text)
        elif cmd == "/new":
            await self._handle_new()
        elif cmd.startswith("/"):
            await self.bot.send_message(
                "That's not a valid command. Type /help to see all commands."
            )
        else:
            # Free text -> spawn agent
            await self._spawn_agent(text)

    async def _handle_help(self) -> None:
        lines = [
            "*CarefulClaw Commands*",
            "",
            "*Direct commands* (instant response):",
            "`/status`  — Active tasks + recent executions",
            "`/tasks`  — List active tasks",
            "`/jobs`  — Job definitions",
            "`/runs`  — Recent execution history",
            "`/skills`  — Available skills",
            "`/help`  — This message",
            "",
            "*Task commands* (run in background):",
            "`/run <skill>`  — Run a named skill",
            "Free text  — Treated as a task, spawns an agent",
            "",
            "*Active task commands:*",
            "`/kill <name>`  — Kill a running task",
            "`/kill all`  — Kill all running tasks",
            "`/reply <name> <message>`  — Send input to a running task",
            "`@<name> <message>`  — Shorthand for /reply",
            "`/new`  — Start a new task with a pending file",
            "",
            "*File attachments:*",
            "Send a file with `@<name>` in the caption to route it",
            "Send a file without a caption to be prompted for routing",
        ]
        await self.bot.send_message("\n".join(lines))

    async def _handle_status(self) -> None:
        sessions = list_sessions()
        executions = list_executions(limit=5)

        lines = []
        if sessions:
            lines.append(f"*Active Tasks ({len(sessions)})*")
            for s in sessions:
                lines.append(f"  `{s.name}` (exec: {s.execution_id[:8]})")
        else:
            lines.append("No active tasks.")

        lines.append("")
        if executions:
            lines.append("*Recent Executions*")
            for e in executions:
                status = e["status"]
                agent = e["agent_name"]
                lines.append(f"  {status} — {agent}")
        else:
            lines.append("No recent executions.")

        await self.bot.send_message("\n".join(lines))

    async def _handle_jobs(self) -> None:
        jobs = list_jobs()
        if not jobs:
            await self.bot.send_message("No jobs defined.")
            return

        lines = ["*Jobs*"]
        for j in jobs:
            task_or_skill = j["skill_name"] or (j["task"] or "")[:40]
            cron = f" `{j['cron_expr']}`" if j.get("cron_expr") else ""
            enabled = " [off]" if not j["enabled"] else ""
            lines.append(f"  {j['name']}{cron}{enabled}: {task_or_skill}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_runs(self) -> None:
        executions = list_executions(limit=10)
        if not executions:
            await self.bot.send_message("No executions found.")
            return

        lines = ["*Recent Executions*"]
        for e in executions:
            job = f" ({e['job_name']})" if e.get("job_name") else ""
            lines.append(f"  {e['status']} — {e['agent_name']}{job}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_skills(self) -> None:
        found = discover_skills()
        if not found:
            await self.bot.send_message("No skills found.")
            return

        lines = ["*Skills*"]
        for s in found:
            desc = f" — {s.description[:60]}" if s.description else ""
            lines.append(f"  {s.name}{desc}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_tasks(self) -> None:
        sessions = list_sessions()
        if not sessions:
            await self.bot.send_message("No active tasks.")
            return

        lines = [f"*Active Tasks ({len(sessions)})*"]
        for s in sessions:
            age = datetime.now(UTC) - s.created_at
            mins = int(age.total_seconds() // 60)
            lines.append(f"  `{s.name}` — running for {mins}m")
        await self.bot.send_message("\n".join(lines))

    async def _handle_kill(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await self.bot.send_message("Usage: /kill <name> or /kill all")
            return

        target = parts[1].strip()
        if target.lower() == "all":
            count = await kill_all_sessions()
            await self.bot.send_message(f"Killed {count} task(s).")
        else:
            killed = await kill_session(target)
            if killed:
                await self.bot.send_message(f"Killed task `{target}`.")
            else:
                await self.bot.send_message(f"No active task named `{target}`.")

    async def _handle_reply(self, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await self.bot.send_message("Usage: /reply <name> <message>")
            return

        name = parts[1]
        message = parts[2]
        sent = await send_to_agent(name, message)
        if not sent:
            await self.bot.send_message(f"No active task named `{name}`.")

    async def _handle_at_reply(self, text: str) -> None:
        """Handle @name message routing."""
        parts = text.split(maxsplit=1)
        name = parts[0][1:]  # strip the @
        if not name:
            await self._spawn_agent(text)
            return

        if len(parts) < 2:
            await self.bot.send_message(f"Usage: @{name} <message>")
            return

        message = parts[1]
        sent = await send_to_agent(name, message)
        if not sent:
            await self.bot.send_message(f"No active task named `{name}`.")

    async def _handle_run(self, text: str) -> None:
        """Parse /run <skill> and spawn an agent."""
        parts = text.split()
        if len(parts) < 2:
            await self.bot.send_message("Usage: /run <skill>")
            return

        skill_name = parts[1]

        skill = get_skill(skill_name)
        if not skill:
            await self.bot.send_message(f"Skill not found: {skill_name}")
            return

        try:
            task = Path(skill.file_path).read_text()
        except OSError:
            await self.bot.send_message(f"Cannot read skill file: {skill.file_path}")
            return

        name = generate_name("S")
        await self.bot.send_message(f"@{name}: Starting skill `{skill_name}`...")
        bot = self.bot

        async def on_message(msg: str) -> None:
            await bot.send_message(msg)

        bg_task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=task,
                on_message=on_message,
                agent_name=f"tg-{name}",
                job_name=f"skill-{skill_name}",
            )
        )
        session = get_session(name)
        if session:
            session.task = bg_task

    async def _spawn_agent(self, text: str) -> None:
        """Spawn a background agent for free-text tasks."""
        name = generate_name("T")
        await self.bot.send_message(f"@{name}: Starting...")
        bot = self.bot

        async def on_message(msg: str) -> None:
            await bot.send_message(msg)

        task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=text,
                on_message=on_message,
                agent_name=f"tg-{name}",
                system_prompt=TELEGRAM_SYSTEM_PROMPT,
                allowed_tools=TELEGRAM_CONVERSATIONAL_TOOLS,
            )
        )
        session = get_session(name)
        if session:
            session.task = task

    async def handle_file_message(self, msg: dict) -> None:
        """Route a file attachment to the appropriate agent."""
        file_info = _extract_file_info(msg)
        if not file_info:
            return
        file_id, file_name, media_type = file_info
        caption = (msg.get("caption") or "").strip()

        # Check for @name in caption
        if caption.startswith("@"):
            parts = caption.split(maxsplit=1)
            target_name = parts[0][1:]
            caption_text = parts[1] if len(parts) > 1 else ""
            session = get_session(target_name)
            if session:
                await self._deliver_file_to_agent(
                    session, file_id, file_name, media_type, caption_text
                )
            else:
                await self.bot.send_message(f"No active task named `{target_name}`.")
            return

        # No @name — check active agents
        sessions = list_sessions()
        if not sessions:
            # No agents running — spawn a new one with the file
            await self._spawn_agent_with_file(file_id, file_name, media_type, caption)
            return

        # Active agents exist — ask which one
        self._pending_file = {
            "file_id": file_id,
            "file_name": file_name,
            "media_type": media_type,
            "caption": caption,
        }
        lines = ["Which task should receive this file?"]
        for s in sessions:
            lines.append(f"  Reply `@{s.name}` to send to that task")
        lines.append("  Reply `/new` to start a new task with this file")
        await self.bot.send_message("\n".join(lines))

    async def _deliver_file_to_agent(
        self,
        session: AgentSession,
        file_id: str,
        file_name: str,
        media_type: str,
        caption: str,
    ) -> None:
        """Download a file and deliver it to an agent's workspace."""
        try:
            tg_file = await self.bot.get_file(file_id)
            tg_path = tg_file.get("file_path", "")
            if not tg_path:
                await self.bot.send_message("Could not retrieve file from Telegram.")
                return

            dest_dir = session.cwd or Path.cwd()
            dest = dest_dir / file_name
            # Deduplicate filename
            counter = 1
            while dest.exists():
                stem = Path(file_name).stem
                suffix = Path(file_name).suffix
                dest = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            await self.bot.download_file(tg_path, dest)

            # Tell the agent about the file
            desc = f"File received ({media_type}): {dest.name}"
            if caption:
                desc += f"\nUser message: {caption}"
            await send_to_agent(session.name, desc)
            await self.bot.send_message(f"@{session.name}: File delivered: `{dest.name}`")
        except Exception:
            logger.exception("Failed to deliver file to agent %s", session.name)
            await self.bot.send_message(f"Failed to deliver file to `{session.name}`.")

    async def _spawn_agent_with_file(
        self,
        file_id: str,
        file_name: str,
        media_type: str,
        caption: str,
    ) -> None:
        """Spawn a new agent and deliver a file to it."""
        task_text = caption or f"Process this {media_type} file: {file_name}"
        name = generate_name("T")
        await self.bot.send_message(f"@{name}: Starting with file...")
        bot = self.bot

        async def on_message(msg: str) -> None:
            await bot.send_message(msg)

        bg_task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=task_text,
                on_message=on_message,
                agent_name=f"tg-{name}",
                system_prompt=TELEGRAM_SYSTEM_PROMPT,
                allowed_tools=TELEGRAM_CONVERSATIONAL_TOOLS,
            )
        )
        session = get_session(name)
        if session:
            session.task = bg_task

        # Wait briefly for workspace to be created, then deliver file
        await asyncio.sleep(0.5)
        session = get_session(name)
        if session:
            await self._deliver_file_to_agent(session, file_id, file_name, media_type, caption)

    async def _handle_new(self) -> None:
        """Handle /new command — spawn a new agent with pending file."""
        if not self._pending_file:
            await self.bot.send_message("No pending file. Send a file first.")
            return

        pf = self._pending_file
        self._pending_file = None
        await self._spawn_agent_with_file(
            pf["file_id"], pf["file_name"], pf["media_type"], pf["caption"]
        )


async def _run_and_reply(
    bot: TelegramBot,
    task: str,
    agent_name: str,
    cwd: str | None = None,
    system_prompt: str | None = None,
) -> None:
    """Run an agent in the background and send the result back via Telegram."""
    try:
        execution = await run_agent(
            agent_name=agent_name,
            task=task,
            cwd=cwd,
            system_prompt=system_prompt,
        )
        if execution.output:
            await bot.send_message(execution.output)
        elif execution.error:
            await bot.send_message(f"Failed: {execution.error}")
        else:
            await bot.send_message(f"Done (status: {execution.status})")
    except Exception:
        logger.exception("Background agent failed")
        await bot.send_message("Agent encountered an error.")


async def run_telegram_listener() -> None:
    """Main loop: long-poll Telegram and route messages."""
    config = load_telegram_config()
    if not config:
        logger.error("Telegram not configured. Add token/chatId to ~/.mcp-telegram/config.json")
        return

    token, chat_id = config
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
                        logger.info("Received file: %s (%s)", file_info[1], file_info[2])
                        try:
                            await router.handle_file_message(msg)
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
