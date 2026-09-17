"""Candidate discovery + refine engine tests (fully mocked).

- cluster_candidates / refine_group are pure functions (embed_batch / LLM mocked).
- API tests build a TestClient over the refine router with get_db + get_memory_instance
  overridden; auth overridden except for the 401 tests.
"""

import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from auth import require_auth  # noqa: E402
from db import get_db  # noqa: E402
from models import MemoryRefineCandidate  # noqa: E402
from routers import refine as refine_router  # noqa: E402
import refine_candidates  # noqa: E402
import refine_memory  # noqa: E402

_ADMIN = SimpleNamespace(role="admin", id="admin-1")


def _memory_with_embeddings(vectors, llm_return=None, llm_raises=None):
    memory = MagicMock()
    memory.embedding_model.embed_batch = MagicMock(return_value=vectors)
    if llm_raises is not None:
        memory.llm.generate_response.side_effect = llm_raises
    else:
        memory.llm.generate_response = MagicMock(return_value=llm_return)
    return memory


def _items(data_list):
    return [{"id": f"m{i}", "data": d} for i, d in enumerate(data_list)]


def _make_app(db, override_auth=True):
    app = FastAPI()
    app.include_router(refine_router.router)
    if override_auth:
        app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


class TestClusterCandidates:
    def test_three_similar_texts_cluster_into_one_candidate(self):
        memory = _memory_with_embeddings([[1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]])

        candidates = refine_memory.cluster_candidates(
            memory, _items(["碎记忆A1", "碎记忆A2", "碎记忆A3"])
        )

        assert len(candidates) == 1
        assert len(candidates[0]["memory_ids"]) == 3
        # Topic is produced by the LLM at proposal time, never by truncating
        # a raw memory here (the old [:20] slice made half-sentence titles).
        assert candidates[0]["topic"] == ""

    def test_three_distinct_topics_produce_three_candidates(self):
        memory = _memory_with_embeddings(
            [
                [1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0],  # topic A
                [0, 1, 0], [0.43589, 0.9, 0], [0.31225, 0.95, 0],  # topic B
                [0, 0, 1], [0, 0.43589, 0.9], [0, 0.31225, 0.95],  # topic C
            ]
        )

        candidates = refine_memory.cluster_candidates(
            memory, _items(["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3"])
        )

        assert len(candidates) == 3

    def test_group_below_min_size_is_filtered(self):
        memory = _memory_with_embeddings([[1, 0, 0], [0.9, 0.43589, 0]])

        candidates = refine_memory.cluster_candidates(
            memory, _items(["碎记忆X", "碎记忆Y"])
        )

        assert candidates == []

    def test_mixed_near_and_far_splits_groups(self):
        memory = _memory_with_embeddings(
            [[1, 0, 0], [0.9, 0.43589, 0], [0, 1, 0]]
        )

        candidates = refine_memory.cluster_candidates(
            memory, _items(["近邻A", "近邻B", "远处C"])
        )

        assert candidates == []  # 2-member near group + 1-member far group both < 3


