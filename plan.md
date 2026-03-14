# CarefulClaudeClaw Orchestrator — Project Plan

## Overview

A security-first, thin Python orchestrator that uses the **Claude Code Agent SDK** to spawn Claude Code as the execution engine for autonomous, scheduled, and event-driven AI workflows. Inspired by the [ClaudeClaw bridge pattern](https://promptadvisers.com) — keep the orchestration layer minimal and let Claude Code do what it already does well.

### Design Principles

- **Don't rebuild what Claude Code already provides.** Skills, MCP servers, CLAUDE.md, file access, permissions, and now `/loop` + cron scheduling — these are built in. We bridge to them, not replicate them.
- **Single source of truth for security.** One `security.yaml` governs all permission layers.
- **File-based configuration, SQLite for operational data.** Human-readable configs in git, queryable operational state in SQLite.
- **Thin orchestration.** The Python layer handles durable cross-session scheduling, event routing, security compilation, and retry logic — nothing more.
- **Delegate intra-session work to native features.** Short-lived polling, sub-task loops, and reminders within a single agent execution should use Claude Code's native `/loop` and cron tools rather than custom Python logic.

---

## Architecture

### Components

```
┌──────────────────────────────────────────────────┐
│              Python Orchestrator                 │
│  ┌───────────┐  ┌──────────┐ ┌────────────────┐  │
│  │ Event     │  │ Cron     │ │ Webhook        │  │
│  │ Queue     │  │ Scheduler│ │ Listener       │  │
│  └─────┬─────┘  └────┬─────┘ └───────┬────────┘  │
│        └─────────────┼───────────────┘           │
│                      ▼                           │
│         ┌────────────────────────┐               │
│         │ Security Config        │               │
│         │ Compiler               │               │
│         │ (security.yaml →       │               │
│         │  .claude/settings.json │               │
│         │  .mcp.json             │               │
│         │  policy instructions)  │               │
│         └───────────┬────────────┘               │
│                     ▼                            │
│         ┌────────────────────────┐               │
│         │ Agent SDK              │               │
│         │ (spawns claude CLI)    │               │
│         └───────────┬────────────┘               │
│                     ▼                            │
│         ┌────────────────────────┐               │
│         │ Retry & Lifecycle      │               │
│         │ Manager                │               │
│         └────────────────────────┘               │
└──────────────────────────────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
    ┌──────────┐ ┌─────────┐ ┌─────────┐
    │ SQLite   │ │ Git     │ │ Claude  │
    │ (ops DB) │ │ (state) │ │ Code    │
    └──────────┘ └─────────┘ └─────────┘
```

### Python Orchestrator

The orchestrator is the only custom code. It handles:

- **Event queue** — Simple Python `queue.Queue` or file-based queue (`pending/`, `processing/`, `completed/`). No external message broker.
- **Cron scheduler** — Python cron library (e.g., `APScheduler` or `schedule`). Fires events into the queue.
- **Webhook listener** — Minimal HTTP server (FastAPI or Flask) that receives external triggers and enqueues events.
- **Security config compiler** — Reads `security.yaml` and generates per-invocation Claude Code settings before each agent spawn.
- **Retry & lifecycle manager** — Handles agent failures with simple backoff retry.

### Agent Execution via Agent SDK

When the orchestrator needs to execute a task:

1. Read task definition and relevant config
2. Compile security permissions from `security.yaml` into Claude Code settings
3. Spawn Claude Code via the Agent SDK (official `claude` CLI subprocess)
4. Claude Code executes using its built-in capabilities (skills, MCP servers, CLAUDE.md, file system, web search)
5. Orchestrator captures results and updates operational state

This follows the ClaudeClaw pattern: the Agent SDK spawns the official Claude CLI, the OAuth token stays in `~/.claude/`, and we remain fully compliant with Anthropic's Terms of Service.

---

## Data Layer

### SQLite (Operational Data)

SQLite is the operational database. Zero dependencies, single file, Python built-in support.

| Table | Purpose |
|---|---|
| `events` | Event log — what happened, when, triggered by what |
| `jobs` | Job history — agent, start time, end time, status, attempt count |
| `memory` | FTS5-indexed memory with semantic and episodic sectors (see Memory section) |
| `sessions` | Claude Code session IDs for conversation resumption |
| `active_agents` | Currently running agents and their directives |

### Git Repository (Configuration & Business State)

All configuration and business state lives in a git repo. This provides audit trail, diffing, rollback, and human readability.

```
repo/
├── security.yaml              # Single source of truth for all permissions
├── agents/
│   ├── invoice-processor/
│   │   ├── config.md          # Agent-specific configuration
│   │   └── instructions.md    # Agent behavioral instructions
│   └── weekly-reporter/
│       ├── config.md
│       └── instructions.md
├── global-state/              # Business state that agents produce/consume
│   ├── clients/
│   ├── reports/
│   └── ...
└── CLAUDE.md                  # Shared Claude Code instructions
```

### Global State Merge Policy

Global state updates follow a git-based workflow. The merge policy is configurable per path in `security.yaml`:

- **Auto-merge paths** (e.g., `events/*`, `reports/*`) — Low-risk state that deterministic Python code merges directly to main.
- **Require-review paths** (e.g., `clients/*`, `security.yaml`) — Changes are committed to a branch; a human reviews and approves before merge.

All merge logic for global state is implemented in **deterministic Python code**, not LLM-driven. This is the most security-sensitive operation in the system.

---

## Security

### Single Configuration File

All security policy is defined in one file: `security.yaml`. The orchestrator compiles this into the three layers Claude Code expects before each invocation.

```yaml
# security.yaml — single source of truth

global:
  retry:
    max_attempts: 2
    backoff_seconds: 30
    on_final_failure: alert    # alert | ignore | queue_for_review

  global_state:
    auto_merge:
      - events/*
      - reports/*
    require_review:
      - clients/*
      - security.yaml
      - agents/*/config.md

agents:
  invoice-processor:
    # Layer 2: Which MCP servers are available at all
    mcp_servers:
      - stripe
      - notion

    # Layer 1 & 3: What actions are permitted
    auto_approve:
      - stripe:read_invoice
      - stripe:list_invoices
      - notion:query_database
    require_confirmation:
      - notion:update_page
      - stripe:create_refund
    hard_deny:
      - shell:*
      - file:write_outside_workspace

    # What state this agent can access
    state_access:
      - clients/*.md
      - invoices/*.md

  weekly-reporter:
    mcp_servers:
      - notion
    auto_approve:
      - notion:query_database
    hard_deny:
      - notion:update_page
      - shell:*
    state_access:
      - reports/*.md
```

### Three Security Layers

The orchestrator compiles `security.yaml` into three concentric security rings:

| Layer | Mechanism | What It Controls | Generated As |
|---|---|---|---|
| **1. Claude Code native permissions** | Hard technical boundary | Which tools can execute at all | `.claude/settings.json` |
| **2. MCP server scoping** | Trust boundary | Which external services exist | `.mcp.json` |
| **3. Policy instructions** | Behavioral boundary | When to ask, when to act autonomously | Prepended instructions to agent |

The agent never sees or touches `security.yaml`. It only sees the derived permissions compiled for its specific invocation.

---

## Native Claude Code Scheduling (v2.1.71+)

Claude Code now includes built-in scheduling features. Understanding their capabilities and limitations is critical for avoiding unnecessary custom work.

### `/loop` Command

Runs a prompt or slash command on a recurring interval within an active session.

```
/loop 5m check the deploy              # leading interval
/loop check the build every 2 hours    # trailing "every" clause
/loop 20m /review-pr 1234              # loop a slash command
/loop check the build                  # defaults to every 10 minutes
```

Supports `s` (seconds, rounded up to minutes), `m` (minutes), `h` (hours), `d` (days).

### Cron Tools (CronCreate, CronDelete, CronList)

Low-level tools Claude uses internally. Full 5-field cron expressions (`MINUTE HOUR DAY-OF-MONTH MONTH DAY-OF-WEEK`). Also supports one-shot reminders ("remind me at 3pm to push").

### Limitations (Why We Still Need the Python Orchestrator)

| Constraint | Impact |
|---|---|
| **Session-scoped only** | Tasks die when you exit the terminal |
| **3-day auto-expiry** | Recurring tasks auto-delete after 3 days |
| **No persistence across restarts** | Nothing survives a process exit |
| **Requires active, idle session** | Tasks only fire when Claude is idle between turns |
| **Max 50 tasks per session** | Hard cap on concurrent scheduled items |
| **Not exposed in Agent SDK** | Can't programmatically create loops from Python |

### Division of Responsibility

| Responsibility | Owner | Why |
|---|---|---|
| Durable cross-session scheduling | Python orchestrator (APScheduler) | Must survive restarts, no expiry limits |
| Intra-session polling / sub-loops | Claude Code native `/loop` | Already built, no Python code needed |
| External trigger handling (webhooks) | Python orchestrator (FastAPI) | Needs HTTP server, not available natively |
| One-shot reminders during a task | Claude Code native cron | Natural language, zero config |
| Security compilation per invocation | Python orchestrator | Requires pre-spawn config generation |
| Job history and audit trail | Python orchestrator (SQLite + git) | Needs durable storage |

---

## Memory System

Adapted from the ClaudeClaw three-layer memory pattern, using SQLite + FTS5.

### Layer 1: Session Resumption

Claude Code session IDs are stored in SQLite. When an agent is invoked for a recurring task, the orchestrator can resume an existing session to maintain conversational context.

### Layer 2: SQLite + FTS5 Memory

Two memory sectors:

- **Semantic** — Long-term facts, preferences, and stable knowledge. Slow decay (~2%/day without access). Trigger words: "my", "I am", "prefer", "remember", "always", "never".
- **Episodic** — Conversations, events, transient context. Fast decay. Fades naturally over time.

Salience model: starts at 1.0, +0.1 on access (capped at 5.0), -2%/day decay, auto-delete below 0.1.

FTS5 provides full-text search built into SQLite. Zero cost, zero latency, no external vector database required.

### Layer 3: Context Injection

Before every agent invocation, the orchestrator:

1. Runs FTS5 search against the incoming task → top 3 relevant memories
2. Fetches 5 most recent memories
3. Deduplicates
4. Prepends as a `[Memory context]` block in the agent's instructions

---

## Retry Logic

Agent invocations are treated as opaque external calls with simple backoff retry.

```
Event arrives → Orchestrator dequeues

  Attempt 1:
    Compile security settings
    Spawn Claude Code via Agent SDK
    ├─ Success → Log to SQLite, proceed to state merge
    └─ Failure → Archive outputs to history in SQLite

  Wait (backoff_seconds from config, default 30s)

  Attempt 2:
    Fresh invocation (no carryover from attempt 1)
    ├─ Success → Log to SQLite, proceed to state merge
    └─ Failure → Mark event as failed, trigger on_final_failure action

  Done — move to next event
```

Each retry is a completely fresh invocation. The agent does not know it is a retry unless explicitly told via context.

---

## Observability

### SQLite Dashboard

The SQLite operational database supports queries for a lightweight dashboard:

- **Currently running** — Query `active_agents` table
- **Recent history** — Query `jobs` table for status, duration, retry count
- **Failure analysis** — Aggregate failures by agent, time period, error type
- **Execution stats** — Average duration, success rate per agent

### Git Audit Trail

The git repository provides complete audit trail for all configuration and business state changes. Every change has a timestamp, author (which agent or human), and diff.

### History Archival

When an agent completes, the orchestrator archives a subset of outputs (event log, key results) to the `jobs` table in SQLite and to the git `history/` path if relevant. Temporary working data is discarded.

---

## Use Cases

See `draft_usecases.md` for the full list. Key themes that drive infrastructure decisions:

1. **Daily briefing** — scheduled agent runs that check email, Slack, calendar, to-dos
2. **Project management** — projects as first-class entities that own agents, to-dos, plans, and status
3. **Agent monitoring** — know what's running, on which project, for how long
4. **Skill management** — directory of global and per-project skills, queryable summaries
5. **Communications management** — catch up on email/Slack, draft replies with human confirmation
6. **Relationship management** — persistent contact database with interaction history
7. **Coding workflows** — consistent git/PR patterns across all projects

Skills for individual use cases are developed independently. The orchestrator provides the infrastructure they run on.

---

## Prototyping Roadmap

### Phase 1: Minimal Loop ✅ COMPLETE

- [x] Python script with a single hardcoded cron job
- [x] Spawns Claude Code via Agent SDK
- [x] Agent does one useful task (e.g., list MCP connections)
- [x] Logs result to SQLite
- [x] Basic retry logic with backoff
- [x] CLI: `claw run` and `claw jobs`
- [x] Tests for models and db
- **Goal:** End-to-end execution works

**Implemented in:** `models.py` (Job, JobStatus), `db.py` (jobs table CRUD), `agent.py` (run_agent with retry), `cli.py` (run/jobs commands)

---

### Phase 2: Project Model & Skill Registry

**Goal:** Projects and skills become first-class entities. The orchestrator knows what projects exist, what skills are available, and can run skills against projects.

#### 2.1 Project model

- [ ] Add to `careful_claude_claw/models.py`:
  - `ProjectStatus` enum: ACTIVE, PAUSED, ARCHIVED
  - `Project` pydantic model: name, path, status, description, created_at
- [ ] Add to `careful_claude_claw/db.py`: `projects` table + insert_project, update_project, list_projects, get_project

#### 2.2 Skill registry

- [ ] Add to `careful_claude_claw/models.py`:
  - `SkillScope` enum: GLOBAL, PROJECT
  - `Skill` pydantic model: name, scope, project_name (optional), description, path
- [ ] Implement `careful_claude_claw/skills.py`:
  - `discover_skills()` — scan global skills dir + per-project skills dirs
  - `list_skills(project_name=None)` — list all or filter by project
  - `get_skill(name)` — return skill metadata
- [ ] Convention: skills live as `.md` files in `skills/` (global) or `<project>/skills/` (project-scoped)

#### 2.3 CLI updates

- [ ] `claw projects` — list registered projects with status
- [ ] `claw project add <name> <path>` — register a project
- [ ] `claw skills` — list available skills (global + per-project)
- [ ] `claw run --skill <name> --project <name>` — run a skill in a project context

#### 2.4 Tests

- [ ] `tests/test_projects.py` — project CRUD, status transitions
- [ ] `tests/test_skills.py` — skill discovery, filtering, global vs project scope

**Files:**
- Edit: `careful_claude_claw/models.py`, `careful_claude_claw/db.py`, `careful_claude_claw/cli.py`
- New: `careful_claude_claw/skills.py`
- New: `tests/test_projects.py`, `tests/test_skills.py`

---

### Phase 3: Scheduler

**Goal:** Cron-based scheduling that fires agent runs on a schedule. Lightweight — no event queue, no webhooks. Just "run this skill/task at this time."

> **Simplification note (v2.1.71):** Claude Code has native `/loop` and cron tools for intra-session polling. This phase handles only **durable, cross-session** scheduling that survives restarts. Intra-session loops are delegated to agents natively.

#### 3.1 Schedule model

- [ ] Add to `careful_claude_claw/models.py`:
  - `Schedule` pydantic model: name, cron_expr, task (or skill name), project_name (optional), enabled
- [ ] Add to `careful_claude_claw/db.py`: `schedules` table + CRUD functions

#### 3.2 Scheduler daemon

- [ ] Implement `careful_claude_claw/scheduler.py`
  - APScheduler `AsyncIOScheduler` wrapper
  - `load_schedules()` — read from SQLite, register with APScheduler
  - Each trigger calls `run_agent()` directly (no event queue layer)
  - `start_scheduler()` / `stop_scheduler()`

#### 3.3 CLI updates

- [ ] `claw schedule add <name> <cron_expr> --task "..." --skill <name> --project <name>`
- [ ] `claw schedule list` — show all schedules with next fire time
- [ ] `claw schedule remove <name>`
- [ ] `claw start` — starts scheduler daemon (foreground process)

#### 3.4 Tests

- [ ] `tests/test_scheduler.py` — mock APScheduler, verify agent runs triggered on schedule

**Files:**
- Edit: `careful_claude_claw/models.py`, `careful_claude_claw/db.py`, `careful_claude_claw/cli.py`
- Edit: `careful_claude_claw/scheduler.py`
- New: `tests/test_scheduler.py`

---

### Phase 4: Agent Monitoring & Status

**Goal:** Know what's running, what finished, and basic stats. Enough to support "show me all active agents" and "what's the status of project X."

#### 4.1 Active agents tracking

- [ ] Add to `careful_claude_claw/db.py`: `active_agents` table (agent_name, job_id, project_name, task, started_at)
- [ ] Wire into `agent.py`: register on start, remove on completion

#### 4.2 CLI status commands

- [ ] `claw status` — show active agents (name, project, task, duration) + recent job results
- [ ] `claw project status <name>` — show project info + its recent jobs + active agents

#### 4.3 Tests

- [ ] `tests/test_status.py` — active agent tracking, status query correctness

**Files:**
- Edit: `careful_claude_claw/db.py`, `careful_claude_claw/agent.py`, `careful_claude_claw/cli.py`
- New: `tests/test_status.py`

---

### Execution Order

Build sequentially: **Phase 2 → 3 → 4**. Each phase builds on the previous:

- Phase 2 (projects + skills) gives us entities to schedule and monitor
- Phase 3 (scheduler) gives us automated execution
- Phase 4 (monitoring) gives us visibility into what's running

Within each phase, work bottom-up: **models → db → core logic → CLI → tests**.

### Verification (after each phase)

```bash
uv run pytest                                  # all tests pass
uv run ruff check . && uv run ruff format .    # clean lint
uv run claw run                                # smoke test end-to-end
```

---

## Future Phases

The following phases are deferred but preserved for when the simplified path proves its value and use cases demand them. They build on the infrastructure from phases 2–4 above.

### Future: Security Config

**Goal:** `security.yaml` is the single source of truth; a compiler turns it into the three layers Claude Code expects.

**When needed:** When we have multiple agents with different permission requirements, or when agents need access to sensitive MCP servers (Stripe, production databases) that warrant hard deny lists.

#### F.1 Define security.yaml Pydantic schema

- [ ] Create `careful_claude_claw/security/models.py`
  - `GlobalRetryConfig` — max_attempts, backoff_seconds, on_final_failure
  - `GlobalStateConfig` — auto_merge paths, require_review paths
  - `GlobalConfig` — retry + global_state
  - `AgentSecurityConfig` — mcp_servers, auto_approve, require_confirmation, hard_deny, state_access
  - `SecurityConfig` root model — global + `dict[str, AgentSecurityConfig]` agents
- [ ] `load_security_config(path: Path) -> SecurityConfig` loader function

#### F.2 Build the compiler

- [ ] Implement in `careful_claude_claw/security/compiler.py`
  - `compile_settings_json(agent_config) -> dict` — maps auto_approve → allowedTools, hard_deny → deniedTools for `.claude/settings.json`
  - `compile_mcp_json(agent_config) -> dict` — generates `.mcp.json` scoping available MCP servers
  - `compile_policy_instructions(agent_config) -> str` — generates behavioral system prompt (require_confirmation items become "ask before using" instructions)
  - `compile_for_agent(config, agent_name) -> CompiledAgentConfig` — bundles all three outputs

#### F.3 Wire into agent execution

- [ ] `run_agent()` accepts optional `SecurityConfig` or compiled config
- [ ] Before spawning, writes `.claude/settings.json` and `.mcp.json` to agent's workspace directory
- [ ] Passes policy instructions via `system_prompt`

#### F.4 Sample config + tests

- [ ] Create sample `security.yaml` at project root with example agent definitions
- [ ] `tests/test_security_models.py` — schema validation (valid loads, invalid raises)
- [ ] `tests/test_security_compiler.py` — compiler output assertions per output type, round-trip: load yaml → compile → verify JSON

---

### Future: Event Queue & Webhooks

**Goal:** Full event-driven architecture with webhooks for external triggers.

**When needed:** When external systems (GitHub, Slack, etc.) need to trigger agent runs via HTTP, or when we need guaranteed exactly-once processing with a durable queue.

#### F.5 Event model + SQLite-backed queue

- [ ] `EventType` enum: CRON, WEBHOOK, MANUAL
- [ ] `EventStatus` enum: PENDING, PROCESSING, COMPLETED, FAILED
- [ ] `Event` pydantic model with atomic dequeue (PENDING → PROCESSING)
- [ ] `process_events()` worker loop

#### F.6 Webhook listener

- [ ] FastAPI app with `POST /webhook/{agent_name}` → enqueues event
- [ ] `GET /health` health check

#### F.7 Upgrade scheduler to use event queue

- [ ] Cron triggers enqueue events instead of calling `run_agent()` directly

---

### Future: Memory & State

**Goal:** Cross-agent memory sharing, salience-based decay, and git-based global state management.

**When needed:** When an orchestration agent needs persistent memory across sessions to manage communications, or when multiple agents need to share context (e.g., relationship manager + daily briefing both need contact history).

> **Note:** Claude Code's native auto-memory (`~/.claude/projects/<path>/memory/MEMORY.md`) handles simple per-agent persistence. This phase is for capabilities beyond what native memory provides.

#### F.8 Evaluate native auto-memory coverage

- [ ] Test auto-memory scoping via `--cwd` with Agent SDK
- [ ] Decision gate: if native memory covers 80%+ of needs, build a thin wrapper instead of full FTS5

#### F.9 Custom FTS5 memory (if needed)

- [ ] SQLite `memory` table + FTS5 virtual table
- [ ] Salience model: +0.1 on access, -2%/day decay, auto-delete below 0.1
- [ ] Context injection: FTS5 search → top memories → prepend to agent prompt

#### F.10 Git-based global state

- [ ] Deterministic Python merge logic (no LLM)
- [ ] Auto-merge paths vs require-review paths from config
- [ ] `commit_state_change(path, content, agent_name)`

---

### Future: Full Observability

**Goal:** Dashboard, alerting, and comprehensive monitoring.

**When needed:** When we're running enough agents on enough schedules that CLI status checks aren't sufficient.

#### F.11 HTML dashboard

- [ ] Hosted dashboard showing active agents, recent history, project status, skill directory
- [ ] Real-time updates via polling or SSE

#### F.12 Alerting

- [ ] On final failure (after max retries), trigger configurable action: alert, queue_for_review, ignore
- [ ] Webhook notification support

#### F.13 Advanced stats

- [ ] Per-agent success rates, average durations
- [ ] Failure analysis grouped by agent, time period, error type

---

## What We Are NOT Building (Now)

- Custom agent framework (Claude Code already is one)
- External databases beyond SQLite
- Workspace isolation per agent (defer unless concurrency issues arise)
- Multi-agent coordination beyond Claude Code's built-in sub-agent delegation
- OAuth token extraction or any approach that violates Anthropic's Terms of Service
- **Intra-session scheduling or polling** — Claude Code's native `/loop` and cron tools handle this; we don't replicate it in Python
- **Simple per-agent memory** — Claude Code's native auto-memory (`memory/MEMORY.md`) handles basic persistence; we only build custom FTS5 memory if cross-agent sharing or salience decay is needed
- **Skills themselves** — Skills are developed independently; the orchestrator provides infrastructure for discovering, registering, and running them
- **Security compilation** — Deferred until multi-agent permission scoping is needed (see Future Phases)
- **Event queue / webhooks** — Deferred; direct scheduler → agent execution is sufficient for now

---

## Key References

- **ClaudeClaw** by Mark Kashef — Bridge pattern, Agent SDK usage, memory system design ([promptadvisers.com](https://promptadvisers.com))
- **Claude Code Agent SDK** — Official Anthropic SDK for spawning Claude Code as a subprocess
- **Claude Code permissions** — `.claude/settings.json` for allow/deny tool patterns
- **MCP (Model Context Protocol)** — Verified connections to external services via `.mcp.json`
- **Claude Code v2.1.71 changelog** — `/loop` command, CronCreate/CronDelete/CronList tools, auto-memory
