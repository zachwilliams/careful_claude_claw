"""Tests for the memory layer (CRUD, FTS5, tags, expiration, scoring)."""

from datetime import datetime, timedelta

import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.memory import (
    add_memory,
    delete_memory,
    expire_old_memories,
    get_memory,
    get_relevant_memories,
    list_memories,
    search_memories,
    update_memory,
)
from careful_claude_claw.models import Memory, MemorySource, MemoryType, score_memory


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


# --- CRUD ---


def test_add_and_get_memory():
    mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="User prefers Python 3.11",
        source=MemorySource.USER,
    )
    result = add_memory(mem)
    assert result.id is not None
    assert isinstance(result.id, int)

    fetched = get_memory(mem.id)
    assert fetched is not None
    assert fetched.content == "User prefers Python 3.11"
    assert fetched.memory_type == MemoryType.OBSERVATION
    assert fetched.source == MemorySource.USER
    assert fetched.is_active is True


def test_get_nonexistent_memory():
    assert get_memory(99999) is None


def test_update_memory():
    mem = Memory(
        memory_type=MemoryType.PREFERENCE,
        content="Likes dark mode",
    )
    add_memory(mem)

    mem.content = "Likes dark mode with blue accents"
    updated = update_memory(mem)
    assert updated.content == "Likes dark mode with blue accents"

    fetched = get_memory(mem.id)
    assert fetched.content == "Likes dark mode with blue accents"


def test_delete_memory_soft():
    mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Temporary fact",
    )
    add_memory(mem)
    assert get_memory(mem.id) is not None

    delete_memory(mem.id)
    # Soft deleted — get_memory returns None (filters is_active=1)
    assert get_memory(mem.id) is None


def test_list_memories():
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Fact 1"))
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Pref 1"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Fact 2"))

    all_mems = list_memories()
    assert len(all_mems) == 3

    observations = list_memories(memory_type=MemoryType.OBSERVATION)
    assert len(observations) == 2
    assert all(m.memory_type == MemoryType.OBSERVATION for m in observations)

    prefs = list_memories(memory_type=MemoryType.PREFERENCE)
    assert len(prefs) == 1


def test_list_memories_limit():
    for i in range(10):
        add_memory(Memory(memory_type=MemoryType.OBSERVATION, content=f"Fact {i}"))

    limited = list_memories(limit=5)
    assert len(limited) == 5


def test_list_memories_empty():
    assert list_memories() == []


# --- FTS5 Search ---


def test_search_memories_fts():
    add_memory(
        Memory(memory_type=MemoryType.OBSERVATION, content="User works with PostgreSQL databases")
    )
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="User enjoys hiking on weekends"))
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="Prefers SQLite for small projects",
        )
    )

    results = search_memories(query="database")
    # Should find PostgreSQL mention
    assert len(results) >= 1
    assert any("PostgreSQL" in m.content for m in results)


def test_search_memories_no_results():
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="User likes Python"))
    results = search_memories(query="javascript")
    assert len(results) == 0


def test_search_memories_with_type_filter():
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Uses vim editor"))
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Prefers vim keybindings"))

    results = search_memories(query="vim", memory_type=MemoryType.PREFERENCE)
    assert len(results) == 1
    assert results[0].memory_type == MemoryType.PREFERENCE


def test_search_memories_with_category():
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="Dark mode",
            category="ui",
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.PREFERENCE,
            content="Concise responses",
            category="communication",
        )
    )

    results = search_memories(category="ui")
    assert len(results) == 1
    assert results[0].content == "Dark mode"


# --- Tag Filtering ---


def test_search_memories_with_tags():
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Knows Python and Rust",
            tags=["python", "rust", "languages"],
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Knows TypeScript",
            tags=["typescript", "languages"],
        )
    )

    results = search_memories(tags=["python"])
    assert len(results) == 1
    assert "Python" in results[0].content

    results = search_memories(tags=["languages"])
    assert len(results) == 2


def test_search_memories_multiple_tags():
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Python web dev",
            tags=["python", "web"],
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Python data science",
            tags=["python", "data"],
        )
    )

    results = search_memories(tags=["python", "web"])
    assert len(results) == 1
    assert "web" in results[0].content.lower()


# --- Expiration ---


def test_cleanup_expired_memories():
    # Active, not expired
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Still valid"))

    # Expired
    expired_mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Old task context",
        expires_at=datetime.now() - timedelta(hours=1),
    )
    add_memory(expired_mem)

    # Future expiration
    future_mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Future task context",
        expires_at=datetime.now() + timedelta(days=1),
    )
    add_memory(future_mem)

    count = expire_old_memories()
    assert count == 1

    # Expired one should be soft-deleted
    assert get_memory(expired_mem.id) is None
    # Others should still be active
    assert get_memory(future_mem.id) is not None
    all_mems = list_memories()
    assert len(all_mems) == 2


