"""沙箱层测试：能容错的地方容错，但"有没有隔离"必须说实话。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.config import settings
from app.engine import sandbox


@pytest.fixture(autouse=True)
def _reset() -> None:
    sandbox.reset_probe_cache()
    original = settings.sandbox_driver
    yield
    settings.sandbox_driver = original
    sandbox.reset_probe_cache()


def test_probe_reports_docker_absence_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 docker 时要说"没装"，而不是假装有隔离。"""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    info = sandbox.probe(refresh=True)
    assert info["docker_binary"] is None
    assert info["docker_server"] is None

    settings.sandbox_driver = "auto"
    st = sandbox.status()
    assert st["driver"] == "local"
    assert st["isolated"] is False
    assert "没有 docker" in st["reason"]
    assert st["limits"] == {}


def test_daemon_down_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """装了 docker 但守护进程没起（Docker Desktop 没开）属于最常见的情况。"""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "C:/fake/docker.exe")
    monkeypatch.setattr(
        sandbox,
        "_run",
        lambda cmd, timeout=20: (1, "Cannot connect to the Docker daemon"),
    )
    info = sandbox.probe(refresh=True)
    assert info["docker_binary"]
    assert info["docker_server"] is None
    assert "daemon" in info["docker_error"]

    st = sandbox.status()
    assert st["driver"] == "local"
    assert "守护进程没起" in st["reason"]


def test_forced_docker_without_docker_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 docker 却不可用时要报错，不能悄悄降级 —— 用户以为隔离了才危险。"""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    settings.sandbox_driver = "docker"
    with pytest.raises(sandbox.SandboxError) as exc:
        sandbox.driver()
    assert "SANDBOX_DRIVER=docker" in str(exc.value)


def test_local_driver_actually_runs_pytest(tmp_path: Path) -> None:
    """兜底驱动也得真能跑，不能只是"检测不到 docker 就摆烂"。"""
    project = tmp_path / "files"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_ok.py").write_text(
        "def test_one():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    settings.sandbox_driver = "local"

    run = sandbox.run_pytest(project, ["tests"], timeout=120)
    assert run["driver"] == "local"
    assert run["exit_code"] == 0
    assert run["timed_out"] is False
    assert run["isolation"]["driver"] == "local"
    assert "无网络" in run["isolation"]["detail"]
    assert run["xml_path"] and Path(run["xml_path"]).exists()


def test_docker_command_carries_the_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器参数是这一层的核心承诺，必须逐条落进命令行。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        captured["cmd"] = cmd

        class P:
            returncode = 0
            stdout = "1 passed"
            stderr = ""

        return P()

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(sandbox, "probe", lambda refresh=False: {
        "docker_binary": "docker",
        "docker_server": "24.0.7",
        "image": sandbox.IMAGE,
        "image_built": True,
        "preferred": "docker",
    })
    monkeypatch.setattr(sandbox, "ensure_image", lambda **_: sandbox.IMAGE)
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    settings.sandbox_driver = "auto"

    run = sandbox.run_pytest(Path("."), ["tests"], timeout=60)

    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in cmd
    assert any(a.startswith("--memory=") for a in cmd)
    assert any(a.startswith("--cpus=") for a in cmd)
    assert any(a.startswith("--pids-limit=") for a in cmd)
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    assert sandbox.IMAGE in cmd
    assert run["isolation"]["driver"] == "docker"
    assert run["isolation"]["limits"]["network"] == "none"


def test_docker_timeout_kills_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时只 kill 掉 docker 客户端的话，容器还在后台跑，必须 docker rm -f。"""
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        seen.append(cmd)
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, 60, output="")

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(sandbox, "probe", lambda refresh=False: {
        "docker_binary": "docker",
        "docker_server": "24.0.7",
        "image": sandbox.IMAGE,
        "image_built": True,
        "preferred": "docker",
    })
    monkeypatch.setattr(sandbox, "ensure_image", lambda **_: sandbox.IMAGE)
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    settings.sandbox_driver = "auto"

    run = sandbox.run_pytest(Path("."), ["tests"], timeout=1)
    assert run["timed_out"] is True
    assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in seen), seen
