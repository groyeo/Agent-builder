"""Debug Agent —— 测试挂了之后，让模型提最小补丁并重跑。

边界（照着方案 6.7 写，别越界）：
- **不许改测试**。`tests/**` 一律拒改。否则"把断言删掉"也算修复，等于自己骗自己。
- **不许改环境**。`.env*` 拒改，`knowledge/`、`data/` 不在 files 根下不属于代码，也不碰。
- **不许无限重试**。max_attempts 默认 2，每次都必须重跑测试拿证据。
- **失败要说失败**。跑完还是红的就 `needs_human=true`，不粉饰。
- **补丁越小越好**。模型被要求整文件重写时只允许改它判断有问题的文件，
  并且每处改动都要给理由。

判"更糟就回滚"：如果补完的失败数比修之前还多，说明方向错了，整体撤销并如实报告。
补丁原文都留在 `design/debug.json` 里，人可以照着看。
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any

from app.core.llm import LLMError, Message, get_llm
from app.engine import project_store, test_runner
from app.engine.project_store import ProjectError

MAX_ATTEMPTS_DEFAULT = 2
MAX_FILE_CHARS = 8000
MAX_EDIT_BYTES = 200_000
FORBIDDEN_PREFIXES = ("tests/",)
FORBIDDEN_EXACT = (".env", ".env.local")
DESIGN_STAGE = "debug"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py")

SYSTEM_PROMPT = """你是一名 Python 项目的调试工程师。你会收到一个自动生成的 Agent 工程，
以及它跑 `pytest` 失败的证据（失败用例、断言信息、原始输出尾部、相关源码）。

你的任务是给出**最小修复补丁**，让测试通过。

硬性规则：
1. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释性文字。
2. **禁止修改 tests/ 下的任何文件**。测试是验收标准，不是待改对象。改测试等于作弊。
3. **禁止修改 .env**，不要新增依赖（requirements.txt 也不要动）。
4. 每处改动给出完整文件内容（不是 diff），只改你判断有问题的文件；能改一行就别重写整个文件。
5. 不要为了让测试通过而删掉功能、吞掉异常、或把断言强转成真。修的是实现，不是证据。
6. 如果失败原因不是代码 bug（例如环境缺依赖、用例本身写错了、需求根本没定义这个行为），
   把 confidence 设为 "low" 并在 diagnosis 里说清，不要硬凑补丁。