class TestRefineGroup:
    def test_valid_llm_json_saves_proposal_without_writing_vector_store(self):
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps(
                {"topic": "记忆精炼", "summary": ["高层抽象1", "高层抽象2"]},
                ensure_ascii=False,
            ),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert out["suggested_text"] == ["高层抽象1", "高层抽象2"]
        assert out["topic"] == "记忆精炼"
        memory.vector_store.insert.assert_not_called()
        memory.vector_store.update.assert_not_called()

    def test_legacy_json_without_topic_falls_back_to_first_summary(self):
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps({"summary": ["高层抽象1", "高层抽象2"]}, ensure_ascii=False),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert out["topic"] == "高层抽象1"

    def test_legacy_fallback_keeps_summary_whole_instead_of_truncating(self):
        long_line = "这是一条相当长的高层抽象事实描述用于验证回退路径不截断"
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps({"summary": [long_line]}, ensure_ascii=False),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert out["topic"] == long_line

    def test_generated_topic_is_not_hard_truncated_at_20_chars(self):
        topic = "RustDesk 登录态 secure_tcp 死锁根因与解决方案"
        assert len(topic) == 33  # regression guard: the old code sliced this to 20
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps({"topic": topic, "summary": ["抽象"]}, ensure_ascii=False),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert out["topic"] == topic

    def test_overlong_topic_is_clamped_to_the_safety_cap(self):
        topic = "长" * (refine_memory.MAX_TOPIC_CHARS + 25)
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps({"topic": topic, "summary": ["抽象"]}, ensure_ascii=False),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert len(out["topic"]) == refine_memory.MAX_TOPIC_CHARS

    def test_safety_cap_is_loose_enough_for_real_titles(self):
        # Measured against production groups: real titles run up to ~65 chars.
        # A cap that clips them re-creates the truncation bug at lower
        # frequency, so the guard must sit well above the prompt's own bound.
        assert refine_memory.MAX_TOPIC_CHARS >= 120

    def test_real_world_title_is_not_clamped(self):
        # Production candidate #39 produced exactly this title (68 chars); the
        # 60-char guard silently clipped it to 60 mid-word.
        topic = "TencentDB-Agent-Memory 的 dsh 集成路线与我方进程内双缝方案的对比及 2026-08-30 用户敲定的选型结论"
        assert len(topic) == 68  # the old 60-char guard clipped this
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps({"topic": topic, "summary": ["抽象"]}, ensure_ascii=False),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "proposed"
        assert out["topic"] == topic

    def test_prompt_asks_for_a_full_sentence_title(self):
        # The 20-char instruction is what made the model emit titles that read
        # like cut-off sentences; it must ask for a complete one-line title
        # while still giving the model a bound to self-limit against.
        prompt = refine_memory.REFINE_SYSTEM_PROMPT
        assert "不要中途截断" in prompt
        assert "20字" not in prompt

    def test_invalid_json_fails_gracefully(self):
        memory = _memory_with_embeddings([], llm_return="这不是JSON")
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "failed"
        assert out["suggested_text"] == []

    def test_llm_exception_fails_gracefully(self):
        memory = _memory_with_embeddings([], llm_raises=TimeoutError("timeout"))
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1", "m2", "m3"]})

        assert out["status"] == "failed"
        assert out["suggested_text"] == []

    def test_no_texts_found_fails_gracefully(self):
        memory = _memory_with_embeddings([], llm_return=json.dumps({"summary": ["x"]}))
        memory.vector_store.get.return_value = None

        out = refine_memory.refine_group(memory, {"memory_ids": ["m1"]})

        assert out["status"] == "failed"
        assert out["suggested_text"] == []


