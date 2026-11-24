"""项目预览：把生成出来的目标 Agent 在本机起成网页，点开就能用。

为什么不做成"在前端里跑一段 JS"：
目标 Agent 的推理在 Python 侧（agent/router.py → llm.py），前端只能当壳。
所以这里起一个真实的本地进程，前端只负责打开它的地址。

几个刻意的取舍：
- **用平台已配置的模型**：用户已经为构建平台填过一次 Key，没道理让他在每个
  目标 Agent 的 .env 里再填一次。启动时把 OPENAI_* 注入子进程环境变量
  （agent/env.py 用 setdefault，环境变量优先于 .env）。
- **只监听 127.0.0.1**：这是本机预览，不是对外服务。
- **进程表放内存**：预览是临时态，重启平台就没了；残留进程由 stop_all 清。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from app.core.config import settings
from app.engine.project_store import ProjectError, get_meta

#: 等网页起来的最长时间（秒）。模板服务是纯标准库，正常 1s 内就绪。
START_TIMEOUT = 15.0
#: 失败时回给前端的日志尾巴长度
LOG_TAIL = 2000

#: project_id -> {proc, url, port, started_at, log_path}
_previews: dict[str, dict[str, Any]] = {}


def _project_dir(project_id: str) -> Path:
    get_meta(project_id)  # 顺带校验项目存在
    path = (Path(settings.data_dir) / "projects" / project_id).resolve()
    if not (path / "files").is_dir():
        raise ProjectError("项目文件还没落盘，先点「更新代码」", code=409)
    return path


def _tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-LOG_TAIL:].strip()


def _free_port(preferred: tuple[int, ...] = (8765, 8766, 8767)) -> int:
    """优先用 8765：地址稳定、好记，用户第二次点开还是同一个。

    同时开多个项目预览时，后面的自动往后挪；都不行才让系统随机分。
    """
    for port in preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # 平台已配真实模型时，直接复用：目标 Agent 不再单独要 Key
    provider = (settings.llm_provider or "").lower()
    if provider != "mock" and settings.llm_api_key:
        env["OPENAI_BASE_URL"] = settings.llm_base_url
        env["OPENAI_API_KEY"] = settings.llm_api_key
        env["OPENAI_MODEL"] = settings.llm_model
    # Windows 控制台默认 GBK，子进程日志里有中文就会写坏
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _alive(item: dict[str, Any]) -> bool:
    proc: subprocess.Popen | None = item.get("proc")
    return proc is not None and proc.poll() is None


def _describe(project_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "running": _alive(item),
        "url": item.get("url"),
        "port": item.get("port"),
        "started_at": item.get("started_at"),
        "log_tail": "" if _alive(item) else _tail(item["log_path"]),
    }


def status(project_id: str) -> dict[str, Any]:
    """只看状态，不启动。进程已经退出时顺手清掉记录。"""
    item = _previews.get(project_id)
    if item is None:
        return {"project_id": project_id, "running": False, "url": None, "port": None, "started_at": None, "log_tail": ""}
    if not _alive(item):
        tail = _tail(item["log_path"])
        _previews.pop(project_id, None)
        return {"project_id": project_id, "running": False, "url": None, "port": None, "started_at": None, "log_tail": tail}
    return _describe(project_id, item)


def _wait_ready(url: str, proc: subprocess.Popen, deadline: float) -> bool:
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urlopen(url + "/api/health", timeout=0.6) as resp:
                if resp.status == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def start(project_id: str) -> dict[str, Any]:
    """起网页。已经在跑就复用，不重复起进程。"""
    item = _previews.get(project_id)
    if item and _alive(item):
        return {**_describe(project_id, item), "reused": True}

    project_dir = _project_dir(project_id)
    files = project_dir / "files"
    if not (files / "server.py").exists():
        raise ProjectError(
            "这个项目还没有网页入口（server.py）。点「更新代码」用最新模板补上再试。",
            code=409,
        )

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = project_dir / "design" / "preview.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - 生命周期跟着子进程
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "server.py", "--no-browser", "--port", str(port)],
            cwd=str(files),
            env=_child_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        log.close()
        raise ProjectError(f"启动失败：{exc}", code=500) from exc

    ok = _wait_ready(url, proc, time.time() + START_TIMEOUT)
    if not ok:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极少见
            proc.kill()
        log.close()
        tail = _tail(log_path)
        _previews.pop(project_id, None)
        raise ProjectError(
            "网页没能起来。" + (f"\n日志：\n{tail}" if tail else "没有日志输出，先跑一次测试确认项目本身能跑。"),
            code=500,
        )

    _previews[project_id] = {
        "proc": proc,
        "url": url,
        "port": port,
        "started_at": time.time(),
        "log_path": log_path,
        "log": log,
    }
    return {**_describe(project_id, _previews[project_id]), "reused": False}


def stop(project_id: str) -> dict[str, Any]:
    item = _previews.pop(project_id, None)
    if item is None:
        return {"project_id": project_id, "running": False, "stopped": False}
    proc: subprocess.Popen = item["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
    handle = item.get("log")
    if handle is not None:
        try:
            handle.close()
        except OSError:  # pragma: no cover
            pass
    return {"project_id": project_id, "running": False, "stopped": True}


def stop_all() -> None:
    """平台退出时清掉所有预览进程，避免留下占端口的孤儿。"""
    for project_id in list(_previews):
        try:
            stop(project_id)
        except Exception:  # noqa: BLE001 - 退出清理不该再抛
            _previews.pop(project_id, None)
