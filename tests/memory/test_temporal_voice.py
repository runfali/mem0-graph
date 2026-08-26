"""Integration tests for the temporal voice in the search pipeline (件 3).

Covers: intent gating, temporal candidate merge + dedup, rerank threshold
exemption, fused recency-aware sorting (strong/weak intent), degradation,
trace observability, intent_to_range/effective_date normalization, and the
async search path. Mock style mirrors tests/memory/test_temporal_usage_notice.py.
"""

import logging
from collections import OrderedDict
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from mem0.memory import main as memory_main
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.temporal_intent import effective_date, intent_to_range

_SHANGHAI_TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _fmt(d):
    return d.strftime("%Y-%m-%d")


def _payload(data, **extra):
    payload = {"data": data, "created_at": "2026-08-01T00:00:00+00:00"}
    payload.update(extra)
    return payload


class _Reranker:
    """Scores each candidate by id; keeps the original order otherwise."""

    def __init__(self, scores):
        self.scores = scores

    def rerank(self, query, memories, limit):
        for m in memories:
            m["rerank_score"] = self.scores.get(m["id"], 0.5)
        return memories


def make_sync_memory(vector_candidates=None, temporal_results=None, reranker=None):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(llm=SimpleNamespace(config={}), enable_search_depth=False, enable_lane=False)
    memory.api_version = "v1.1"
    memory.reranker = reranker
    memory.graph = None
    memory._search_depth_cache = OrderedDict()
    memory.vector_store = MagicMock()
    memory.vector_store.temporal_search = MagicMock(return_value=temporal_results or [])
    memory._search_vector_store = MagicMock(return_value=vector_candidates or [])
    return memory


def make_async_memory(vector_candidates=None, temporal_results=None, reranker=None):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(llm=SimpleNamespace(config={}), enable_search_depth=False, enable_lane=False)
    memory.api_version = "v1.1"
    memory.reranker = reranker
    memory.graph = None
    memory._search_depth_cache = OrderedDict()
    memory.vector_store = MagicMock()
    memory.vector_store.temporal_search = MagicMock(return_value=temporal_results or [])
    memory._search_vector_store = AsyncMock(return_value=vector_candidates or [])
    return memory


def _patch_harness(monkeypatch):
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_first_run_notice", MagicMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", MagicMock())
    monkeypatch.setattr(memory_main, "display_scale_threshold_notice", MagicMock())
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", AsyncMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice_async", AsyncMock())
    monkeypatch.setattr(memory_main, "display_scale_threshold_notice_async", AsyncMock())