class TestRefineApi:
    def _stale_memory_ids(self):
        return [("m1",), ("m2",), ("m3",)]

    def test_post_generates_and_get_lists_proposed_candidates(self, mocker):
        db = MagicMock()
        db.execute.return_value.all.return_value = self._stale_memory_ids()

        memory = MagicMock()
        memory.vector_store.get.side_effect = lambda mid: SimpleNamespace(
            payload={"data": f"碎记忆 {mid}", "user_id": "u1"}
        )
        memory.embedding_model.embed_batch.return_value = [
            [1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]
        ]
        memory.llm.generate_response.return_value = json.dumps(
            {"summary": ["高层抽象"]}, ensure_ascii=False
        )
        mocker.patch.object(refine_router, "get_memory_instance", return_value=memory)

        client = _make_app(db)

        post = client.post("/memory/refine/candidates", params={"user_id": "u1"})

        assert post.status_code == 200
        cands = post.json()["candidates"]
        assert len(cands) == 1
        assert cands[0]["status"] == "proposed"
        assert cands[0]["suggested_text"] == ["高层抽象"]
        assert len(cands[0]["memory_ids"]) == 3
        db.add.assert_called()
        db.commit.assert_called_once()

        row = MemoryRefineCandidate(
            user_id="u1",
            memory_ids=cands[0]["memory_ids"],
            topic=cands[0]["topic"],
            status="proposed",
            suggested_text=cands[0]["suggested_text"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        row.id = 7
        db.execute.return_value.scalars.return_value.all.return_value = [row]

        get = client.get("/memory/refine/candidates", params={"user_id": "u1"})

        assert get.status_code == 200
        listed = get.json()["candidates"]
        assert len(listed) == 1
        assert listed[0]["id"] == 7
        assert listed[0]["status"] == "proposed"
        assert listed[0]["suggested_text"] == ["高层抽象"]
        assert listed[0]["memory_ids"] == cands[0]["memory_ids"]

    def test_endpoints_require_auth(self, mocker):
        import auth as auth_mod

        mocker.patch.object(auth_mod, "AUTH_DISABLED", False)
        db = MagicMock()
        client = _make_app(db, override_auth=False)

        assert client.post("/memory/refine/candidates").status_code == 401
        assert client.get("/memory/refine/candidates").status_code == 401


def _candidate_row(user_id="u1", status="proposed", suggested_text=None, memory_ids=None, refined_memory_ids=None):
    row = MemoryRefineCandidate(
        user_id=user_id,
        memory_ids=memory_ids or [],
        topic="t",
        status=status,
        suggested_text=suggested_text or [],
        refined_memory_ids=refined_memory_ids,
    )
    row.id = 1
    return row


class TestRefineApply:
    def _apply_client(self, mocker, db, row, memory):
        db.get.return_value = row
        mocker.patch.object(refine_router, "get_memory_instance", return_value=memory)
        return _make_app(db)

    def test_apply_proposed_writes_and_marks_applied(self, mocker):
        db = MagicMock()
        row = _candidate_row(suggested_text=["摘要1", "摘要2"], memory_ids=["m1", "m2", "m3"])
        memory = MagicMock()
        memory.add.side_effect = [
            {"results": [{"id": "n1"}]},
            {"results": [{"id": "n2"}]},
        ]
        memory.vector_store.get.return_value = SimpleNamespace(
            payload={"data": "碎记忆", "user_id": "u1"}
        )
        client = self._apply_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/apply", json={"candidate_id": 1})

        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["refined_memory_ids"] == ["n1", "n2"]
        assert memory.add.call_count == 2
        for call in memory.add.call_args_list:
            assert call.kwargs["infer"] is False
            assert call.kwargs["user_id"] == "u1"
        upd = memory.vector_store.update.call_args.kwargs
        assert upd["payload"]["superseded_by"] == "n1"
        assert "superseded_at" in upd["payload"]
        assert upd["payload"]["data"] == "碎记忆"
        assert row.status == "applied"
        assert row.refined_memory_ids == ["n1", "n2"]
        db.commit.assert_called()

    def test_apply_rejects_non_proposed(self, mocker):
        db = MagicMock()
        row = _candidate_row(status="applied")
        memory = MagicMock()
        client = self._apply_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/apply", json={"candidate_id": 1})

        assert resp.status_code == 409
        memory.add.assert_not_called()
        memory.vector_store.update.assert_not_called()

    def test_apply_add_failure_leaves_proposed(self, mocker):
        db = MagicMock()
        row = _candidate_row(suggested_text=["摘要"], memory_ids=["m1"])
        memory = MagicMock()
        memory.add.side_effect = TimeoutError("llm down")
        client = self._apply_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/apply", json={"candidate_id": 1})

        assert resp.status_code == 200
        assert resp.json()["status"] == "proposed"
        assert row.status == "proposed"
        memory.vector_store.update.assert_not_called()


class TestRefineRollback:
    def _rollback_client(self, mocker, db, row, memory):
        db.get.return_value = row
        mocker.patch.object(refine_router, "get_memory_instance", return_value=memory)
        return _make_app(db)

    def test_rollback_applied_deletes_new_and_restores_originals(self, mocker):
        db = MagicMock()
        row = _candidate_row(
            status="applied",
            memory_ids=["m1", "m2"],
            refined_memory_ids=["n1", "n2"],
        )
        memory = MagicMock()
        memory.vector_store.get.return_value = SimpleNamespace(
            payload={"data": "碎记忆", "user_id": "u1", "superseded_by": "n1", "superseded_at": "2026-01-01"}
        )
        client = self._rollback_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/rollback", json={"candidate_id": 1})

        assert resp.status_code == 200
        assert resp.json()["status"] == "rolled_back"
        memory.delete.assert_any_call("n1")
        memory.delete.assert_any_call("n2")
        upd = memory.vector_store.update.call_args.kwargs
        assert "superseded_by" not in upd["payload"]
        assert "superseded_at" not in upd["payload"]
        assert upd["payload"]["data"] == "碎记忆"
        assert row.status == "rolled_back"

    def test_rollback_rejects_non_applied(self, mocker):
        db = MagicMock()
        row = _candidate_row(status="proposed")
        memory = MagicMock()
        client = self._rollback_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/rollback", json={"candidate_id": 1})

        assert resp.status_code == 409
        memory.delete.assert_not_called()


class TestRefineDelete:
    def _delete_client(self, mocker, db, row, memory):
        db.get.return_value = row
        mocker.patch.object(refine_router, "get_memory_instance", return_value=memory)
        return _make_app(db)

    def test_delete_proposed_drops_record_only(self, mocker):
        db = MagicMock()
        row = _candidate_row(status="proposed", memory_ids=["m1", "m2"])
        memory = MagicMock()
        client = self._delete_client(mocker, db, row, memory)

        resp = client.post("/memory/refine/delete", json={"candidate_id": 1})

        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        db.delete.assert_called_once_with(row)
        memory.delete.assert_not_called()
        memory.vector_store.update.assert_not_called()

    def test_delete_applied_without_memories_keeps_refined_and_superseded(self, mocker):
        db = MagicMock()
        row = _candidate_row(status="applied", memory_ids=["m1"], refined_memory_ids=["n1"])
        memory = MagicMock()
        client = self._delete_client(mocker, db, row, memory)

        resp = client.post(
            "/memory/refine/delete",
            json={"candidate_id": 1, "with_memories": False},
        )

        assert resp.status_code == 200
        db.delete.assert_called_once_with(row)
        memory.delete.assert_not_called()
        memory.vector_store.update.assert_not_called()  # superseded marks kept

    def test_delete_applied_with_memories_removes_refined_and_restores_originals(
        self, mocker
    ):
        db = MagicMock()
        row = _candidate_row(status="applied", memory_ids=["m1"], refined_memory_ids=["n1"])
        memory = MagicMock()
        memory.vector_store.get.return_value = SimpleNamespace(
            payload={"data": "碎记忆", "user_id": "u1", "superseded_by": "n1", "superseded_at": "2026-01-01"}
        )
        client = self._delete_client(mocker, db, row, memory)

        resp = client.post(
            "/memory/refine/delete",
            json={"candidate_id": 1, "with_memories": True},
        )

        assert resp.status_code == 200
        memory.delete.assert_called_once_with("n1")
        upd = memory.vector_store.update.call_args.kwargs
        assert "superseded_by" not in upd["payload"]
        db.delete.assert_called_once_with(row)

    def test_delete_missing_returns_404(self, mocker):
        db = MagicMock()
        db.get.return_value = None
        memory = MagicMock()
        client = self._delete_client(mocker, db, None, memory)

        resp = client.post("/memory/refine/delete", json={"candidate_id": 999})

        assert resp.status_code == 404
        memory.delete.assert_not_called()


class TestRefineHistory:
    def test_history_lists_applied_and_rolled_back(self, mocker):
        db = MagicMock()
        r1 = _candidate_row(status="applied", suggested_text=["s"], refined_memory_ids=["n1"])
        r2 = _candidate_row(status="rolled_back", suggested_text=["s2"])
        r1.id, r2.id = 1, 2
        db.execute.return_value.scalars.return_value.all.return_value = [r1, r2]
        client = _make_app(db)

        resp = client.get("/memory/refine/history", params={"user_id": "u1"})

        assert resp.status_code == 200
        hist = resp.json()["history"]
        assert [h["status"] for h in hist] == ["applied", "rolled_back"]
        assert hist[0]["refined_memory_ids"] == ["n1"]
        assert hist[0]["suggested_text"] == ["s"]


class TestRefineAuth:
    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("POST", "/memory/refine/apply", {"candidate_id": 1}),
            ("POST", "/memory/refine/rollback", {"candidate_id": 1}),
            ("POST", "/memory/refine/delete", {"candidate_id": 1, "with_memories": False}),
            ("GET", "/memory/refine/history", None),
        ],
    )
    def test_apply_rollback_history_require_auth(self, mocker, method, path, body):
        import auth as auth_mod

        mocker.patch.object(auth_mod, "AUTH_DISABLED", False)
        db = MagicMock()
        client = _make_app(db, override_auth=False)

        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json=body)

        assert resp.status_code == 401


