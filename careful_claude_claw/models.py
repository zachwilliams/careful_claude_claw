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
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    task: str
    status: JobStatus = JobStatus.PENDING
    attempt: int = 1
    project_name: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    output: str | None = None
    error: str | None = None


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Project(BaseModel):
    name: str
    path: str
    status: ProjectStatus = ProjectStatus.ACTIVE
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class SkillScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


class Skill(BaseModel):
    name: str
    scope: SkillScope = SkillScope.GLOBAL
    project_name: str | None = None
    description: str = ""
    file_path: str = ""


class Schedule(BaseModel):
    name: str
    cron_expr: str
    task: str = ""
    skill_name: str | None = None
    project_name: str | None = None
    enabled: bool = True
    allowed_tools: list[str] | None = None
