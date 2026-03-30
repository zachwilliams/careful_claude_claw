"""
Shared contracts for the dashboard package.

All dashboard components (PTYManager, AgentRegistry, TokenTracker, Textual app)
code against the types and Protocols defined here. No implementation lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Shared key
# ---------------------------------------------------------------------------

# SessionID is the stable join key between PTYSession (PTYManager) and
# AgentState (AgentRegistry). Both sides create/look up records using this
# value. The caller that spawns a PTY process is responsible for generating
# the ID before calling PTYManager.spawn() and AgentRegistry.register(),
# so both sides are always seeded with the same value.
#
# Use: uuid4().hex or a human-readable slug like "orchestrator-<timestamp>".
#
# Sub-agents (AgentKind.SUB_AGENT) are detected via psutil, not spawned.
# Their SessionID is derived internally by AgentRegistry.poll_subagents()
# using the convention "sub-<pid>". They have NO corresponding PTYSession —
# PTYManager.get_session() will return None for a sub-agent SessionID.
SessionID = str


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentStatus(str, Enum):
    """Lifecycle status of an agent process."""

    ACTIVE = "active"       # just spawned, no output yet
    WORKING = "working"     # output received in the last 10s
    IDLE = "idle"           # no output for >10s, process still alive
    DONE = "done"           # process exited cleanly
    ERROR = "error"         # process exited with non-zero status


class AgentKind(str, Enum):
    """
    Execution origin and attach capability of an agent.

    Two-tier model:
      - Interactive tier (TOP_LEVEL): agents spawned directly by the dashboard via
        PTYManager. Have a PTYSession with a PTY fd. Fully attachable. Security
        policy resolver MUST be called before PTYManager.spawn().
      - Automated tier (EXTERNAL): agents spawned outside the dashboard by the
        scheduler, Telegram, or Slack via the Claude Code Agent SDK. No PTYSession.
        Monitoring only (status, tokens, sub-agent tree). Not attachable.
      - SUB_AGENT: child claude processes detected under either tier via psutil.
        Read-only. No PTYSession. Nested under their parent in the roster.
    """

    TOP_LEVEL = "top_level"   # spawned by dashboard PTYManager; has PTYSession; attachable
    EXTERNAL = "external"     # spawned by scheduler/Telegram/Slack via SDK; no PTYSession; read-only
    SUB_AGENT = "sub_agent"   # psutil-detected child process; no PTYSession; read-only


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


@dataclass
class PTYSession:
    """A running PTY subprocess."""

    session_id: SessionID       # join key — matches AgentState.session_id
    pid: int
    fd: int                     # PTY master file descriptor
    pty_name: str               # e.g. /dev/pts/3 — used for crash recovery
    cmd: list[str]
    started_at: datetime
    output_buffer: bytearray = field(default_factory=bytearray)  # rolling tail buffer
    output_buffer_max: int = 500_000                              # ~500 KB cap

    # Ring-buffer semantics: when len(output_buffer) >= output_buffer_max,
    # PTYManager trims from the front (oldest bytes) before appending new data,
    # keeping the buffer at output_buffer_max bytes. The Terminal widget must
    # treat the buffer as a tail snapshot — not a complete history.


@dataclass
class AgentState:
    """Observable state of one agent (top-level or sub-agent)."""

    session_id: SessionID       # join key — matches PTYSession.session_id for TOP_LEVEL
    kind: AgentKind
    pid: int
    name: str
    task: str
    status: AgentStatus
    started_at: datetime
    last_output_at: datetime | None

    # Monitoring data
    input_tokens: int = 0
    output_tokens: int = 0
    # context_limit is None until parsed from the session start event.
    # context_pct returns 0.0 while None — widgets should render "?" for the
    # denominator rather than showing a percentage against a stale default.
    context_limit: int | None = None
    skill: str | None = None
    mcps: list[str] = field(default_factory=list)

    # Hierarchy
    parent_session_id: SessionID | None = None    # None = top-level
    children: list[SessionID] = field(default_factory=list)

    # Execution linkage (matches existing SQLite active_agents row, if any).
    # Set when an agent was spawned via the existing SDK path (claw start /
    # scheduler / Telegram) and its active_agents row was reconciled on startup.
    # None for agents spawned directly by the dashboard.
    execution_id: int | None = None
    job_name: str | None = None

    @property
    def used_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def context_pct(self) -> float:
        if self.context_limit is None or self.context_limit == 0:
            return 0.0
        return min(self.used_tokens / self.context_limit, 1.0)


@dataclass
class TokenEvent:
    """A single usage observation parsed from PTY output."""

    session_id: SessionID
    input_tokens: int
    output_tokens: int
    recorded_at: datetime


@dataclass
class TokenSummary:
    """Aggregate token usage for the status bar."""

    total_input: int
    total_output: int
    window_hours: float     # rolling window size used for this summary
    recorded_at: datetime

    @property
    def total(self) -> int:
        return self.total_input + self.total_output


# ---------------------------------------------------------------------------
# Protocols — implemented by PTYManager, AgentRegistry, TokenTracker
# ---------------------------------------------------------------------------


@runtime_checkable
class PTYManagerProtocol(Protocol):
    """
    Manages PTY subprocess lifecycle.

    Lifecycle: call start() before any other method. Call close() on shutdown
    (or use as an async context manager). PTYManager owns background asyncio
    tasks that read from each PTY fd; these tasks are started by start() and
    cancelled by close().

    Fan-out contract: PTYManager owns reading raw bytes from each PTY fd.
    Consumers (Terminal widget, TokenTracker, future: audit logger, alerter)
    subscribe via output_stream(). The app layer is responsible for routing
    each chunk to all subscribers — PTYManager has no knowledge of consumers.

    Full-screen attach: NOT a PTYManager responsibility. The app layer handles
    this via Textual's app.suspend() context manager, using get_session() to
    retrieve the PTY fd for direct terminal handoff.
    """

    async def start(self) -> None:
        """
        Start background read tasks. Must be called before spawn() or output_stream().
        Safe to call from Textual's on_mount().
        """
        ...

    async def close(self) -> None:
        """
        Cancel all background read tasks and close open PTY fds.
        Call from Textual's on_unmount() or app teardown.
        """
        ...

    async def __aenter__(self) -> PTYManagerProtocol:
        ...

    async def __aexit__(self, *args: object) -> None:
        ...

    def spawn(self, cmd: list[str], session_id: SessionID, name: str, task: str) -> PTYSession:
        """Spawn a new PTY process. Raises if session_id already exists."""
        ...

    def write(self, session_id: SessionID, data: bytes) -> None:
        """Write bytes to a PTY's stdin. Used for graceful stop messages."""
        ...

    async def output_stream(self, session_id: SessionID) -> AsyncIterator[bytes]:
        """
        Async iterator of raw byte chunks from a PTY's stdout.

        The app layer consumes this and fans out each chunk to:
          - Terminal widget (render)
          - TokenTracker.feed() (parse usage events)
          - Any future consumers (logging, alerting, etc.)

        Terminates when the PTY process exits.
        """
        ...
        yield b""  # satisfy type checker for AsyncGenerator structural subtype

    def resize(self, session_id: SessionID, rows: int, cols: int) -> None:
        """Propagate terminal resize to the PTY process."""
        ...

    def kill(self, session_id: SessionID, *, hard: bool = False) -> None:
        """Terminate a PTY process. hard=False sends SIGTERM, hard=True sends SIGKILL."""
        ...

    def get_session(self, session_id: SessionID) -> PTYSession | None:
        """
        Return the PTYSession for a running session, or None.
        Always returns None for SUB_AGENT session IDs (they have no PTY fd).
        """
        ...

    def list_sessions(self) -> list[PTYSession]:
        """Return all active PTY sessions (top-level only — no sub-agents)."""
        ...


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """In-memory agent state store, backed by SQLite active_agents."""

    def register(self, state: AgentState) -> None:
        """Add or replace an agent's state entry."""
        ...

    def unregister(self, session_id: SessionID) -> None:
        """Remove an agent (e.g. after it exits)."""
        ...

    def get(self, session_id: SessionID) -> AgentState | None:
        """Return state for one agent."""
        ...

    def list_top_level(self) -> list[AgentState]:
        """Return all TOP_LEVEL (dashboard-spawned, attachable) agents, ordered by started_at."""
        ...

    def list_external(self) -> list[AgentState]:
        """Return all EXTERNAL (SDK-spawned, read-only) agents, ordered by started_at."""
        ...

    def list_children(self, parent_session_id: SessionID) -> list[AgentState]:
        """Return sub-agents under a given parent."""
        ...

    def update_status(self, session_id: SessionID, status: AgentStatus) -> None:
        """Update just the status field (called frequently from output polling)."""
        ...

    def update_tokens(self, session_id: SessionID, input_tokens: int, output_tokens: int) -> None:
        """
        Accumulate token counts from a parsed usage event.
        TokenEvent values are per-turn deltas; this method adds to the running total.
        """
        ...

    def update_context_limit(self, session_id: SessionID, limit: int) -> None:
        """Set the model context limit once parsed from session start."""
        ...

    async def poll_subagents(self) -> None:
        """
        Async task: poll psutil process trees for each top-level agent's PID.

        On each poll:
          - Detect new child claude processes → register as SUB_AGENT entries.
            SessionID for detected sub-agents is derived as "sub-<pid>".
          - Detect child processes that have exited → call update_status(DONE/ERROR)
            and unregister() for those entries.

        Should be run as a background asyncio task at ~2s intervals.
        """
        ...

    async def reconcile_with_db(self) -> None:
        """
        Called once on dashboard startup. Reads SQLite active_agents and registers
        any currently-running processes as EXTERNAL agents with execution_id/job_name
        populated. These agents were spawned outside the dashboard (scheduler, Telegram,
        Slack) and have no PTYSession — monitoring only, not attachable.

        Uses psutil to verify the PID is still alive before registering. Skips dead rows.
        """
        ...


@runtime_checkable
class TokenTrackerProtocol(Protocol):
    """Parses PTY output for token usage and maintains a rolling hourly count."""

    def feed(self, session_id: SessionID, data: bytes) -> list[TokenEvent]:
        """
        Feed raw PTY output bytes from one chunk delivered by the app-layer fan-out.
        Returns any TokenEvents parsed from this chunk.

        Implementations must maintain per-session state for incomplete JSON fragments,
        since PTY output arrives in arbitrary byte chunks and a "usage": JSON object
        may be split across multiple feed() calls.

        Does not call AgentRegistry directly — the app layer is responsible for
        forwarding returned events to AgentRegistry.update_tokens().
        """
        ...

    def record(self, event: TokenEvent) -> None:
        """Persist a TokenEvent to SQLite token_events table."""
        ...

    def rolling_summary(self, window_hours: float = 1.0) -> TokenSummary:
        """Return aggregate token usage across all sessions in the last window_hours."""
        ...

    def session_summary(self, session_id: SessionID) -> tuple[int, int]:
        """Return (input_tokens, output_tokens) total for one session."""
        ...
