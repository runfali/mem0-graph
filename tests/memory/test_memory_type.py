"""Memory-type classification on the write path (Phase 2.7).

LLM extraction outputs a ``metadata.memory_type`` label per memory; when it is
missing or invalid, keyword-based rule fallback assigns one. Retrieval/ranking
is untouched in this batch (zero behavior change on search).
"""

import json
from unittest.mock import MagicMock


from mem0.memory.main import Memory

_VALID_TYPES = ("FACTS", "PREFERENCES", "EXPERIENCES", "OBSERVATIONS", "DECISIONS")


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


def _memory(mocker):
    _setup_mocks(mocker)
    memory = Memory()
    memory.config = mocker.MagicMock()
    memory.config.custom_instructions = None
    memory.config.custom_update_memory_prompt = None
    memory.custom_instructions = None
    memory.api_version = "v1.1"
    memory.db.get_last_messages = MagicMock(return_value=[])
    memory.db.save_messages = MagicMock()
    memory.embedding_model.embed_batch.side_effect = lambda texts, *a, **k: [[0.1, 0.2, 0.3]] * len(texts)
    return memory


def _stored_payload(memory):
    insert_kwargs = memory.vector_store.insert.call_args.kwargs
    if "payloads" in insert_kwargs:
        return insert_kwargs["payloads"][0]
    return memory.vector_store.insert.call_args.args[2][0]


class TestClassifyMemoryType:
    def test_keyword_groups_map_to_types(self):
        from mem0.memory.main import classify_memory_type

        assert classify_memory_type("发哥喜欢喝咖啡") == "PREFERENCES"
        assert classify_memory_type("部署遇到踩坑，修复了超时问题") == "EXPERIENCES"
        assert classify_memory_type("我观察到系统行为有变化") == "OBSERVATIONS"
        assert classify_memory_type("团队决定采用 pgvector") == "DECISIONS"
        assert classify_memory_type("服务器部署在 192.0.2.163") == "FACTS"

    def test_no_keywords_defaults_to_facts(self):
        from mem0.memory.main import classify_memory_type

        assert classify_memory_type("") == "FACTS"
        assert classify_memory_type("数据库当前 11 条记忆") == "FACTS"

    def test_short_text_weak_word_classifies(self):
        from mem0.memory.main import classify_memory_type

        assert classify_memory_type("发现服务延迟升高") == "OBSERVATIONS"
        assert classify_memory_type("配置了 MEM0_RERANK_SCORE_THRESHOLD") == "EXPERIENCES"

    def test_long_text_weak_word_defaults_to_facts(self):
        from mem0.memory.main import classify_memory_type

        long_doc = (
            "本方案旨在解决大规模分布式系统部署与运维的工程问题，覆盖容器编排、存储、网络、"
            "安全多个维度；在实施过程中发现若干配置与监控盲区，并通过自动化与文档沉淀形成"
            "可复用的实践流程，最终产出完整的评估报告供团队参考。"
        )
        assert len(long_doc) > 100
        assert classify_memory_type(long_doc) == "FACTS"

    def test_long_text_with_strong_word_still_classifies(self):
        from mem0.memory.main import classify_memory_type

        long_doc = (
            "在部署过程中踩坑无数次：先是 pgvector 维度不匹配导致建表失败，接着图存储连接"
            "超时，最后通过逐项排查与索引重建解决了问题，整个过程耗时两天并记录了完整的排查日志，"
            "后续同类问题可以直接参考这套处置流程。"
        )
        assert len(long_doc) > 100
        assert classify_memory_type(long_doc) == "EXPERIENCES"

    def test_valid_llm_type_passthrough(self):
        from mem0.memory.main import classify_memory_type

        assert classify_memory_type("服务器在 192.0.2.163", llm_type="EXPERIENCES") == "EXPERIENCES"
        assert classify_memory_type("服务器在 192.0.2.163", llm_type="FACTS") == "FACTS"

    def test_invalid_llm_type_falls_back_to_rules(self):
        from mem0.memory.main import classify_memory_type

        assert classify_memory_type("发哥喜欢咖啡", llm_type="TEMP") == "PREFERENCES"
        assert classify_memory_type("决定采用 pgvector", llm_type="INVALID") == "DECISIONS"


class TestMemoryTypeOnWritePath:
    def test_valid_llm_memory_type_passthrough_to_payload(self, mocker):
        memory = _memory(mocker)
        memory.llm.generate_response.return_value = json.dumps(
            {
                "memory": [
                    {
                        "id": "0",
                        "text": "发哥换了新的数据库",
                        "event": "ADD",
                        "metadata": {"lane": "normal", "importance": 3, "memory_type": "EXPERIENCES"},
                    }
                ]
            },
            ensure_ascii=False,
        )

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        payload = _stored_payload(memory)
        assert payload["memory_type"] == "EXPERIENCES"

    def test_missing_memory_type_falls_back_by_keywords(self, mocker):
        memory = _memory(mocker)
        memory.llm.generate_response.return_value = json.dumps(
            {
                "memory": [
                    {
                        "id": "0",
                        "text": "发哥喜欢 pgvector",
                        "event": "ADD",
                        "metadata": {"lane": "normal"},
                    }
                ]
            },
            ensure_ascii=False,
        )

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        payload = _stored_payload(memory)
        assert payload["memory_type"] == "PREFERENCES"

    def test_invalid_memory_type_falls_back_by_keywords(self, mocker):
        memory = _memory(mocker)
        memory.llm.generate_response.return_value = json.dumps(
            {
                "memory": [
                    {
                        "id": "0",
                        "text": "我踩坑了，最后修复了问题",
                        "event": "ADD",
                        "metadata": {"lane": "normal", "importance": 3, "memory_type": "TEMP"},
                    }
                ]
            },
            ensure_ascii=False,
        )

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        payload = _stored_payload(memory)
        assert payload["memory_type"] == "EXPERIENCES"

    def test_bare_string_memory_gets_rule_fallback(self, mocker):
        memory = _memory(mocker)
        memory.llm.generate_response.return_value = json.dumps(
            {"memory": ["发哥喜欢咖啡"]}, ensure_ascii=False
        )

        memory._add_to_vector_store(
            messages=[{"role": "user", "content": "test"}], metadata={}, filters={}, infer=True
        )

        payload = _stored_payload(memory)
        assert payload["memory_type"] == "PREFERENCES"
