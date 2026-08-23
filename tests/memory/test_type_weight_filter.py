"""Retrieval-side memory_type support: filter alias + ranking type weights.

- ``filters={"type": ...}`` is aliased to the payload field ``memory_type``.
- ``MEM0_TYPE_WEIGHT_*`` env vars give per-type multiplicative boosts; default
  1.0 (no env) is identical to pre-feature ranking (zero behavior change).
"""

import os
from unittest.mock import MagicMock

import pytest

from mem0.memory.main import AsyncMemory, Memory, _build_type_weights
from mem0.utils.scoring import score_and_rank


@pytest.fixture(autouse=True)
def _clean_type_weight_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MEM0_TYPE_WEIGHT_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _setup_mocks(mocker):
    """Common mock wiring for the mem0 core (recipe: core-lib-test-mocking)."""
    mock_embedder = mocker.MagicMock()
    mock_embedder.return_value.embed.return_value = [0.1, 0.2, 0.3]
    mocker.patch("mem0.utils.factory.EmbedderFactory.create", mock_embedder)

    mock_vector_store = mocker.MagicMock()
    mock_vector_store.return_value.search.return_value = []
    mocker.patch(
        "mem0.utils.factory.VectorStoreFactory.create",
        side_effect=[mock_vector_store.return_value, mocker.MagicMock()],
    )

    mock_llm = mocker.MagicMock()
    mocker.patch("mem0.utils.factory.LlmFactory.create", mock_llm)

    mocker.patch("mem0.memory.main.SQLiteManager", mocker.MagicMock())
    mocker.patch("mem0.memory.main.MEM0_TELEMETRY", False)
    mocker.patch("mem0.memory.main.capture_event")

    return mock_llm, mock_vector_store


def _memory_instance(mocker, memory_cls):
    _setup_mocks(mocker)
    memory = memory_cls()
    memory.config = mocker.MagicMock()
    memory.config.custom_instructions = None
    memory.config.custom_update_memory_prompt = None
    memory.custom_instructions = None
    memory.api_version = "v1.1"
    memory.graph = None
    return memory


def _candidate(mem_id, score, memory_type):
    mem = MagicMock()
    mem.id = mem_id
    mem.score = score
    mem.payload = {"memory_type": memory_type, "data": "记忆文本"}
    return mem


class TestScoreAndRankTypeWeight:
    def test_type_weight_fn_multiplies_score(self):
        results = [{"id": "m1", "score": 0.5, "payload": {"memory_type": "PREFERENCES"}}]

        boosted = score_and_rank(
            results, {}, {}, threshold=0.0, top_k=10, type_weight_fn=lambda p: 2.0
        )

        assert boosted[0]["score"] == pytest.approx(1.0)

    def test_type_weight_none_and_one_identical(self):
        results = [
            {"id": "m1", "score": 0.5, "payload": {"memory_type": "PREFERENCES"}},
            {"id": "m2", "score": 0.4, "payload": {}},
            {"id": "m3", "score": 0.3, "payload": {"memory_type": "FACTS"}},
        ]

        baseline = score_and_rank(results, {"m1": 0.2, "m2": 0.1}, {"m3": 0.3}, threshold=0.0, top_k=10)
        weighted = score_and_rank(
            results, {"m1": 0.2, "m2": 0.1}, {"m3": 0.3}, threshold=0.0, top_k=10,
            type_weight_fn=lambda p: 1.0,
        )

        assert [r["id"] for r in baseline] == [r["id"] for r in weighted]
        assert [r["score"] for r in baseline] == [r["score"] for r in weighted]

    def test_missing_memory_type_gets_weight_one(self):
        weights = _build_type_weights()
        results = [{"id": "m1", "score": 0.5, "payload": {}}]

        out = score_and_rank(
            results, {}, {}, threshold=0.0, top_k=10,
            type_weight_fn=lambda p: weights.get(p.get("memory_type"), 1.0),
        )

        assert out[0]["score"] == pytest.approx(0.5)


class TestTypeWeightEnv:
    def test_build_type_weights_from_env(self, monkeypatch):
        monkeypatch.setenv("MEM0_TYPE_WEIGHT_PREFERENCES", "1.5")

        weights = _build_type_weights()

        assert weights["PREFERENCES"] == 1.5
        assert weights["FACTS"] == 1.0
        assert weights["EXPERIENCES"] == 1.0
        assert weights["OBSERVATIONS"] == 1.0
        assert weights["DECISIONS"] == 1.0

    def test_invalid_env_falls_back_to_one(self, monkeypatch):
        monkeypatch.setenv("MEM0_TYPE_WEIGHT_PREFERENCES", "abc")

        weights = _build_type_weights()

        assert weights["PREFERENCES"] == 1.0


