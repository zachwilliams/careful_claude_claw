"""MCP tools for the persistent orchestrator.

Provides memory CRUD, sub-agent management, and system info tools
using the Claude Agent SDK's in-process MCP server.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .agent_session import (
    generate_name,
    kill_session,
    list_sessions,
    run_interactive_agent,
    send_to_agent,
)
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

# Module-level callback set by orchestrator before each message
_message_callback: Callable[[str], Awaitable[None]] | None = None


def set_message_callback(cb: Callable[[str], Awaitable[None]] | None) -> None:
    global _message_callback
    _message_callback = cb


# --- Memory Tools ---


@tool(
    name="memory_search",
    description=(
        "Search memories using full-text search with scored results."
        " Returns relevant memories ranked by composite score."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query text"},
            "type": {
                "type": "string",
                "enum": ["preference", "decision", "observation", "procedure"],
                "description": "Filter by memory type",
            },
            "category": {"type": "string", "description": "Filter by category"},
            "limit": {
                "type": "integer",
                "description": "Max results (default 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
)
async def memory_search_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        query = params["query"]
        memory_type = params.get("type")
        category = params.get("category")
        limit = params.get("limit", 10)

        results = search_memories(
            query=query,
            memory_type=memory_type,
            category=category,
            limit=limit,
        )

        # Score and sort
        scored = [(m, score_memory(m)) for m in results]
        scored.sort(key=lambda x: x[1], reverse=True)

        memories_out = []
        for mem, sc in scored:
            memories_out.append(
                {
                    "id": mem.id,
                    "type": mem.memory_type,
                    "content": mem.content,
                    "category": mem.category,
                    "tags": mem.tags,
                    "importance": mem.importance,
                    "score": round(sc, 3),
                }
            )

        text = _format_memories(memories_out)
        return _text_result(f"Found {len(memories_out)} memories:\n{text}")
    except Exception as e:
        return _text_result(f"Error searching memories: {e}", is_error=True)


@tool(
    name="memory_write",
    description=(
        "Store a new memory. Use this to remember important info"
        " about the user, decisions, observations, or procedures."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory content"},
            "type": {
                "type": "string",
                "enum": ["preference", "decision", "observation", "procedure"],
                "description": "Memory type",
            },
            "category": {"type": "string", "description": "Short category label"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keyword tags",
            },
            "importance": {
                "type": "number",
                "description": "Importance 0.0-1.0 (default 0.5)",
                "default": 0.5,
            },
        },
        "required": ["content", "type"],
    },
)
async def memory_write_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        mem_type = MemoryType(params["type"])
        memory = Memory(
            memory_type=mem_type,
            content=params["content"],
            source=MemorySource.AGENT,
            category=params.get("category"),
            tags=params.get("tags", []),
            importance=params.get("importance", 0.5),
            decay_rate=MEMORY_DECAY_RATES.get(mem_type, 0.0),
        )
        add_memory(memory)
        return _text_result(f"Memory stored (id={memory.id}): {memory.content[:80]}")
    except Exception as e:
        return _text_result(f"Error writing memory: {e}", is_error=True)


@tool(
    name="memory_update",
    description="Update an existing memory's content.",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Memory ID to update"},
            "content": {"type": "string", "description": "New content"},
        },
        "required": ["id", "content"],
    },
)
async def memory_update_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        mem = get_memory(params["id"])
        if not mem:
            return _text_result(f"Memory {params['id']} not found.", is_error=True)
        mem.content = params["content"]
        update_memory(mem)
        return _text_result(f"Memory {mem.id} updated.")
    except Exception as e:
        return _text_result(f"Error updating memory: {e}", is_error=True)


@tool(
    name="memory_delete",
    description="Soft-delete a memory by ID.",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Memory ID to delete"},
        },
        "required": ["id"],
    },
)
async def memory_delete_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        delete_memory(params["id"])
        return _text_result(f"Memory {params['id']} deleted.")
    except Exception as e:
        return _text_result(f"Error deleting memory: {e}", is_error=True)


@tool(
    name="memory_list",
    description="List memories with optional type filter.",
    input_schema={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["preference", "decision", "observation", "procedure"],
                "description": "Filter by memory type",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 20)",
                "default": 20,
            },
        },
    },
)
async def memory_list_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        mem_type = params.get("type")
        limit = params.get("limit", 20)
        results = list_memories(memory_type=mem_type, limit=limit)

        if not results:
            return _text_result("No memories found.")

        memories_out = []
        for mem in results:
            memories_out.append(
                {
                    "id": mem.id,
                    "type": mem.memory_type,
                    "content": mem.content,
                    "category": mem.category,
                    "tags": mem.tags,
                    "importance": mem.importance,
                }
            )

        text = _format_memories(memories_out)
        return _text_result(f"Found {len(memories_out)} memories:\n{text}")
    except Exception as e:
        return _text_result(f"Error listing memories: {e}", is_error=True)


# --- Sub-agent Tools ---


@tool(
    name="spawn_agent",
    description=(
        "Spawn a sub-agent for a coding task. Runs in background, results sent via callback."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Task prompt for the agent"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
            "skill": {"type": "string", "description": "Skill name to use (optional)"},
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Allowed tools (optional, defaults to safe set)",
            },
        },
        "required": ["task"],
    },
)
async def spawn_agent_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        task = params["task"]
        cwd = params.get("cwd")
        skill_name = params.get("skill")
        allowed_tools = params.get("allowed_tools")

        # Resolve skill if provided
        if skill_name:
            from pathlib import Path

            skill = None
            for s in discover_skills():
                if s.name == skill_name:
                    skill = s
                    break
            if skill:
                try:
                    task = Path(skill.file_path).read_text()
                except OSError:
                    return _text_result(f"Cannot read skill file: {skill.file_path}", is_error=True)
            else:
                return _text_result(f"Skill not found: {skill_name}", is_error=True)

        name = generate_name("A")

        async def on_message(msg: str) -> None:
            if _message_callback:
                await _message_callback(f"@{name}: {msg}")

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

        return _text_result(f"Agent @{name} spawned for: {task[:100]}")
    except Exception as e:
        return _text_result(f"Error spawning agent: {e}", is_error=True)


@tool(
    name="list_agents",
    description="Show currently running sub-agents.",
    input_schema={"type": "object", "properties": {}},
)
async def list_agents_tool(params: dict[str, Any]) -> dict[str, Any]:
    sessions = list_sessions()
    if not sessions:
        return _text_result("No active agents.")

    lines = [f"Active agents ({len(sessions)}):"]
    for s in sessions:
        lines.append(f"  @{s.name} (exec: {s.execution_id})")
    return _text_result("\n".join(lines))


@tool(
    name="kill_agent",
    description="Kill a running sub-agent by name.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Agent name to kill"},
        },
        "required": ["name"],
    },
)
async def kill_agent_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        killed = await kill_session(params["name"])
        if killed:
            return _text_result(f"Agent {params['name']} killed.")
        return _text_result(f"No active agent named {params['name']}.", is_error=True)
    except Exception as e:
        return _text_result(f"Error killing agent: {e}", is_error=True)


@tool(
    name="send_to_agent",
    description="Send a follow-up message to a running sub-agent.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Agent name"},
            "message": {"type": "string", "description": "Message to send"},
        },
        "required": ["name", "message"],
    },
)
async def send_to_agent_tool(params: dict[str, Any]) -> dict[str, Any]:
    try:
        sent = await send_to_agent(params["name"], params["message"])
        if sent:
            return _text_result(f"Message sent to @{params['name']}.")
        return _text_result(f"No active agent named {params['name']}.", is_error=True)
    except Exception as e:
        return _text_result(f"Error sending to agent: {e}", is_error=True)


# --- System Tools ---


@tool(
    name="list_jobs",
    description="Show configured scheduled jobs.",
    input_schema={"type": "object", "properties": {}},
)
async def list_jobs_tool(params: dict[str, Any]) -> dict[str, Any]:
    jobs = db_list_jobs()
    if not jobs:
        return _text_result("No jobs configured.")

    lines = [f"Jobs ({len(jobs)}):"]
    for j in jobs:
        task_or_skill = j["skill_name"] or (j["task"] or "")[:50]
        cron = f" [{j['cron_expr']}]" if j.get("cron_expr") else ""
        enabled = " (disabled)" if not j["enabled"] else ""
        lines.append(f"  {j['name']}{cron}{enabled}: {task_or_skill}")
    return _text_result("\n".join(lines))


@tool(
    name="list_skills",
    description="Show available skills.",
    input_schema={"type": "object", "properties": {}},
)
async def list_skills_tool(params: dict[str, Any]) -> dict[str, Any]:
    skills = discover_skills()
    if not skills:
        return _text_result("No skills found.")

    lines = [f"Skills ({len(skills)}):"]
    for s in skills:
        desc = f" — {s.description[:60]}" if s.description else ""
        lines.append(f"  {s.name}{desc}")
    return _text_result("\n".join(lines))


# --- Helpers ---


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """Return a properly formatted MCP tool result."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


def _format_memories(memories: list[dict]) -> str:
    lines = []
    for m in memories:
        tags_str = ", ".join(m.get("tags", []))
        cat = f" ({m.get('category', '')})" if m.get("category") else ""
        score_str = f" [score={m['score']:.3f}]" if "score" in m else ""
        lines.append(
            f"  [{m['id']}] {m['type']}{cat}: {m['content'][:100]}"
            f" | imp={m.get('importance', 0.5)}{score_str}"
            f"{f' tags=[{tags_str}]' if tags_str else ''}"
        )
    return "\n".join(lines)


# --- MCP Server Factory ---

ALL_TOOLS = [
    memory_search_tool,
    memory_write_tool,
    memory_update_tool,
    memory_delete_tool,
    memory_list_tool,
    spawn_agent_tool,
    list_agents_tool,
    kill_agent_tool,
    send_to_agent_tool,
    list_jobs_tool,
    list_skills_tool,
]


def create_orchestrator_mcp_server() -> McpSdkServerConfig:
    """Create an in-process MCP server with all orchestrator tools."""
    return create_sdk_mcp_server("claw_orchestrator", tools=ALL_TOOLS)
