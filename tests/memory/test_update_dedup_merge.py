"""Chunked extraction may emit multiple UPDATE events for the same memory id.

Each chunk calls the LLM independently, so several chunks can output an UPDATE
event targeting the same existing memory. The old pipeline merged+appended one
update_record per UPDATE event, producing duplicate ids in update_records; the
execution stage then DELETEd the old vector (committed) and INSERTed several
rows with the same primary key -> UniqueViolation -> whole batch rolled back ->
old memory gone, new one never written (data loss seen in production).

Fix contract:
1. UPDATE facts are aggregated per real_id (order preserved).
2. Old text + all new facts are merged in a single LLM call -> one merged text.
3. Each real_id produces exactly one update_record (one delete, one insert).
4. Same-id DELETE+UPDATE -> DELETE wins; single UPDATE behaves as before.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.memory.main import AsyncMemory, Memory

_OLD_TEXT = "服务部署在 192.0.2.163，API 端口 8888"
_NEW_FACT_A = "后端使用 pgvector"
_NEW_FACT_B = "前端使用 React"

_DUAL_UPDATE = json.dumps(
    {
        "memory": [
            {"id": "0", "text": _NEW_FACT_A, "event": "UPDATE", "metadata": {}},
            {"id": "0", "text": _NEW_FACT_B, "event": "UPDATE", "metadata": {}},
        ]
    }
)
_MERGED_TEXT = "合并结果：pgvector + React"
_MERGED_JSON = json.dumps({"merged_text": _MERGED_TEXT})


def _setup_mocks(mocker):
    """Common mock wiring for the extraction pipeline (recipe: core-lib-test-mocking)."""
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


def _memory_with_existing(mocker, memory_cls=Memory):
    _setup_mocks(mocker)
    memory = memory_cls()
    memory.config = mocker.MagicMock()
    memory.config.custom_instructions = None
    memory.config.custom_update_memory_prompt = None
    memory.custom_instructions = None
    memory.api_version = "v1.1"
    memory.db.get_last_messages = MagicMock(return_value=[])
    memory.db.save_messages = MagicMock()
    memory.embedding_model.embed_batch.side_effect = lambda texts, *a, **k: [[0.1, 0.2, 0.3]] * len(texts)
    existing = SimpleNamespace(
        id="uuid-1",
        payload={
            "data": _OLD_TEXT,
            "user_id": "user-1",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    memory.vector_store.search.return_value = [existing]
    memory.vector_store.get.return_value = existing
    return memory


def _merge_router(messages=None, **kwargs):
    """Route generate_response by content: extraction vs semantic merge."""
    if messages and any("【旧记忆】" in (m.get("content") or "") for m in messages):
        return _MERGED_JSON
    return _DUAL_UPDATE


def _assert_single_update(insert_kwargs):
    assert insert_kwargs["ids"] == ["uuid-1"]
    assert len(insert_kwargs["payloads"]) == 1
    return insert_kwargs["payloads"][0]


class TestSyncUpdateDedup:
    def test_dual_update_same_memory_dedupes_to_one_record(self, mocker):
        memory = _memory_with_existing(mocker)
        memory.llm.generate_response.side_effect = _merge_router

        result = memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        assert result == []
        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == _MERGED_TEXT
        # UPDATE 走同 id upsert（零 delete）；去重后仍只写一条记录
        assert memory.vector_store.delete.call_count == 0
        # exactly one merge LLM call, not one per UPDATE event
        assert memory.llm.generate_response.call_count == 2
        merge_call = memory.llm.generate_response.call_args_list[1]
        content = merge_call.kwargs["messages"][1]["content"]
        assert _OLD_TEXT in content
        assert _NEW_FACT_A in content
        assert _NEW_FACT_B in content
        assert "1." in content and "2." in content

    def test_single_update_behavior_unchanged(self, mocker):
        memory = _memory_with_existing(mocker)
        single = json.dumps({"memory": [{"id": "0", "text": _NEW_FACT_A, "event": "UPDATE", "metadata": {}}]})
        memory.llm.generate_response.side_effect = [single, _MERGED_TEXT]

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == _MERGED_TEXT
        assert memory.llm.generate_response.call_count == 2

    def test_delete_wins_over_update_for_same_id(self, mocker):
        memory = _memory_with_existing(mocker)
        delete_update = json.dumps(
            {
                "memory": [
                    {"id": "0", "text": "删除理由", "event": "DELETE", "metadata": {}},
                    {"id": "0", "text": _NEW_FACT_A, "event": "UPDATE", "metadata": {}},
                ]
            }
        )
        memory.llm.generate_response.return_value = delete_update
        memory._remove_memory_from_entity_store = MagicMock()
        memory.on_memory_deleted = MagicMock()

        result = memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        assert result == []
        # DELETE only: no re-insert, no merge LLM call
        assert memory.vector_store.insert.call_count == 0
        memory.vector_store.delete.assert_called_once_with(vector_id="uuid-1")
        assert memory.llm.generate_response.call_count == 1

    def test_merge_failure_falls_back_to_joined_facts_single_record(self, mocker, caplog):
        memory = _memory_with_existing(mocker)
        memory.llm.generate_response.side_effect = [_DUAL_UPDATE, TimeoutError("timeout")]

        with caplog.at_level("WARNING"):
            memory._add_to_vector_store(
                messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
            )

        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == f"{_NEW_FACT_A}\n{_NEW_FACT_B}"


class TestAsyncUpdateDedup:
    @pytest.mark.asyncio
    async def test_async_dual_update_same_memory_dedupes_to_one_record(self, mocker):
        memory = _memory_with_existing(mocker, AsyncMemory)
        memory.llm.generate_response.side_effect = _merge_router

        result = await memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, effective_filters={}, infer=True
        )

        assert result == []
        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == _MERGED_TEXT
        # async UPDATE 同样走 upsert（零 delete）
        assert memory.vector_store.delete.call_count == 0
        assert memory.llm.generate_response.call_count == 2
        merge_call = memory.llm.generate_response.call_args_list[1]
        content = merge_call.kwargs["messages"][1]["content"]
        assert _OLD_TEXT in content
        assert _NEW_FACT_A in content
        assert _NEW_FACT_B in content

    @pytest.mark.asyncio
    async def test_async_single_update_behavior_unchanged(self, mocker):
        memory = _memory_with_existing(mocker, AsyncMemory)
        single = json.dumps({"memory": [{"id": "0", "text": _NEW_FACT_A, "event": "UPDATE", "metadata": {}}]})
        memory.llm.generate_response.side_effect = [single, _MERGED_TEXT]

        await memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, effective_filters={}, infer=True
        )

        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == _MERGED_TEXT
        assert memory.llm.generate_response.call_count == 2

    @pytest.mark.asyncio
    async def test_async_delete_wins_over_update_for_same_id(self, mocker):
        memory = _memory_with_existing(mocker, AsyncMemory)
        delete_update = json.dumps(
            {
                "memory": [
                    {"id": "0", "text": "删除理由", "event": "DELETE", "metadata": {}},
                    {"id": "0", "text": _NEW_FACT_A, "event": "UPDATE", "metadata": {}},
                ]
            }
        )
        memory.llm.generate_response.return_value = delete_update
        memory._remove_memory_from_entity_store = mocker.AsyncMock()
        memory.on_memory_deleted = MagicMock()

        result = await memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, effective_filters={}, infer=True
        )

        assert result == []
        assert memory.vector_store.insert.call_count == 0
        memory.vector_store.delete.assert_called_once_with(vector_id="uuid-1")
        assert memory.llm.generate_response.call_count == 1

    @pytest.mark.asyncio
    async def test_async_merge_failure_falls_back_to_joined_facts_single_record(self, mocker, caplog):
        memory = _memory_with_existing(mocker, AsyncMemory)
        memory.llm.generate_response.side_effect = [_DUAL_UPDATE, TimeoutError("timeout")]

        with caplog.at_level("WARNING"):
            await memory._add_to_vector_store(
                messages=[{"role": "user", "content": "test"}], metadata={}, effective_filters={}, infer=True
            )

        assert memory.vector_store.insert.call_count == 1
        payload = _assert_single_update(memory.vector_store.insert.call_args.kwargs)
        assert payload["data"] == f"{_NEW_FACT_A}\n{_NEW_FACT_B}"
