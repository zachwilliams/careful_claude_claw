import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    task: str
    status: JobStatus = JobStatus.PENDING
    attempt: int = 1
    started_at: datetime | None = None
    ended_at: datetime | None = None
    output: str | None = None
    error: str | None = None
