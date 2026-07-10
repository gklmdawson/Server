"""SQLAlchemy models and session plumbing for the coordinator.

Time convention: all datetimes are stored as NAIVE UTC (`utcnow()` below).
SQLite drops timezone info anyway, so mixing aware and naive values would blow
up comparisons — instead the coordinator is uniformly naive-UTC internally and
serializes with an explicit 'Z' suffix at the API boundary (`iso_z`).

The coordinator stamps every timestamp itself; workstation clocks are never
trusted.
"""
from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from shared.schemas import JobStatus, ProjectStatus


def utcnow() -> datetime:
    """Naive UTC now — the single time source for everything stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a stored naive-UTC datetime as ISO-8601 with a Z suffix."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def new_uuid() -> str:
    return str(uuidlib.uuid4())


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200))
    client: Mapped[str] = mapped_column(String(200), default="")
    project_number: Mapped[str] = mapped_column(String(100), default="")
    sensor_type: Mapped[str] = mapped_column(String(50), default="")
    root_path: Mapped[str] = mapped_column(Text, default="")
    date_folder: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.ACTIVE.value)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="project")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=new_uuid)
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED.value, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    depends_on_json: Mapped[list] = mapped_column(JSON, default=list)   # list of job uuids
    parameters_json: Mapped[dict] = mapped_column(JSON, default=dict)   # opaque to the coordinator
    assigned_node: Mapped[str] = mapped_column(String(100), default="", index=True)
    scratch_path: Mapped[str] = mapped_column(Text, default="")         # reserved (DESIGN §12.1)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)
    assign_attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_runtime_minutes: Mapped[int] = mapped_column(Integer, default=1440)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_progress_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    progress_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    processor_version: Mapped[str] = mapped_column(String(50), default="")
    agent_version: Mapped[str] = mapped_column(String(50), default="")

    project: Mapped[Optional[Project]] = relationship(back_populates="jobs")
    events: Mapped[list["JobEvent"]] = relationship(back_populates="job")


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    capabilities_json: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    draining: Mapped[bool] = mapped_column(Boolean, default=False)
    agent_version: Mapped[str] = mapped_column(String(50), default="")
    computer_name: Mapped[str] = mapped_column(String(100), default="")
    current_user: Mapped[str] = mapped_column(String(100), default="")
    accepting_jobs: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_telemetry_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def is_online(self, now: datetime, offline_after_seconds: int) -> bool:
        if self.last_sync_at is None:
            return False
        return (now - self.last_sync_at).total_seconds() < offline_after_seconds


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    node_name: Mapped[str] = mapped_column(String(100), default="")

    job: Mapped[Job] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------

def make_engine(db_path: str) -> Engine:
    url = db_path if "://" in db_path else f"sqlite:///{db_path}"
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def log_event(
    session,
    job: Job,
    type_: str,
    message: str = "",
    details: Optional[dict[str, Any]] = None,
    node_name: str = "",
) -> JobEvent:
    ev = JobEvent(job_id=job.id, type=type_, message=message,
                  details_json=details or {}, node_name=node_name, ts=utcnow())
    session.add(ev)
    return ev
