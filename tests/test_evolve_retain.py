"""Tests for the "点点点决策" actions:
- POST /evolve/memory/{memory_id}/retain (manual keep / review marker)
- DELETE /memories/{memory_id} cascades evolve_salience cleanup
"""

import os
import uuid
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-retain")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# server/ modules use bare imports (from db import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base, get_db  # noqa: E402
from evolve_cleanup import register_delete_cleanup  # noqa: E402
from models import EvolveFeedback, EvolveSalience, EvolveSalienceAdjustment, User  # noqa: E402

# main.py builds a real Memory (pgvector → postgres) at import time via
# initialize_state; stub it out BEFORE importing main so collection succeeds
# without a live DB.
import server_state  # noqa: E402


class _FakeMemory:
    def __init__(self, session_factory=None, delete_error=None):
        self._delete_error = delete_error
        self.on_memory_deleted = None
        if session_factory is not None:
            register_delete_cleanup(self, session_factory)

    def delete(self, memory_id, **kwargs):
        if self._delete_error:
            raise self._delete_error
        if self.on_memory_deleted is not None:
            self.on_memory_deleted(memory_id)
        return True


server_state.initialize_state = lambda config: None
server_state.get_memory_instance = lambda: _FakeMemory()

from fastapi.testclient import TestClient  # noqa: E402
import main as server_main  # noqa: E402


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE mem0_memories (id VARCHAR(36) PRIMARY KEY)"))
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    app = server_main.app
    def _fake_admin():
        return User(
            id=uuid.UUID(int=1),
            name="test-admin",
            email="admin@test.local",
            password_hash="",
            role="admin",
        )

    app.dependency_overrides[require_auth] = _fake_admin
    # _persist_evolve_* write via main's module-level SessionLocal (postgres in
    # prod); point it at the test DB so rows land in sqlite.
    server_main.SessionLocal = TestingSessionLocal
    server_main.get_memory_instance = lambda: _FakeMemory(session_factory=TestingSessionLocal)

    # Endpoint handlers use get_db (from db.py), not main's SessionLocal, so the
    # dependency must be overridden too.
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    from routers import evolve as evolve_router
    from unittest.mock import patch

    with patch.object(
        evolve_router,
        "get_current_config",
        return_value={"vector_store": {"config": {"collection_name": "mem0_memories"}}},
    ):
        yield TestClient(app, raise_server_exceptions=False), TestingSessionLocal


def _now():
    return datetime.now(timezone.utc)


def _retain(client, memory_id):
    return client[0].post(f"/evolve/memory/{memory_id}/retain")


def test_retain_updates_last_access_at(client):
    old = _now() - timedelta(days=30)
    with client[1]() as db:
        db.add(EvolveSalience(memory_id="m1", salience_score=0.6, access_count=2, last_access_at=old))
        db.commit()

    resp = _retain(client, "m1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["memory_id"] == "m1"
    assert "last_access_at" in body

    with client[1]() as db:
        row = db.get(EvolveSalience, "m1")
        assert row.last_access_at is not None
        # sqlite round-trips naive; normalize before comparing
        assert abs((row.last_access_at.replace(tzinfo=timezone.utc) - _now()).total_seconds()) < 10


def test_retain_drops_memory_from_stale_report(client):
    old = _now() - timedelta(days=30)
    with client[1]() as db:
        db.execute(text("INSERT INTO mem0_memories (id) VALUES ('m2')"))
        db.add(EvolveSalience(memory_id="m2", salience_score=1.0, access_count=1, last_access_at=old))
        db.commit()

    stale_before = {
        m["memory_id"] for m in client[0].get("/evolve/report").json()["heat"]["stale"]
    }
    assert "m2" in stale_before

    assert _retain(client, "m2").status_code == 200
    stale_after = {
        m["memory_id"] for m in client[0].get("/evolve/report").json()["heat"]["stale"]
    }
    assert "m2" not in stale_after


def test_retain_creates_row_when_missing(client):
    resp = _retain(client, "m3")
    assert resp.status_code == 200
    with client[1]() as db:
        row = db.get(EvolveSalience, "m3")
        assert row is not None
        assert row.last_access_at is not None
        assert row.access_count == 0


def test_delete_memory_cascades_evolve_salience(client):
    now = _now()
    with client[1]() as db:
        db.add_all(
            [
                EvolveSalience(memory_id="gone", salience_score=0.4, access_count=2, last_access_at=now - timedelta(days=30)),
                EvolveSalience(memory_id="kept", salience_score=0.9, access_count=5, last_access_at=now),
                EvolveSalienceAdjustment(memory_id="gone", delta=0.1, reason="evolve_boost", created_at=now),
                EvolveSalienceAdjustment(memory_id="kept", delta=0.05, reason="evolve_boost", created_at=now),
                EvolveFeedback(memory_id="gone", feedback_type="useful", created_at=now),
                EvolveFeedback(memory_id="kept", feedback_type="correction", created_at=now),
            ]
        )
        db.commit()

    resp = client[0].delete("/memories/gone")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Memory deleted successfully"}

    with client[1]() as db:
        assert db.get(EvolveSalience, "gone") is None
        assert db.scalar(
            select(EvolveSalienceAdjustment).where(EvolveSalienceAdjustment.memory_id == "gone")
        ) is None
        assert db.scalar(select(EvolveFeedback).where(EvolveFeedback.memory_id == "gone")) is None
        assert db.get(EvolveSalience, "kept") is not None
        assert db.scalar(
            select(EvolveSalienceAdjustment).where(EvolveSalienceAdjustment.memory_id == "kept")
        ) is not None
        assert db.scalar(select(EvolveFeedback).where(EvolveFeedback.memory_id == "kept")) is not None
