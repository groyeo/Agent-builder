"""在生成的项目里真跑一遍 pytest。

为什么单独做这件事（对照 codegen_validator 的边界）：
- `codegen_validator` 只读代码：语法能不能解析、YAML 合不合法、组件覆盖全不全。
  它证明不了"这东西跑起来是对的"；
- 这一层真的 `pytest` 进程下去跑，拿回通过/失败和失败原因，才叫可验证交付。

安全边界（如实说明，不假装有沙箱）：
- 执行本身交给 `engine/sandbox.py`：本机有 docker 就在容器里跑
  （`--network=none` + 内存/CPU/进程数限额），没有就退回子进程；
- 究竟用了哪一档写进结果的 `isolation` 字段，前端照实显示 ——
  子进程不会被包装成"沙箱"；

结果的去向：`design/tests.json`。`read_design("tests")` 能直接读回来，
前端刷新页面后结果还在（和别的设计产物一个待遇）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from app.engine import project_store, sandbox
from app.engine.project_store import ProjectError

DESIGN_STAGE = "tests"
DEFAULT_TIMEOUT = 180
OUTPUT_TAIL_CHARS = 4000


def _describe(run: dict, targets: list[str]) -> str:
    """给用户看的"实际执行的命令"。容器和本机不一样，别糊弄成一条。"""
    if run["isolation"]["driver"] == "docker":
        return (
            f"docker run --rm --network=none {run['isolation'].get('image', '')} "
            f"python -m pytest {' '.join(targets)} -q"
        )
    return f"python -m pytest {' '.join(targets)} -q"


def _pretty_name(classname: str, test_name: str) -> str:
    """把 junit 的模块点路径还原成 `tests/test_x.py::用例名`。

    pytest 给的 classname 是 `data.projects.xxx.files.tests.test_clarify` 这种
    完整点路径，直接展示又长又没信息量，用户认不出是哪个文件。
    """
    parts = [p for p in classname.split(".") if p]
    if "tests" in parts:
        idx = len(parts) - 1 - parts[::-1].index("tests")
        module_parts = parts[idx:]
    else:
        module_parts = parts[-1:]
    if module_parts:
        module_parts[-1] = f"{module_parts[-1]}.py"
        rel = "/".join(module_parts)
        return f"{rel}::{test_name}" if test_name else rel
    return test_name


def _parse_junit(xml_path: Path) -> list[dict[str, Any]]:
    """把 junit xml 拆成逐条用例。pytest 的 xml 结构稳定，比解析 stdout 可靠。"""
    cases: list[dict[str, Any]] = []
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return cases
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        classname = case.get("classname") or ""
        status = "passed"
        message = ""
        for child in case:
            tag = child.tag
            if tag in ("failure", "error"):
                status = "failed" if tag == "failure" else "error"
                message = (child.get("message") or "").strip()
                text = (child.text or "").strip()
                if not message:
                    message = text.splitlines()[0] if text else ""
            elif tag == "skipped":
                status = "skipped"
                message = (child.get("message") or "").strip()
        cases.append(
            {
                "name": _pretty_name(classname, name),
                "status": status,
                "duration": float(case.get("time") or 0),
                "message": message[:800],
            }
        )
    return cases


def _parse_summary(text: str) -> dict[str, int]:
    """从 pytest 最后一行抠数量。xml 缺失时兜底用。"""
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for line in reversed(text.splitlines()):
        hits = re.findall(
            r"(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed)", line
        )
        if hits:
            for num, word in hits:
                key = "error" if word.startswith("error") else word
                if key in counts:
                    counts[key] = int(num)
            break
    return counts


def _collect_targets(files_dir: Path) -> list[str]:
    """决定跑什么。

    有 tests/ 就跑 tests/；没有的话退回去跑仓库内的 test_*.py —— 生成物正常
    一定有 tests/，真没有也别伪装成"0 个用例通过"。
    """
    if (files_dir / "tests").is_dir():
        return ["tests"]
    loose = sorted(p.name for p in files_dir.glob("test_*.py"))
    return loose


def run_tests(
    project_id: str, *, timeout: int = DEFAULT_TIMEOUT, extra_args: list[str] | None = None
) -> dict[str, Any]:
    """跑一遍 pytest，保存并返回结果。失败（有用例挂）不是接口错误，是结果。"""
    meta = project_store.get_meta(project_id)
    files_dir = project_store._root() / project_id / project_store.FILES_DIR  # noqa: SLF001
    if not files_dir.is_dir():
        raise ProjectError("该项目没有任何文件，无法运行测试", code=404)

    targets = _collect_targets(files_dir)
    if not targets:
        raise ProjectError(
            "项目里没有 tests/ 目录，也没有 test_*.py，没东西可跑", code=409
        )

    try:
        run = sandbox.run_pytest(files_dir, targets, timeout=timeout, extra_args=extra_args)
    except sandbox.SandboxError as exc:
        raise ProjectError(f"执行环境不可用：{exc}", code=500) from exc

    stdout = run["stdout"]
    stderr = run["stderr"]
    code = run["exit_code"]
    timed_out = run["timed_out"]
    duration = run["duration_s"]

    xml_file = Path(run["xml_path"]) if run.get("xml_path") else None
    cases = _parse_junit(xml_file) if xml_file else []
    counts = _parse_summary(stdout + "\n" + stderr)
    if cases:
        counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
        for c in cases:
            key = c["status"]
            counts[key] = counts.get(key, 0) + 1

    total = sum(counts.values())
    ok = (not timed_out) and code == 0 and counts.get("failed", 0) == 0 and counts.get("error", 0) == 0

    notes: list[str] = []
    if timed_out:
        notes.append(f"超过 {timeout}s 未跑完，已强制结束（可能有网络调用或死循环）")
    if code == 5:
        notes.append("pytest 退出码 5：一个用例都没收集到，检查测试文件是否被跳过")
    if "ModuleNotFoundError" in (stdout + stderr):
        notes.append("缺依赖：先跑 python start.py --check，它会装上 requirements.txt")

    result: dict[str, Any] = {
        "project_id": project_id,
        "project_name": meta.get("name"),
        "ran_at": time.time(),
        "ok": bool(ok),
        "timed_out": timed_out,
        "exit_code": code,
        "duration_s": duration,
        "command": _describe(run, targets),
        "targets": targets,
        "total": total,
        **counts,
        "cases": cases[:200],
        "output_tail": (stdout + ("\n" + stderr if stderr.strip() else ""))[
            -OUTPUT_TAIL_CHARS:
        ].strip(),
        "notes": notes,
        "isolation": run["isolation"],
    }

    design_dir = project_store._root() / project_id / project_store.DESIGN_DIR  # noqa: SLF001
    project_store._dump_json(design_dir / f"{DESIGN_STAGE}.json", result)  # noqa: SLF001
    _touch_meta(project_id, result)
    return result


def _touch_meta(project_id: str, result: dict[str, Any]) -> None:
    """把测试结论写进 project.json，让列表页不用打开详情就能看到红绿。"""
    try:
        meta = project_store.get_meta(project_id)
        meta["tests_ok"] = result["ok"]
        meta["tests_total"] = result["total"]
        meta["tests_failed"] = result["failed"] + result["error"]
        meta["tests_ran_at"] = result["ran_at"]
        project_store._dump_json(  # noqa: SLF001
            project_store._root() / project_id / project_store.META_FILE,  # noqa: SLF001
            meta,
        )
    except Exception:  # noqa: BLE001 - 写不进元信息不能影响测试结论本身
        pass


def read_last(project_id: str) -> dict[str, Any]:
    """读上一次的结果（没跑过就 404，让前端区分"没跑"和"跑了挂了"）。"""
    path = (
        project_store._root() / project_id / project_store.DESIGN_DIR  # noqa: SLF001
        / f"{DESIGN_STAGE}.json"
    )
    if not path.exists():
        raise ProjectError("这个项目还没跑过测试", code=404)
    return project_store._load_json(path)  # noqa: SLF001