_NON_ADMIN = SimpleNamespace(role="user", id="user-9")


class TestRefineAdminScope:
    def _make_client(self, mocker, db, auth):
        app = FastAPI()
        app.include_router(refine_router.router)
        app.dependency_overrides[require_auth] = lambda: auth
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app, raise_server_exceptions=False)

    def _stmt_params(self, db):
        stmt = db.execute.call_args.args[0]
        return stmt.compile().params

    def test_admin_get_candidates_without_user_id_returns_all_users(self, mocker):
        db = MagicMock()
        r1 = _candidate_row(user_id="u1", memory_ids=["m1"])
        r2 = _candidate_row(user_id="u2", memory_ids=["m2"])
        r1.id, r2.id = 1, 2
        db.execute.return_value.scalars.return_value.all.return_value = [r1, r2]
        client = self._make_client(mocker, db, _ADMIN)

        resp = client.get("/memory/refine/candidates")

        assert resp.status_code == 200
        users = {c["user_id"] for c in resp.json()["candidates"]}
        assert users == {"u1", "u2"}
        assert "user_id" not in self._stmt_params(db)

    def test_admin_get_candidates_with_user_id_filters(self, mocker):
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []
        client = self._make_client(mocker, db, _ADMIN)

        resp = client.get("/memory/refine/candidates", params={"user_id": "u1"})

        assert resp.status_code == 200
        params = self._stmt_params(db)
        assert any(v == "u1" for k, v in params.items() if "user_id" in k)

    def test_non_admin_get_candidates_without_user_id_scopes_to_self(self, mocker):
        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = []
        client = self._make_client(mocker, db, _NON_ADMIN)

        resp = client.get("/memory/refine/candidates")

        assert resp.status_code == 200
        params = self._stmt_params(db)
        assert any(v == "user-9" for k, v in params.items() if "user_id" in k)

    def test_admin_get_history_without_user_id_returns_all_users(self, mocker):
        db = MagicMock()
        r1 = _candidate_row(user_id="u1", status="applied", memory_ids=["m1"])
        r2 = _candidate_row(user_id="u2", status="rolled_back", memory_ids=["m2"])
        r1.id, r2.id = 1, 2
        db.execute.return_value.scalars.return_value.all.return_value = [r1, r2]
        client = self._make_client(mocker, db, _ADMIN)

        resp = client.get("/memory/refine/history")

        assert resp.status_code == 200
        users = {c["user_id"] for c in resp.json()["history"]}
        assert users == {"u1", "u2"}
        assert "user_id" not in self._stmt_params(db)

    def test_admin_post_candidates_generates_for_all_users(self, mocker):
        db = MagicMock()
        memory = MagicMock()
        mocker.patch.object(refine_router, "get_memory_instance", return_value=memory)
        mocker.patch.object(refine_router, "_collect_stale_items", return_value=[])
        mocker.patch.object(refine_router, "_discover_users", return_value=["u1", "u2"])
        mocker.patch.object(
            refine_router,
            "discover_refine_candidates",
            side_effect=lambda mem, sess, uid, items=None: [
                {
                    "id": 1,
                    "user_id": uid,
                    "memory_ids": [uid],
                    "topic": None,
                    "status": "proposed",
                    "suggested_text": [],
                    "refined_memory_id": None,
                    "refined_memory_ids": [],
                    "created_at": None,
                    "updated_at": None,
                }
            ],
        )
        client = self._make_client(mocker, db, _ADMIN)

        resp = client.post("/memory/refine/candidates")

        assert resp.status_code == 200
        users = {c["user_id"] for c in resp.json()["candidates"]}
        assert users == {"u1", "u2"}
        called = [c.args[2] for c in refine_router.discover_refine_candidates.call_args_list]
        assert called == ["u1", "u2"]


