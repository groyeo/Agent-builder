"""项目预览（本机网页）的进程管理测试。

不真起 server.py：这里测的是"起/复用/停/失败清理"的状态机，
真实 HTTP 行为由 test_generated_web.py 用子进程覆盖。两层分开，失败时好定位。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.engine import preview
from app.engine.project_store import ProjectError


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """把 data_dir 指到临时目录，项目存在性用桩替代。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(preview, "get_meta", lambda project_id: {"id": project_id})
    monkeypatch.setattr(preview, "_free_port", lambda: 18080)
    preview._previews.clear()
    yield tmp_path
    preview._previews.clear()


def _make_project(root: Path, *, with_server: bool = True) -> str:
    project_id = "p1"
    base = root / "projects" / project_id
    files = base / "files"
    files.mkdir(parents=True)
    (base / "design").mkdir()
    if with_server:
        (files / "server.py").write_text("print('ok')", encoding="utf-8")
    return project_id


def test_start_requires_server_py(env) -> None:
    project_id = _make_project(env, with_server=False)
    with pytest.raises(ProjectError) as info:
        preview.start(project_id)
    assert info.value.code == 409
    assert "server.py" in info.value.message


def test_start_then_reuse_then_stop(env, monkeypatch) -> None:
    project_id = _make_project(env)
    spawned: list[FakeProc] = []

    def fake_popen(*args, **kwargs):
        proc = FakeProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(preview.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(preview, "_wait_ready", lambda url, proc, deadline: True)

    first = preview.start(project_id)
    assert first["running"] is True
    assert first["url"] == "http://127.0.0.1:18080"
    assert first["reused"] is False
    assert len(spawned) == 1

    # 已在跑就别再起一个：本机端口是稀缺资源，进程也是
    second = preview.start(project_id)
    assert second["reused"] is True
    assert len(spawned) == 1
    assert preview.status(project_id)["running"] is True

    stopped = preview.stop(project_id)
    assert stopped["stopped"] is True
    assert spawned[0].terminated is True
    assert preview.status(project_id)["running"] is False


def test_failed_start_cleans_up(env, monkeypatch) -> None:
    project_id = _make_project(env)
    proc = FakeProc()
    monkeypatch.setattr(preview.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(preview, "_wait_ready", lambda url, p, deadline: False)

    with pytest.raises(ProjectError) as info:
        preview.start(project_id)
    assert info.value.code == 500
    assert proc.terminated is True
    # 失败不能留下"看着像在跑"的记录
    assert project_id not in preview._previews
    assert preview.status(project_id)["running"] is False


def test_child_env_reuses_platform_model(env, monkeypatch) -> None:
    """目标 Agent 不该再配一遍 Key：平台有的直接注入。"""
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "llm_model", "deepseek-chat")

    env_map = preview._child_env()
    assert env_map["OPENAI_API_KEY"] == "sk-test"
    assert env_map["OPENAI_MODEL"] == "deepseek-chat"
    assert env_map["PYTHONUTF8"] == "1"


def test_child_env_skips_mock_provider(env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")

    env_map = preview._child_env()
    # mock 模式的 Key 是假的，注入进去只会让人误以为能真跑
    assert "OPENAI_API_KEY" not in env_map or env_map["OPENAI_API_KEY"] != "sk-test"
