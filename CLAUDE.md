# CLAUDE.md

Project instructions and conventions for Claude Code.

## Project Overview

Security-first Python platform for managing Claude Code agents across messaging interfaces, scheduled tasks, and the command line. Uses the Claude Code Agent SDK to spawn agents as the execution engine. See `plan.md` for the full architecture and roadmap.

## Environment Setup

This project uses `uv` for dependency management.

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create virtual environment and install dependencies
uv sync

# Add a runtime dependency
uv add <package>

# Add a dev-only dependency
uv add --dev <package>
```

## Common Commands

```bash
# Run the CLI
uv run claw

# Run a one-shot agent task
uv run claw run --task "..." --project <name> --skill <name>

# Start scheduler + Telegram listener
uv run claw start

# Show status
uv run claw status

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=careful_claude_claw --cov-report=term-missing

# Lint and format
uv run ruff check .
uv run ruff format .
```

## Project Structure

```
.
├── CLAUDE.md
├── plan.md                          # Full architecture and roadmap
├── pyproject.toml                   # Project metadata and dependencies (managed by uv)
├── pyrightconfig.json               # Pyright/pylance type checker config
├── skills/                          # Global skills directory (*.md files)
├── bin/
│   └── claw-services                # Service management script
├── careful_claude_claw/             # Main application package
│   ├── __init__.py
│   ├── cli.py                       # CLI entrypoint (click) — run, jobs, status, projects, skills, schedules, start, telegram
│   ├── agent.py                     # One-shot agent spawner (query) + retry logic
│   ├── agent_session.py             # Interactive agent sessions (ClaudeSDKClient) + session registry
│   ├── models.py                    # Pydantic models: Job, Project, Skill, Schedule
│   ├── db.py                        # SQLite operations: jobs, projects, schedules, active_agents
│   ├── scheduler.py                 # APScheduler cron wrapper
│   ├── skills.py                    # Skill discovery from skills/ directories
│   ├── telegram.py                  # Telegram bot: long-polling, command routing, agent spawning
│   └── security/
│       └── __init__.py              # Placeholder — security policy enforcement (Phase 5)
└── tests/                           # Test suite (mirrors careful_claude_claw/ structure)
    ├── test_models.py
    ├── test_db.py
    ├── test_projects.py
    ├── test_skills.py
    ├── test_scheduler.py
    ├── test_agent_session.py
    ├── test_telegram.py
    └── test_agent_integration.py    # Integration test (requires live Claude CLI)
```

## Code Conventions

- Python 3.11+
- Follow PEP 8; enforced via `ruff`
- Use type hints throughout
- Use `pydantic` models for all data structures
- Skills are markdown files in `skills/` (global) or `<project>/skills/` (project-scoped)
- Security policy will live in `security.yaml`; never hardcode permissions elsewhere

## Testing

- Use `pytest` for all tests
- Place tests in `tests/` mirroring the `careful_claude_claw/` structure
- Name test files `test_<module>.py` and test functions `test_<behavior>`
- Unit tests on models/config/security; integration tests on agent (marked `@pytest.mark.integration`)
- DB tests use `monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")` + `autouse` fixture

## Dependencies

- All dependencies are managed in `pyproject.toml` via `uv`
- Runtime deps: `uv add <package>`
- Dev/test deps: `uv add --dev <package>`
- Do not manually edit the `uv.lock` file
