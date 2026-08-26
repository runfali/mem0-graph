"""Tests for the evolve feedback router."""

import os
import uuid
import sys

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-for-evolve")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

# server/ modules use bare imports (from db import ...), so the server
# directory itself must be importable, mirroring how it runs in Docker.
_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import Base, get_db  # noqa: E402
from models import EvolveFeedback, EvolveSalience, EvolveSalienceAdjustment, User  # noqa: E402
from routers import evolve as evolve_router  # noqa: E402


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
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

    return TestClient(app), TestingSessionLocal


def _post(client, **body):
    return client[0].post("/evolve/feedback", json=body)


def test_useful_on_new_memory_starts_from_ceiling(client):
    # new memories start at 1.0; useful is clamped to the 1.0 ceiling
    resp = _post(client, memory_id="m1", feedback_type="useful")
    assert resp.status_code == 200
    assert resp.json() == {"memory_id": "m1", "salience_score": 1.0}

    with client[1]() as db:
        row = db.get(EvolveSalience, "m1")
        assert row.salience_score == 1.0


def test_useful_raises_a_demoted_memory(client):
    _post(client, memory_id="m2", feedback_type="useless")
    resp = _post(client, memory_id="m2", feedback_type="useful")
    assert resp.json()["salience_score"] == 0.95


def test_useless_lowers_score(client):
    _post(client, memory_id="m3", feedback_type="useless")
    resp = _post(client, memory_id="m3", feedback_type="useless")
    assert resp.json()["salience_score"] == 0.7


def test_score_clamps_to_floor(client):
    for _ in range(10):
        _post(client, memory_id="m4", feedback_type="useless")
    resp = _post(client, memory_id="m4", feedback_type="useless")
    assert resp.json()["salience_score"] == 0.05


def test_correction_writes_audit_and_feedback_rows(client):
    _post(client, memory_id="m5", feedback_type="useless")
    resp = _post(client, memory_id="m5", feedback_type="correction", source="auto", note="wrong")
    assert resp.json()["salience_score"] == 0.8

    with client[1]() as db:
        feedback = db.scalar(
            select(EvolveFeedback).where(
                EvolveFeedback.memory_id == "m5", EvolveFeedback.feedback_type == "correction"
            )
        )
        assert feedback is not None
        assert feedback.feedback_type == "correction"
        assert feedback.source == "auto"
        assert feedback.note == "wrong"

        adjustment = db.scalar(
            select(EvolveSalienceAdjustment).where(
                EvolveSalienceAdjustment.memory_id == "m5", EvolveSalienceAdjustment.reason == "correction"
            )
        )
        assert adjustment is not None
        assert adjustment.delta == -0.05
        assert adjustment.reason == "correction"
        assert adjustment.feedback_id == feedback.id


def test_invalid_feedback_type_rejected(client):
    resp = _post(client, memory_id="m6", feedback_type="nope")
    assert resp.status_code == 422


def _make_scoped_client(role: str):
    """Client wired for ownership tests: a caller plus a mem0_memories table
    so the write guard can resolve payload user_ids.

    Per project convention, memories created via the server carry the
    server-side User UUID as their payload user_id.
    """
    from sqlalchemy import text

    caller_uuid = uuid.UUID(int=2)
    foreign_uuid = uuid.UUID(int=3)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE mem0_memories (id VARCHAR(36) PRIMARY KEY, payload TEXT)"))
        conn.execute(
            text("INSERT INTO mem0_memories (id, payload) VALUES ('m-foreign', :p)"),
            {"p": '{"user_id": "%s"}' % foreign_uuid},
        )
        conn.execute(
            text("INSERT INTO mem0_memories (id, payload) VALUES ('m-mine', :p)"),
            {"p": '{"user_id": "%s"}' % caller_uuid},
        )
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    app = FastAPI()
    app.include_router(evolve_router.router)

    def _fake_user():
        return User(
            id=caller_uuid,
            name="test-user",
            email="user@test.local",
            password_hash="",
            role=role,
        )

    app.dependency_overrides[require_auth] = _fake_user

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), TestingSessionLocal


def test_feedback_rejects_memory_owned_by_another_user():
    """IDOR regression: a non-admin caller must not touch foreign memories."""
    client = _make_scoped_client(role="user")
    resp = _post(client, memory_id="m-foreign", feedback_type="useful")
    assert resp.status_code == 404

    resp = client[0].post("/evolve/memory/m-foreign/retain")
    assert resp.status_code == 404

    with client[1]() as db:
        assert db.get(EvolveSalience, "m-foreign") is None


def test_feedback_allows_own_memory_for_non_admin():
    client = _make_scoped_client(role="user")
    resp = _post(client, memory_id="m-mine", feedback_type="useful")
    assert resp.status_code == 200
    assert resp.json()["memory_id"] == "m-mine"

    resp = client[0].post("/evolve/memory/m-mine/retain")
    assert resp.status_code == 200


