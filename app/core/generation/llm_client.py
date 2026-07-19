"""OpenAI-compatible 回答模型客户端。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """回答模型无法产生可用结果。"""

    status_code = 502

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class LLMConfigurationError(LLMError):
    """回答模型尚未正确配置。"""

    status_code = 503


class LLMTimeoutError(LLMError):
    """回答模型调用超时。"""

    status_code = 504


class LLMResponseError(LLMError):
    """回答模型返回了不符合契约的内容。"""


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class LLMCompletion:
    content: str
    model: str
    usage: LLMUsage | None = None


def normalize_llm_provider(provider: str) -> str:
    """归一化回答模型提供方名称。"""
    value = provider.strip().lower()
    if value in {"api", "openai", "openai_compatible", "remote", "bailian", "dashscope"}:
        return "api"
    if value in {"none", "disabled", "off"}:
        return "none"
    raise LLMConfigurationError(f"Unsupported LLM provider: {provider}")


def build_chat_completions_url(base_url: str) -> str:
    """接受 API 根地址或完整端点，生成 Chat Completions URL。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


class LLMClient:
    """调用 OpenAI-compatible Chat Completions 的轻量客户端。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._provider = normalize_llm_provider(self._settings.llm_provider)

    async def complete(self, messages: list[dict[str, Any]]) -> LLMCompletion:
        """生成一次非流式回答。"""
        if self._provider == "none":
            raise LLMConfigurationError("Answer generation is disabled; configure RAG_LLM_PROVIDER")
        base_url = self._settings.llm_base_url
        if not base_url:
            raise LLMConfigurationError("RAG_LLM_BASE_URL is required")
        if not self._settings.llm_model.strip():
            raise LLMConfigurationError("RAG_LLM_MODEL is required")
        api_key = self._settings.llm_api_key or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise LLMConfigurationError("RAG_LLM_API_KEY or DASHSCOPE_API_KEY is required")

        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_output_tokens,
            "stream": False,
        }
        if self._settings.llm_json_response_format:
            payload["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds) as client:
                response = await client.post(
                    build_chat_completions_url(base_url),
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("LLM API request timed out", retryable=True) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise LLMError(
                f"LLM API request failed with HTTP {status}",
                retryable=status == 429 or status >= 500,
            ) from exc
        except httpx.RequestError as exc:
            raise LLMError("LLM API request failed", retryable=True) from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            model = str(body.get("model") or self._settings.llm_model)
            usage = _parse_usage(body.get("usage"))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMResponseError("LLM API returned an invalid response") from exc

        if usage is not None:
            logger.info(
                "回答 API 用量：model=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                model,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            )
        return LLMCompletion(content=content.strip(), model=model, usage=usage)


def _parse_usage(value: Any) -> LLMUsage | None:
    if not isinstance(value, dict):
        return None
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(not isinstance(value.get(field), int) or value[field] < 0 for field in fields):
        return None
    return LLMUsage(**{field: value[field] for field in fields})


def get_llm_client() -> LLMClient:
    return LLMClient()