class TestDiscoverIdempotency:
    """Dedup guards on the shared discovery pipeline (timer + manual POST)."""

    def _memory(self, vectors, payloads=None):
        memory = MagicMock()
        memory.embedding_model.embed_batch = MagicMock(return_value=vectors)
        memory.llm.generate_response = MagicMock(
            return_value=json.dumps({"summary": ["摘要"]}, ensure_ascii=False)
        )
        memory.vector_store.get = MagicMock(
            side_effect=lambda mid: SimpleNamespace(
                payload=(payloads or {}).get(mid, {"data": f"碎记忆{mid}", "user_id": "u1"})
            )
        )
        return memory

    def test_stale_skips_superseded_memories(self):
        memory = self._memory(
            [[1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]],
            {
                "m2": {
                    "data": "已精炼的原记忆",
                    "user_id": "u1",
                    "superseded_by": "n1",
                }
            },
        )
        db = MagicMock()
        db.execute.return_value.all.return_value = [("m1",), ("m2",), ("m3",)]

        items = refine_router._collect_stale_items(memory, db, "u1")

        assert [it["id"] for it in items] == ["m1", "m3"]

    def test_discover_skips_group_already_proposed(self):
        memory = self._memory([[1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]])
        db = MagicMock()
        stale_rows = [("m1",), ("m2",), ("m3",)]
        # first execute (stale query) returns rows; second (existing groups) has the group
        db.execute.side_effect = [
            SimpleNamespace(all=lambda: stale_rows),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [["m1", "m2", "m3"]])),
        ]

        created = refine_router.discover_refine_candidates(memory, db, "u1")

        assert created == []
        db.add.assert_not_called()

    def test_discover_keeps_groups_not_in_existing_candidates(self):
        memory = self._memory([[1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]])
        db = MagicMock()
        db.execute.side_effect = [
            SimpleNamespace(all=lambda: [("m1",), ("m2",), ("m3",)]),
            SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [["m9", "m10", "m11"]])
            ),
        ]

        created = refine_router.discover_refine_candidates(memory, db, "u1")

        assert len(created) == 1
        assert created[0]["memory_ids"] == ["m1", "m2", "m3"]
        assert db.add.call_count == 1


