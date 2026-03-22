"""Abstract bot interface and shared command router.

Provides a platform-agnostic BotClient ABC and CommandRouter that works
with any messaging platform (Telegram, Slack, etc.).
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

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
from .db import list_executions, list_jobs
from .skills import discover_skills, get_skill

logger = logging.getLogger(__name__)

CONVERSATIONAL_SYSTEM_PROMPT = (
    "You are littleflame, a personal AI assistant. Respond concisely."
)
CONVERSATIONAL_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "WebSearch",
    "WebFetch",
    "mcp__*",
]


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split a message into chunks of at most max_len characters."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


class BotClient(ABC):
    """Abstract base class for messaging platform clients."""

    @property
    @abstractmethod
    def platform(self) -> str:
        """Platform identifier, e.g. 'telegram' or 'slack'."""

    @abstractmethod
    async def send_message(self, text: str) -> None:
        """Send a message to the user."""

    @abstractmethod
    async def download_file(self, file_id: str, destination: Path) -> Path:
        """Download a file attachment to a local path."""

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""

    def get_reply_fn(self) -> Callable[[str], Awaitable[None]]:
        """Return a send function bound to the current reply context.

        Override in platform clients that have per-message reply context
        (e.g. Slack channel ID) to snapshot it at spawn time so background
        agent callbacks reply to the right place.
        """
        return self.send_message


class CommandRouter:
    """Routes incoming bot messages to handlers.

    Works with any BotClient implementation.
    """

    def __init__(self, bot: BotClient) -> None:
        self.bot = bot
        self._pending_file: dict | None = None
        self._commands: dict[str, object] = {
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
        # Strip bot mention suffix (e.g. /status@littleflame)
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
            await self._spawn_agent(text)

    async def _handle_help(self) -> None:
        lines = [
            "*littleflame Commands*",
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
        reply_fn = self.bot.get_reply_fn()

        async def on_message(msg: str) -> None:
            await reply_fn(msg)

        bg_task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=task,
                on_message=on_message,
                agent_name=f"{self.bot.platform}-{name}",
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
        reply_fn = self.bot.get_reply_fn()

        async def on_message(msg: str) -> None:
            await reply_fn(msg)

        task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=text,
                on_message=on_message,
                agent_name=f"{self.bot.platform}-{name}",
                system_prompt=CONVERSATIONAL_SYSTEM_PROMPT,
                allowed_tools=CONVERSATIONAL_TOOLS,
            )
        )
        session = get_session(name)
        if session:
            session.task = task

    async def handle_file_message(
        self,
        file_id: str,
        file_name: str,
        media_type: str,
        caption: str,
    ) -> None:
        """Route a file attachment to the appropriate agent."""
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
            dest_dir = session.cwd or Path.cwd()
            dest = dest_dir / file_name
            # Deduplicate filename
            counter = 1
            while dest.exists():
                stem = Path(file_name).stem
                suffix = Path(file_name).suffix
                dest = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            await self.bot.download_file(file_id, dest)

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
        reply_fn = self.bot.get_reply_fn()

        async def on_message(msg: str) -> None:
            await reply_fn(msg)

        bg_task = asyncio.create_task(
            run_interactive_agent(
                name=name,
                task=task_text,
                on_message=on_message,
                agent_name=f"{self.bot.platform}-{name}",
                system_prompt=CONVERSATIONAL_SYSTEM_PROMPT,
                allowed_tools=CONVERSATIONAL_TOOLS,
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
