"""一条命令起整套开发环境：python start.py

同时拉起后端（FastAPI，8000）和前端（Vite，5173），Ctrl+C 一起停。
解决的是每次开工要开两个终端、还要分别记两条命令的问题。

    python start.py            启动前后端
    python start.py --backend  只起后端（调接口/跑测试时用）
    python start.py --no-open  起完不自动开浏览器

Windows 也可以双击 start.bat。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
API = "http://127.0.0.1:8000"
WEB = "http://localhost:5173"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def find_python() -> str:
    """优先用项目自己的 conda 环境，找不到就退回当前解释器。

    写死路径容易在别人机器上失效，所以按"能用就用"的顺序找。
    """
    candidates = [
        Path("D:/anaconda3/anaconda3/envs/agent-builder/python.exe"),
        Path(os.environ.get("CONDA_PREFIX", "")) / "python.exe",
        Path(sys.executable),
    ]
    for path in candidates:
        if path and path.exists():
            return str(path)
    return sys.executable


def check_env() -> bool:
    """缺什么先说清楚，别让用户对着 ImportError 猜。"""
    ok = True
    python = find_python()
    say(f"[i] 解释器：{python}")

    probe = subprocess.run(
        [python, "-c", "import fastapi, uvicorn, httpx, yaml"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        ok = False
        say("[!] 后端依赖缺失，执行：")
        say(f"    {python} -m pip install -r backend/requirements.txt")

    if not (BACKEND / ".env").exists():
        example = BACKEND / ".env.example"
        if example.exists():
            shutil.copyfile(example, BACKEND / ".env")
            say("[+] 已由 backend/.env.example 生成 backend/.env")
        say("[!] 记得填 backend/.env 里的 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")

    if not (FRONTEND / "node_modules").exists():
        ok = False
        say("[!] 前端依赖未安装，执行：cd frontend && npm install")
    return ok


def spawn(cmd: list[str], cwd: Path, name: str) -> subprocess.Popen:
    say(f"[>] 启动{name}：{' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=str(cwd))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--backend", action="store_true", help="只启动后端")
    parser.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    args = parser.parse_args(argv[1:])

    python = find_python()
    if not check_env():
        say("")
        say("[!] 依赖/配置还没就绪，按上面的提示补完再跑")
        return 2

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        procs.append(
            (
                "后端",
                spawn(
                    [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
                    BACKEND,
                    "后端",
                ),
            )
        )
        if not args.backend:
            npm = shutil.which("npm") or "npm"
            procs.append(("前端", spawn([npm, "run", "dev", "--", "--port", "5173"], FRONTEND, "前端")))

        time.sleep(4)
        say("")
        say(f"  接口文档  {API}/docs")
        say(f"  健康检查  {API}/api/health")
        if not args.backend:
            say(f"  工作台    {WEB}")
        say("")
        say("Ctrl+C 一起停止。")
        if not args.no_open and not args.backend:
            webbrowser.open(WEB)

        # 任一进程挂掉就整体退出：留着半套服务只会让人误判
        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    say(f"[!] {name}进程已退出（code={proc.returncode}），一并停止其余进程")
                    return proc.returncode or 1
            time.sleep(1)
    except KeyboardInterrupt:
        say("\n[<] 收到 Ctrl+C，正在停止…")
        return 0
    finally:
        for name, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for name, proc in procs:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
