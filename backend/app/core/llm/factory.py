"""模型适配器工厂：按配置挑选具体实现。"""

from __future__ import annotations

from app.core.config import settings
from app.core.llm import thinking
from app.core.llm.base import LLMAdapter, LLMError
from app.core.llm.mock import MockAdapter
from app.core.llm.openai_compatible import OpenAICompatibleAdapter


def effective_thinking() -> str:
    """当前生效的思考档位：显式设置优先，否则按旧开关推导。"""
    configured = (settings.llm_thinking or "").strip()
    if configured:
        return thinking.normalize(configured)
    return thinking.OFF if settings.llm_disable_thinking else thinking.MEDIUM


def _build_extra_body() -> dict:
    """把思考档位翻译成请求体字段，再允许用户自定义 extra_body 整体覆盖。"""
    body = thinking.build_body(settings.llm_provider, effective_thinking())
    # 用户显式配置优先：即使是同一个 key 也整体覆盖
    body.update(settings.llm_extra_body or {})
    return body


def build_llm() -> LLMAdapter:
    if settings.llm_provider.strip().lower() == "mock":
        return MockAdapter()

    adapter = OpenAICompatibleAdapter(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
        default_temperature=settings.llm_temperature,
        extra_body=_build_extra_body(),
    )

    if not adapter.available:
        if settings.llm_allow_mock_fallback:
            # 降级要透明：调用方从 provider 字段能看出这是假模型
            return MockAdapter()
        raise LLMError(
            f"未配置 {settings.llm_provider} 的 API Key（LLM_API_KEY）",
            provider=settings.llm_provider,
        )

    return adapter


_llm: LLMAdapter | None = None


def get_llm() -> LLMAdapter:
    """进程内复用同一个适配器（内部无状态，复用只为省去重复构造）。"""
    global _llm
    if _llm is None:
        _llm = build_llm()
    return _llm


def reset_llm() -> None:
    """配置变更后（测试中）重建适配器。"""
    global _llm
    _llm = None
