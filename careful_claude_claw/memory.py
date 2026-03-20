"""Memory layer for CarefulClaudeClaw.

Provides CRUD + search operations over the memories table,
building on the raw db functions with model conversion and convenience APIs.
"""

import json
from datetime import datetime

from .db import (
    cleanup_expired_memories,
    insert_memory,
    record_memory_access,
    search_memories_fts,
)
from .db import (
    delete_memory as db_delete_memory,
)
from .db import (
    get_memory as db_get_memory,
)
from .db import (
    list_memories as db_list_memories,
)
from .db import (
    search_memories as db_search_memories,
)
from .db import (
    update_memory as db_update_memory,
)
from .models import MEMORY_DECAY_RATES, Memory, MemorySource, MemoryType, score_memory


def _row_to_memory(row: dict) -> Memory:
    """Convert a database row dict to a Memory model."""
    tags = json.loads(row["tags"]) if row.get("tags") else []
    metadata = json.loads(row["metadata"]) if row.get("metadata") else None
    return Memory(
        id=row["id"],
        memory_type=MemoryType(row["memory_type"]),
        content=row["content"],
        source=MemorySource(row["source"]) if row.get("source") else MemorySource.EXPLICIT,
        source_id=row.get("source_id"),
        tags=tags,
        category=row.get("category"),
        metadata=metadata,
        importance=row.get("importance", 0.5),
        decay_rate=row.get("decay_rate", 0.0),
        access_count=row.get("access_count", 0),
        last_accessed_at=(
            datetime.fromisoformat(row["last_accessed_at"]) if row.get("last_accessed_at") else None
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row.get("expires_at") else None,
        is_active=bool(row["is_active"]),
    )


def add_memory(memory: Memory) -> Memory:
    """Insert a new memory and return it with its ID set."""
    # Set default decay rate from type if not explicitly set
    if memory.decay_rate == 0.0 and memory.memory_type in MEMORY_DECAY_RATES:
        memory.decay_rate = MEMORY_DECAY_RATES[memory.memory_type]
    insert_memory(memory)
    return memory


def get_memory(memory_id: int) -> Memory | None:
    """Get a single active memory by ID."""
    row = db_get_memory(memory_id)
    return _row_to_memory(row) if row else None


def update_memory(memory: Memory) -> Memory:
    """Update an existing memory. Sets updated_at to now."""
    memory.updated_at = datetime.now()
    db_update_memory(memory)
    return memory


def delete_memory(memory_id: int) -> None:
    """Soft-delete a memory."""
    db_delete_memory(memory_id)


def list_memories(
    memory_type: MemoryType | str | None = None,
    limit: int = 50,
) -> list[Memory]:
    """List active memories, optionally filtered by type."""
    type_str = str(memory_type) if memory_type else None
    rows = db_list_memories(memory_type=type_str, limit=limit)
    return [_row_to_memory(row) for row in rows]


def search_memories(
    query: str | None = None,
    memory_type: MemoryType | str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[Memory]:
    """Search memories with optional FTS5 query and filters."""
    type_str = str(memory_type) if memory_type else None
    rows = db_search_memories(
        query=query,
        memory_type=type_str,
        category=category,
        tags=tags,
        limit=limit,
    )
    return [_row_to_memory(row) for row in rows]


def get_relevant_memories(
    context: str,
    memory_types: list[MemoryType] | None = None,
    limit: int = 10,
) -> list[Memory]:
    """Retrieve memories relevant to a given context string using FTS5 + scoring.

    Always includes preferences. Uses composite scoring for ranking.
    """
    results: list[Memory] = []

    # Always include active preferences
    preferences = list_memories(memory_type=MemoryType.PREFERENCE, limit=limit)
    results.extend(preferences)

    # FTS5 search for contextually relevant memories
    fts_rows = search_memories_fts(context, limit=limit * 2)
    seen_ids = {m.id for m in results}
    for row in fts_rows:
        mem = _row_to_memory(row)
        if mem.id not in seen_ids:
            if memory_types is None or mem.memory_type in memory_types:
                results.append(mem)
                seen_ids.add(mem.id)

    # Score and sort all results
    results.sort(key=lambda m: score_memory(m), reverse=True)

    # Record access for returned memories
    for mem in results[:limit]:
        if mem.id is not None:
            record_memory_access(mem.id)

    return results[:limit]


def expire_old_memories() -> int:
    """Clean up expired memories. Returns count soft-deleted."""
    return cleanup_expired_memories()
