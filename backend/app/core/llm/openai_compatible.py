"""OpenAI 兼容协议的通用适配器。

DeepSeek、通义、Kimi、智谱、Ollama、vLLM 等大多数服务都实现了 /chat/completions，
因此一套实现即可覆盖，只需在 .env 里换 base_url / model / api_key。
"""

from __future__ import annotations

import time

import httpx

from app.core.llm.base import LLMAdapter, LLMError, LLMResult, Message
from app.core.progress import emit


class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        default_temperature: float = 0.2,
        extra_body: dict | None = None,
    ) -> None:
        self.name = provider
        self._provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._default_temperature = default_temperature
        # 各家控制"是否输出思考过程"的字段名互不相同（Qwen 用 chat_template_kwargs、
        # 部分网关用 thinking / reasoning_effort），因此这里做通用透传而不是硬编码。
        self._extra_body = extra_body or {}

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def chat(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResult:
        if not self.available:
            raise LLMError(
                f"{self._provider} 未配置 API Key，请在 backend/.env 中设置",
                provider=self._provider,
                retryable=False,
            )

        payload: dict = {
            "model": self._model,
            "messages": [m.as_dict() for m in messages],
            "temperature": (
                self._default_temperature if temperature is None else temperature
            ),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self._extra_body:
            payload.update(self._extra_body)

        started = time.perf_counter()
        try:
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"{self._provider} 请求超时（{self._timeout}s）",
                provider=self._provider,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"{self._provider} 网络错误：{exc}", provider=self._provider, retryable=True
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 401:
            raise LLMError(
                f"{self._provider} 鉴权失败，请检查 API Key",
                provider=self._provider,
            )
        if resp.status_code == 429:
            raise LLMError(
                f"{self._provider} 触发限流，请稍后重试",
                provider=self._provider,
                retryable=True,
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            hint = ""
            # 部分聚合平台（如 ModelScope）只对快照版本的模型 id 开放服务，
            # 报错文本是 "has no provider supported"，容易被误判为 Key 问题。
            if "no provider supported" in detail:
                hint = (
                    f"（提示：该平台通常只接受带版本号的模型 id，"
                    f"如 xxx-Flash-1017，当前配置为 {self._model}）"
                )
            raise LLMError(
                f"{self._provider} 返回 {resp.status_code}：{detail}{hint}",
                provider=self._provider,
                retryable=resp.status_code >= 500,
            )

        try:
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(
                f"{self._provider} 返回结构异常：{resp.text[:300]}",
                provider=self._provider,
            ) from exc

        if not content or not content.strip():
            raise LLMError(
                f"{self._provider} 返回空内容", provider=self._provider, retryable=True
            )

        usage = body.get("usage") or {}
        # 把 token 用量写进进度流：成本可见才谈得上控制成本
        emit(
            f"模型返回：输入 {usage.get('prompt_tokens', '?')} / "
            f"输出 {usage.get('completion_tokens', '?')} tokens，"
            f"耗时 {latency_ms / 1000:.1f}s"
        )

        return LLMResult(
            content=content,
            model=body.get("model", self._model),
            provider=self._provider,
            usage=body.get("usage", {}) or {},
            latency_ms=latency_ms,
        )
