"""/auth/register 冒烟（三轮审计：此前零覆盖，成功路径曾引用已删变量必 500）。

覆盖：空表成功注册拿 token；已有用户后注册被拒（403）。
"""

import os

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-register")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from db import get_db  # noqa: E402
from models import Base  # noqa: E402
from routers.auth import router as auth_router  # noqa: E402


@pytest.fixture
def client(tmp_path):
    # 五轮审计：limiter 为模块级单例，跨用例累计计数——每个用例重置限流状态
    try:
        from rate_limit import limiter as _limiter
        _limiter.reset()
    except Exception:
        pass
    # 四轮审计：并发用例需要 file 库（:memory: + StaticPool 单连接会被多线程
    # 串扰），QueuePool 每线程独立连接 + busy timeout 兜底 sqlite 文件锁
    db_path = tmp_path / "register_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    app = FastAPI()
    app.include_router(auth_router)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # require_auth 走 auth.SessionLocal（模块级）——必须与测试库同源
    from unittest.mock import patch

    with patch("auth.SessionLocal", TestingSessionLocal):
        yield TestClient(app)


def test_register_success_returns_token(client):
    resp = client.post(
        "/auth/register",
        json={"name": "admin", "email": "admin@test.dev", "password": "StrongPass123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("access_token")
    assert data.get("refresh_token")


def test_register_second_admin_rejected(client):
    ok = client.post(
        "/auth/register",
        json={"name": "a", "email": "a@test.dev", "password": "StrongPass123"},
    )
    assert ok.status_code == 200
    resp = client.post(
        "/auth/register",
        json={"name": "b", "email": "b@test.dev", "password": "StrongPass123"},
    )
    assert resp.status_code == 403


def test_concurrent_register_only_one_admin_wins(client):
    """四轮审计：并发首注册只有一方成功（同 role 的 unique 索引兜底）。"""
    import threading

    results = []

    def do_register(email):
        resp = client.post(
            "/auth/register",
            json={"name": "u", "email": email, "password": "StrongPass123"},
        )
        results.append(resp.status_code)

    threads = [
        threading.Thread(target=do_register, args=(f"c{i}@test.dev",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2, results
    assert results.count(200) == 1, results
    assert results.count(403) == 1, results


def test_revoke_refresh_idempotent_and_effective(client):
    """五轮审计：revoke-refresh 幂等（重复/过期 token 均 200）+ 吊销后 refresh 401。"""
    # 注册拿 refresh
    r = client.post(
        "/auth/register",
        json={"name": "rv", "email": "rv@test.dev", "password": "StrongPass123"},
    )
    assert r.status_code == 200
    refresh_token = r.json()["refresh_token"]

    # 吊销
    rev = client.post("/auth/revoke-refresh", json={"refresh_token": refresh_token})
    assert rev.status_code == 200 and rev.json()["ok"] is True

    # 吊销后 refresh 被拒
    ref = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert ref.status_code == 401

    # 重复吊销幂等 200
    rev2 = client.post("/auth/revoke-refresh", json={"refresh_token": refresh_token})
    assert rev2.status_code == 200

    # 过期/垃圾 token 幂等 200（五轮审计：decode 失败也视为已吊销）
    junk = client.post("/auth/revoke-refresh", json={"refresh_token": "not-a-jwt"})
    assert junk.status_code == 200


def test_change_password_revokes_existing_refresh(client):
    """五轮审计：改密后旧 refresh token 立即失效（吊销未消费 jti）。"""
    # 注册 + 登录拿 refresh
    reg = client.post(
        "/auth/register",
        json={"name": "cp", "email": "cp@test.dev", "password": "StrongPass123"},
    )
    assert reg.status_code == 200
    access = reg.json()["access_token"]
    refresh = reg.json()["refresh_token"]

    # 改密
    chg = client.post(
        "/auth/change-password",
        json={"current_password": "StrongPass123", "new_password": "NewStrong456"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert chg.status_code == 200, chg.text

    # 旧 refresh 必须失效
    ref = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert ref.status_code == 401
