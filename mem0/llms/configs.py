from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class LlmConfig(BaseModel):
    provider: str = Field(description="Provider of the LLM (e.g., 'ollama', 'openai')", default="openai")
    config: Optional[dict] = Field(description="Configuration for the specific LLM", default={})
    fallbacks: List["LlmConfig"] = Field(default=[], description="兜底 LLM 配置列表")
    layer_timeout: float = Field(default=120.0, description="FallbackLLM 每层超时（秒），与 fallbacks 平级")

    @field_validator("config")
    def validate_config(cls, v, values):
        provider = values.data.get("provider")
        if provider in (
            "openai",
            "ollama",
            "anthropic",
            "groq",
            "together",
            "aws_bedrock",
            "litellm",
            "azure_openai",
            "openai_structured",
            "azure_openai_structured",
            "gemini",
            "deepseek",
            "minimax",
            "xai",
            "sarvam",
            "lmstudio",
            "vllm",
            "langchain",
        ):
            return v
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")


LlmConfig.model_rebuild()