class TestRefreshTopic:
    """Topic-only regeneration for candidates created before the title fix."""

    def test_regenerates_topic_without_touching_vector_store(self):
        memory = _memory_with_embeddings(
            [],
            llm_return=json.dumps(
                {"topic": "RustDesk 登录态 secure_tcp 死锁根因与解决方案", "summary": ["摘要"]},
                ensure_ascii=False,
            ),
        )
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refresh_topic(
            memory, {"memory_ids": ["m1", "m2", "m3"], "topic": "RustDesk 登录态 secure_"}
        )

        assert out == {
            "status": "updated",
            "topic": "RustDesk 登录态 secure_tcp 死锁根因与解决方案",
        }
        memory.vector_store.insert.assert_not_called()
        memory.vector_store.update.assert_not_called()

    def test_failed_when_no_source_texts_found(self):
        memory = _memory_with_embeddings([], llm_return=json.dumps({"topic": "x"}))
        memory.vector_store.get.return_value = None

        out = refine_memory.refresh_topic(memory, {"memory_ids": ["m1"]})

        assert out["status"] == "failed"
        assert out["topic"] is None

    def test_failed_when_llm_unavailable(self):
        memory = _memory_with_embeddings([], llm_raises=TimeoutError("timeout"))
        memory.vector_store.get.return_value = SimpleNamespace(payload={"data": "碎记忆"})

        out = refine_memory.refresh_topic(memory, {"memory_ids": ["m1"]})

        assert out["status"] == "failed"
        assert out["topic"] is None


