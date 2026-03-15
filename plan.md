# CarefulClaudeClaw — Project Plan

## Overview

A security-first Python platform for managing Claude Code agents across messaging interfaces, scheduled tasks, and the command line. Uses the **Claude Code Agent SDK** to spawn Claude Code as the execution engine for autonomous, scheduled, and interactive AI workflows.

### Design Principles

- **Security-first.** Every agent execution is governed by explicit tool allowlists and behavioral policies. The `security.yaml` file is the single source of truth for what agents can do.
- **Don't rebuild what Claude Code already provides.** Skills, MCP servers, CLAUDE.md, file access, and native scheduling — these are built in. We bridge to them, not replicate them.
- **Skills are the unit of work.** Reusable markdown-based skill files define what agents do. Agents are ephemeral executors of skills.
- **Multiple interfaces, one agent layer.** CLI, Telegram, and (future) Slack all route through the same agent execution and job tracking infrastructure.
- **Thin orchestration.** The Python layer handles durable scheduling, messaging interfaces, security policy enforcement, agent lifecycle, and job tracking — nothing more.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                  Interfaces                      │
│  ┌──────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ CLI      │  │ Telegram  │  │ Scheduler    │  │
│  │ (click)  │  │ (polling) │  │ (APScheduler)│  │
│  └────┬─────┘  └─────┬─────┘  └──────┬───────┘  │
│       └───────────────┼───────────────┘          │
│                       ▼                          │
│  ┌────────────────────────────────────────────┐  │
│  │            Security Layer                  │  │
│  │  security.yaml → allowed_tools             │  │
│  │                 + system_prompt policies    │  │
│  │                 + hooks (future)            │  │
│  └────────────────────┬───────────────────────┘  │
│                       ▼                          │
│  ┌────────────────────────────────────────────┐  │
│  │            Agent Layer                     │  │
│  │  ┌──────────────┐  ┌───────────────────┐   │  │
│  │  │ One-shot     │  │ Interactive       │   │  │
│  │  │ (query)      │  │ (ClaudeSDKClient) │   │  │
│  │  │ + retry      │  │ + session mgmt    │   │  │
│  │  └──────────────┘  └───────────────────┘   │  │
│  └────────────────────────────────────────────┘  │
│                       │                          │
│  ┌────────────────────┼───────────────────────┐  │
│  │            Entity Layer                    │  │
│  │  Projects · Skills · Schedules · Jobs      │  │
│  └────────────────────────────────────────────┘  │
│                       │                          │
│               ┌───────┴───────┐                  │
│               │    SQLite     │                  │
│               └───────────────┘                  │
└──────────────────────────────────────────────────┘
                        │
                ┌───────┴───────┐
                │  Claude Code  │
                │  (CLI / SDK)  │
                └───────────────┘
```

### Interfaces

Three ways to trigger agent execution:

- **CLI** (`click`) — `claw run`, `claw status`, `claw schedule`, etc. For local development and admin.
- **Telegram** — Long-polling bot with command routing (`/status`, `/run`, `/kill`) and free-text agent spawning. Primary remote interface.
- **Scheduler** (`APScheduler`) — Cron-based triggers that fire one-shot agent runs on a schedule. Durable across restarts.

### Agent Layer

Two execution modes, both using the Claude Code Agent SDK:

- **One-shot** (`agent.py`) — `query()` with retry logic. Fire-and-forget: send task, get result. Used by CLI `claw run` and scheduler.
- **Interactive** (`agent_session.py`) — `ClaudeSDKClient` with bidirectional messaging. Supports follow-up messages (`/reply`, `@name`), interrupts, and session lifecycle. Used by Telegram for conversational agent interactions.

### Entity Layer

Four core entities, all persisted in SQLite:

- **Projects** — Registered codebases with name, path, status. Provide `--cwd` context for agent runs.
- **Skills** — Markdown files in `skills/` (global) or `<project>/skills/` (project-scoped). The reusable unit of work.
- **Schedules** — Cron expressions mapped to tasks or skills. Loaded by APScheduler on startup.
- **Jobs** — Execution history: agent name, task, status, attempts, output, errors, timing.

### Security Layer

Security policy is defined in `security.yaml` and enforced at the agent layer before every invocation. Three enforcement mechanisms:

1. **Tool allowlists** — `allowed_tools` parameter on `ClaudeAgentOptions`. Controls which tools the agent can use. Already wired into both `run_agent()` and `run_interactive_agent()`.
2. **Behavioral policies** — `system_prompt` injection. Policy text prepended to every agent invocation (e.g., "do not modify files outside the project directory").
3. **Hooks** (future) — Claude Code pre/post hooks on tool calls for hard denies and audit logging at the tool-call level.

---

## Data Layer

### SQLite (All Operational Data)

SQLite is the single operational store. Zero dependencies, single file, Python built-in.

| Table | Purpose |
|---|---|
| `jobs` | Job history — agent, task, status, attempts, timing, output, errors |
| `projects` | Project registry — name, path, status, description |
| `schedules` | Cron schedules — expression, task/skill reference, project, enabled |
| `active_agents` | Currently running agents — job_id, agent_name, project, task, start time |

### Skills (File-Based)

Skills are markdown files discovered by convention:

```
skills/                    # Global skills (available to all projects)
  hello.md
  daily-briefing.md
