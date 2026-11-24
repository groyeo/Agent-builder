"""思考强度（reasoning / thinking budget）的统一抽象。

为什么要单独一模块：
- 不同厂商控制"思考多少"的字段完全不一样，且都是可选扩展字段，写错就 400；
- 但业务上只有一个诉求：**在"省钱快"和"想得深"之间选一档**。

因此这里定义四档（off / low / medium / high），再按 provider 翻译成各家字段：

    provider 族                     字段
    modelscope / qwen / dashscope   chat_template_kwargs.enable_thinking + thinking_budget
    deepseek / zhipu / glm          thinking.type + reasoning_effort
    其他 OpenAI 兼容                 thinking.type + reasoning_effort（兜底）

映射结果只是"默认值"，.env 的 LLM_EXTRA_BODY 仍然整体覆盖，
遇到不认这些字段的网关，用它显式改写即可。
"""

from __future__ import annotations

from typing import Any

OFF = "off"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"

LEVELS: tuple[str, ...] = (OFF, LOW, MEDIUM, HIGH)

LABELS: dict[str, str] = {
    OFF: "关闭",
    LOW: "低",
    MEDIUM: "中",
    HIGH: "高",
}

DESCRIPTIONS: dict[str, str] = {
    OFF: "不出思考过程，最快最省。每一步都要求严格 JSON 时基本够用。",
    LOW: "少量思考（约 1k token 预算），适合字段抽取这类简单结构。",
    MEDIUM: "中等思考（约 4k token 预算），复杂需求/架构取舍更稳。",
    HIGH: "深度思考（约 16k token 预算），最贵最慢，只在结论反复出错时用。",
}

# 思考预算（token）：只对支持 thinking_budget 的厂商生效
BUDGETS: dict[str, int] = {LOW: 1024, MEDIUM: 4096, HIGH: 16384}

# 常见别名，方便 .env 里手写
_ALIASES: dict[str, str] = {
    "none": OFF,
    "no": OFF,
    "false": OFF,
    "0": OFF,
    "minimal": LOW,
    "min": LOW,
    "1": LOW,
    "mid": MEDIUM,
    "balanced": MEDIUM,
    "normal": MEDIUM,
    "max": HIGH,
    "maximum": HIGH,
    "3": HIGH,
}

_QWEN_FAMILY = {"qwen", "dashscope", "modelscope", "qwq", "qwen3"}
_THINKING_BLOCK_FAMILY = {"deepseek", "zhipu", "glm", "moonshot", "kimi"}


def normalize(level: str | None) -> str:
    """把外部输入收敛成四档之一；无法识别时退回 off（宁可省钱）。"""
    raw = (level or "").strip().lower()
    if raw in LEVELS:
        return raw
    return _ALIASES.get(raw, OFF)


def build_body(provider: str, level: str) -> dict[str, Any]:
    """把档位翻译成 /chat/completions 的附加字段。"""
    lv = normalize(level)
    p = (provider or "").strip().lower()
    thinking_on = lv != OFF

    if p in _QWEN_FAMILY:
        kwargs: dict[str, Any] = {"enable_thinking": thinking_on}
        if thinking_on:
            kwargs["thinking_budget"] = BUDGETS[lv]
        return {"chat_template_kwargs": kwargs}

    if p in _THINKING_BLOCK_FAMILY:
        if not thinking_on:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": lv}

    # 兜底：OpenAI 官方有 reasoning_effort，多数兼容网关也能容忍 thinking 块
    if not thinking_on:
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": lv}


def options() -> list[dict[str, str]]:
    """给前端渲染下拉框用。"""
    return [
        {"value": lv, "label": LABELS[lv], "description": DESCRIPTIONS[lv]}
        for lv in LEVELS
    ]