class TestRefreshTopicScript:
    """The one-shot backfill for candidates whose topic predates the fix."""

    def _config(self, tmp_path, monkeypatch):
        config = {
            "vector_store": {"provider": "pgvector", "config": {}},
            "llm": {"provider": "openai", "config": {}},
            "embedder": {"provider": "openai", "config": {}},
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MEM0_CONFIG_PATH", str(config_path))

    def _memory(self, mocker, topic="完整的新标题"):
        memory = MagicMock()
        memory.vector_store.get.side_effect = lambda mid: SimpleNamespace(
            payload={"data": f"碎记忆{mid}", "user_id": "u1"}
        )
        memory.llm.generate_response.return_value = json.dumps(
            {"topic": topic, "summary": ["摘要"]}, ensure_ascii=False
        )
        mocker.patch("mem0.Memory.from_config", return_value=memory)
        return memory

    def _db(self, mocker, rows):
        import refresh_refine_topics

        db = MagicMock()
        db.execute.return_value.scalars.return_value.all.return_value = rows
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db
        db_ctx.__exit__.return_value = False
        mocker.patch.object(refresh_refine_topics, "SessionLocal", return_value=db_ctx)
        return db

    def _row(self, topic="RustDesk 登录态 secure_"):
        row = SimpleNamespace(
            id=52, topic=topic, memory_ids=["m1", "m2", "m3"], status="proposed"
        )
        return row

    def test_backfills_existing_topic_and_never_touches_memories(
        self, mocker, tmp_path, monkeypatch
    ):
        import refresh_refine_topics

        self._config(tmp_path, monkeypatch)
        memory = self._memory(mocker, topic="RustDesk 登录态 secure_tcp 死锁根因与解决方案")
        row = self._row()
        db = self._db(mocker, [row])

        rc = refresh_refine_topics.main()

        assert rc == 0
        assert row.topic == "RustDesk 登录态 secure_tcp 死锁根因与解决方案"
        db.commit.assert_called()
        memory.add.assert_not_called()
        memory.vector_store.insert.assert_not_called()

    def test_dry_run_reports_without_writing(self, mocker, tmp_path, monkeypatch):
        import refresh_refine_topics

        self._config(tmp_path, monkeypatch)
        self._memory(mocker)
        row = self._row()
        db = self._db(mocker, [row])
        monkeypatch.setenv("REFRESH_TOPIC_DRY_RUN", "true")

        rc = refresh_refine_topics.main()

        assert rc == 0
        assert row.topic == "RustDesk 登录态 secure_"
        db.commit.assert_not_called()

    def test_llm_failure_leaves_topic_untouched_and_continues(
        self, mocker, tmp_path, monkeypatch
    ):
        import refresh_refine_topics

        self._config(tmp_path, monkeypatch)
        memory = MagicMock()
        memory.vector_store.get.side_effect = lambda mid: SimpleNamespace(
            payload={"data": f"碎记忆{mid}", "user_id": "u1"}
        )
        memory.llm.generate_response.side_effect = TimeoutError("timeout")
        mocker.patch("mem0.Memory.from_config", return_value=memory)
        row = self._row()
        db = self._db(mocker, [row])

        rc = refresh_refine_topics.main()

        assert rc == 0
        assert row.topic == "RustDesk 登录态 secure_"


class TestRefineCronScript:
    def test_refine_candidates_script_discover_only(self, mocker, tmp_path, monkeypatch):
        config = {
            "vector_store": {"provider": "pgvector", "config": {}},
            "llm": {"provider": "openai", "config": {}},
            "embedder": {"provider": "openai", "config": {}},
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        monkeypatch.setenv("MEM0_CONFIG_PATH", str(config_path))

        fake_memory = MagicMock()
        fake_memory.vector_store.list.return_value = [
            [SimpleNamespace(id="u-row", payload={"user_id": "u1", "data": "x"})]
        ]
        fake_memory.vector_store.get.side_effect = lambda mid: SimpleNamespace(
            payload={"data": f"碎记忆{mid}", "user_id": "u1"}
        )
        fake_memory.embedding_model.embed_batch.return_value = [
            [1, 0, 0], [0.9, 0.43589, 0], [0.95, 0.31225, 0]
        ]
        fake_memory.llm.generate_response.return_value = json.dumps(
            {"summary": ["摘要"]}, ensure_ascii=False
        )
        mocker.patch("mem0.Memory.from_config", return_value=fake_memory)

        db = MagicMock()
        db.execute.return_value.all.return_value = [("m1",), ("m2",), ("m3",)]
        db_ctx = MagicMock()
        db_ctx.__enter__.return_value = db
        db_ctx.__exit__.return_value = False
        mocker.patch.object(refine_candidates, "SessionLocal", return_value=db_ctx)

        rc = refine_candidates.main()

        assert rc == 0
        fake_memory.add.assert_not_called()
        db.commit.assert_called()