def test_expired_memory_not_returned_by_search():
    """Expired and soft-deleted memory should not appear in search results."""
    mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Expired observation about databases",
        expires_at=datetime.now() - timedelta(hours=1),
    )
    add_memory(mem)
    expire_old_memories()

    results = search_memories(query="databases")
    assert len(results) == 0


# --- Relevant Memories ---


def test_get_relevant_memories_includes_preferences():
    add_memory(Memory(memory_type=MemoryType.PREFERENCE, content="Prefers concise code"))
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Works on web projects"))

    results = get_relevant_memories("anything")
    # Should always include preferences
    pref_results = [m for m in results if m.memory_type == MemoryType.PREFERENCE]
    assert len(pref_results) >= 1


def test_get_relevant_memories_fts_match():
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="The project uses FastAPI for the backend",
        )
    )
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="User has a cat named Whiskers"))

    results = get_relevant_memories("FastAPI backend")
    contents = [m.content for m in results]
    assert any("FastAPI" in c for c in contents)


def test_get_relevant_memories_scored_ranking():
    """Higher importance memories should rank higher."""
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="Low importance Python fact",
            importance=0.1,
        )
    )
    add_memory(
        Memory(
            memory_type=MemoryType.OBSERVATION,
            content="High importance Python decision",
            importance=0.9,
        )
    )

    results = get_relevant_memories("Python")
    if len(results) >= 2:
        # Higher importance should come first
        assert results[0].importance >= results[1].importance


def test_get_relevant_memories_records_access():
    """Verify access_count and last_accessed_at are updated after retrieval."""
    mem = Memory(
        memory_type=MemoryType.PREFERENCE,
        content="User prefers TypeScript",
        importance=0.8,
    )
    add_memory(mem)

    # Before retrieval
    fetched_before = get_memory(mem.id)
    assert fetched_before.access_count == 0

    get_relevant_memories("TypeScript")

    # After retrieval
    fetched_after = get_memory(mem.id)
    assert fetched_after.access_count >= 1
    assert fetched_after.last_accessed_at is not None


# --- Memory with metadata ---


def test_memory_with_metadata():
    mem = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Has metadata",
        metadata={"confidence": 0.9, "source_url": "https://example.com"},
    )
    add_memory(mem)

    fetched = get_memory(mem.id)
    assert fetched.metadata is not None
    assert fetched.metadata["confidence"] == 0.9


# --- Decay rate defaults ---


def test_add_memory_sets_decay_rate():
    mem = Memory(memory_type=MemoryType.OBSERVATION, content="test")
    add_memory(mem)
    assert mem.decay_rate == 0.3  # OBSERVATION default

    pref = Memory(memory_type=MemoryType.PREFERENCE, content="test pref")
    add_memory(pref)
    assert pref.decay_rate == 0.0  # PREFERENCE is permanent


# --- Scoring ---


def test_score_observation_decays_over_time():
    """Old observation should score lower than fresh one."""
    fresh = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Fresh obs",
        importance=0.5,
        decay_rate=0.3,
        updated_at=datetime.now(),
    )
    old = Memory(
        memory_type=MemoryType.OBSERVATION,
        content="Old obs",
        importance=0.5,
        decay_rate=0.3,
        updated_at=datetime.now() - timedelta(days=30),
    )

    assert score_memory(fresh) > score_memory(old)


def test_score_preference_never_decays():
    """Preference score should be same regardless of age."""
    fresh = Memory(
        memory_type=MemoryType.PREFERENCE,
        content="Fresh pref",
        importance=0.5,
        decay_rate=0.0,
        updated_at=datetime.now(),
    )
    old = Memory(
        memory_type=MemoryType.PREFERENCE,
        content="Old pref",
        importance=0.5,
        decay_rate=0.0,
        updated_at=datetime.now() - timedelta(days=365),
    )

    assert score_memory(fresh) == pytest.approx(score_memory(old), abs=0.001)


def test_fts_special_characters():
    """Search with punctuation should not crash."""
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Uses C++ and C#"))
    # These should not raise
    results = search_memories(query="C++")
    assert isinstance(results, list)
    results = search_memories(query="what's up?")
    assert isinstance(results, list)


def test_fts_partial_match():
    """FTS should match partial words like 'data' matching 'database'."""
    add_memory(Memory(memory_type=MemoryType.OBSERVATION, content="Uses PostgreSQL database daily"))
    results = search_memories(query="data*")
    # FTS5 prefix queries use *
    assert len(results) >= 1