<project-path>/skills/     # Project-scoped skills (override global by name)
  review-pr.md
  run-tests.md
```

Project-scoped skills take priority over global skills with the same name.

### Persistent State (Future)

For state that agents produce and consume across sessions (e.g., contact notes, project summaries), a simple approach:

- **State table** in SQLite — key-value store with namespace, markdown content, and timestamps.
- **State files** — Optional markdown files in a `state/` directory for human-readable state that agents can read/write directly.

No git-based merge logic. State is either agent-written (SQLite/files) or human-reviewed (PRs). Keeping these paths separate avoids the complexity of automated merge policies.

---

## Security

### Design

Security is enforced per-invocation, not per-agent-identity. Every time the system spawns an agent — whether from CLI, Telegram, or scheduler — the security layer resolves the applicable policy and passes it to the Agent SDK.

The policy resolution order:

1. **Skill-level overrides** — A skill file can declare required tools or restrictions in frontmatter.
2. **Project-level defaults** — Each project can define default tool allowlists and policies.
3. **Global defaults** — Fallback tool allowlist and behavioral policies.

### security.yaml

```yaml
# security.yaml — single source of truth

defaults:
  allowed_tools:
    - Read
    - Glob
    - Grep
  max_turns: 10
  max_attempts: 2
  backoff_seconds: 30
  system_prompt: |
    You are a careful assistant. Do not modify files unless explicitly
    instructed. Do not execute shell commands unless the task requires it.

projects:
  my-webapp:
    allowed_tools:
      - Read
      - Glob
      - Grep
      - Edit
      - Write
      - Bash
    system_prompt: |
      You are working on the my-webapp project. Follow the project's
      CLAUDE.md conventions. Run tests after making changes.

skills:
  daily-briefing:
    allowed_tools:
      - Read
      - Glob
      - Grep
      - WebSearch
      - WebFetch
    max_turns: 20

  deploy:
    allowed_tools:
      - Read
      - Glob
      - Grep
      - Bash
    require_confirmation: true   # Prompt user before executing
    system_prompt: |
      You are deploying code. Double-check the branch and environment
      before running any deploy commands.
