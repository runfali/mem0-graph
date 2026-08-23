"""Tests for the evolve report endpoint (GET /evolve/report)."""

import os
import uuid
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve-report")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    EvolveFeedback,
    EvolveQuery,
    EvolveSalience,
    EvolveSalienceAdjustment,
    RequestLog,
    User,
)
from routers import evolve as evolve_router  # noqa: E402
from sqlalchemy import text  # noqa: E402


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # mem0_memories is the pgvector collection table (not a SQLAlchemy model here).
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


def _get(client, **params):
    return client[0].get("/evolve/report", params=params)


def _now():
    return datetime.now(timezone.utc)


def test_empty_db_returns_full_structure(client):
    resp = _get(client)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"search_quality", "feedback", "heat", "operations", "recall"}

    sq = data["search_quality"]
    assert set(sq) == {"windows", "daily_trend", "top_zero_hits"}
    assert set(sq["windows"]) == {"7", "30"}
    for window in sq["windows"].values():
        assert window == {"total_queries": 0, "zero_hit_rate": 0, "avg_score": 0, "avg_latency_ms": 0}
    assert len(sq["daily_trend"]) == 7
    for day in sq["daily_trend"]:
        assert set(day) == {"date", "queries", "avg_score", "zero_hits"}
        assert day["queries"] == 0 and day["avg_score"] == 0 and day["zero_hits"] == 0
    assert sq["top_zero_hits"] == []

    fb = data["feedback"]
    assert fb["type_distribution"] == {"useful": 0, "useless": 0, "correction": 0}
    assert fb["most_corrected"] == []

    heat = data["heat"]
    assert heat["score_distribution"] == {"lt_0.5": 0, "0.5_0.9": 0, "0.9_1.1": 0, "gt_1.1": 0}
    assert heat["high_frequency"] == []
    assert heat["stale"] == []
    assert heat["boost_adjustments"] == []

    ops = data["operations"]
    assert set(ops) == {"windows"}
    assert set(ops["windows"]) == {"7", "30"}
    for window in ops["windows"].values():
        assert window == {"total_requests": 0, "avg_latency_ms": 0, "success_rate": 0}

    assert data["recall"] == {"stages": [], "recent": []}


