"""执行隔离层：跑生成物的代码时，尽量把它关进容器里。

为什么要单开一层（对照 test_runner / debug_agent 的边界）：
- 它们关心"怎么判通过"，不关心"在哪里跑"；
- 这一层只回答一件事：这次执行到底有没有隔离，隔离到什么程度。

两档驱动，**不说谎**：

| 驱动 | 条件 | 实际隔离 |
| --- | --- | --- |
| `docker` | 本机有 docker 且守护进程可用 | 独立容器：`--network=none`、内存/CPU/进程数限额、只挂载项目目录、`--cap-drop=ALL` |
| `local` | 没有 docker，或显式指定 | 仅子进程 + 工作目录限制 + 超时强杀。**没有网络隔离，没有文件隔离** |

选择顺序由 `SANDBOX_DRIVER` 决定：`auto`（默认，能用容器就用）、`docker`（强制，
没有就报错）、`local`（强制走本机，调试用）。真正用了哪一档会写进测试结果
（`isolation.driver`），前端照实显示，不把子进程包装成"沙箱"。

镜像里的依赖是预装的（httpx / pyyaml / pytest / pytest-asyncio），所以容器内
`--network=none` 也能跑；生成物的 requirements 变了会按内容哈希重新构建一次。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import settings

IMAGE_REPO = "agent-builder-sandbox"
# 与生成物 requirements.txt 保持一致的运行期依赖；只多不少
SANDBOX_DEPS = (
    "httpx>=0.27",
    "pyyaml>=6.0",
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
)
IMAGE_TAG = "py311-" + hashlib.sha256(
    "\n".join(SANDBOX_DEPS).encode("utf-8")
).hexdigest()[:8]
IMAGE = f"{IMAGE_REPO}:{IMAGE_TAG}"

_DOCKERFILE = """FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install {deps}
WORKDIR /work
"""

_lock = threading.Lock()
_probe_cache: dict[str, Any] | None = None


class SandboxError(Exception):
    """隔离层跑不起来（不是"测试失败"）。"""


# ---------------------------------------------------------------- 探测


def _docker_bin() -> str | None:
    return shutil.which("docker")


def _run(cmd: list[str], *, timeout: int = 20) -> tuple[int, str]:
    """跑一条探测命令，异常一律折成 (非0, 说明)，探测阶段不该抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_clean_env(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def _clean_env() -> dict[str, str]:
    """探测/执行都要剥掉宿主的 PYTHONPATH 钩子，否则结果和用户终端不一致。"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "PYTHONPATH" and not k.startswith("CODEBUDDY_")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def probe(*, refresh: bool = False) -> dict[str, Any]:
    """探测本机执行能力。结果缓存，避免每次跑测试都 docker info 一次。"""
    global _probe_cache
    with _lock:
        if _probe_cache is not None and not refresh:
            return _probe_cache

        binary = _docker_bin()
        info: dict[str, Any] = {
            "docker_binary": binary,
            "docker_server": None,
            "image": IMAGE,
            "image_built": False,
            "preferred": settings.sandbox_driver,
        }

        if binary:
            code, out = _run([binary, "version", "--format", "{{.Server.Version}}"])
            if code == 0 and out.strip():
                info["docker_server"] = out.strip().splitlines()[0].strip()
            else:
                # 守护进程没起（Docker Desktop 没开）是最常见的情况，要能说清楚
                tail = " ".join(out.strip().splitlines()[-2:])[:200]
                info["docker_error"] = tail or "docker version 无输出"

        if info["docker_server"]:
            code, out = _run([binary, "image", "inspect", IMAGE, "--format", "ok"])
            info["image_built"] = code == 0 and "ok" in out

        _probe_cache = info
        return info


def reset_probe_cache() -> None:
    """测试用：强制下次重新探测。"""
    global _probe_cache
    with _lock:
        _probe_cache = None


def driver() -> str:
    """当前实际会用哪一档：docker / local。"""
    want = settings.sandbox_driver
    if want == "local":
        return "local"
    info = probe()
    usable = bool(info["docker_server"])
    if want == "docker" and not usable:
        raise SandboxError(
            "SANDBOX_DRIVER=docker，但本机 docker 不可用："
            + (info.get("docker_error") or "未安装 docker")
            + "。改成 auto 可自动退回本机子进程。"
        )
    return "docker" if usable else "local"


def status() -> dict[str, Any]:
    """给界面看的隔离状态：用了哪档、为什么、容器限额是多少。"""
    info = probe()
    try:
        active = driver()
    except SandboxError as exc:
        return {
            "driver": "none",
            "isolated": False,
            "reason": str(exc),
            "docker": info,
            "limits": {},
        }
    if active == "docker":
        reason = f"容器内执行（Docker {info['docker_server']}）"
    elif info["docker_binary"] and not info["docker_server"]:
        reason = "本机装了 docker 但守护进程没起，已退回子进程执行"
    else:
        reason = "本机没有 docker，只能子进程执行"
    return {
        "driver": active,
        "isolated": active == "docker",
        "reason": reason,
        "image": IMAGE,
        "image_built": info["image_built"],
        "docker": info,
        "limits": _limits() if active == "docker" else {},
    }


def _limits() -> dict[str, Any]:
    return {
        "network": "none",
        "memory": settings.sandbox_memory,
        "cpus": settings.sandbox_cpus,
        "pids": settings.sandbox_pids,
        "caps": "drop ALL",
    }


# ---------------------------------------------------------------- 镜像


def ensure_image(*, timeout: int = 600) -> str:
    """构建（或复用）沙箱镜像。只在选 docker 驱动时调用，失败要报清楚原因。"""
    info = probe()
    if info["image_built"]:
        return IMAGE
    binary = _docker_bin()
    if not binary:
        raise SandboxError("找不到 docker 命令")

    build_dir = Path(tempfile.mkdtemp(prefix="ab-sbx-build-"))
    (build_dir / "Dockerfile").write_text(
        _DOCKERFILE.format(deps=" ".join(f'"{d}"' for d in SANDBOX_DEPS)),
        encoding="utf-8",
    )
    started = time.time()
    code, out = _run(
        [binary, "build", "-t", IMAGE, str(build_dir)],
        timeout=timeout,
    )
    shutil.rmtree(build_dir, ignore_errors=True)
    if code != 0:
        tail = "\n".join(out.strip().splitlines()[-8:])
        raise SandboxError(
            f"构建沙箱镜像失败（{round(time.time() - started)}s）。"
            f"首次构建需要能拉取 python:3.11-slim 和 pip 包：\n{tail}"
        )
    probe(refresh=True)
    return IMAGE


# ---------------------------------------------------------------- 执行


def run_pytest(
    files_dir: Path,
    targets: list[str],
    *,
    timeout: int = 180,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """跑 pytest，返回统一的执行结果（stdout/stderr/退出码/超时/驱动/xml 路径）。

    调用方（test_runner）只负责解析结果，不关心是容器还是子进程。
    """
    out_dir = Path(tempfile.mkdtemp(prefix="ab-junit-"))
    xml_path = out_dir / "report.xml"
    args = ["-q", "--tb=short", "-p", "no:cacheprovider", *(extra_args or [])]

    active = driver()
    if active == "docker":
        ensure_image()
        result = _run_docker(files_dir, targets, args, xml_path, out_dir, timeout)
    else:
        result = _run_local(files_dir, targets, args, xml_path, timeout)

    result["xml_path"] = str(xml_path) if xml_path.exists() else None
    result["driver"] = active
    return result


def _run_docker(
    files_dir: Path,
    targets: list[str],
    args: list[str],
    xml_path: Path,
    out_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    binary = _docker_bin()
    if not binary:
        raise SandboxError("docker 命令不见了（探测后被移除？）")
    name = f"ab-sbx-{uuid.uuid4().hex[:10]}"
    # 路径要是 docker 能认的绝对路径；Windows 下 docker CLI 接受 C:\... 形式
    mount_src = str(files_dir.resolve())
    mount_out = str(out_dir.resolve())

    cmd = [
        binary,
        "run",
        "--rm",
        "--name",
        name,
        "--network=none",
        f"--memory={settings.sandbox_memory}",
        f"--memory-swap={settings.sandbox_memory}",
        f"--cpus={settings.sandbox_cpus}",
        f"--pids-limit={settings.sandbox_pids}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-e",
        "PYTHONPATH=",
        "-e",
        "PYTHONIOENCODING=utf-8",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-v",
        f"{mount_src}:/work",
        "-v",
        f"{mount_out}:/out",
        "-w",
        "/work",
        IMAGE,
        "python",
        "-m",
        "pytest",
        *targets,
        *args,
        "--junitxml=/out/report.xml",
    ]

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_clean_env(),
        )
        stdout, stderr, code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        code = -1
        # docker run 被 kill 之后容器还在跑，必须显式删掉，否则会一直占资源
        _run([binary, "rm", "-f", name], timeout=30)
    except OSError as exc:
        raise SandboxError(f"无法启动 docker：{exc}") from exc

    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "exit_code": code,
        "timed_out": timed_out,
        "duration_s": round(time.time() - started, 2),
        "notes": [],
        "isolation": {
            "driver": "docker",
            "detail": "容器内执行，网络关闭、内存/CPU/进程数受限",
            "image": IMAGE,
            "limits": _limits(),
        },
    }


def _run_local(
    files_dir: Path,
    targets: list[str],
    args: list[str],
    xml_path: Path,
    timeout: int,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        *args,
        f"--junitxml={xml_path}",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(files_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_clean_env(),
        )
        stdout, stderr, code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        code = -1
    except OSError as exc:
        raise SandboxError(f"无法启动 pytest：{exc}") from exc

    return {
        "stdout": stdout or "",
        "stderr": stderr or "",
        "exit_code": code,
        "timed_out": timed_out,
        "duration_s": round(time.time() - started, 2),
        "notes": [],
        "isolation": {
            "driver": "local",
            "detail": "本机子进程执行，无网络/文件隔离（仅限工作目录 + 超时强杀）",
            "limits": {},
        },
    }
