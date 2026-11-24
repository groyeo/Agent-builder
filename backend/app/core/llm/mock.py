"""离线 Mock 适配器。

作用：在没有 API Key、或想零成本回归测试时，让整条链路（Prompt → 模型 → Schema 校验 → 接口）
仍然可以端到端跑通。它不做真实理解，只按简单规则产出结构合法的 JSON，
因此**结果不能用于评估需求分析质量**，仅用于验证工程链路。
"""

from __future__ import annotations

import json
import re

from app.core.llm.base import LLMAdapter, LLMResult, Message

_SPLIT_RE = re.compile(r"[、，,；;\n]|以及|并且|同时")


class MockAdapter(LLMAdapter):
    name = "mock"

    def chat(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> LLMResult:
        raw = ""
        for msg in reversed(messages):
            if msg.role == "user":
                raw = msg.content
                break

        # 只取用户原文里"需求原文："之后的部分，避免把提示词模板当成需求
        text = raw.split("需求原文：")[-1].strip() if "需求原文：" in raw else raw.strip()
        for marker in ("请按系统提示", "用户在澄清环节的回答"):
            text = text.split(marker)[0].strip()
        segments = [s.strip() for s in _SPLIT_RE.split(text) if s.strip()]
        features = [s for s in segments if 2 <= len(s) <= 40][:6] or [text[:40]]

        payload = {
            "agent_name": (segments[0][:20] if segments else "未命名 Agent"),
            "goal": text[:120] or "（未提供需求）",
            "target_users": [],
            "inputs": [],
            "outputs": [],
            "features": features,
            "constraints": [],
            "quality_requirements": [],
            "external_dependencies": [],
            "acceptance_criteria": [],
            "architecture_hint": "undecided",
            "complexity_level": "moderate",
            "clarifying_questions": [
                {
                    "id": "q_input",
                    "question": "这个 Agent 的输入数据从哪里来、是什么格式？",
                    "why": "输入形态直接决定是否需要文件解析、数据库或外部接口工具。",
                    "options": ["用户上传文件", "数据库查询", "开放 API", "对话输入"],
                    "blocking": True,
                },
                {
                    "id": "q_output",
                    "question": "期望的输出是什么形态？",
                    "why": "输出形态决定是否需要报告生成、可视化或结构化写回。",
                    "options": ["文字结论", "结构化 JSON", "图表报告", "写入外部系统"],
                    "blocking": False,
                },
            ],
            "confidence": 0.35,
        }

        return LLMResult(
            content=json.dumps(payload, ensure_ascii=False),
            model="mock",
            provider="mock",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            latency_ms=0,
        )
