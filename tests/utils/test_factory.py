from unittest.mock import Mock, patch

from mem0.configs.llms.anthropic import AnthropicConfig
from mem0.configs.llms.aws_bedrock import AWSBedrockConfig
from mem0.configs.llms.base import BaseLlmConfig
from mem0.configs.llms.openai import OpenAIConfig
from mem0.utils.factory import LlmFactory


def _capture_config(provider_name, config):
    """Build an LLM via the factory and return the config it was constructed with."""
    captured = {}

    def fake_llm_class(built_config):
        captured["config"] = built_config
        return Mock()

    with patch("mem0.utils.factory.load_class", return_value=fake_llm_class):
        LlmFactory.create(provider_name, config)

    return captured["config"]


def test_base_to_openai_preserves_reasoning_fields():
    base_config = BaseLlmConfig(model="o3", reasoning_effort="high", is_reasoning_model=True)

    built = _capture_config("openai", base_config)

    assert isinstance(built, OpenAIConfig)
    assert built.reasoning_effort == "high"
    assert built.is_reasoning_model is True


def test_base_to_kwargs_provider_preserves_reasoning_fields():
    # AWSBedrockConfig accepts the reasoning fields via **kwargs, so they must survive.
    base_config = BaseLlmConfig(model="amazon.nova", reasoning_effort="medium", is_reasoning_model=True)

    built = _capture_config("aws_bedrock", base_config)

    assert isinstance(built, AWSBedrockConfig)
    assert built.reasoning_effort == "medium"
    assert built.is_reasoning_model is True


def test_base_to_provider_without_reasoning_fields_still_builds():
    # Anthropic config does not accept reasoning_effort/is_reasoning_model;
    # the conversion must not forward unsupported kwargs to it.
    base_config = BaseLlmConfig(model="claude-3-5-sonnet-20240620")

    built = _capture_config("anthropic", base_config)

    assert isinstance(built, AnthropicConfig)
    assert built.model == "claude-3-5-sonnet-20240620"


def test_dict_config_to_provider_without_reasoning_param_still_builds():
    # dict 路径同护栏：兜底层继承注入的 reasoning_effort 不得打崩
    # 未声明该形参且无 **kwargs 的 provider（2026-08-30 审计 P1：anthropic 兜底 + L0 开启继承即 TypeError）
    built = _capture_config("anthropic", {"model": "claude-3-5", "api_key": "x", "reasoning_effort": "none"})

    assert isinstance(built, AnthropicConfig)
    assert built.model == "claude-3-5"
    # 未声明形参 → 键被剥掉，实例仅剩 BaseLlmConfig 默认 None（未被 L0 值污染）
    assert built.reasoning_effort is None


def test_dict_config_reasoning_effort_preserved_when_declared():
    # 声明过 reasoning_effort 的 provider（openai/azure 等）必须原样保留
    built = _capture_config("openai", {"model": "gpt-4o", "api_key": "x", "reasoning_effort": "none"})

    assert isinstance(built, OpenAIConfig)
    assert built.reasoning_effort == "none"