JSON 字段定义：
{
  "diagnosis": "string，一句话说清根因（不是复述报错）",
  "confidence": "high | medium | low",
  "edits": [
    {
      "path": "相对项目根的路径，如 agent/router.py",
      "reason": "string，为什么改这里",
      "content": "string，改完之后的完整文件内容"
    }
  ]
}
如果确实没法在不改测试的前提下修复，就返回 edits: [] 并说明原因。
"""


# ---------------------------------------------------------------- 上下文组装


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _pick_files(files_dir: Path, failures: list[dict[str, Any]]) -> dict[str, str]:
    """挑出该给模型看的文件：失败的用例文件 + 报错里点到的源码 + 入口编排。

    全量塞进去太贵（一个工程 1500 行 ≈ 15k token），所以按证据挑，最多 6 个。
    """
    wanted: list[str] = []
    for case in failures:
        rel = case.get("name", "").split("::")[0]
        if rel:
            wanted.append(rel)
        for hit in _PATH_RE.findall(case.get("message") or ""):
            wanted.append(hit)

    for fallback in ("agent/router.py", "agent/tools.py"):
        if (files_dir / fallback).is_file():
            wanted.append(fallback)

    picked: dict[str, str] = {}
    for rel in wanted:
        if rel in picked or len(picked) >= 6:
            continue
        if rel.startswith("../") or rel.startswith("/"):
            continue
        target = (files_dir / rel).resolve()
        if not str(target).startswith(str(files_dir.resolve())) or not target.is_file():
            continue
        text = _read_text(target)
        picked[rel] = text[:MAX_FILE_CHARS]
    return picked


def _layout(files_dir: Path) -> str:
    """给一份文件清单：模型需要知道"能改的东西都在哪"，但不需要全部正文。"""
    rows = []
    for path in sorted(files_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(files_dir).as_posix()
            rows.append(f"{rel} ({len(_read_text(path).splitlines())} 行)")
    return "\n".join(rows[:120])


def _build_user_message(
    report: dict[str, Any],
    files_dir: Path,
    *,
    previous: list[dict[str, Any]],
) -> str:
    failures = [c for c in report["cases"] if c["status"] in ("failed", "error")]
    files = _pick_files(files_dir, failures)

    parts = [
        "## 测试结果",
        f"共 {report['total']} 个用例：{report['passed']} 通过，"
        f"{report['failed'] + report['error']} 失败。失败用例：",
    ]
    for case in failures[:10]:
        parts.append(f"- {case['name']}\n  {case['message'] or '（无断言信息）'}")

    parts.append("\n## pytest 输出尾部\n```\n" + report["output_tail"][-2500:] + "\n```")

    parts.append("\n## 项目文件清单\n```\n" + _layout(files_dir) + "\n```")

    parts.append("\n## 相关文件全文")
    for rel, content in files.items():
        parts.append(f"### {rel}\n```python\n{content}\n```")

    if previous:
        parts.append("\n## 之前的修复尝试（都没成功，别重复同样的思路）")
        for item in previous:
            diag = item.get("diagnosis") or "（无诊断）"
            paths = "、".join(e.get("path", "?") for e in item.get("edits") or []) or "无改动"
            result = item.get("result") or {}
            parts.append(
                f"- 第 {item['attempt']} 次：{diag}；改了 {paths}；"
                f"改完仍然 {result.get('failed', 0) + result.get('error', 0)} 个失败"
            )

    parts.append("\n现在按规则输出 JSON。")
    return "\n".join(parts)


# ---------------------------------------------------------------- 补丁校验与应用


def _validate_edits(files_dir: Path, raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """把模型给的 edits 过一遍安检。返回 (可应用的改动, 拒收原因)。"""
    edits: list[dict[str, Any]] = []
    rejects: list[str] = []
    if not isinstance(raw, list):
        return edits, ["edits 不是数组"]

    for item in raw:
        if not isinstance(item, dict):
            rejects.append("edits 里有非对象元素")
            continue
        rel = str(item.get("path") or "").strip().replace("\\", "/")
        content = item.get("content")
        if not rel or not isinstance(content, str):
            rejects.append(f"缺 path 或 content：{rel or '(空路径)'}")
            continue
        if rel.startswith("/") or ".." in Path(rel).parts:
            rejects.append(f"路径越界：{rel}")
            continue
        if rel in FORBIDDEN_EXACT or rel.startswith(FORBIDDEN_PREFIXES):
            rejects.append(f"不允许改 {rel}（测试和 .env 不是修复对象）")
            continue
        if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
            rejects.append(f"{rel} 超过 200KB，拒收")
            continue
        if rel.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                rejects.append(f"{rel} 语法错误：{exc.msg}（第 {exc.lineno} 行）")
                continue
        edits.append(
            {"path": rel, "content": content, "reason": str(item.get("reason") or "")[:300]}
        )
    return edits, rejects


def _apply(files_dir: Path, edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """先备份再写。返回的备份用来判"更糟就回滚"。"""
    backups: list[dict[str, Any]] = []
    for edit in edits:
        target = files_dir / edit["path"]
        existed = target.is_file()
        backups.append(
            {
                "path": edit["path"],
                "existed": existed,
                "original": _read_text(target) if existed else "",
            }
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit["content"], encoding="utf-8")
    return backups


def _revert(files_dir: Path, backups: list[dict[str, Any]]) -> None:
    for item in backups:
        target = files_dir / item["path"]
        if item["existed"]:
            target.write_text(item["original"], encoding="utf-8")
        elif target.is_file():
            target.unlink()


# ---------------------------------------------------------------- 主流程


def heal(
    project_id: str, *, max_attempts: int = MAX_ATTEMPTS_DEFAULT, timeout: int = 180
) -> dict[str, Any]:
    """跑测试 → 失败就请模型提补丁 → 重跑。返回完整过程，不抛"业务失败"。"""
    project_store.get_meta(project_id)
    files_dir = project_store._root() / project_id / project_store.FILES_DIR  # noqa: SLF001
    if not files_dir.is_dir():
        raise ProjectError("该项目没有任何文件，无法修复", code=404)

    started = time.time()
    # 动手之前先留一份底：自动修复是"模型改你的代码"，改完不满意得能回到这一步
    try:
        project_store.snapshot(project_id, reason="自动修复前", note="Debug Agent 即将修改代码")
    except Exception:  # noqa: BLE001 - 快照失败不该阻断修复
        pass
    report = test_runner.run_tests(project_id, timeout=timeout)
    first = _brief(report)
    attempts: list[dict[str, Any]] = []
    all_backups: list[dict[str, Any]] = []
    changed: list[str] = []
    reverted = False

    if report["ok"]:
        return _finish(
            project_id,
            started,
            max_attempts,
            first,
            first,
            attempts,
            changed,
            reverted=False,
            note="测试本来就是全绿的，没有需要修的东西",
        )

    for attempt in range(1, max_attempts + 1):
        try:
            llm = get_llm()
            reply = llm.chat(
                [
                    Message("system", SYSTEM_PROMPT),
                    Message(
                        "user",
                        _build_user_message(report, files_dir, previous=attempts),
                    ),
                ],
                json_mode=True,
                temperature=0.1,
            )
            payload = _parse(reply.content)
        except LLMError as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "diagnosis": "",
                    "confidence": "",
                    "edits": [],
                    "rejected": [],
                    "result": _brief(report),
                    "error": f"模型调用失败：{exc}",
                }
            )
            break

        edits, rejects = _validate_edits(files_dir, payload.get("edits"))
        entry: dict[str, Any] = {
            "attempt": attempt,
            "diagnosis": str(payload.get("diagnosis") or "")[:500],
            "confidence": str(payload.get("confidence") or ""),
            "edits": [
                {"path": e["path"], "reason": e["reason"], "lines": len(e["content"].splitlines())}
                for e in edits
            ],
            "rejected": rejects,
        }

        if not edits:
            entry["result"] = _brief(report)
            entry["note"] = "模型没有给出可应用的补丁" + (
                "（有改动被安检拒收）" if rejects else ""
            )
            attempts.append(entry)
            break

        all_backups.extend(_apply(files_dir, edits))
        changed.extend(e["path"] for e in edits)
        report = test_runner.run_tests(project_id, timeout=timeout)
        entry["result"] = _brief(report)
        attempts.append(entry)

        if report["ok"]:
            break

    final = _brief(report)
    if not report["ok"] and changed:
        # 越修越糟就整体撤销：留着半成品比原来更误导人。
        # "更糟"有两种形态：失败数变多；或者收集到的用例变少
        # （后者是 import 被写坏的典型信号 —— 整个模块炸了，用例根本没跑起来）。
        before = first["failed"] + first["error"]
        after = final["failed"] + final["error"]
        if after > before or final["total"] < first["total"]:
            _revert(files_dir, all_backups)
            reverted = True
            report = test_runner.run_tests(project_id, timeout=timeout)
            final = _brief(report)

    note = ""
    if report["ok"]:
        note = f"第 {len(attempts)} 次尝试后测试通过"
    elif reverted:
        note = "补丁让情况变糟（失败数增加），已全部撤销，需要人工介入"
    elif attempts and attempts[-1].get("error"):
        note = "模型调用失败，未能修复"
    else:
        note = f"试了 {len(attempts)} 次仍未通过，需要人工介入"

    return _finish(
        project_id,
        started,
        max_attempts,
        first,
        final,
        attempts,
        sorted(set(changed)),
        reverted=reverted,
        note=note,
    )


def _brief(report: dict[str, Any]) -> dict[str, Any]:
    failed = [c for c in report["cases"] if c["status"] in ("failed", "error")]
    return {
        "ok": report["ok"],
        "total": report["total"],
        "passed": report["passed"],
        "failed": report["failed"],
        "error": report["error"],
        "duration_s": report["duration_s"],
        "failing_cases": [c["name"] for c in failed][:20],
    }


def _parse(text: str) -> dict[str, Any]:
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 模型偶尔会在 JSON 前后加一句话，抠出第一个大括号块再试一次
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("模型没有返回可解析的 JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的 JSON 解析失败：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 不是对象")
    return data


def _finish(
    project_id: str,
    started: float,
    max_attempts: int,
    first: dict[str, Any],
    final: dict[str, Any],
    attempts: list[dict[str, Any]],
    changed: list[str],
    *,
    reverted: bool,
    note: str,
) -> dict[str, Any]:
    result = {
        "project_id": project_id,
        "ran_at": time.time(),
        "elapsed_s": round(time.time() - started, 1),
        "max_attempts": max_attempts,
        "initial": first,
        "final": final,
        "attempts": attempts,
        "changed_files": changed,
        "reverted": reverted,
        "needs_human": not final["ok"],
        "note": note,
    }
    design_dir = project_store._root() / project_id / project_store.DESIGN_DIR  # noqa: SLF001
    project_store._dump_json(design_dir / f"{DESIGN_STAGE}.json", result)  # noqa: SLF001
    return result


def read_last(project_id: str) -> dict[str, Any]:
    path = (
        project_store._root() / project_id / project_store.DESIGN_DIR  # noqa: SLF001
        / f"{DESIGN_STAGE}.json"
    )
    if not path.exists():
        raise ProjectError("这个项目还没试过自动修复", code=404)
    return project_store._load_json(path)  # noqa: SLF001
