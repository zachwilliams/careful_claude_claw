"""
DashboardApp — Textual TUI for the CarefulClaudeClaw agent management platform.

Wires PTYManager, AgentRegistry, and TokenTracker into a live terminal UI.

Layout:
  ┌─────────────────┬────────────────────────────────┐
  │  Agent Roster   │  PTY Output (tabbed)           │
  │  (Tree)         │  [Agent A] [Agent B] ...       │
  │                 │                                │
  │  ▶ orchestrator │ (terminal output for selected) │
  │    sub-agent-1  │                                │
  │  ○ ext-agent    │                                │
  ├─────────────────┴────────────────────────────────┤
  │  Detail strip: name, status, pid, context_pct    │
  ├──────────────────────────────────────────────────┤
  │  Footer: tokens (1hr): 45,231  ~estimate         │
  └──────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import termios
import tty
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, RichLog, Static, Tree

from careful_claude_claw.db import init_db
from careful_claude_claw.paths import CLAW_DIR, ORCHESTRATOR_MIAO_DIR

from .agent_registry import AgentRegistry
from .interfaces import (
    AgentKind,
    AgentRegistryProtocol,
    AgentState,
    AgentStatus,
    PTYManagerProtocol,
    SessionID,
    TokenTrackerProtocol,
)
from .pty_manager import PTYManager
from .token_tracker import TokenTracker

# ---------------------------------------------------------------------------
# Which-key overlay screen
# ---------------------------------------------------------------------------

LEADER_BINDINGS: list[tuple[str, str]] = [
    ("k", "kill (SIGTERM)"),
    ("K", "hard kill (SIGKILL)"),
    ("f", "full-screen attach"),
    ("n", "new agent"),
    ("q", "quit dashboard"),
    ("r", "refresh roster"),
]


class WhichKeyScreen(ModalScreen[str | None]):
    """Modal overlay that shows available leader-key bindings and waits for input."""

    CSS = """
    WhichKeyScreen {
        align: center middle;
    }
    #which-key-panel {
        width: 40;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    #which-key-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .which-key-row {
        margin-bottom: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="which-key-panel"):
            yield Label("Space + ...", id="which-key-title")
            for key, desc in LEADER_BINDINGS:
                yield Label(f"  [{key}]  {desc}", classes="which-key-row")

    def on_key(self, event: object) -> None:
        """Capture any key press and return it."""
        from textual.events import Key  # local import to avoid circular

        if isinstance(event, Key):
            event.stop()
            if event.key == "escape":
                self.dismiss(None)
            else:
                self.dismiss(event.character or event.key)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# New-agent dialog
# ---------------------------------------------------------------------------


class NewAgentScreen(ModalScreen[str | None]):
    """Simple modal to ask for a command when spawning a new agent."""

    CSS = """
    NewAgentScreen {
        align: center middle;
    }
    #new-agent-panel {
        width: 60;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    #new-agent-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="new-agent-panel"):
            yield Label("New Agent — enter command:", id="new-agent-title")
            yield Input(placeholder="e.g. claude --dangerously-skip-permissions", id="cmd-input")

    def on_mount(self) -> None:
        self.query_one("#cmd-input", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main dashboard application
# ---------------------------------------------------------------------------

_ORCHESTRATOR_CLAUDE_MD = """\
# Miao — Persistent Orchestrator

You are 小火苗 (Miao), a persistent AI assistant with memory and sub-agent capabilities.

## Session Start
At the start of each session, recall relevant context:
```
memory_search("recent interactions preferences active projects")
```

## Memory
- `memory_write` — store preferences, decisions, observations, procedures
- `memory_search` — recall relevant context before responding
- `memory_update` — revise outdated memories
- `memory_list` — browse all stored memories

Types: `preference` (permanent), `decision` (slow decay), `observation` (weeks), `procedure` (permanent)

