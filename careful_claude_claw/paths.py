"""Stable filesystem paths for all claw state directories."""

from pathlib import Path

# Root directory for all per-user claw state
CLAW_DIR = Path.home() / ".claw"

# Per-agent-type workspace roots
ORCHESTRATOR_MIAO_DIR = CLAW_DIR / "orchestrator_miao"
SDK_SESSIONS_DIR = CLAW_DIR / "sdk_sessions"
JOBS_DIR = CLAW_DIR / "jobs"