class TestSyncTemporalVoice:
    def test_full_depth_time_intent_merges_temporal_candidates(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_TEMPORAL_TOP_K", "15")
        monkeypatch.setenv("MEM0_TEMPORAL_HALFLIFE_HOURS", "96")
        today = _SHANGHAI_TODAY
        temporal = SimpleNamespace(
            id="mem-t",
            score=0.9,
            payload=_payload("最近部署了新版网关", temporal_date=_fmt(today - timedelta(days=1))),
        )
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("语义候选")}],
            temporal_results=[temporal],
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"})

        memory.vector_store.temporal_search.assert_called_once_with(
            filters={"user_id": "u1"},
            start=_fmt(today - timedelta(days=7)),
            end=_fmt(today),
            top_k=15,
            half_life_hours=96,
        )
        merged = next(m for m in result["results"] if m["id"] == "mem-t")
        assert merged["source"] == "temporal"
        assert merged["recall_channel"] == "temporal"
        assert merged["memory"] == "最近部署了新版网关"
        assert merged["score"] == 0.9

    def test_plain_query_does_not_call_temporal_search(self, monkeypatch):
        _patch_harness(monkeypatch)
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        Memory.search(memory, "部署了什么", filters={"user_id": "u1"})

        memory.vector_store.temporal_search.assert_not_called()

    def test_force_full_disabled_standard_depth_does_not_call_temporal_search(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
        monkeypatch.setenv("MEM0_TEMPORAL_FORCE_FULL", "false")
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, depth="standard")

        memory.vector_store.temporal_search.assert_not_called()

    def test_disabled_by_env_flag_does_not_call_temporal_search(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_TEMPORAL_VOICE", "false")
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"})

        memory.vector_store.temporal_search.assert_not_called()

    def test_exclusion_word_query_does_not_call_temporal_search(self, monkeypatch):
        _patch_harness(monkeypatch)
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        Memory.search(memory, "今天天气怎么样", filters={"user_id": "u1"})

        memory.vector_store.temporal_search.assert_not_called()

    def test_temporal_candidate_dedup_by_id(self, monkeypatch):
        _patch_harness(monkeypatch)
        payload = _payload("最近部署了网关", temporal_date=_fmt(_SHANGHAI_TODAY))
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-same", "score": 0.8, "payload": payload}],
            temporal_results=[SimpleNamespace(id="mem-same", score=0.95, payload=payload)],
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"})

        assert [m["id"] for m in result["results"]].count("mem-same") == 1

    def test_temporal_candidate_exempt_from_rerank_threshold(self, monkeypatch):
        _patch_harness(monkeypatch)
        reranker = _Reranker({"mem-v1": 0.9, "mem-v2": 0.8, "mem-t": 0.2})
        memory = make_sync_memory(
            vector_candidates=[
                {"id": "mem-v1", "score": 0.8, "payload": _payload("a")},
                {"id": "mem-v2", "score": 0.7, "payload": _payload("b")},
            ],
            temporal_results=[
                SimpleNamespace(id="mem-t", score=0.0, payload=_payload("最近部署", temporal_date=_fmt(_SHANGHAI_TODAY)))
            ],
            reranker=reranker,
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, rerank=True)

        assert "mem-t" in [m["id"] for m in result["results"]]

    def test_standard_depth_time_intent_upgrades_to_full(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
        temporal = SimpleNamespace(
            id="mem-t", score=0.9, payload=_payload("最近部署", temporal_date=_fmt(_SHANGHAI_TODAY))
        )
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}],
            temporal_results=[temporal],
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, depth="standard", trace=True)

        memory.vector_store.temporal_search.assert_called_once()
        assert result["trace"]["depth"] == "full"
        assert any(m.get("source") == "temporal" for m in result["results"])

    def test_minimal_depth_time_intent_upgrades_to_full(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
        temporal = SimpleNamespace(
            id="mem-t", score=0.9, payload=_payload("昨天部署", temporal_date=_fmt(_SHANGHAI_TODAY))
        )
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}],
            temporal_results=[temporal],
        )

        result = Memory.search(memory, "昨天部署了什么", filters={"user_id": "u1"}, depth="minimal", trace=True)

        memory.vector_store.temporal_search.assert_called_once()
        assert result["trace"]["depth"] == "full"
        assert any(m.get("source") == "temporal" for m in result["results"])

    def test_plain_query_standard_depth_not_upgraded(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        result = Memory.search(memory, "部署了什么", filters={"user_id": "u1"}, depth="standard", trace=True)

        memory.vector_store.temporal_search.assert_not_called()
        assert result["trace"]["depth"] == "standard"

    def test_standard_depth_time_intent_runs_rerank(self, monkeypatch):
        _patch_harness(monkeypatch)
        monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
        reranker = _Reranker({"mem-v": 0.9, "mem-t": 0.3})
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.8, "payload": _payload("x")}],
            temporal_results=[
                SimpleNamespace(id="mem-t", score=0.9, payload=_payload("最近部署", temporal_date=_fmt(_SHANGHAI_TODAY)))
            ],
            reranker=reranker,
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, depth="standard", rerank=True)

        memory.vector_store.temporal_search.assert_called_once()
        assert "mem-t" in [m["id"] for m in result["results"]]

    def test_fusion_sort_ranks_recent_first(self, monkeypatch):
        _patch_harness(monkeypatch)
        today = _SHANGHAI_TODAY
        reranker = _Reranker({"mem-recent": 0.5, "mem-old": 0.6, "mem-temp": 0.4})
        memory = make_sync_memory(
            vector_candidates=[
                {"id": "mem-recent", "score": 0.8, "payload": _payload("近期部署", temporal_date=_fmt(today))},
                {"id": "mem-old", "score": 0.7, "payload": _payload("旧部署", temporal_date=_fmt(today - timedelta(days=100)))},
            ],
            temporal_results=[
                SimpleNamespace(id="mem-temp", score=0.75, payload=_payload("最近部署", temporal_date=_fmt(today - timedelta(days=3))))
            ],
            reranker=reranker,
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, rerank=True)

        assert [m["id"] for m in result["results"]] == ["mem-recent", "mem-temp", "mem-old"]

    def test_no_intent_keeps_existing_sort(self, monkeypatch):
        _patch_harness(monkeypatch)
        today = _SHANGHAI_TODAY
        reranker = _Reranker({"mem-recent": 0.5, "mem-old": 0.6})
        memory = make_sync_memory(
            vector_candidates=[
                {"id": "mem-recent", "score": 0.8, "payload": _payload("近期部署", temporal_date=_fmt(today))},
                {"id": "mem-old", "score": 0.7, "payload": _payload("旧部署", temporal_date=_fmt(today - timedelta(days=100)))},
            ],
            reranker=reranker,
        )

        result = Memory.search(memory, "部署了什么", filters={"user_id": "u1"}, rerank=True)

        assert [m["id"] for m in result["results"]] == ["mem-old", "mem-recent"]

    def test_temporal_search_failure_degrades_gracefully(self, monkeypatch, caplog):
        _patch_harness(monkeypatch)
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )
        memory.vector_store.temporal_search = MagicMock(side_effect=RuntimeError("pg down"))

        with caplog.at_level(logging.WARNING):
            result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"})

        assert any("Temporal search failed" in r.message for r in caplog.records)
        assert [m["id"] for m in result["results"]] == ["mem-v"]
        assert not any(m.get("source") == "temporal" for m in result["results"])

    def test_trace_reports_temporal_stage_when_triggered(self, monkeypatch):
        _patch_harness(monkeypatch)
        temporal = SimpleNamespace(
            id="mem-t", score=0.9, payload=_payload("最近部署", temporal_date=_fmt(_SHANGHAI_TODAY))
        )
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}],
            temporal_results=[temporal],
        )

        result = Memory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, trace=True)

        trace = result["trace"]
        assert trace["temporal_triggered"] is True
        stages = {s["stage"]: s for s in trace["stages"]}
        assert stages["temporal"]["count"] == 1
        assert stages["temporal"]["latency_ms"] >= 0

    def test_trace_no_intent_reports_temporal_triggered_false(self, monkeypatch):
        _patch_harness(monkeypatch)
        memory = make_sync_memory(
            vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
        )

        result = Memory.search(memory, "部署了什么", filters={"user_id": "u1"}, trace=True)

        assert result["trace"]["temporal_triggered"] is False
        assert {s["stage"] for s in result["trace"]["stages"]} >= {"temporal"}


