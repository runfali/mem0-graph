"""Tests for the RECALL funnel block in GET /evolve/report (T6c).

Aggregates per-stage average hit counts and latency from the last 7 days of
evolve_queries.trace, skipping NULL traces and rows outside the window.
"""

import os
import uuid
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-recall")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base, get_db  # noqa: E402
from models import EvolveQuery, User  # noqa: E402
from routers import evolve as evolve_router  # noqa: E402


def _trace(depth="full", **stage_counts):
    return {
        "depth": depth,
        "stages": [
            {"stage": s, "count": c, "latency_ms": lat}
            for s, (c, lat) in stage_counts.items()
        ],
    }


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

    app = FastAPI()
    app.include_router(evolve_router.router)
    def _fake_admin():
        return User(
            id=uuid.UUID(int=1),
            name="test-admin",
            email="admin@test.local",
            password_hash="",
            role="admin",
        )

    app.dependency_overrides[require_auth] = _fake_admin

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    from unittest.mock import patch

    with patch.object(
        evolve_router,
        "get_current_config",
        return_value={"vector_store": {"config": {"collection_name": "mem0_memories"}}},
    ):
        yield TestClient(app), TestingSessionLocal


def _get(client):
    resp = client[0].get("/evolve/report", params={"days": 30})
    assert resp.status_code == 200
    return resp.json()["recall"]


def test_recall_empty_when_no_trace(client):
    recall = _get(client)
    assert recall == {"stages": [], "recent": []}


def test_recall_aggregates_stage_averages(client):
    now = datetime.now(timezone.utc)
    with client[1]() as db:
        db.add_all(
            [
                EvolveQuery(
                    query="qA",
                    result_count=5,
                    avg_score=0.8,
                    latency_ms=100,
                    created_at=now - timedelta(days=2),
                    trace=_trace(
                        candidates=(42, 12.3),
                        threshold=(8, 1.2),
                        decay=(8, 0.0),
                        graph=(0, 0.0),
                        temporal=(0, 0.0),
                        rerank=(5, 3.4),
                        final=(5, 0.5),
                    ),
                ),
                EvolveQuery(
                    query="qB",
                    result_count=4,
                    avg_score=0.7,
                    latency_ms=90,
                    created_at=now - timedelta(days=1),
                    trace=_trace(
                        candidates=(20, 10.0),
                        threshold=(6, 2.0),
                        decay=(6, 0.0),
                        graph=(1, 1.0),
                        temporal=(1, 0.5),
                        rerank=(4, 2.0),
                        final=(4, 1.0),
                    ),
                ),
            ]
        )
        db.commit()

    recall = _get(client)
    assert set(recall) == {"stages", "recent"}

    assert [s["stage"] for s in recall["stages"]] == [
        "candidates",
        "threshold",
        "decay",
        "graph",
        "temporal",
        "rerank",
        "final",
    ]
    by_stage = {s["stage"]: s for s in recall["stages"]}
    assert by_stage["candidates"]["avg_count"] == pytest.approx(31)
    assert by_stage["candidates"]["avg_latency_ms"] == pytest.approx(11.15)
    assert by_stage["threshold"]["avg_count"] == pytest.approx(7)
    assert by_stage["threshold"]["avg_latency_ms"] == pytest.approx(1.6)
    assert by_stage["decay"]["avg_count"] == pytest.approx(7)
    assert by_stage["graph"]["avg_count"] == pytest.approx(0.5)
    assert by_stage["graph"]["avg_latency_ms"] == pytest.approx(0.5)
    assert by_stage["temporal"]["avg_count"] == pytest.approx(0.5)
    assert by_stage["temporal"]["avg_latency_ms"] == pytest.approx(0.25)
    assert by_stage["rerank"]["avg_count"] == pytest.approx(4.5)
    assert by_stage["final"]["avg_count"] == pytest.approx(4.5)
    assert by_stage["final"]["avg_latency_ms"] == pytest.approx(0.75)

    assert len(recall["recent"]) == 2
    assert recall["recent"][0]["query"] == "qB"
    assert recall["recent"][1]["query"] == "qA"
    assert [s["stage"] for s in recall["recent"][0]["stages"]] == [
        "candidates",
        "threshold",
        "decay",
        "graph",
        "temporal",
        "rerank",
        "final",
    ]


def test_recall_skips_null_and_out_of_window_traces(client):
    now = datetime.now(timezone.utc)
    with client[1]() as db:
        db.add_all(
            [
                EvolveQuery(
                    query="fresh",
                    result_count=3,
                    created_at=now - timedelta(days=1),
                    trace=_trace(candidates=(30, 8.0), threshold=(5, 1.0), final=(4, 0.5)),
                ),
                EvolveQuery(
                    query="no-trace",
                    result_count=2,
                    created_at=now - timedelta(days=1),
                    trace=None,
                ),
                EvolveQuery(
                    query="stale",
                    result_count=4,
                    created_at=now - timedelta(days=10),
                    trace=_trace(candidates=(99, 20.0), final=(9, 2.0)),
                ),
            ]
        )
        db.commit()

    recall = _get(client)
    by_stage = {s["stage"]: s for s in recall["stages"]}
    assert by_stage["candidates"]["avg_count"] == pytest.approx(30)
    assert by_stage["final"]["avg_count"] == pytest.approx(4)
    assert [s["stage"] for s in recall["stages"]] == ["candidates", "threshold", "final"]
    assert [r["query"] for r in recall["recent"]] == ["fresh"]
