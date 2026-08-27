import pytest

from mem0.llms.configs import LlmConfig
from mem0.llms.fallback import FallbackLLM
from mem0.memory import main as memory_main


class MockLLM:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.call_count = 0
        self.call_kwargs = []

    def generate_response(self, messages, response_format=None, tools=None, tool_choice="auto", **kwargs):
        self.call_count += 1
        self.call_kwargs.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


class HTTP500(Exception):
    def __init__(self):
        super().__init__("internal server error")
        self.status_code = 500


class TypeErrorThenSuccessLLM:
    def __init__(self, result=None):
        self.result = result
        self.call_count = 0
        self.call_kwargs = []

    def generate_response(self, messages, response_format=None, tools=None, tool_choice="auto", **kwargs):
        self.call_count += 1
        self.call_kwargs.append(kwargs)
        if self.call_count == 1:
            raise TypeError("unexpected keyword argument 'timeout'")
        return self.result


class OpenAILikeLLM:
    def __init__(self, result=None):
        self.result = result
        self.call_count = 0
        self.call_kwargs = []

    def generate_response(self, messages, response_format=None, tools=None, tool_choice="auto", **kwargs):
        self.call_count += 1
        self.call_kwargs.append(kwargs)
        if "max_retries" in kwargs:
            raise TypeError("Completions.create() got an unexpected keyword argument 'max_retries'")
        return self.result


class FakeClient:
    def __init__(self, max_retries=2):
        self.max_retries = max_retries


class ClientfulLLM(MockLLM):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.client = FakeClient()


def _make(primary, *fallbacks, layer_timeout=120.0):
    return FallbackLLM(primary, list(fallbacks), layer_timeout=layer_timeout)


def test_primary_success_no_fallback_calls():
    primary = MockLLM(result="ok")
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert primary.call_count == 1
    assert fb1.call_count == 0


def test_injects_timeout_only():
    primary = MockLLM(result="ok")
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    fallback.generate_response([{"role": "user", "content": "hi"}])

    assert primary.call_kwargs[0]["timeout"] == 120.0
    assert "max_retries" not in primary.call_kwargs[0]


def test_openai_path_does_not_trigger_type_error():
    primary = OpenAILikeLLM(result="ok")
    fallback = _make(primary)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert primary.call_count == 1
    assert "timeout" in primary.call_kwargs[0]


def test_type_error_fallback_calls_without_timeout():
    primary = TypeErrorThenSuccessLLM(result="ok")
    fallback = _make(primary)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "ok"
    assert primary.call_count == 2
    assert "timeout" in primary.call_kwargs[0]
    assert "timeout" not in primary.call_kwargs[1]


def test_init_disables_client_retries():
    primary = ClientfulLLM(result="ok")
    fb1 = ClientfulLLM(result="fb1")
    _make(primary, fb1)  # construction is the behavior under test: retries disabled at init

    assert primary.client.max_retries == 0
    assert fb1.client.max_retries == 0


def test_init_skips_llm_without_client():
    primary = MockLLM(result="ok")
    fb1 = MockLLM(result="fb1")

    fallback = _make(primary, fb1)

    assert fallback.layer_timeout == 120.0


def test_default_layer_timeout_is_120():
    primary = MockLLM(result="ok")

    fallback = FallbackLLM(primary, [])

    assert fallback.layer_timeout == 120.0


def test_custom_layer_timeout_is_used():
    primary = MockLLM(result="ok")

    fallback = FallbackLLM(primary, [], layer_timeout=30.0)
    fallback.generate_response([{"role": "user", "content": "hi"}])

    assert fallback.layer_timeout == 30.0
    assert primary.call_kwargs[0]["timeout"] == 30.0


def test_primary_raises_fallback1_used():
    primary = MockLLM(exc=ValueError("fail"))
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "fb1"
    assert fb1.call_count == 1


def test_primary_and_fallback1_fail_fallback2_returns():
    primary = MockLLM(exc=ValueError("fail"))
    fb1 = MockLLM(exc=ValueError("fail"))
    fb2 = MockLLM(result="fb2")
    fallback = _make(primary, fb1, fb2)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "fb2"
    assert fb2.call_count == 1


def test_all_fail_raises_last_layer_exception():
    last_exc = ValueError("last layer")
    primary = MockLLM(exc=ValueError("a"))
    fb1 = MockLLM(exc=ValueError("b"))
    fb2 = MockLLM(exc=last_exc)
    fallback = _make(primary, fb1, fb2)

    with pytest.raises(ValueError) as excinfo:
        fallback.generate_response([{"role": "user", "content": "hi"}])

    assert excinfo.value is last_exc