class TestIntentToRange:
    def test_recent_intent(self):
        start, end = intent_to_range({"type": "recent", "days": 7}, today=date(2026, 8, 13))
        assert (start, end) == ("2026-08-06", "2026-08-13")

    def test_date_intent(self):
        start, end = intent_to_range({"type": "date", "date": "2026-08-10"}, today=date(2026, 8, 13))
        assert (start, end) == ("2026-08-10", "2026-08-10")

    def test_range_intent_single_sided(self):
        assert intent_to_range({"type": "range", "start": None, "end": "2026-08-01"}, today=date(2026, 8, 13)) == (
            None,
            "2026-08-01",
        )
        assert intent_to_range({"type": "range", "start": "2026-08-01", "end": None}, today=date(2026, 8, 13)) == (
            "2026-08-01",
            None,
        )

    def test_recent_defaults_to_today_when_omitted(self):
        start, end = intent_to_range({"type": "recent", "days": 7})
        today = _SHANGHAI_TODAY
        assert (start, end) == (_fmt(today - timedelta(days=7)), _fmt(today))


class TestEffectiveDate:
    def test_temporal_date_wins(self):
        assert effective_date({"temporal_date": "2026-08-10", "created_at": "2026-08-01T00:00:00+00:00"}) == date(2026, 8, 10)

    def test_created_at_converted_to_shanghai(self):
        assert effective_date({"created_at": "2026-08-11T20:00:00+00:00"}) == date(2026, 8, 12)

    def test_dirty_data_never_raises(self):
        assert effective_date({"created_at": "garbage"}) is None
        assert effective_date({"temporal_date": "not-a-date", "created_at": "2026-08-01T00:00:00+00:00"}) == date(2026, 8, 1)
        assert effective_date(None) is None
        assert effective_date({}) is None