```

### Enforcement Points

| Mechanism | What It Controls | How It's Passed |
|---|---|---|
| `allowed_tools` | Which tools the agent can call | `ClaudeAgentOptions.allowed_tools` |
| `system_prompt` | Behavioral boundaries and instructions | `ClaudeAgentOptions.system_prompt` |
| `max_turns` | How long the agent can run | `ClaudeAgentOptions.max_turns` |
| `require_confirmation` | Whether to prompt user before executing | Interface-level gate (Telegram confirmation, CLI prompt) |
| Hooks (future) | Hard denies, audit logging at tool-call level | Claude Code hook system |

---

## Native Claude Code Scheduling

Claude Code includes built-in scheduling features. Understanding the boundary between native and orchestrator scheduling:

| Constraint | Impact |
|---|---|
| Session-scoped only | Tasks die when you exit the terminal |
| 3-day auto-expiry | Recurring tasks auto-delete after 3 days |
| No persistence across restarts | Nothing survives a process exit |
| Requires active, idle session | Tasks only fire when Claude is idle |
| Not exposed in Agent SDK | Can't programmatically create loops from Python |

**Division of responsibility:**

| Responsibility | Owner | Why |
|---|---|---|
| Durable cross-session scheduling | Python orchestrator (APScheduler) | Must survive restarts |
| Intra-session polling / sub-loops | Claude Code native `/loop` | Already built, no Python code needed |
| One-shot reminders during a task | Claude Code native cron | Natural language, zero config |

---

## Completed Phases

### Phase 1: Minimal Loop ✅

- [x] Spawn Claude Code via Agent SDK with `query()`
- [x] Retry logic with configurable backoff
- [x] Job tracking in SQLite (insert, update, list)
- [x] CLI: `claw run` and `claw jobs`
- [x] Tests for models and db

**Files:** `models.py` (Job, JobStatus), `db.py` (jobs table), `agent.py` (run_agent), `cli.py` (run/jobs commands)

### Phase 2: Project Model & Skill Registry ✅

- [x] Project model with status (ACTIVE, PAUSED, ARCHIVED)
- [x] Project CRUD in SQLite
- [x] Skill discovery from `skills/` directories (global + project-scoped)
- [x] CLI: `claw project add/list/status`, `claw skills`
- [x] `claw run --skill <name> --project <name>`
- [x] Tests for projects and skills

**Files:** `models.py` (Project, Skill, SkillScope), `db.py` (projects table), `skills.py`, `cli.py`

### Phase 3: Scheduler ✅

- [x] APScheduler with 5-field cron parsing
- [x] Schedule model with task/skill reference and project context
- [x] Schedules table in SQLite
- [x] `claw schedule add/list/remove`
- [x] `claw start` — runs scheduler daemon
- [x] Skill content resolution at execution time
- [x] Tests for cron parsing

**Files:** `models.py` (Schedule), `db.py` (schedules table), `scheduler.py`, `cli.py`

### Phase 4: Agent Monitoring & Telegram ✅

- [x] Active agents table — register on start, unregister on completion
- [x] `claw status` — active agents + recent jobs
- [x] `claw project status <name>` — project-specific view
- [x] Interactive agent sessions via `ClaudeSDKClient`
- [x] Session registry with kill/reply support
- [x] Telegram bot with command routing and free-text agent spawning
- [x] `claw start --telegram` — scheduler + Telegram listener
- [x] Security: only processes messages from configured chat_id
- [x] Tests for agent sessions and Telegram routing

**Files:** `agent_session.py`, `telegram.py`, `db.py` (active_agents table), `cli.py`

---

## Next Phases

### Phase 5: Security Policy Enforcement

**Goal:** `security.yaml` governs every agent invocation. Tool allowlists, behavioral policies, and turn limits are resolved from config before spawning.

#### 5.1 Security config model

- [ ] Create `careful_claude_claw/security/models.py`:
  - `SecurityDefaults` — allowed_tools, max_turns, max_attempts, backoff_seconds, system_prompt
  - `ProjectSecurity` — per-project overrides (allowed_tools, system_prompt)
  - `SkillSecurity` — per-skill overrides (allowed_tools, max_turns, require_confirmation, system_prompt)
  - `SecurityConfig` root model — defaults + `dict[str, ProjectSecurity]` projects + `dict[str, SkillSecurity]` skills
- [ ] `load_security_config(path: Path) -> SecurityConfig`

#### 5.2 Policy resolver

- [ ] Implement `careful_claude_claw/security/resolver.py`:
  - `resolve_policy(config, skill_name, project_name) -> ResolvedPolicy`
  - Merges: global defaults ← project overrides ← skill overrides
  - `ResolvedPolicy`: allowed_tools, system_prompt, max_turns, max_attempts, backoff_seconds, require_confirmation

#### 5.3 Wire into agent execution

- [ ] `run_agent()` and `run_interactive_agent()` accept optional `ResolvedPolicy`
- [ ] CLI, Telegram, and scheduler all resolve policy before spawning
- [ ] `require_confirmation` triggers a confirmation prompt in the active interface (Telegram inline keyboard, CLI y/n prompt)

#### 5.4 Sample config + tests

- [ ] Create `security.yaml` at project root with sensible defaults
- [ ] `tests/test_security_models.py` — schema validation
- [ ] `tests/test_security_resolver.py` — merge logic, override precedence

**Files:**
- New: `careful_claude_claw/security/models.py`, `careful_claude_claw/security/resolver.py`
- Edit: `agent.py`, `agent_session.py`, `cli.py`, `telegram.py`, `scheduler.py`
- New: `security.yaml`, `tests/test_security_models.py`, `tests/test_security_resolver.py`

---

### Phase 6: Messaging Interface Abstraction

**Goal:** Extract Telegram into a generic messaging interface. Add Slack. Make the active interface config-driven.

#### 6.1 Interface protocol

- [ ] Define `careful_claude_claw/interfaces/base.py`:
  - `MessagingInterface` protocol/ABC: `send_message()`, `run_listener()`, command routing hooks
  - Common command set: status, jobs, projects, skills, schedules, agents, run, kill, reply
- [ ] Move shared command logic out of `telegram.py` into a `CommandHandler` that interfaces delegate to

#### 6.2 Refactor Telegram

- [ ] Move `telegram.py` → `careful_claude_claw/interfaces/telegram.py`
- [ ] Implement `MessagingInterface` protocol
- [ ] Preserve all existing functionality

#### 6.3 Add Slack

- [ ] Implement `careful_claude_claw/interfaces/slack.py`
  - Slack Bot via socket mode or webhook (evaluate at implementation time)
  - Same command set as Telegram
  - Slack-specific formatting (blocks, threads)

#### 6.4 Config-driven interface selection

- [ ] Add to config: `interface: telegram | slack` (or support multiple simultaneously)
- [ ] `claw start` reads config and launches the selected interface(s)

#### 6.5 Tests

- [ ] `tests/test_interfaces.py` — shared command handler tests
- [ ] `tests/test_slack.py` — Slack-specific routing and formatting

**Files:**
- New: `careful_claude_claw/interfaces/base.py`, `careful_claude_claw/interfaces/telegram.py`, `careful_claude_claw/interfaces/slack.py`
- Delete: `careful_claude_claw/telegram.py` (moved)
- Edit: `cli.py`

---

### Phase 7: Persistent State

**Goal:** Simple cross-session state that agents can read and write. No git merge logic — just a state store.

#### 7.1 State table

- [ ] Add `state` table to SQLite: namespace (TEXT), key (TEXT), value (TEXT/markdown), updated_at, updated_by (agent name)
- [ ] CRUD functions: `get_state()`, `set_state()`, `list_state()`, `delete_state()`
- [ ] Namespaces map to projects or global scope

#### 7.2 State skill integration

- [ ] Agents can read/write state via a `state/` directory convention or system prompt instructions
- [ ] State context injection: before agent invocation, relevant state entries are included in the system prompt

#### 7.3 Tests

- [ ] `tests/test_state.py` — CRUD, namespacing, agent attribution

**Files:**
- Edit: `db.py`, `agent.py`, `agent_session.py`
- New: `tests/test_state.py`

---

### Phase 8: Observability

**Goal:** Better visibility into what's running and what happened.

#### 8.1 Enhanced status

- [ ] `claw dashboard` — Rich TUI with live-updating active agents, recent jobs, schedule next-fire times
- [ ] Failure analysis: aggregate failures by agent, time period, error type

#### 8.2 Alerting

- [ ] On final failure (after max retries), send alert via active messaging interface
- [ ] Configurable in `security.yaml`: `on_final_failure: alert | ignore`

#### 8.3 Hooks-based audit logging (depends on Phase 5)

- [ ] Claude Code hooks for pre/post tool call logging
- [ ] Audit trail: which tools each agent actually used, not just what was allowed

**Files:**
- Edit: `cli.py`, `agent.py`, `scheduler.py`
- New: `careful_claude_claw/hooks/` (if hooks integration warrants its own module)

---

## Execution Order

Build sequentially: **Phase 5 → 6 → 7 → 8**. Each phase builds on the previous:

- Phase 5 (security) establishes the policy layer that all interfaces enforce
- Phase 6 (messaging abstraction) enables multi-channel access with consistent security
- Phase 7 (state) gives agents persistent context across sessions
- Phase 8 (observability) provides visibility and alerting

Within each phase, work bottom-up: **models → core logic → integration → CLI/interface → tests**.

### Verification (after each phase)

```bash
uv run pytest                                  # all tests pass
uv run ruff check . && uv run ruff format .    # clean lint
uv run claw run                                # smoke test end-to-end
```

---

## What We Are NOT Building

- Custom agent framework (Claude Code already is one)
- External databases beyond SQLite
- Event queue or webhook listener (direct execution is sufficient; Telegram/Slack cover remote triggers)
- Git-based state merge logic (too complex for the value; simple state store instead)
- Multi-agent coordination beyond Claude Code's built-in sub-agent delegation
- OAuth token extraction or any approach that violates Anthropic's Terms of Service
- **Intra-session scheduling or polling** — Claude Code's native `/loop` and cron tools handle this
- **Simple per-agent memory** — Claude Code's native auto-memory handles basic persistence
- **Named agent identities** — Agents are ephemeral; skills and projects define the work, not agent personas
- **Skills themselves** — Skills are developed independently; the platform provides infrastructure for discovering, registering, and running them

---

## Key References

- **Claude Code Agent SDK** — Official Anthropic SDK for spawning Claude Code as a subprocess
- **Claude Code permissions** — `allowed_tools` parameter, `.claude/settings.json` for allow/deny patterns
- **Claude Code hooks** — Pre/post hooks on tool calls for interception and audit
- **MCP (Model Context Protocol)** — Verified connections to external services via `.mcp.json`
- **Claude Code native scheduling** — `/loop` command, CronCreate/CronDelete/CronList tools
