"""模型运行时设置的出参/入参结构。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ThinkingOption(BaseModel):
    value: str
    label: str
    description: str


class LLMSettings(BaseModel):
    provider: str
    model: str
    """未配置 Key 时为 true（走 Mock），前端据此提示"当前是假模型"。"""
    mock: bool
    thinking: str
    thinking_label: str
    options: list[ThinkingOption]
    temperature: float
    """真正会发到 /chat/completions 的附加字段，便于核对是否生效。"""
    request_body: dict[str, Any] = Field(default_factory=dict)
    persisted: bool
    note: str


class LLMSettingsUpdate(BaseModel):
    thinking: str | None = Field(
        default=None, description="off / low / medium / high"
    )
    temperature: float | None = Field(default=None, ge=0, le=2)
