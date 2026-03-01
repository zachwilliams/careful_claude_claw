# CLAUDE.md

Project instructions and conventions for Claude Code.

## Project Overview

Security-first Python orchestrator that uses the Claude Code Agent SDK to spawn Claude Code as the execution engine for autonomous, scheduled, and event-driven AI workflows. See `plan.md` for the full architecture.

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
├── security.yaml                    # Single source of truth for all agent permissions
├── careful_claude_claw/             # Main application package
│   ├── __init__.py
│   ├── cli.py                       # CLI entrypoint (click)
│   ├── config.py                    # Configuration management (pydantic-settings)
│   ├── models.py                    # Data models (events, jobs, memory, sessions)
│   ├── db.py                        # SQLite operations
│   ├── agent.py                     # Agent SDK spawner + retry logic
│   ├── scheduler.py                 # Cron scheduler (APScheduler)
│   ├── webhook.py                   # Webhook listener (FastAPI)
│   ├── queue.py                     # Event queue
│   └── security/
│       ├── __init__.py
│       └── compiler.py              # security.yaml → .claude/settings.json + .mcp.json
└── tests/                           # Test suite (mirrors careful_claude_claw/ structure)
```

## Code Conventions

- Python 3.11+
- Follow PEP 8; enforced via `ruff`
- Use type hints throughout
- Use `pydantic` models for all data structures
- Keep security compilation logic isolated in `careful_claude_claw/security/`
- All agent permission policy lives in `security.yaml`; never hardcode permissions elsewhere

## Testing

- Use `pytest` for all tests
- Place tests in `tests/` mirroring the `careful_claude_claw/` structure
- Name test files `test_<module>.py` and test functions `test_<behavior>`
- Unit tests on models/config/security compiler; integration tests on agent/scheduler/webhook

## Dependencies

- All dependencies are managed in `pyproject.toml` via `uv`
- Runtime deps: `uv add <package>`
- Dev/test deps: `uv add --dev <package>`
- Do not manually edit the `uv.lock` file
