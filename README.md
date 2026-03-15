# CarefulClaudeClaw

Security-first Python orchestrator that uses the Claude Code Agent SDK to spawn Claude Code as the execution engine for autonomous, scheduled, and event-driven AI workflows.



## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Claude Code CLI](https://claude.ai/code) installed and authenticated

## Setup

```bash
# Install dependencies
uv sync
```

## Running the POC

Run the default task (lists and describes all Python source files in the current directory):

```bash
uv run claw run
```

Run a custom task:

```bash
uv run claw run --task "Summarize the project structure"
```

Options:

```
--task          Task for the agent to execute
--agent-name    Name to identify this agent  (default: demo)
--cwd           Working directory for the agent
--skill         Run a named skill instead of a raw task
--max-attempts  Maximum retry attempts on failure  (default: 2)
--backoff       Seconds to wait between retries  (default: 5)
```

Start the scheduler and Telegram listener:

```bash
uv run claw start              # scheduler + auto-detect Telegram
uv run claw start --telegram   # force Telegram on
uv run claw telegram           # standalone Telegram listener (dev)
```

Manage services (background):

```bash
bin/claw-services start
bin/claw-services stop
bin/claw-services restart
bin/claw-services status
bin/claw-services logs
```

View jobs and status:

```bash
uv run claw jobs list
uv run claw jobs add myjob --task "..." --cron "0 9 * * *"
uv run claw jobs runs
uv run claw status
uv run claw skills
```

Dev reset (wipe database and Telegram chat history):

```bash
uv run claw reset              # wipe DB + Telegram messages (with confirmation)
uv run claw reset -y           # skip confirmation
```

## Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=careful_claude_claw --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_db.py
```

## Lint and Format

```bash
uv run ruff check .
uv run ruff format .
```
