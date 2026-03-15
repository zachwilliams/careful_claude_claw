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
