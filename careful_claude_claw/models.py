import uuid
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
    SUMMARY = "summary"
    FACT = "fact"
    TASK_CONTEXT = "task_context"


class MemorySource(StrEnum):
    USER = "user"
    EXTRACTION = "extraction"
    EXPLICIT = "explicit"


class Memory(BaseModel):
    """A persistent memory entry for cross-session context."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    memory_type: MemoryType
    content: str
    source: MemorySource = MemorySource.EXPLICIT
    source_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    metadata: dict | None = None
    embedding: bytes | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    expires_at: datetime | None = None
    is_active: bool = True


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

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
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