def test_http_500_retries_3_times_then_switches_layer():
    primary = MockLLM(exc=HTTP500())
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "fb1"
    assert primary.call_count == 3
    assert fb1.call_count == 1


def test_timeout_switches_layer_without_retry():
    primary = MockLLM(exc=TimeoutError("timed out"))
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == "fb1"
    assert primary.call_count == 1
    assert fb1.call_count == 1


def test_primary_normal_return_no_switch():
    payload = {"data": 200}
    primary = MockLLM(result=payload)
    fb1 = MockLLM(result="fb1")
    fallback = _make(primary, fb1)

    result = fallback.generate_response([{"role": "user", "content": "hi"}])

    assert result == payload
    assert fb1.call_count == 0


def _patch_factory(monkeypatch, created):
    def fake_create(provider, config, **kwargs):
        m = MockLLM(result=provider)
        created.append(m)
        return m

    monkeypatch.setattr(memory_main.LlmFactory, "create", staticmethod(fake_create))


def test_build_llm_returns_fallback_when_fallbacks_configured(monkeypatch):
    created = []
    _patch_factory(monkeypatch, created)
    cfg = LlmConfig(
        provider="openai",
        config={"model": "gpt-4o"},
        fallbacks=[
            LlmConfig(provider="anthropic", config={"model": "claude-3-5"}),
            LlmConfig(provider="deepseek", config={"model": "deepseek-chat"}),
        ],
    )

    llm = memory_main._build_llm(cfg)

    assert isinstance(llm, FallbackLLM)
    assert len(created) == 3
    assert len(llm._llms) == 3


def test_build_llm_wires_layer_timeout(monkeypatch):
    created = []
    _patch_factory(monkeypatch, created)
    cfg = LlmConfig(
        provider="openai",
        config={"model": "gpt-4o"},
        fallbacks=[LlmConfig(provider="anthropic", config={"model": "claude-3-5"})],
        layer_timeout=45.0,
    )

    llm = memory_main._build_llm(cfg)

    assert isinstance(llm, FallbackLLM)
    assert llm.layer_timeout == 45.0


def test_build_llm_returns_primary_when_no_fallbacks(monkeypatch):
    created = []
    _patch_factory(monkeypatch, created)
    cfg = LlmConfig(provider="openai", config={"model": "gpt-4o"})

    llm = memory_main._build_llm(cfg)

    assert not isinstance(llm, FallbackLLM)
    assert len(created) == 1


def test_build_llm_fallbacks_inherit_sampling_params(monkeypatch):
    """兜底层缺省 temperature/max_tokens 时继承主层（config.json/env/dashboard 三路共用此修复）。"""
    captured = []

    def fake_create(provider, config, **kwargs):
        captured.append(dict(config or {}))
        return MockLLM(result=provider)

    monkeypatch.setattr(memory_main.LlmFactory, "create", staticmethod(fake_create))
    cfg = LlmConfig(
        provider="openai",
        config={"model": "main", "temperature": 0.1, "max_tokens": 8192},
        fallbacks=[
            LlmConfig(provider="openai", config={"model": "fb-missing"}),
            LlmConfig(provider="anthropic", config={"model": "fb-explicit", "max_tokens": 1024}),
        ],
    )

    llm = memory_main._build_llm(cfg)

    assert isinstance(llm, FallbackLLM)
    assert captured[1]["temperature"] == 0.1 and captured[1]["max_tokens"] == 8192  # 继承
    assert captured[2]["max_tokens"] == 1024 and captured[2]["temperature"] == 0.1  # 显式优先、缺的键仍继承


def test_build_llm_skips_inheritance_when_primary_lacks_keys(monkeypatch):
    """主层自身缺 temperature/max_tokens（或值为 None）→ 兜底层不注入、不炸。"""
    captured = []

    def fake_create(provider, config, **kwargs):
        captured.append(dict(config or {}))
        return MockLLM(result=provider)

    monkeypatch.setattr(memory_main.LlmFactory, "create", staticmethod(fake_create))
    cfg = LlmConfig(
        provider="openai",
        config={"model": "main"},
        fallbacks=[
            LlmConfig(provider="deepseek", config={"model": "fb1"}),
            LlmConfig(provider="openai", config={"model": "fb2", "temperature": None}),
        ],
    )

    llm = memory_main._build_llm(cfg)

    assert isinstance(llm, FallbackLLM)
    assert "temperature" not in captured[1] and "max_tokens" not in captured[1]
    assert "temperature" not in captured[2] and "max_tokens" not in captured[2]
