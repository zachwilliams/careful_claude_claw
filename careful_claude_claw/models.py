import math
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MemoryType(StrEnum):
    PREFERENCE = "preference"
    DECISION = "decision"
    OBSERVATION = "observation"
    PROCEDURE = "procedure"


# Decay rates per type (days)
MEMORY_DECAY_RATES: dict[MemoryType, float] = {
    MemoryType.PREFERENCE: 0.0,  # permanent
    MemoryType.DECISION: 0.05,  # very slow
    MemoryType.OBSERVATION: 0.3,  # weeks
    MemoryType.PROCEDURE: 0.0,  # permanent
}

# Type weights for scoring
MEMORY_TYPE_WEIGHTS: dict[MemoryType, float] = {
    MemoryType.PREFERENCE: 1.2,
    MemoryType.DECISION: 1.1,
    MemoryType.OBSERVATION: 0.8,
    MemoryType.PROCEDURE: 1.0,
}


class MemorySource(StrEnum):
    USER = "user"
    AGENT = "agent"
    EXPLICIT = "explicit"
    CONSOLIDATION = "consolidation"


class Memory(BaseModel):
    """A persistent memory entry for cross-session context."""

    id: int | None = None
    memory_type: MemoryType
    content: str
    source: MemorySource = MemorySource.EXPLICIT
    source_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    metadata: dict | None = None
    importance: float = 0.5
    decay_rate: float = 0.0
    access_count: int = 0
    last_accessed_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    expires_at: datetime | None = None
    is_active: bool = True


def score_memory(memory: Memory, query_relevance: float = 1.0) -> float:
    """Composite scoring: relevance * recency * importance * type_weight."""
    age_days = (datetime.now() - memory.updated_at).total_seconds() / 86400
    recency = math.exp(-memory.decay_rate * age_days)
    type_weight = MEMORY_TYPE_WEIGHTS.get(memory.memory_type, 1.0)
    return query_relevance * recency * memory.importance * type_weight


class RequestType(StrEnum):
    COMMAND = "command"
    MEMORY_QUERY = "memory_query"
    MEMORY_ADD = "memory_add"
    TASK = "task"
    FOLLOW_UP = "follow_up"


class OrchestratorResult(BaseModel):
    """Result of an orchestrator request."""

    request_type: RequestType
    response: str = ""
    execution: "Execution | None" = None


class OrchestratorState(BaseModel):
    """Persistent state for the orchestrator agent."""

    session_id: str | None = None
    is_awake: bool = False
    last_wake_at: datetime | None = None
    last_sleep_at: datetime | None = None
    core_briefing: str = ""
    total_messages_handled: int = 0


class Job(BaseModel):
    """A task definition that can be one-shot or recurring (cron)."""

    name: str
    task: str = ""
    skill_name: str | None = None
    cron_expr: str | None = None
    cwd: str | None = None
    enabled: bool = True
    allowed_tools: list[str] | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class Execution(BaseModel):
    """A single run of a job (manual or scheduled)."""

    id: int | None = None
    job_name: str
    agent_name: str
    status: JobStatus = JobStatus.PENDING
    attempt: int = 1
    started_at: datetime | None = None
    ended_at: datetime | None = None
    output: str | None = None
    error: str | None = None


class SkillScope(StrEnum):
    GLOBAL = "global"


class Skill(BaseModel):
    name: str
    scope: SkillScope = SkillScope.GLOBAL
    description: str = ""
    file_path: str = ""
