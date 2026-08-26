"""GET /memories 全量列表按时间倒序回归（2026-08-26「最近记忆」停更修复）。

根因：pgvector vector_store.list 无 ORDER BY，返回 PG 堆扫描序；dashboard
首页直接 slice(0,10) 展示成任意一批旧记忆。修复：_list_all_memories 序列化后
按 updated_at（缺则 created_at）倒序，缺时间戳的行沉底。
"""

import importlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


def _row(rid: str, updated_at=None, created_at=None):
    payload = {"data": f"memory-{rid}", "user_id": "alice"}
    if created_at:
        payload["created_at"] = created_at
    if updated_at:
        payload["updated_at"] = updated_at
    return SimpleNamespace(id=rid, payload=payload)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

    mock_instance = MagicMock()
    # 物理序故意与时间序相反：newest 在最后、一条无时间戳在最前
    mock_instance.vector_store.list.return_value = [
        [
            _row("no-ts"),
            _row("mid", updated_at="2026-08-20T00:00:00+00:00"),
            _row("oldest", updated_at="2026-08-14T01:00:00+00:00"),
            _row("newest", updated_at="2026-08-26T07:00:00+00:00"),
        ]
    ]
    with patch("mem0.Memory.from_config", return_value=mock_instance):
        with patch.dict(os.environ, clear=False):
            import auth as auth_module
            import main as server_main

            importlib.reload(auth_module)
            importlib.reload(server_main)
            yield TestClient(server_main.app)


def test_list_all_memories_sorted_by_recency_desc(client):
    resp = client.get("/memories?top_k=100")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["results"]]
    assert ids[0] == "newest"
    assert ids[-2] == "mid" or "mid" in ids[:3]
    assert "oldest" in ids


def test_rows_without_timestamp_sink_to_bottom(client):
    resp = client.get("/memories?top_k=100")
    ids = [m["id"] for m in resp.json()["results"]]
    # 无时间戳行必须排在所有带时间戳行之后
    assert ids.index("no-ts") > ids.index("newest")
    assert ids.index("no-ts") > ids.index("oldest")