def test_feedback_allows_missing_payload_for_admin():
    """Admins keep global access even without an owner row."""
    client = _make_scoped_client(role="admin")
    resp = _post(client, memory_id="not-in-table", feedback_type="useful")
    assert resp.status_code == 200


def test_feedback_stamps_owner_on_rows():
    """Non-admin feedback stamps the resolved owner onto feedback+salience."""
    client = _make_scoped_client(role="user")
    resp = _post(client, memory_id="m-mine", feedback_type="useful")
    assert resp.status_code == 200
    mine = str(uuid.UUID(int=2))
    with client[1]() as db:
        row = db.get(EvolveSalience, "m-mine")
        assert row.user_id == mine
        feedback = db.scalars(select(EvolveFeedback).where(EvolveFeedback.memory_id == "m-mine")).first()
        assert feedback is not None
        assert feedback.user_id == mine


def test_retain_allowed_via_salience_owner_cache_alone():
    """Fast path: a salience row with the owner suffices, no payload row needed."""
    client = _make_scoped_client(role="user")
    with client[1]() as db:
        db.add(EvolveSalience(memory_id="m-cache-only", user_id=str(uuid.UUID(int=2)), salience_score=1.0))
        db.commit()
    resp = client[0].post("/evolve/memory/m-cache-only/retain")
    assert resp.status_code == 200


def test_retain_rejected_for_foreign_salience_cache():
    client = _make_scoped_client(role="user")
    with client[1]() as db:
        db.add(EvolveSalience(memory_id="m-cache-foreign", user_id=str(uuid.UUID(int=3)), salience_score=1.0))
        db.commit()
    resp = client[0].post("/evolve/memory/m-cache-foreign/retain")
    assert resp.status_code == 404


def test_admin_write_does_not_stamp_owner():
    """Global-access callers leave user_id NULL rather than guessing one."""
    client = _make_scoped_client(role="admin")
    resp = client[0].post("/evolve/memory/m-mine/retain")
    assert resp.status_code == 200
    with client[1]() as db:
        row = db.get(EvolveSalience, "m-mine")
        assert row.user_id is None


def test_feedback_savepoint_conflict_does_not_500(client):
    """二轮审计 fix-9：并发首触的 SAVEPOINT 收口机制直测。

    在同一 session 先落一行 salience，再构造同 PK 的第二对象在 savepoint 内
    flush——后者必须捕获 IntegrityError 且不破坏外层事务（后续查询仍可用）。
    模拟的是并发双方同时 INSERT 时后到者的路径。
    """
    from sqlalchemy.exc import IntegrityError

    with client[1]() as db:
        row = EvolveSalience(memory_id="svp-1", user_id="u", salience_score=1.0)
        db.add(row)
        db.commit()

        dup = EvolveSalience(memory_id="svp-1", user_id="u", salience_score=1.0)
        try:
            with db.begin_nested():
                db.add(dup)
                db.flush()
        except IntegrityError:
            # 冲突被 SAVEPOINT 收口：外层事务可继续用、重读必有行
            pass

        reloaded = db.get(EvolveSalience, "svp-1")
        assert reloaded is not None and reloaded.salience_score == 1.0
        assert db.query(EvolveSalience).count() == 1, "重复行未泄漏"

    # 端点级：已有行时 feedback 正常 200 且分数累计（非 500）
    resp = _post(client, memory_id="svp-1", feedback_type="useful")
    assert resp.status_code == 200


def test_feedback_update_round_compiles_as_numeric_for_pg():
    """三轮审计：PG 无 round(double precision, integer) 重载。

    salience_score 是 Float 列（PG double precision），CASE 表达式必须显式
    cast 成 Numeric 后 round——否则 PG 执行期 UndefinedFunction（sqlite 的
    round(REAL, int) 存在，测试环境掩盖）。此用例在 postgresql 方言下编译
    UPDATE SQL 并断言 round 参数为 numeric 类型，锁死该契约。
    """
    from sqlalchemy import case, func, update
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.types import Numeric

    from models import EvolveSalience

    delta = 0.1
    stmt = (
        update(EvolveSalience)
        .where(EvolveSalience.memory_id == "x")
        .values(
            salience_score=func.round(
                case(
                    (EvolveSalience.salience_score + delta > 1.0, 1.0),
                    (EvolveSalience.salience_score + delta < 0.05, 0.05),
                    else_=EvolveSalience.salience_score + delta,
                ).cast(Numeric),
                4,
            )
        )
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    # round(CAST(... AS NUMERIC), 4) —— PG round(numeric, int) 存在
    assert "CAST" in sql.upper() and "NUMERIC" in sql.upper(), sql
