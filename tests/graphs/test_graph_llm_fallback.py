from unittest import mock

import pytest

from mem0.configs.base import MemoryConfig
from mem0.graphs.falkordb import graph_memory as gm
from mem0.graphs.configs import GraphStoreConfig
from mem0.graphs.falkordb.config import FalkorDBConfig
from mem0.llms.configs import LlmConfig
from mem0.llms.fallback import FallbackLLM


def _llm_cfg(provider, model, fallbacks=(), layer_timeout=120.0):
    return LlmConfig(
        provider=provider,
        config={"model": model},
        fallbacks=[LlmConfig(provider=p, config={"model": m}) for p, m in fallbacks],
        layer_timeout=layer_timeout,
    )


def _memory_config(graph_llm=None, global_llm=None):
    gs = GraphStoreConfig(
        provider="falkordb",
        config=FalkorDBConfig(host="localhost", port=6379, database="mem0"),
        llm=graph_llm,
    )
    return MemoryConfig(graph_store=gs, llm=global_llm or LlmConfig())


@pytest.fixture
def patched():
    with mock.patch.object(gm, "FalkorDB") as falkor_cls, mock.patch.object(
        gm.EmbedderFactory, "create", return_value=mock.MagicMock()
    ), mock.patch.object(gm.LlmFactory, "create") as llm_create:
        falkor_cls.return_value.select_graph.side_effect = lambda name: mock.MagicMock()
        yield llm_create


def test_graph_llm_is_fallback_when_graph_store_llm_has_fallbacks(patched):
    llm_create = patched
    llm_create.side_effect = lambda provider, config: mock.MagicMock()
    cfg = _memory_config(
        graph_llm=_llm_cfg(
            "openai", "gpt-4o",
            fallbacks=[("anthropic", "claude-3-5"), ("deepseek", "deepseek-chat")],
        )
    )

    graph = gm.MemoryGraph(cfg)

    assert isinstance(graph.llm, FallbackLLM)
    assert len(graph.llm._llms) == 3
    providers = [call.args[0] for call in llm_create.call_args_list]
    assert providers == ["openai", "anthropic", "deepseek"]


def test_graph_llm_is_plain_when_no_fallbacks(patched):
    llm_create = patched
    llm_create.side_effect = lambda provider, config: mock.MagicMock()
    cfg = _memory_config(graph_llm=_llm_cfg("openai", "gpt-4o"))

    graph = gm.MemoryGraph(cfg)

    assert not isinstance(graph.llm, FallbackLLM)
    assert llm_create.call_count == 1


def test_graph_llm_uses_global_fallbacks_when_graph_store_llm_unset(patched):
    llm_create = patched
    llm_create.side_effect = lambda provider, config: mock.MagicMock()
    cfg = _memory_config(
        global_llm=_llm_cfg(
            "openai", "gpt-4o",
            fallbacks=[("anthropic", "claude-3-5"), ("deepseek", "deepseek-chat")],
        )
    )

    graph = gm.MemoryGraph(cfg)

    assert isinstance(graph.llm, FallbackLLM)
    assert len(graph.llm._llms) == 3
    providers = [call.args[0] for call in llm_create.call_args_list]
    assert providers == ["openai", "anthropic", "deepseek"]


def test_graph_llm_fallbacks_inherit_primary_sampling_params(patched):
    llm_create = patched
    captured = []

    def capturing(provider, config):
        captured.append(dict(config or {}))
        return mock.MagicMock()

    llm_create.side_effect = capturing
    cfg = _memory_config(
        graph_llm=_llm_cfg(
            "openai", "gpt-4o",
            fallbacks=[("anthropic", "claude-3-5"), ("deepseek", "deepseek-chat")],
        )
    )
    cfg.graph_store.llm.config["temperature"] = 0.1
    cfg.graph_store.llm.config["max_tokens"] = 8192
    cfg.graph_store.llm.config["reasoning_effort"] = "none"

    graph = gm.MemoryGraph(cfg)

    assert isinstance(graph.llm, FallbackLLM)
    assert captured[1]["temperature"] == 0.1 and captured[1]["max_tokens"] == 8192
    assert captured[1]["reasoning_effort"] == "none"
    assert captured[2]["temperature"] == 0.1 and captured[2]["max_tokens"] == 8192
    assert captured[2]["reasoning_effort"] == "none"


def test_graph_llm_wires_layer_timeout(patched):
    llm_create = patched
    llm_create.side_effect = lambda provider, config: mock.MagicMock()
    cfg = _memory_config(
        graph_llm=_llm_cfg(
            "openai", "gpt-4o", fallbacks=[("anthropic", "claude-3-5")], layer_timeout=45.0,
        )
    )

    graph = gm.MemoryGraph(cfg)

    assert isinstance(graph.llm, FallbackLLM)
    assert graph.llm.layer_timeout == 45.0
