"""Tests for the /memories/search + /memories/types-distribution endpoints.

Fully mocked: DB session is a MagicMock whose execute() returns rows via
.all() and totals via .scalar(); auth and get_current_config are overridden.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import require_auth  # noqa: E402
from db import get_db  # noqa: E402
from routers import memories_search as memories_search_router  # noqa: E402

_ADMIN = SimpleNamespace(role="admin", id="admin-1")


def _make_app(db, override_auth=True):
    app = FastAPI()
    app.include_router(memories_search_router.router)
    if override_auth:
        app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


def _patch_config(mocker):
    mocker.patch.object(
        memories_search_router,
        "get_current_config",
        return_value={"vector_store": {"config": {"collection_name": "mem0_memories"}}},
    )


class TestSearchMemoryTypeFilter:
    def test_memory_type_adds_sql_condition(self, mocker):
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            ("uuid-1", {"data": "发哥喜欢咖啡", "memory_type": "PREFERENCES", "user_id": "u1"})
        ]
        db.execute.return_value.scalar.return_value = 1
        _patch_config(mocker)
        client = _make_app(db)

        resp = client.get("/memories/search", params={"q": "咖啡", "user_id": "u1", "memory_type": "PREFERENCES"})

        assert resp.status_code == 200
        data_call = db.execute.call_args_list[0]
        assert "payload->>'memory_type' = :mt" in data_call.args[0].text
        assert data_call.args[1]["mt"] == "PREFERENCES"
        assert resp.json()["results"][0]["memory_type"] == "PREFERENCES"
        assert resp.json()["total"] == 1

    def test_no_memory_type_keeps_old_sql(self, mocker):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        db.execute.return_value.scalar.return_value = 0
        _patch_config(mocker)
        client = _make_app(db)

        resp = client.get("/memories/search", params={"q": "咖啡", "user_id": "u1"})

        assert resp.status_code == 200
        data_call = db.execute.call_args_list[0]
        assert "memory_type" not in data_call.args[0].text
        assert resp.json() == {"results": [], "total": 0}


class TestTypesDistribution:
    def test_distribution_includes_unclassified(self, mocker):
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            ("EXPERIENCES", 14),
            ("FACTS", 5),
            ("unclassified", 3),
        ]
        _patch_config(mocker)
        client = _make_app(db)

        resp = client.get("/memories/types-distribution")

        assert resp.status_code == 200
        assert resp.json() == {
            "distribution": [
                {"type": "EXPERIENCES", "count": 14},
                {"type": "FACTS", "count": 5},
                {"type": "unclassified", "count": 3},
            ]
        }
        sql = db.execute.call_args.args[0].text
        assert "COALESCE(payload->>'memory_type', 'unclassified')" in sql


class TestMemoriesSearchAuth:
    def test_search_requires_auth(self, mocker):
        import auth as auth_mod

        mocker.patch.object(auth_mod, "AUTH_DISABLED", False)
        db = MagicMock()
        client = _make_app(db, override_auth=False)

        resp = client.get("/memories/search", params={"q": "x", "user_id": "u1"})

        assert resp.status_code == 401

    def test_types_distribution_requires_auth(self, mocker):
        import auth as auth_mod

        mocker.patch.object(auth_mod, "AUTH_DISABLED", False)
        db = MagicMock()
        client = _make_app(db, override_auth=False)

        resp = client.get("/memories/types-distribution")

        assert resp.status_code == 401
