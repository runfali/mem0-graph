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
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    return TestClient(app)


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