class TestFilterAlias:
    def test_type_alias_mapped_to_memory_type_sync(self, mocker):
        memory = _memory_instance(mocker, Memory)
        captured = {}

        def fake_search(query, filters, limit, threshold=0.1, explain=False, show_expired=False, trace_stats=None):
            captured["filters"] = filters
            return []

        memory._search_vector_store = fake_search

        memory.search("test", filters={"type": "PREFERENCES", "user_id": "u1"}, depth="standard")

        assert captured["filters"].get("memory_type") == "PREFERENCES"
        assert "type" not in captured["filters"]
        assert captured["filters"]["user_id"] == "u1"

    def test_memory_type_passthrough_sync(self, mocker):
        memory = _memory_instance(mocker, Memory)
        captured = {}

        def fake_search(query, filters, limit, threshold=0.1, explain=False, show_expired=False, trace_stats=None):
            captured["filters"] = filters
            return []

        memory._search_vector_store = fake_search

        memory.search("test", filters={"memory_type": "PREFERENCES", "user_id": "u1"}, depth="standard")

        assert captured["filters"]["memory_type"] == "PREFERENCES"

    @pytest.mark.asyncio
    async def test_type_alias_mapped_async(self, mocker):
        memory = _memory_instance(mocker, AsyncMemory)
        captured = {}

        async def fake_search(query, filters, limit, threshold=0.1, explain=False, show_expired=False, trace_stats=None):
            captured["filters"] = filters
            return []

        memory._search_vector_store = fake_search

        await memory.search("test", filters={"type": "PREFERENCES", "user_id": "u1"}, depth="standard")

        assert captured["filters"].get("memory_type") == "PREFERENCES"
        assert "type" not in captured["filters"]

    @pytest.mark.asyncio
    async def test_memory_type_passthrough_async(self, mocker):
        memory = _memory_instance(mocker, AsyncMemory)
        captured = {}

        async def fake_search(query, filters, limit, threshold=0.1, explain=False, show_expired=False, trace_stats=None):
            captured["filters"] = filters
            return []

        memory._search_vector_store = fake_search

        await memory.search("test", filters={"memory_type": "PREFERENCES", "user_id": "u1"}, depth="standard")

        assert captured["filters"]["memory_type"] == "PREFERENCES"


class TestTypeWeightIntegration:
    def test_weight_boosts_score_sync(self, mocker, monkeypatch):
        monkeypatch.setenv("MEM0_TYPE_WEIGHT_PREFERENCES", "1.5")
        memory = _memory_instance(mocker, Memory)
        memory.vector_store.search.return_value = [
            _candidate("m1", 0.8, "PREFERENCES"),
            _candidate("m2", 0.7, "FACTS"),
        ]

        results = memory._search_vector_store("q", filters={"user_id": "u1"}, limit=10, threshold=0.5)

        by_id = {r["id"]: r for r in results}
        assert by_id["m1"]["score"] == pytest.approx(0.8 * 1.5)
        assert by_id["m2"]["score"] == pytest.approx(0.7)

    def test_no_env_weights_unchanged_sync(self, mocker):
        memory = _memory_instance(mocker, Memory)
        memory.vector_store.search.return_value = [
            _candidate("m1", 0.8, "PREFERENCES"),
            _candidate("m2", 0.7, "FACTS"),
        ]

        results = memory._search_vector_store("q", filters={"user_id": "u1"}, limit=10, threshold=0.5)

        by_id = {r["id"]: r for r in results}
        assert by_id["m1"]["score"] == pytest.approx(0.8)
        assert by_id["m2"]["score"] == pytest.approx(0.7)
        assert results[0]["id"] == "m1"

    @pytest.mark.asyncio
    async def test_weight_boosts_score_async(self, mocker, monkeypatch):
        monkeypatch.setenv("MEM0_TYPE_WEIGHT_PREFERENCES", "1.5")
        memory = _memory_instance(mocker, AsyncMemory)
        memory.vector_store.search.return_value = [
            _candidate("m1", 0.8, "PREFERENCES"),
            _candidate("m2", 0.7, "FACTS"),
        ]

        results = await memory._search_vector_store("q", filters={"user_id": "u1"}, limit=10, threshold=0.5)

        by_id = {r["id"]: r for r in results}
        assert by_id["m1"]["score"] == pytest.approx(0.8 * 1.5)
        assert by_id["m2"]["score"] == pytest.approx(0.7)

    @pytest.mark.asyncio
    async def test_no_env_weights_unchanged_async(self, mocker):
        memory = _memory_instance(mocker, AsyncMemory)
        memory.vector_store.search.return_value = [
            _candidate("m1", 0.8, "PREFERENCES"),
            _candidate("m2", 0.7, "FACTS"),
        ]

        results = await memory._search_vector_store("q", filters={"user_id": "u1"}, limit=10, threshold=0.5)

        by_id = {r["id"]: r for r in results}
        assert by_id["m1"]["score"] == pytest.approx(0.8)
        assert by_id["m2"]["score"] == pytest.approx(0.7)
        assert results[0]["id"] == "m1"