## Sub-agents
Spawn sub-agents for coding tasks, file work, or parallelisable jobs:
- `spawn_agent` — dispatch a task
- `list_agents` — check running agents
- `kill_agent` — stop an agent
- `send_to_agent` — send a follow-up

## System
- `list_jobs` — configured scheduled jobs
- `list_skills` — available skills

## Style
Be concise. Spawn sub-agents for substantial coding or file tasks. Ask when scope is unclear.
"""


class DashboardApp(App[None]):
    """
    Textual TUI dashboard for CarefulClaudeClaw.

    Accepts PTYManager, AgentRegistry, and TokenTracker instances at
    construction time so the app can be tested with mock implementations.
    If not provided, real instances are created.
    """

    CSS = """
    DashboardApp {
        layers: base overlay;
    }
    #main-layout {
        height: 1fr;
    }
    #roster-pane {
        width: 30%;
        min-width: 20;
        border-right: solid $accent-darken-2;
    }
    #roster-title {
        background: $accent-darken-2;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    #roster-tree {
        height: 1fr;
        overflow-y: auto;
    }
    #output-pane {
        width: 70%;
    }
    #output-title {
        background: $accent-darken-2;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    #output-log {
        height: 1fr;
        overflow-y: auto;
    }
    #detail-strip {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
    }
    #token-footer {
        height: 1;
        background: $surface-darken-2;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("tab", "cycle_focus", "Switch pane", show=True),
    ]

    def __init__(
        self,
        pty_manager: PTYManagerProtocol | None = None,
        registry: AgentRegistryProtocol | None = None,
        token_tracker: TokenTrackerProtocol | None = None,
        start_scheduler: bool = True,
    ) -> None:
        super().__init__()
        self.pty_manager: PTYManagerProtocol = pty_manager or PTYManager()
        self.registry: AgentRegistryProtocol = registry or AgentRegistry()
        self.token_tracker: TokenTrackerProtocol = token_tracker or TokenTracker()
        self._start_scheduler = start_scheduler
        self._scheduler_task: asyncio.Task[None] | None = None

        # Currently selected agent session ID.
        self._selected_session_id: SessionID | None = None

        # Background streaming task for the current agent output.
        self._stream_task: asyncio.Task[None] | None = None


        # Whether Space (leader) has been pressed.
        self._leader_active: bool = False

        # Map from Tree node id → session_id for lookup on selection.
        self._node_to_session: dict[int, SessionID] = {}

        # Whether the PTY output pane currently has "keyboard passthrough" focus.
        self._pty_focused: bool = False

        # Buffer for detecting Ctrl+B D (detach) sequence in PTY pass-through.
        self._passthrough_ctrl_b_seen: bool = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="main-layout"):
                # Left pane — roster
                with Vertical(id="roster-pane"):
                    yield Static("Agent Roster", id="roster-title")
                    yield Tree("Agents", id="roster-tree")
                # Right pane — output
                with Vertical(id="output-pane"):
                    yield Static("PTY Output", id="output-title")
                    yield RichLog(id="output-log", markup=True, highlight=False, wrap=True)
            yield Label("No agent selected", id="detail-strip")
            yield Label("tokens (1hr): — (loading…)", id="token-footer")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_mount(self) -> None:
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

        init_db()
        await self.pty_manager.start()
        await self.registry.reconcile_with_db()

        try:
            await self._spawn_orchestrator()
        except Exception:
            logging.getLogger(__name__).warning("Orchestrator PTY spawn failed", exc_info=True)

        if self._start_scheduler:
            from careful_claude_claw.scheduler import run_scheduler
            self._scheduler_task = asyncio.get_running_loop().create_task(
                run_scheduler(), name="claw-scheduler"
            )

        self._refresh_roster()
        self.set_interval(2.0, self._poll_subagents)
        self.set_interval(2.0, self._refresh_roster)
        self.set_interval(5.0, self._reconcile_external)
        self.set_interval(30.0, self._refresh_token_bar)
        self._refresh_token_bar()

    async def on_unmount(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await self.pty_manager.close()

    async def _reconcile_external(self) -> None:
        """Periodic task: sync EXTERNAL agents from SQLite active_agents into the registry."""
        await self.registry.reconcile_with_db()
        self._refresh_roster()

    async def _spawn_orchestrator(self) -> None:
        """Spawn the orchestrator_miao session as a TOP_LEVEL PTY agent."""
        import json

        ORCHESTRATOR_MIAO_DIR.mkdir(parents=True, exist_ok=True)

        # Write CLAUDE.md on first run so the orchestrator has its instructions.
        claude_md = ORCHESTRATOR_MIAO_DIR / "CLAUDE.md"
        if not claude_md.exists():
            claude_md.write_text(_ORCHESTRATOR_CLAUDE_MD)

        # Locate the project root (where pyproject.toml lives) for the MCP server cwd.
        import careful_claude_claw
        project_root = Path(careful_claude_claw.__file__).parent.parent
        mcp_config_path = CLAW_DIR / "mcp_orchestrator.json"
        mcp_config = {
            "mcpServers": {
                "claw_orchestrator": {
                    "command": "uv",
                    "args": ["run", "python", "-m", "careful_claude_claw.mcp_server"],
                    "cwd": str(project_root),
                }
            }
        }
        mcp_config_path.write_text(json.dumps(mcp_config, indent=2))

        cmd = [
            "claude",
            "--continue",
            "--mcp-config", str(mcp_config_path),
            "--allowedTools", "Read,Glob,Grep,Bash,WebSearch,WebFetch,mcp__claw_orchestrator__*",
            "--permission-mode", "bypassPermissions",
        ]

        session_id: SessionID = "orchestrator_miao"
        pty_session = self.pty_manager.spawn(
            cmd,
            session_id,
            "Miao (orchestrator)",
            "Persistent orchestrator",
            cwd=str(ORCHESTRATOR_MIAO_DIR),
        )
        self.registry.register(AgentState(
            session_id=session_id,
            kind=AgentKind.TOP_LEVEL,
            pid=pty_session.pid,
            name="Miao (orchestrator)",
            task="Persistent orchestrator",
            status=AgentStatus.ACTIVE,
            started_at=datetime.now(UTC),
            last_output_at=None,
        ))

    # ------------------------------------------------------------------
    # Roster management
    # ------------------------------------------------------------------

    def _refresh_roster(self) -> None:
        """Rebuild the agent roster Tree widget from current registry state."""
        tree = self.query_one("#roster-tree", Tree)

        tree.clear()
        self._node_to_session.clear()

        top_level = self.registry.list_top_level()
        external = self.registry.list_external()

        def _status_label(state: AgentState) -> str:
            return f" [{state.status.value}]"

        # TOP_LEVEL agents — shown as root nodes with ▶ prefix.
        for agent in top_level:
            label = f"▶ {agent.name}{_status_label(agent)}"
            node = tree.root.add(label, expand=True)
            self._node_to_session[id(node)] = agent.session_id

            # SUB_AGENT children nested underneath.
            children = self.registry.list_children(agent.session_id)
            for child in children:
                child_label = f"· {child.name}{_status_label(child)}"
                child_node = node.add_leaf(child_label)
                self._node_to_session[id(child_node)] = child.session_id

        # EXTERNAL agents — shown as root nodes with ○ prefix.
        for agent in external:
            label = f"○ {agent.name}{_status_label(agent)}"
            node = tree.root.add_leaf(label)
            self._node_to_session[id(node)] = agent.session_id

        # Expand root so nodes are visible.
        tree.root.expand()

    async def _poll_subagents(self) -> None:
        """Poll registry for sub-agent changes."""
        await self.registry.poll_subagents()

    # ------------------------------------------------------------------
    # Detail strip
    # ------------------------------------------------------------------

    def _update_detail_strip(self, session_id: SessionID | None) -> None:
        strip = self.query_one("#detail-strip", Label)
        if session_id is None:
            strip.update("No agent selected")
            return
        state = self.registry.get(session_id)
        if state is None:
            strip.update(f"[dim]{session_id} — not found[/dim]")
            return
        pid_str = str(state.pid) if state.pid >= 0 else "?"
        if state.context_limit is None:
            ctx_str = "ctx: ?"
        else:
            pct = state.context_pct * 100
            ctx_str = f"ctx: {pct:.0f}%"
        strip.update(
            f"{state.name}  |  {state.status.value}  |  pid:{pid_str}  |  {ctx_str}"
        )

    # ------------------------------------------------------------------
    # Token footer
    # ------------------------------------------------------------------

    def _refresh_token_bar(self) -> None:
        summary = self.token_tracker.rolling_summary(window_hours=1.0)
        label = self.query_one("#token-footer", Label)
        label.update(
            f"tokens (1hr): {summary.total:,}  ~estimate  "
            f"[dim]claude.ai/settings[/dim]"
        )

    # ------------------------------------------------------------------
    # Agent selection & output streaming
    # ------------------------------------------------------------------

    def _select_agent(self, session_id: SessionID) -> None:
        """Select an agent and start streaming its output."""
        self._selected_session_id = session_id
        self._update_detail_strip(session_id)
        self._start_output_stream(session_id)

    def _start_output_stream(self, session_id: SessionID) -> None:
        """Cancel any existing stream task and start a new one for session_id."""
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            self._stream_task = None

        log = self.query_one("#output-log", RichLog)
        log.clear()

        state = self.registry.get(session_id)
        if state is None:
            log.write("[dim]Agent not found.[/dim]")
            return

        if state.kind != AgentKind.TOP_LEVEL:
            log.write("[dim]No PTY output (externally managed agent)[/dim]")
            return

        # Replay the ring-buffer snapshot first.
        session = self.pty_manager.get_session(session_id)
        if session is None:
            log.write("[dim]No PTY session found for this agent.[/dim]")
            return

        buf_snapshot = bytes(session.output_buffer)
        if buf_snapshot:
            log.write(Text.from_ansi(buf_snapshot.decode("utf-8", errors="replace")))

        # Start background streaming task.
        self._stream_task = asyncio.get_running_loop().create_task(
            self._stream_output(session_id),
            name=f"stream-{session_id}",
        )

    async def _stream_output(self, session_id: SessionID) -> None:
        """Background task: stream PTY output to RichLog + TokenTracker."""
        log = self.query_one("#output-log", RichLog)
        try:
            async for chunk in self.pty_manager.output_stream(session_id):
                # Feed to token tracker first.
                events = self.token_tracker.feed(session_id, chunk)
                for event in events:
                    self.token_tracker.record(event)
                    self.registry.update_tokens(
                        event.session_id, event.input_tokens, event.output_tokens
                    )

                # Decode ANSI and write to RichLog.
                log.write(Text.from_ansi(chunk.decode("utf-8", errors="replace")))

                # Update agent status to WORKING on output.
                self.registry.update_status(session_id, AgentStatus.WORKING)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write(f"[red]Stream error: {exc}[/red]")

    # ------------------------------------------------------------------
    # Tree selection events
    # ------------------------------------------------------------------

    @on(Tree.NodeSelected)
    def _on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        session_id = self._node_to_session.get(id(node))
        if session_id is not None:
            self._select_agent(session_id)

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def on_key(self, event: object) -> None:
        from textual.events import Key  # local import

        if not isinstance(event, Key):
            return

        # When PTY output pane is in pass-through mode, route keys to the PTY.
        if self._pty_focused:
            self._handle_pty_passthrough(event)
            return

        key = event.key

        # j/k navigation in the roster.
        if key in ("j", "k") and not self._leader_active:
            tree = self.query_one("#roster-tree", Tree)
            if tree.has_focus:
                if key == "j":
                    tree.action_cursor_down()
                else:
                    tree.action_cursor_up()
                event.stop()
                return

        # Enter to select the highlighted tree node.
        if key == "enter" and not self._leader_active:
            # Tree widget handles enter natively via NodeSelected; nothing extra needed.
            return

        # Tab to toggle focus between panes.
        if key == "tab" and not self._leader_active:
            self.action_cycle_focus()
            event.stop()
            return

        # Space — activate leader mode.
        if key == "space" and not self._leader_active:
            tree = self.query_one("#roster-tree", Tree)
            if tree.has_focus:
                self._leader_active = True
                event.stop()
                self._show_which_key()
                return

        # Leader mode: if a key arrives before the which-key screen handles it.
        if self._leader_active:
            self._leader_active = False
            event.stop()

    def _show_which_key(self) -> None:
        """Push the which-key overlay screen."""

        async def _on_dismiss(result: str | None) -> None:
            self._leader_active = False
            if result is not None:
                await self._execute_leader_action(result)

        self.push_screen(WhichKeyScreen(), _on_dismiss)

    async def _execute_leader_action(self, key: str) -> None:
        """Execute the leader-bound action for the given key."""
        if key == "k":
            await self._kill_selected(hard=False)
        elif key == "K":
            await self._kill_selected(hard=True)
        elif key == "f":
            await self._fullscreen_attach()
        elif key == "n":
            await self._spawn_new_agent()
        elif key == "q":
            self.exit()
        elif key == "r":
            self._refresh_roster()

    # ------------------------------------------------------------------
    # Focus cycling
    # ------------------------------------------------------------------

    def action_cycle_focus(self) -> None:
        tree = self.query_one("#roster-tree", Tree)
        log = self.query_one("#output-log", RichLog)
        if tree.has_focus:
            log.focus()
            self._pty_focused = True
        else:
            tree.focus()
            self._pty_focused = False

    # ------------------------------------------------------------------
    # Kill actions
    # ------------------------------------------------------------------

    async def _kill_selected(self, *, hard: bool) -> None:
        session_id = self._selected_session_id
        if session_id is None:
            self.notify("No agent selected.", severity="warning")
            return
        state = self.registry.get(session_id)
        if state is None or state.kind != AgentKind.TOP_LEVEL:
            self.notify("Cannot kill: agent not spawned by dashboard.", severity="warning")
            return
        self.pty_manager.kill(session_id, hard=hard)
        sig_name = "SIGKILL" if hard else "SIGTERM"
        self.notify(f"Sent {sig_name} to {state.name}.")
        self.registry.update_status(session_id, AgentStatus.DONE)
        self._refresh_roster()

    # ------------------------------------------------------------------
    # Full-screen attach (Space f)
    # ------------------------------------------------------------------

    async def _fullscreen_attach(self) -> None:
        session_id = self._selected_session_id
        if session_id is None:
            self.notify("No agent selected.", severity="warning")
            return
        state = self.registry.get(session_id)
        if state is None or state.kind != AgentKind.TOP_LEVEL:
            self.notify("Cannot attach: agent was not spawned by dashboard.", severity="warning")
            return
        pty_session = self.pty_manager.get_session(session_id)
        if pty_session is None:
            self.notify("No PTY session found.", severity="warning")
            return

        async with self.suspend():
            await self._run_fullscreen_passthrough(pty_session.fd)

    async def _run_fullscreen_passthrough(self, pty_fd: int) -> None:
        """
        Raw terminal passthrough loop.

        Reads from PTY fd → writes to stdout.
        Reads from stdin → writes to PTY fd.
        Exits when Ctrl+B D is detected or PTY process closes.
        """
        loop = asyncio.get_running_loop()
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        # Save terminal settings so we can restore on exit.
        old_tty_settings = termios.tcgetattr(stdin_fd)

        try:
            tty.setraw(stdin_fd)

            done = asyncio.Event()
            ctrl_b_seen = False

            def _read_pty() -> None:
                nonlocal ctrl_b_seen
                try:
                    data = os.read(pty_fd, 4096)
                except OSError:
                    done.set()
                    return
                if not data:
                    done.set()
                    return
                os.write(stdout_fd, data)

            def _read_stdin() -> None:
                nonlocal ctrl_b_seen
                try:
                    data = os.read(stdin_fd, 1024)
                except OSError:
                    done.set()
                    return
                if not data:
                    done.set()
                    return

                # Detect Ctrl+B D detach sequence (\x02\x64).
                for _i, byte in enumerate(data):
                    if ctrl_b_seen and byte == 0x64:  # 'd'
                        done.set()
                        return
                    ctrl_b_seen = byte == 0x02  # Ctrl+B

                try:
                    os.write(pty_fd, data)
                except OSError:
                    done.set()

            loop.add_reader(pty_fd, _read_pty)
            loop.add_reader(stdin_fd, _read_stdin)

            await done.wait()

        finally:
            loop.remove_reader(pty_fd)
            loop.remove_reader(stdin_fd)
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty_settings)

    # ------------------------------------------------------------------
    # PTY keyboard passthrough (when output pane is focused)
    # ------------------------------------------------------------------

    def _handle_pty_passthrough(self, event: object) -> None:
        from textual.events import Key

        if not isinstance(event, Key):
            return

        # Ctrl+B D — detach from PTY pass-through, return focus to roster.
        if event.key == "ctrl+b":
            self._passthrough_ctrl_b_seen = True
            event.stop()
            return

        if self._passthrough_ctrl_b_seen:
            self._passthrough_ctrl_b_seen = False
            if event.character == "d" or event.key == "d":
                # Detach — return focus to roster.
                self._pty_focused = False
                self.query_one("#roster-tree", Tree).focus()
                event.stop()
                return

        self._passthrough_ctrl_b_seen = False

        # Forward keypress to PTY.
        session_id = self._selected_session_id
        if session_id is None:
            return
        state = self.registry.get(session_id)
        if state is None or state.kind != AgentKind.TOP_LEVEL:
            return
        char = event.character
        if char is not None:
            try:
                self.pty_manager.write(session_id, char.encode())
            except (KeyError, OSError):
                pass
        event.stop()

    # ------------------------------------------------------------------
    # Spawn new agent dialog (Space n)
    # ------------------------------------------------------------------

    async def _spawn_new_agent(self) -> None:
        async def _on_cmd(cmd_str: str | None) -> None:
            if not cmd_str:
                return
            await self._do_spawn(cmd_str)

        self.push_screen(NewAgentScreen(), _on_cmd)

    async def _do_spawn(self, cmd_str: str) -> None:
        """Spawn a new PTY agent from a command string."""
        session_id: SessionID = f"session-{uuid4().hex[:8]}"

        # Parse the command string into a list.
        import shlex

        try:
            cmd = shlex.split(cmd_str)
        except ValueError:
            cmd = cmd_str.split()

        if not cmd:
            self.notify("Empty command — nothing to spawn.", severity="warning")
            return

        # Extract a friendly name from the first token.
        name = cmd[0].split("/")[-1]  # basename of executable
        task = cmd_str

        try:
            self.pty_manager.spawn(cmd, session_id, name, task)
        except Exception as exc:
            self.notify(f"Failed to spawn: {exc}", severity="error")
            return

        state = AgentState(
            session_id=session_id,
            kind=AgentKind.TOP_LEVEL,
            pid=self.pty_manager.get_session(session_id).pid,  # type: ignore[union-attr]
            name=name,
            task=task,
            status=AgentStatus.ACTIVE,
            started_at=datetime.now(UTC),
            last_output_at=None,
        )
        self.registry.register(state)
        self._refresh_roster()
        self._select_agent(session_id)
        self.notify(f"Spawned agent '{name}' ({session_id}).")
