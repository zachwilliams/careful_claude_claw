"""Stdio MCP server for the orchestrator_miao PTY session.

Exposes memory CRUD, sub-agent management, and system info tools
via the MCP stdio transport so they are available to the PTY-spawned
claude orchestrator process.

Run as: uv run python -m careful_claude_claw.mcp_server
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .agent_session import (
    generate_name,
    kill_session,
    list_sessions,
    run_interactive_agent,
    send_to_agent as _send_to_agent,
)
from .db import init_db
from .db import list_jobs as db_list_jobs
from .memory import (
    add_memory,
    delete_memory,
    get_memory,
    list_memories,
    search_memories,
    update_memory,
)
from .models import MEMORY_DECAY_RATES, Memory, MemorySource, MemoryType, score_memory
from .skills import discover_skills

logger = logging.getLogger(__name__)

mcp = FastMCP("claw_orchestrator")
init_db()


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_search(
    query: str,
    memory_type: str | None = None,
    category: str | None = None,
    limit: int = 10,
) -> str:
    """Search memories using full-text search with scored results.

    memory_type: preference | decision | observation | procedure
    """
    results = search_memories(query=query, memory_type=memory_type, category=category, limit=limit)
    scored = sorted([(m, score_memory(m)) for m in results], key=lambda x: x[1], reverse=True)
    if not scored:
        return "No memories found."
    lines = [f"Found {len(scored)} memories:"]
    for mem, sc in scored:
        tags_str = ", ".join(mem.tags) if mem.tags else ""
        cat = f" ({mem.category})" if mem.category else ""
        lines.append(
            f"  [{mem.id}] {mem.memory_type}{cat}: {mem.content[:120]}"
            f" | imp={mem.importance} score={sc:.3f}"
            + (f" tags=[{tags_str}]" if tags_str else "")
        )
    return "\n".join(lines)


@mcp.tool()
async def memory_write(
    content: str,
    memory_type: str,
    category: str | None = None,
    tags: list[str] | None = None,
    importance: float = 0.5,
) -> str:
    """Store a new memory.

    memory_type: preference | decision | observation | procedure
    importance: 0.0–1.0 (default 0.5)
    """
    mem_type = MemoryType(memory_type)
    memory = Memory(
        memory_type=mem_type,
        content=content,
        source=MemorySource.AGENT,
        category=category,
        tags=tags or [],
        importance=importance,
        decay_rate=MEMORY_DECAY_RATES.get(mem_type, 0.0),
    )
    add_memory(memory)
    return f"Memory stored (id={memory.id}): {memory.content[:80]}"


@mcp.tool()
async def memory_update(id: int, content: str) -> str:
    """Update an existing memory's content by ID."""
    mem = get_memory(id)
    if not mem:
        return f"Memory {id} not found."
    mem.content = content
    update_memory(mem)
    return f"Memory {id} updated."


@mcp.tool()
async def memory_delete(id: int) -> str:
    """Soft-delete a memory by ID."""
    delete_memory(id)
    return f"Memory {id} deleted."


@mcp.tool()
async def memory_list(memory_type: str | None = None, limit: int = 20) -> str:
    """List memories, optionally filtered by type.

    memory_type: preference | decision | observation | procedure
    """
    results = list_memories(memory_type=memory_type, limit=limit)
    if not results:
        return "No memories found."
    lines = [f"Found {len(results)} memories:"]
    for mem in results:
        tags_str = ", ".join(mem.tags) if mem.tags else ""
        cat = f" ({mem.category})" if mem.category else ""
        lines.append(
            f"  [{mem.id}] {mem.memory_type}{cat}: {mem.content[:120]}"
            f" | imp={mem.importance}"
            + (f" tags=[{tags_str}]" if tags_str else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sub-agent tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def spawn_agent(
    task: str,
    cwd: str | None = None,
    skill: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """Spawn a sub-agent for a task. Runs in background; use list_agents to check status."""
    if skill:
        for s in discover_skills():
            if s.name == skill:
                try:
                    task = Path(s.file_path).read_text()
                except OSError:
                    return f"Cannot read skill file: {s.file_path}"
                break
        else:
            return f"Skill not found: {skill}"

    name = generate_name("A")

    async def on_message(msg: str) -> None:
        logger.info("sub-agent @%s: %s", name, msg)

    asyncio.create_task(
        run_interactive_agent(
            name=name,
            task=task,
            on_message=on_message,
            agent_name=f"sub-{name}",
            cwd=cwd,
            allowed_tools=allowed_tools,
        )
    )
    return f"Agent @{name} spawned for: {task[:100]}"


@mcp.tool()
async def list_agents() -> str:
    """Show currently running sub-agents."""
    sessions = list_sessions()
    if not sessions:
        return "No active agents."
    lines = [f"Active agents ({len(sessions)}):"]
    for s in sessions:
        lines.append(f"  @{s.name} (exec: {s.execution_id})")
    return "\n".join(lines)


@mcp.tool()
async def kill_agent(name: str) -> str:
    """Kill a running sub-agent by name."""
    killed = await kill_session(name)
    return f"Agent {name} killed." if killed else f"No active agent named {name}."


@mcp.tool()
async def send_to_agent(name: str, message: str) -> str:
    """Send a follow-up message to a running sub-agent."""
    sent = await _send_to_agent(name, message)
    return f"Message sent to @{name}." if sent else f"No active agent named {name}."


# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_jobs() -> str:
    """Show configured scheduled jobs."""
    jobs = db_list_jobs()
    if not jobs:
        return "No jobs configured."
    lines = [f"Jobs ({len(jobs)}):"]
    for j in jobs:
        task_or_skill = j["skill_name"] or (j["task"] or "")[:50]
        cron = f" [{j['cron_expr']}]" if j.get("cron_expr") else ""
        enabled = " (disabled)" if not j["enabled"] else ""
        lines.append(f"  {j['name']}{cron}{enabled}: {task_or_skill}")
    return "\n".join(lines)


@mcp.tool()
async def list_skills() -> str:
    """Show available skills."""
    skills = discover_skills()
    if not skills:
        return "No skills found."
    lines = [f"Skills ({len(skills)}):"]
    for s in skills:
        desc = f" — {s.description[:60]}" if s.description else ""
        lines.append(f"  {s.name}{desc}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
