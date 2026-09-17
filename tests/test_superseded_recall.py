"""已被精炼（superseded）的原记忆必须退出召回结果。

背景：refine apply 会给原记忆打 payload["superseded_by"]（软标记，不物理删除），
但该标记此前只有一个消费方——_collect_stale_items 跳过它们，防止被重复精炼。
召回路径完全不过滤，导致已精炼的原记忆与精炼产物同时出现在结果里（2026-09-17 实测）。

过滤点选在 _search_vector_store 的 Step 9 格式化循环：那里能拿到 payload，
且位于 rerank/top_k 截断之前——被精炼的条目不该占用召回名额。
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from mem0.configs.base import MemoryConfig


class MockVectorMemory:
    """与 tests/test_memory.py 同形的向量库结果对象。"""

    def __init__(self, memory_id: str, payload: dict, score: float = 0.8):
        self.id = memory_id
        self.payload = payload
        self.score = score


def _build_memory():
    with patch("mem0.utils.factory.EmbedderFactory.create") as emb, \
         patch("mem0.utils.factory.VectorStoreFactory.create") as vec, \
         patch("mem0.utils.factory.LlmFactory.create") as llm, \
         patch("mem0.memory.main.SQLiteManager"):
        emb.return_value = MagicMock()
        llm.return_value = MagicMock()
        store = MagicMock()
        store.keyword_search.return_value = []
        vec.return_value = store
        from mem0.memory.main import Memory as MemoryClass
        memory = MemoryClass(MemoryConfig())
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1, 0.2, 0.3]
        memory.embedding_model = mock_embedder
        return memory, store


def _search_with(rows, monkeypatch=None, enabled=None):
    memory, store = _build_memory()
    store.search.return_value = rows
    if enabled is not None:
        os.environ["MEM0_FILTER_SUPERSEDED"] = enabled
    try:
        return memory._search_vector_store("test", {"user_id": "u1"}, 10)
    finally:
        os.environ.pop("MEM0_FILTER_SUPERSEDED", None)


def test_superseded_memory_is_excluded_from_recall():
    rows = [
        MockVectorMemory("active", {"data": "仍有效的记忆", "user_id": "u1"}),
        MockVectorMemory(
            "old",
            {"data": "已被精炼的原记忆", "user_id": "u1", "superseded_by": "new-1"},
        ),
    ]
    out = _search_with(rows)
    assert [m["id"] for m in out] == ["active"]


def test_active_memory_is_unaffected():
    rows = [MockVectorMemory("a", {"data": "A", "user_id": "u1"})]
    out = _search_with(rows)
    assert [m["id"] for m in out] == ["a"]


def test_empty_superseded_value_does_not_filter():
    """空串/None 一律视为未精炼，避免误杀。"""
    rows = [
        MockVectorMemory("e1", {"data": "A", "user_id": "u1", "superseded_by": ""}),
        MockVectorMemory("e2", {"data": "B", "user_id": "u1", "superseded_by": None}),
    ]
    out = _search_with(rows)
    assert [m["id"] for m in out] == ["e1", "e2"]


def test_env_switch_disables_filtering():
    """出问题时可一键退回：MEM0_FILTER_SUPERSEDED=false。"""
    rows = [
        MockVectorMemory("active", {"data": "A", "user_id": "u1"}),
        MockVectorMemory("old", {"data": "B", "user_id": "u1", "superseded_by": "n1"}),
    ]
    out = _search_with(rows, enabled="false")
    assert sorted(m["id"] for m in out) == ["active", "old"]


def test_filtering_happens_before_top_k_truncation():
    """被精炼条目不该占用召回名额：上限内应补足有效记忆。"""
    rows = [
        MockVectorMemory("old1", {"data": "x", "user_id": "u1", "superseded_by": "n"}),
        MockVectorMemory("old2", {"data": "y", "user_id": "u1", "superseded_by": "n"}),
        MockVectorMemory("keep1", {"data": "z", "user_id": "u1"}),
        MockVectorMemory("keep2", {"data": "w", "user_id": "u1"}),
    ]
    memory, store = _build_memory()
    store.search.return_value = rows
    out = memory._search_vector_store("test", {"user_id": "u1"}, 2)
    assert sorted(m["id"] for m in out) == ["keep1", "keep2"]
