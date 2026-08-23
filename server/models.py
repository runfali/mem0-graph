import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    key_prefix: Mapped[str] = mapped_column(String(12))
    key_hash: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    auth_type: Mapped[str] = mapped_column(String(32), default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RefreshTokenJti(Base):
    __tablename__ = "refresh_token_jtis"

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )


class EvolveFeedback(Base):
    __tablename__ = "evolve_feedback"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    memory_id: Mapped[str] = mapped_column(String(255), index=True)
    # Row-level owner (payload user_id of the target memory) so ownership
    # checks and per-user analytics don't need vector-store re-queries.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    feedback_type: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16), default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EvolveSalience(Base):
    __tablename__ = "evolve_salience"

    memory_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Row-level owner (payload user_id of the memory), stamped by the
    # on_memory_added hook at write time and lazily on later touches.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    salience_score: Mapped[float] = mapped_column(Float, default=1.0)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_access_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )


class EvolveSalienceAdjustment(Base):
    __tablename__ = "evolve_salience_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    memory_id: Mapped[str] = mapped_column(String(255), index=True)
    delta: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(64))
    feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evolve_feedback.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EvolveQuery(Base):
    __tablename__ = "evolve_queries"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_new_uuid)
    query: Mapped[str] = mapped_column(Text)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rerank: Mapped[bool] = mapped_column(default=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    is_zero_hit: Mapped[bool] = mapped_column(default=False)
    temporal_triggered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class MemoryRefineCandidate(Base):
    __tablename__ = "memory_refine_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255))
    memory_ids: Mapped[list] = mapped_column(JSON)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    suggested_text: Mapped[list | None] = mapped_column(JSON, nullable=True)
    refined_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    refined_memory_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
    )
