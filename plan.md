# CarefulClaudeClaw Orchestrator — Project Plan

## Overview

A security-first, thin Python orchestrator that uses the **Claude Code Agent SDK** to spawn Claude Code as the execution engine for autonomous, scheduled, and event-driven AI workflows. Inspired by the [ClaudeClaw bridge pattern](https://promptadvisers.com) — keep the orchestration layer minimal and let Claude Code do what it already does well.

### Design Principles

- **Don't rebuild what Claude Code already provides.** Skills, MCP servers, CLAUDE.md, file access, permissions — these are built in. We bridge to them, not replicate them.
- **Single source of truth for security.** One `security.yaml` governs all permission layers.
- **File-based configuration, SQLite for operational data.** Human-readable configs in git, queryable operational state in SQLite.
- **Thin orchestration.** The Python layer handles scheduling, event routing, security compilation, and retry logic — nothing more.

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

## Prototyping Roadmap

### Phase 1: Minimal Loop

- [ ] Python script with a single hardcoded cron job
- [ ] Spawns Claude Code via Agent SDK
- [ ] Agent does one useful task (e.g., summarize today's calendar)
- [ ] Logs result to SQLite
- [ ] **Goal:** End-to-end execution works

### Phase 2: Security Config

- [ ] Define `security.yaml` schema
- [ ] Build the compiler that generates `.claude/settings.json`, `.mcp.json`, and policy instructions
- [ ] Test that agent permissions are correctly scoped per invocation
- [ ] **Goal:** Single config file controls all permissions

### Phase 3: Event Infrastructure

- [ ] Add cron scheduler (APScheduler or similar)
- [ ] Add webhook listener (FastAPI)
- [ ] Implement event queue
- [ ] Add retry logic with backoff
- [ ] **Goal:** Multiple triggers, reliable execution

### Phase 4: Memory & State

- [ ] Set up SQLite FTS5 memory tables
- [ ] Implement context injection before agent invocations
- [ ] Set up git repo for global state
- [ ] Build deterministic Python merge logic
- [ ] Configure auto-merge vs. require-review per path
- [ ] **Goal:** Agents have memory, state changes are auditable

### Phase 5: Observability

- [ ] Build SQLite queries for dashboard data
- [ ] Simple dashboard view (CLI, markdown report, or lightweight web UI)
- [ ] Alerting on final failures
- [ ] **Goal:** Know what's running, what failed, and why

---

## What We Are NOT Building

- Custom agent framework (Claude Code already is one)
- External databases beyond SQLite
- Workspace isolation per agent (defer unless concurrency issues arise)
- Multi-agent coordination beyond Claude Code's built-in sub-agent delegation
- Custom skill or MCP server implementations (use what exists)
- OAuth token extraction or any approach that violates Anthropic's Terms of Service

---

## Key References

- **ClaudeClaw** by Mark Kashef — Bridge pattern, Agent SDK usage, memory system design ([promptadvisers.com](https://promptadvisers.com))
- **Claude Code Agent SDK** — Official Anthropic SDK for spawning Claude Code as a subprocess
- **Claude Code permissions** — `.claude/settings.json` for allow/deny tool patterns
- **MCP (Model Context Protocol)** — Verified connections to external services via `.mcp.json`
