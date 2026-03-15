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
    generate_name,
    get_session,
    kill_all_sessions,
    kill_session,
    list_sessions,
    run_interactive_agent,
    send_to_agent,
)
from .db import init_db, list_jobs, list_projects, list_schedules
from .skills import discover_skills, get_skill

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_SYSTEM_PROMPT = (
    "You are CarefulClaw, a personal AI assistant responding via Telegram. Respond concisely."
)
TELEGRAM_CONVERSATIONAL_TOOLS = [
    "Read", "Glob", "Grep", "Bash", "WebSearch", "WebFetch",
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


class CommandRouter:
    """Routes incoming Telegram messages to handlers."""

    def __init__(self, bot: TelegramBot) -> None:
        self.bot = bot
        self._commands: dict[str, callable] = {
            "/help": self._handle_help,
            "/status": self._handle_status,
            "/jobs": self._handle_jobs,
            "/projects": self._handle_projects,
            "/skills": self._handle_skills,
            "/schedules": self._handle_schedules,
            "/agents": self._handle_agents,
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
        else:
            # Free text -> spawn agent
            await self._spawn_agent(text)

    async def _handle_help(self) -> None:
        lines = [
            "*CarefulClaw Commands*",
            "",
            "*Direct commands* (instant response):",
            "`/status`  — Active agents + recent jobs",
            "`/agents`  — List active agent sessions",
            "`/jobs`  — Last 10 jobs with status",
            "`/projects`  — Registered projects",
            "`/skills`  — Available skills (global + per-project)",
            "`/schedules`  — Scheduled tasks with cron expressions",
            "`/help`  — This message",
            "",
            "*Agent commands* (run in background):",
            "`/run <skill>`  — Run a named skill",
            "`/run <skill> --project <name>`  — Run skill with project context",
            "Free text  — Treated as a task, spawns an agent",
            "",
            "*Interactive agent commands:*",
            "`/kill <name>`  — Kill a running agent",
            "`/kill all`  — Kill all running agents",
            "`/reply <name> <message>`  — Send input to a running agent",
            "`@<name> <message>`  — Shorthand for /reply",
        ]
        await self.bot.send_message("\n".join(lines))

    async def _handle_status(self) -> None:
        sessions = list_sessions()
        jobs = list_jobs(limit=5)

        lines = []
        if sessions:
            lines.append(f"*Active Agents ({len(sessions)})*")
            for s in sessions:
                lines.append(f"  `{s.name}` (job: {s.job_id[:8]})")
        else:
            lines.append("No active agents.")

        lines.append("")
        if jobs:
            lines.append("*Recent Jobs*")
            for j in jobs:
                status = j["status"]
                agent = j["agent_name"]
                lines.append(f"  {status} — {agent}")
        else:
            lines.append("No recent jobs.")

        await self.bot.send_message("\n".join(lines))

    async def _handle_jobs(self) -> None:
        jobs = list_jobs(limit=10)
        if not jobs:
            await self.bot.send_message("No jobs found.")
            return

        lines = ["*Recent Jobs*"]
        for j in jobs:
            task_preview = (j["task"] or "")[:40]
            proj = f" ({j['project_name']})" if j.get("project_name") else ""
            lines.append(f"  {j['status']} — {j['agent_name']}{proj}: {task_preview}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_projects(self) -> None:
        projects = list_projects()
        if not projects:
            await self.bot.send_message("No projects registered.")
            return

        lines = ["*Projects*"]
        for p in projects:
            desc = f" — {p['description']}" if p.get("description") else ""
            lines.append(f"  {p['name']} [{p['status']}]{desc}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_skills(self) -> None:
        found = discover_skills()
        if not found:
            await self.bot.send_message("No skills found.")
            return

        lines = ["*Skills*"]
        for s in found:
            scope = f"[{s.scope.value}]"
            proj = f" ({s.project_name})" if s.project_name else ""
            desc = f" — {s.description[:60]}" if s.description else ""
            lines.append(f"  {s.name} {scope}{proj}{desc}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_schedules(self) -> None:
        schedules = list_schedules()
        if not schedules:
            await self.bot.send_message("No schedules configured.")
            return

        lines = ["*Schedules*"]
        for s in schedules:
            enabled = "on" if s["enabled"] else "off"
            task_or_skill = s["skill_name"] or (s["task"] or "")[:40]
            proj = f" ({s['project_name']})" if s.get("project_name") else ""
            lines.append(f"  {s['name']} [{enabled}] `{s['cron_expr']}` {task_or_skill}{proj}")

        await self.bot.send_message("\n".join(lines))

    async def _handle_agents(self) -> None:
        sessions = list_sessions()
        if not sessions:
            await self.bot.send_message("No active agent sessions.")
            return

        lines = [f"*Active Agent Sessions ({len(sessions)})*"]
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
        if target == "all":
            count = await kill_all_sessions()
            await self.bot.send_message(f"Killed {count} agent(s).")
        else:
            killed = await kill_session(target)
            if killed:
                await self.bot.send_message(f"Killed agent `{target}`.")
            else:
                await self.bot.send_message(f"No active agent named `{target}`.")

    async def _handle_reply(self, text: str) -> None:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await self.bot.send_message("Usage: /reply <name> <message>")
            return

        name = parts[1]
        message = parts[2]
        sent = await send_to_agent(name, message)
        if not sent:
            await self.bot.send_message(f"No active agent named `{name}`.")

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
            await self.bot.send_message(f"No active agent named `{name}`.")

    async def _handle_run(self, text: str) -> None:
        """Parse /run <skill> [--project <name>] and spawn an agent."""
        parts = text.split()
        if len(parts) < 2:
            await self.bot.send_message("Usage: /run <skill> [--project <name>]")
            return

        skill_name = parts[1]
        project_name = None
        if "--project" in parts:
            idx = parts.index("--project")
            if idx + 1 < len(parts):
                project_name = parts[idx + 1]

        skill = get_skill(skill_name, project_name)
        if not skill:
            await self.bot.send_message(f"Skill not found: {skill_name}")
            return

        try:
            task = Path(skill.file_path).read_text()
        except OSError:
            await self.bot.send_message(f"Cannot read skill file: {skill.file_path}")
            return

        cwd = None
        if project_name:
            from .db import get_project

            proj = get_project(project_name)
            if proj:
                cwd = proj["path"]

        name = generate_name(f"skill-{skill_name}")
        await self.bot.send_message(f"[{name}] Starting skill `{skill_name}`...")
        bot = self.bot

        async def on_message(msg: str) -> None:
            await bot.send_message(msg)

        bg_task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=task,
                on_message=on_message,
                agent_name=f"tg-{name}",
                project_name=project_name,
                cwd=cwd,
            )
        )
        session = get_session(name)
        if session:
            session.task = bg_task

    async def _spawn_agent(self, text: str) -> None:
        """Spawn a background agent for free-text tasks."""
        name = generate_name("task")
        await self.bot.send_message(f"[{name}] Starting...")
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


async def _run_and_reply(
    bot: TelegramBot,
    task: str,
    agent_name: str,
    project_name: str | None = None,
    cwd: str | None = None,
    system_prompt: str | None = None,
) -> None:
    """Run an agent in the background and send the result back via Telegram."""
    try:
        job = await run_agent(
            agent_name=agent_name,
            task=task,
            project_name=project_name,
            cwd=cwd,
            system_prompt=system_prompt,
        )
        if job.output:
            await bot.send_message(job.output)
        elif job.error:
            await bot.send_message(f"Failed: {job.error}")
        else:
            await bot.send_message(f"Done (status: {job.status})")
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