@pytest.mark.asyncio
async def test_async_time_intent_merges_and_fusion_sorts(monkeypatch):
    _patch_harness(monkeypatch)
    today = _SHANGHAI_TODAY
    reranker = _Reranker({"mem-recent": 0.5, "mem-old": 0.6, "mem-temp": 0.4})
    memory = make_async_memory(
        vector_candidates=[
            {"id": "mem-recent", "score": 0.8, "payload": _payload("近期部署", temporal_date=_fmt(today))},
            {"id": "mem-old", "score": 0.7, "payload": _payload("旧部署", temporal_date=_fmt(today - timedelta(days=100)))},
        ],
        temporal_results=[
            SimpleNamespace(id="mem-temp", score=0.75, payload=_payload("最近部署", temporal_date=_fmt(today - timedelta(days=3))))
        ],
        reranker=reranker,
    )

    result = await AsyncMemory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, rerank=True)

    memory.vector_store.temporal_search.assert_called_once()
    assert [m["id"] for m in result["results"]] == ["mem-recent", "mem-temp", "mem-old"]
    assert any(m.get("source") == "temporal" for m in result["results"])


@pytest.mark.asyncio
async def test_async_no_intent_does_not_call_temporal_search(monkeypatch):
    _patch_harness(monkeypatch)
    memory = make_async_memory(
        vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}]
    )

    await AsyncMemory.search(memory, "部署了什么", filters={"user_id": "u1"})

    memory.vector_store.temporal_search.assert_not_called()


@pytest.mark.asyncio
async def test_async_standard_depth_time_intent_upgrades_to_full(monkeypatch):
    _patch_harness(monkeypatch)
    monkeypatch.setenv("MEM0_SEARCH_DEPTH_AUTO", "false")
    memory = make_async_memory(
        vector_candidates=[{"id": "mem-v", "score": 0.7, "payload": _payload("x")}],
        temporal_results=[
            SimpleNamespace(id="mem-t", score=0.9, payload=_payload("最近部署", temporal_date=_fmt(_SHANGHAI_TODAY)))
        ],
    )

    result = await AsyncMemory.search(memory, "最近部署了什么", filters={"user_id": "u1"}, depth="standard", trace=True)

    memory.vector_store.temporal_search.assert_called_once()
    assert result["trace"]["depth"] == "full"
    assert any(m.get("source") == "temporal" for m in result["results"])


def test_iso_to_weak_range_extensions():
    """五轮审计：ISO 起点 + 中文连接词/弱终点组合的 range 语义。"""
    from mem0.memory.temporal_intent import detect_temporal_intent

    # 到 + 今天（四轮基线）
    r = detect_temporal_intent("2026-08-01 到今天")
    assert r["type"] == "range" and r["start"] == "2026-08-01"
    assert r["end"] is not None
    # 之后 + 今天（残留收口）
    r = detect_temporal_intent("2026-08-01 之后到今天")
    assert r["type"] == "range" and r["start"] == "2026-08-01"
    # 以来 → 单侧开区间
    r = detect_temporal_intent("2026-08-01 以来")
    assert r["type"] == "range" and r["start"] == "2026-08-01" and r["end"] is None
    # 至今 → 到今天
    r = detect_temporal_intent("2026-08-01 至今")
    assert r["type"] == "range" and r["start"] == "2026-08-01" and r["end"] is not None
    # 双 ISO + 尾随弱词 → 双 ISO 优先
    r = detect_temporal_intent("2026-08-01 到 2026-08-10 今天")
    assert r["type"] == "range" and r["start"] == "2026-08-01" and r["end"] == "2026-08-10"
    # 反向：今天到 ISO
    r = detect_temporal_intent("今天到 2026-08-01")
    assert r["type"] == "range" and r["end"] == "2026-08-01"
