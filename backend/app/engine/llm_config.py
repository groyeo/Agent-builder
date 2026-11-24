"""运行时模型设置（目前只有"思考强度"）。

为什么要落一个文件而不是只改 .env：
- 改 .env 要重启进程，而用户切换思考强度是为了"这次想快一点/想得深一点"，
  属于使用过程中的临时决定，不该逼人重启；
- 存 data/llm_settings.json 后，重启仍然记得上一次的选择。

生效时机：下次构建（适配器在每次任务开始时构造，已被 reset_llm 置空）。
正在跑的任务不会中途变速 —— 半路换档会让同一份 JSON 的产出风格不一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.llm import thinking
from app.core.llm.factory import effective_thinking, reset_llm

FILE_NAME = "llm_settings.json"


def _path() -> Path:
    return Path(settings.data_dir) / FILE_NAME


def _read() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(payload: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load() -> None:
    """启动时把磁盘上的偏好灌回 settings（不触发适配器重建）。"""
    data = _read()
    level = data.get("thinking")
    if isinstance(level, str) and level.strip():
        settings.llm_thinking = thinking.normalize(level)
    temp = data.get("temperature")
    if isinstance(temp, (int, float)) and 0 <= float(temp) <= 2:
        settings.llm_temperature = float(temp)


def current() -> dict[str, Any]:
    level = effective_thinking()
    provider = settings.llm_provider
    return {
        "provider": provider,
        "model": settings.llm_model,
        "mock": provider.strip().lower() == "mock" or not settings.llm_api_key,
        "thinking": level,
        "thinking_label": thinking.LABELS[level],
        "options": thinking.options(),
        "temperature": settings.llm_temperature,
        # 展示真正会发出去的字段，避免"我设了却没生效"的扯皮
        "request_body": {
            **thinking.build_body(provider, level),
            **(settings.llm_extra_body or {}),
        },
        "persisted": _path().exists(),
        "note": "改动在下次构建时生效；正在运行的任务不受影响。",
    }


def update(
    *,
    thinking_level: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    payload = _read()

    if thinking_level is not None:
        raw = thinking_level.strip().lower()
        if raw not in thinking.LEVELS:
            raise ValueError(
                f"未知的思考强度：{thinking_level}（可选：{' / '.join(thinking.LEVELS)}）"
            )
        settings.llm_thinking = raw
        payload["thinking"] = raw

    if temperature is not None:
        temp = float(temperature)
        if not 0 <= temp <= 2:
            raise ValueError("temperature 需在 0~2 之间")
        settings.llm_temperature = temp
        payload["temperature"] = temp

    _write(payload)
    # 适配器里缓存着旧的 extra_body，必须重建，否则改了半天还是老请求
    reset_llm()
    return current()