def test_seeded_data_populates_panels(client):
    now = _now()
    h1 = now - timedelta(hours=1)
    d2 = now - timedelta(days=2)
    d20 = now - timedelta(days=20)

    with client[1]() as db:
        db.execute(text("INSERT INTO mem0_memories (id) VALUES ('cold'), ('never')"))
        db.add_all(
            [
                EvolveQuery(query="qz", result_count=0, is_zero_hit=True, avg_score=None, latency_ms=40, created_at=h1),
                EvolveQuery(query="q1", result_count=0, is_zero_hit=True, avg_score=None, latency_ms=50, created_at=d2),
                EvolveQuery(query="q2", result_count=3, is_zero_hit=False, avg_score=0.8, latency_ms=100, created_at=d2),
                EvolveQuery(query="q3", result_count=2, is_zero_hit=False, avg_score=0.6, latency_ms=80, created_at=d20),
            ]
        )
        db.add_all(
            [
                EvolveFeedback(memory_id="m_a", feedback_type="useful", created_at=h1),
                EvolveFeedback(memory_id="m_b", feedback_type="useless", created_at=d2),
                EvolveFeedback(memory_id="m_c", feedback_type="correction", created_at=d2),
                EvolveFeedback(memory_id="m_c", feedback_type="correction", created_at=h1),
                EvolveFeedback(memory_id="m_b", feedback_type="correction", created_at=h1),
            ]
        )
        db.add_all(
            [
                EvolveSalience(memory_id="hot", salience_score=1.0, access_count=50, last_access_at=h1),
                EvolveSalience(memory_id="boosted", salience_score=1.2, access_count=20, last_access_at=h1),
                EvolveSalience(memory_id="warm", salience_score=0.6, access_count=10, last_access_at=h1),
                EvolveSalience(memory_id="cold", salience_score=0.4, access_count=2, last_access_at=d20),
                EvolveSalience(memory_id="never", salience_score=1.0, access_count=0, last_access_at=None),
            ]
        )
        db.add_all(
            [
                EvolveSalienceAdjustment(memory_id="boosted", delta=0.05, reason="evolve_boost", created_at=h1),
                EvolveSalienceAdjustment(memory_id="oldboost", delta=0.02, reason="evolve_boost", created_at=d20),
                EvolveSalienceAdjustment(memory_id="feedbackonly", delta=-0.05, reason="useless", created_at=h1),
                EvolveSalienceAdjustment(
                    memory_id="toostale", delta=0.01, reason="evolve_boost",
                    created_at=now - timedelta(days=40),
                ),
            ]
        )
        db.add_all(
            [
                RequestLog(method="POST", path="/search", status_code=200, latency_ms=60, auth_type="api_key", created_at=h1),
                RequestLog(method="POST", path="/search", status_code=404, latency_ms=90, auth_type="api_key", created_at=h1),
                RequestLog(method="POST", path="/search", status_code=500, latency_ms=120, auth_type="api_key", created_at=d2),
                RequestLog(method="POST", path="/search", status_code=200, latency_ms=30, auth_type="api_key", created_at=d20),
            ]
        )
        db.commit()

    data = _get(client).json()
    sq = data["search_quality"]

    w7 = sq["windows"]["7"]
    assert w7["total_queries"] == 3
    assert w7["zero_hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert w7["avg_score"] == pytest.approx(0.8)
    assert w7["avg_latency_ms"] == pytest.approx((40 + 50 + 100) / 3, abs=1e-4)

    w30 = sq["windows"]["30"]
    assert w30["total_queries"] == 4
    assert w30["zero_hit_rate"] == pytest.approx(0.5)
    assert w30["avg_score"] == pytest.approx(0.7, abs=1e-4)
    assert w30["avg_latency_ms"] == pytest.approx((40 + 50 + 100 + 80) / 4, abs=1e-4)

    trend = {day["date"]: day for day in sq["daily_trend"]}
    today_str = now.date().isoformat()
    d2_str = (now.date() - timedelta(days=2)).isoformat()
    assert trend[today_str]["queries"] == 1
    assert trend[today_str]["zero_hits"] == 1
    assert trend[today_str]["avg_score"] == 0
    assert trend[d2_str]["queries"] == 2
    assert trend[d2_str]["zero_hits"] == 1
    assert trend[d2_str]["avg_score"] == pytest.approx(0.8)
    other = [day for day in sq["daily_trend"] if day["date"] not in {today_str, d2_str}]
    assert all(day["queries"] == 0 for day in other)

    assert sq["top_zero_hits"] == [{"query": "qz", "count": 1}]

    fb = data["feedback"]
    assert fb["type_distribution"] == {"useful": 1, "useless": 1, "correction": 3}
    assert fb["most_corrected"] == [
        {"memory_id": "m_c", "count": 2},
        {"memory_id": "m_b", "count": 1},
    ]

    heat = data["heat"]
    assert heat["score_distribution"] == {"lt_0.5": 1, "0.5_0.9": 1, "0.9_1.1": 2, "gt_1.1": 1}
    assert [m["memory_id"] for m in heat["high_frequency"][:3]] == ["hot", "boosted", "warm"]
    assert heat["high_frequency"][0]["access_count"] == 50
    assert heat["high_frequency"][1]["salience_score"] == pytest.approx(1.2)

    stale_ids = {m["memory_id"] for m in heat["stale"]}
    assert stale_ids == {"cold", "never"}
    by_id = {m["memory_id"]: m for m in heat["stale"]}
    assert by_id["cold"]["access_count"] == 2
    # sqlite stores datetimes without tz offset, so the round-trip is naive
    assert by_id["cold"]["last_access_at"] == d20.replace(tzinfo=None).isoformat()
    assert by_id["never"]["last_access_at"] is None

    boosts = [(m["memory_id"], m["delta"]) for m in heat["boost_adjustments"]]
    assert boosts == [("boosted", pytest.approx(0.05)), ("oldboost", pytest.approx(0.02))]

    ops = data["operations"]
    assert ops["windows"]["7"] == {
        "total_requests": 3,
        "avg_latency_ms": pytest.approx(90, abs=1e-4),
        "success_rate": pytest.approx(2 / 3, abs=1e-4),
    }
    assert ops["windows"]["30"] == {
        "total_requests": 4,
        "avg_latency_ms": pytest.approx(75, abs=1e-4),
        "success_rate": pytest.approx(0.75),
    }


def test_stale_excludes_orphan_salience(client):
    """Salience rows whose memory no longer exists in the vector store are ghosts."""
    now = _now()
    with client[1]() as db:
        db.execute(text("INSERT INTO mem0_memories (id) VALUES ('live')"))
        db.add_all(
            [
                EvolveSalience(memory_id="live", salience_score=0.5, access_count=1, last_access_at=now - timedelta(days=20)),
                EvolveSalience(memory_id="ghost", salience_score=0.5, access_count=1, last_access_at=now - timedelta(days=20)),
            ]
        )
        db.commit()

    stale_ids = {m["memory_id"] for m in _get(client).json()["heat"]["stale"]}

    assert "live" in stale_ids
    assert "ghost" not in stale_ids


def test_days_param_narrows_windows(client):
    now = _now()
    with client[1]() as db:
        db.add_all(
            [
                EvolveQuery(query="old", result_count=1, is_zero_hit=False, avg_score=0.5, latency_ms=10, created_at=now - timedelta(days=20)),
                EvolveQuery(query="new", result_count=1, is_zero_hit=False, avg_score=0.9, latency_ms=20, created_at=now - timedelta(days=2)),
            ]
        )
        db.commit()

    data7 = _get(client, days=7).json()
    sq7 = data7["search_quality"]
    assert set(sq7["windows"]) == {"7"}
    assert sq7["windows"]["7"]["total_queries"] == 1
    assert len(sq7["daily_trend"]) == 7

    data30 = _get(client).json()
    sq30 = data30["search_quality"]
    assert set(sq30["windows"]) == {"7", "30"}
    assert sq30["windows"]["30"]["total_queries"] == 2

    data1 = _get(client, days=1).json()
    sq1 = data1["search_quality"]
    assert set(sq1["windows"]) == {"1"}
    assert len(sq1["daily_trend"]) == 1


def test_invalid_days_rejected(client):
    assert _get(client, days=0).status_code == 422
    assert _get(client, days=91).status_code == 422


class _FakeTrendResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DateKeySession:
    """Delegates to a real session, but returns the daily_trend aggregation
    (first query) with ``datetime.date`` keys, simulating PostgreSQL where
    ``func.date()`` yields a date object instead of sqlite's string."""

    def __init__(self, real, trend_rows):
        self._real = real
        self._trend_rows = trend_rows
        self._first = True

    def execute(self, stmt, *args, **kwargs):
        if self._first:
            self._first = False
            return _FakeTrendResult(self._trend_rows)
        return self._real.execute(stmt, *args, **kwargs)


def test_daily_trend_matches_date_object_keys(client):
    now = _now()
    today = now.date()
    two_days_ago = today - timedelta(days=2)
    trend_rows = [
        (today, 3, 0.6, 1),
        (two_days_ago, 2, 0.8, 0),
    ]
    with client[1]() as real:
        report = evolve_router.evolve_report(
            _auth=None, db=_DateKeySession(real, trend_rows), days=7
        )

    trend = {day["date"]: day for day in report["search_quality"]["daily_trend"]}
    assert trend[today.isoformat()]["queries"] == 3
    assert trend[today.isoformat()]["avg_score"] == pytest.approx(0.6)
    assert trend[today.isoformat()]["zero_hits"] == 1
    assert trend[two_days_ago.isoformat()]["queries"] == 2
    assert trend[two_days_ago.isoformat()]["avg_score"] == pytest.approx(0.8)
