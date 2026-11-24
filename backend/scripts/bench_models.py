"""模型选型基准：同一条真实需求，跑完整三步链路，统计 token / 耗时 / 质量。

用法（在 backend 目录下，需已配置 .env）：
    python scripts/bench_models.py                              # 用 .env 里的模型
    python scripts/bench_models.py --models a b c               # 指定多个模型对比
    python scripts/bench_models.py --raw "你的需求文本"
    python scripts/bench_models.py --thinking                  # 打开思考过程做对照

为什么需要它：模型名里的 "Flash" 不代表省 token。实测在同一份 Prompt 下，
Qwen3.8-Flash-Next 的输出 token 是 DeepSeek-V4.1-Flash 的 1.7 倍，因为它在输出
思考过程。没有实测数据的模型切换等于赌博。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.architecture_planner import ArchitecturePlanner  # noqa: E402
from app.agents.component_designer import ComponentDesigner  # noqa: E402
from app.agents.requirement_analyzer import RequirementAnalyzer  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.llm.openai_compatible import OpenAICompatibleAdapter  # noqa: E402

DEFAULT_RAW = (
    "做一个客服工单智能分流 Agent，读取我们工单系统里的新工单，"
    "判断属于技术/账单/物流哪一类，并自动回复常见问题，处理不了的转人工。"
)

# 实测各平台的"关闭思考"字段不同，这里只覆盖已验证可用的写法
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def counted(adapter: OpenAICompatibleAdapter) -> tuple[OpenAICompatibleAdapter, dict]:
    totals = {"in": 0, "out": 0, "calls": 0}
    original = adapter.chat

    def chat(messages, **kwargs):
        result = original(messages, **kwargs)
        usage = result.usage or {}
        totals["in"] += usage.get("prompt_tokens", 0) or 0
        totals["out"] += usage.get("completion_tokens", 0) or 0
        totals["calls"] += 1
        return result

    adapter.chat = chat  # type: ignore[method-assign]
    return adapter, totals


def run_one(model: str, raw: str, thinking: bool) -> dict:
    s = get_settings()
    adapter = OpenAICompatibleAdapter(
        provider=s.llm_provider,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=model,
        timeout=300,
        default_temperature=0.2,
        extra_body={} if thinking else THINKING_OFF,
    )
    adapter, totals = counted(adapter)

    started = time.time()
    row: dict = {"model": model, "ok": False}
    try:
        requirement = RequirementAnalyzer(llm=adapter).analyze(raw)
        t1 = time.time() - started
        plan = ArchitecturePlanner(llm=adapter).plan(requirement.spec)
        t2 = time.time() - started
        components = ComponentDesigner(llm=adapter).design(
            requirement.spec, plan.spec
        )
        t3 = time.time() - started

        row.update(
            ok=True,
            total_s=round(t3, 1),
            req_s=round(t1, 1),
            arch_s=round(t2 - t1, 1),
            comp_s=round(t3 - t2, 1),
            tokens_in=totals["in"],
            tokens_out=totals["out"],
            tokens_total=totals["in"] + totals["out"],
            calls=totals["calls"],
            # 质量信号：结构是否通过、重试了几次、产出规模
            arch_type=plan.spec.architecture_type,
            nodes=len(plan.spec.agents),
            arch_passed=plan.validation.passed,
            arch_attempts=plan.attempts,
            comp_passed=components.validation.passed,
            comp_attempts=components.attempts,
            components=len(components.plan.components),
            questions=len(requirement.spec.clarifying_questions),
            confidence=requirement.spec.confidence,
        )
    except Exception as exc:  # noqa: BLE001 - 基准脚本要看到失败而不是崩掉
        row.update(error=f"{type(exc).__name__}: {exc}"[:200], total_s=round(time.time() - started, 1))
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="*", default=None)
    parser.add_argument("--raw", default=DEFAULT_RAW)
    parser.add_argument("--thinking", action="store_true", help="保留思考过程做对照")
    args = parser.parse_args()

    models = args.models or [get_settings().llm_model]
    print(f"思考过程：{'开启' if args.thinking else '关闭'}｜共 {len(models)} 个模型\n")

    rows = []
    for model in models:
        print(f"› {model} 运行中…", flush=True)
        rows.append(run_one(model, args.raw, args.thinking))

    header = (
        f"{'模型':<34}{'耗时':>7}{'输入':>7}{'输出':>7}{'总计':>7}"
        f"{'调用':>5}  {'架构':<12}{'节点':>4}{'重试':>4}{'组件':>4}  结果"
    )
    print("\n" + header)
    print("-" * len(header))
    for row in rows:
        if not row["ok"]:
            print(f"{row['model']:<34}{row['total_s']:>6}s{'':>26}  FAILED {row['error']}")
            continue
        print(
            f"{row['model']:<34}{row['total_s']:>6}s{row['tokens_in']:>7}"
            f"{row['tokens_out']:>7}{row['tokens_total']:>7}{row['calls']:>5}  "
            f"{row['arch_type']:<12}{row['nodes']:>4}{row['arch_attempts']:>4}"
            f"{row['components']:>4}  "
            f"{'OK' if row['arch_passed'] and row['comp_passed'] else 'CHECK'}"
        )

    ok = [r for r in rows if r["ok"]]
    if len(ok) > 1:
        best = min(ok, key=lambda r: r["tokens_total"])
        print(f"\n最省 token：{best['model']}（{best['tokens_total']}）")


if __name__ == "__main__":
    main()
