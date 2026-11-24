"""项目管理测试。

覆盖两件事：
1. 落库本身（自动/手动、幂等、磁盘结构与磁盘一致）；
2. 管理动作（改名、归档、改文件、打包、删除）以及**路径穿越防护**。
"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.llm.mock import MockAdapter
from app.engine import project_store
from app.engine.job_store import job_store
from app.engine.pipeline import run_pipeline
from app.main import app

RAW = "做一个销售周报 Agent，读取销售明细 Excel，按区域汇总并生成周报邮件草稿。"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_store():
    job_store.clear()
    yield
    job_store.clear()


@pytest.fixture
def mock_llm(monkeypatch):
    adapter = MockAdapter()
    for mod in (
        "app.agents.requirement_analyzer",
        "app.agents.architecture_planner",
        "app.agents.component_designer",
    ):
        monkeypatch.setattr(f"{mod}.get_llm", lambda: adapter)
    return adapter


@pytest.fixture
def done_job(mock_llm):
    job = job_store.create(RAW, {"pause_on_blocking": False})
    asyncio.run(run_pipeline(job))
    assert job.status == "done"
    return job


def test_pipeline_auto_saves_project(done_job, isolated_data_dir) -> None:
    project_id = done_job.result.get("project_id")
    assert project_id, "自动保存后 result 里应带 project_id"

    project_dir = isolated_data_dir / "projects" / project_id
    assert (project_dir / "project.json").exists()
    assert (project_dir / "codegen.json").exists()
    assert (project_dir / "design" / "requirement.json").exists()

    # 磁盘上的文件必须与生成结果逐个一致
    codegen = done_job.result["codegen"]
    for f in codegen["files"]:
        on_disk = project_dir / "files" / f["path"]
        assert on_disk.exists(), f["path"]
        assert on_disk.read_text(encoding="utf-8") == f["content"]


def test_manual_create_is_idempotent(done_job) -> None:
    created = done_job.result["project_id"]
    resp = client.post("/api/projects", json={"job_id": done_job.id})
    assert resp.status_code in (200, 201)
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["id"] == created

    listed = client.get("/api/projects").json()["data"]
    assert len(listed) == 1


def test_manual_create_requires_codegen(mock_llm) -> None:
    job = job_store.create(RAW, {"pause_on_blocking": False})
    job.result["requirement"] = {"spec": {}}
    resp = client.post("/api/projects", json={"job_id": job.id})
    assert resp.status_code == 409


def test_list_and_detail(done_job) -> None:
    project_id = done_job.result["project_id"]
    detail = client.get(f"/api/projects/{project_id}").json()["data"]

    assert detail["id"] == project_id
    assert detail["status"] == "active"
    assert detail["files"] == len(detail["file_list"]) > 0
    assert detail["has_requirement"] and detail["has_architecture"]
    assert "README.md" in [f["path"] for f in detail["file_list"]]


def test_read_file_and_traversal_blocked(done_job) -> None:
    project_id = done_job.result["project_id"]

    ok = client.get(f"/api/projects/{project_id}/files", params={"path": "README.md"})
    assert ok.status_code == 200
    assert ok.json()["data"]["content"]

    for evil in ("../../.env", "/etc/passwd", "..\\..\\project.json", "a/../../x"):
        bad = client.get(f"/api/projects/{project_id}/files", params={"path": evil})
        assert bad.status_code == 422, evil


def test_write_file_updates_detail(done_job) -> None:
    project_id = done_job.result["project_id"]
    resp = client.put(
        f"/api/projects/{project_id}/files",
        params={"path": "README.md"},
        json={"content": "# 改过的标题\n\n第二行\n"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["lines"] == 3

    reread = client.get(
        f"/api/projects/{project_id}/files", params={"path": "README.md"}
    ).json()["data"]
    assert reread["content"].startswith("# 改过的标题")


def test_patch_rename_and_archive(done_job) -> None:
    project_id = done_job.result["project_id"]
    resp = client.patch(
        f"/api/projects/{project_id}",
        json={"name": "销售周报 v2", "description": "给运营用的", "tags": ["demo"]},
    )
    data = resp.json()["data"]
    assert data["name"] == "销售周报 v2"
    assert data["tags"] == ["demo"]

    client.patch(f"/api/projects/{project_id}", json={"status": "archived"})
    active_only = client.get(
        "/api/projects", params={"include_archived": False}
    ).json()["data"]
    assert active_only == []

    assert client.patch(f"/api/projects/{project_id}", json={"name": "  "}).status_code == 422


def test_download_zip(done_job) -> None:
    project_id = done_job.result["project_id"]
    resp = client.get(f"/api/projects/{project_id}/zip")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
    assert any(n.endswith("README.md") for n in names)
    assert all(n.startswith(f"{project_id}/") for n in names)


def test_delete_is_permanent(done_job, isolated_data_dir) -> None:
    """彻底删除：目录消失、没有回收站兜底、列表里也没了。"""
    project_id = done_job.result["project_id"]
    project_dir = isolated_data_dir / "projects" / project_id
    assert project_dir.is_dir()

    resp = client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deleted"] is True
    assert data["files_removed"] > 0

    assert not project_dir.exists()
    assert not (isolated_data_dir / ".trash").exists()

    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert client.get("/api/projects").json()["data"] == []


def test_delete_rejects_path_traversal() -> None:
    """project_id 来自 URL，不能让 ../../ 把 data 目录外面删掉。"""
    assert client.delete("/api/projects/..%2F..%2Fetc").status_code in (404, 422)
    assert client.delete("/api/projects/nope-not-here").status_code == 404


def test_auto_save_can_be_disabled(mock_llm, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auto_save_project", False)
    job = job_store.create(RAW, {"pause_on_blocking": False})
    asyncio.run(run_pipeline(job))

    assert job.status == "done"
    assert "project_id" not in job.result
    assert project_store.list_projects() == []

    # 关掉自动保存后仍可手动落库
    resp = client.post("/api/projects", json={"job_id": job.id, "name": "手动保存的项目"})
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "手动保存的项目"


def test_missing_project_returns_404() -> None:
    assert client.get("/api/projects/nope-000000").status_code == 404
    assert client.post("/api/projects", json={"job_id": "nope"}).status_code == 404


def test_generic_codegen_name_falls_back_to_agent_name(done_job) -> None:
    """生成器把中文名洗成 "agent" 时，展示名应该退回需求里的 agent_name。"""
    from app.schemas.codegen import CodegenResult

    codegen = CodegenResult.model_validate(done_job.result["codegen"])
    meta = project_store.create_project(
        name="agent",
        codegen=codegen,
        requirement={"spec": {"agent_name": "销售周报生成Agent"}},
    )
    assert meta["name"] == "销售周报生成Agent"
    assert meta["id"].startswith("销售周报生成agent-")

    # 中文名不能直接进响应头，必须走 filename* 编码
    resp = client.get(f"/api/projects/{meta['id']}/zip")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E9%94%80" in disposition  # "销" 的 UTF-8 百分号编码


# --------------------------------------------------- 用最新模板重新生成（老项目补救）
def test_refresh_rewrites_code_but_keeps_user_assets(done_job, isolated_data_dir) -> None:
    """模板改过之后，老项目要能一键跟上，且不能动用户的 .env / 语料 / 数据。

    背景：`agent/env.py`（.env 载入器）是后来才加进模板的，加之前生成的项目
    一律"自检通过、一跑就报缺少 OPENAI_BASE_URL"。重新生成是补救手段。
    """
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"

    # 模拟老项目：删掉后来新增的 env.py，并制造用户自己的资产
    (files / "agent" / "env.py").unlink()
    (files / ".env").write_text("OPENAI_API_KEY=sk-user-owned\n", encoding="utf-8")
    (files / "knowledge").mkdir(exist_ok=True)
    (files / "knowledge" / "faq.md").write_text("用户自己的语料\n", encoding="utf-8")

    resp = client.post(f"/api/projects/{project_id}/refresh")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["files_written"] > 0
    assert "agent/env.py" in data["written"]

    assert (files / "agent" / "env.py").exists()
    assert (files / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-user-owned\n"
    assert (files / "knowledge" / "faq.md").read_text(encoding="utf-8") == "用户自己的语料\n"
    assert ".env" not in data["written"]
    assert "knowledge/faq.md" not in data["written"]


def test_refresh_is_idempotent(done_job) -> None:
    """连点两次不该反复重写：第二次全部命中"内容没变"。"""
    project_id = done_job.result["project_id"]
    assert client.post(f"/api/projects/{project_id}/refresh").status_code == 200
    second = client.post(f"/api/projects/{project_id}/refresh").json()["data"]
    assert second["files_written"] == 0


def test_refresh_missing_project_404() -> None:
    assert client.post("/api/projects/nope-000000/refresh").status_code == 404


# --------------------------------------------------- 用最新模板重新生成（老项目补救）
def test_refresh_rewrites_code_but_keeps_user_assets(done_job, isolated_data_dir) -> None:
    """模板改过之后，老项目要能一键跟上，且不能动用户的 .env / 语料 / 数据。

    背景：`agent/env.py`（.env 载入器）是后来才加进模板的，加之前生成的项目
    一律"自检通过、一跑就报缺少 OPENAI_BASE_URL"。重新生成是补救手段。
    """
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"

    # 模拟老项目：删掉后来新增的 env.py，并制造用户自己的资产
    (files / "agent" / "env.py").unlink()
    (files / ".env").write_text("OPENAI_API_KEY=sk-user-owned\n", encoding="utf-8")
    (files / "knowledge").mkdir(exist_ok=True)
    (files / "knowledge" / "faq.md").write_text("用户自己的语料\n", encoding="utf-8")

    resp = client.post(f"/api/projects/{project_id}/refresh")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["files_written"] > 0
    assert "agent/env.py" in data["written"]

    assert (files / "agent" / "env.py").exists()
    assert (files / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-user-owned\n"
    assert (files / "knowledge" / "faq.md").read_text(encoding="utf-8") == "用户自己的语料\n"
    assert ".env" not in data["written"]
    assert "knowledge/faq.md" not in data["written"]


def test_refresh_is_idempotent(done_job) -> None:
    """连点两次不该反复重写：第二次全部命中"内容没变"。"""
    project_id = done_job.result["project_id"]
    assert client.post(f"/api/projects/{project_id}/refresh").status_code == 200
    second = client.post(f"/api/projects/{project_id}/refresh").json()["data"]
    assert second["files_written"] == 0


def test_refresh_missing_project_404() -> None:
    assert client.post("/api/projects/nope-000000/refresh").status_code == 404


def test_copied_project_dir_uses_dir_name_as_id(done_job, isolated_data_dir) -> None:
    """整目录复制出来的项目，id 必须以目录名为准。

    真实踩到过：把项目目录 `cp` 一份做实验，里面的 project.json 还带着旧 id，
    列表里就出现两个 id 相同的项目 —— 点开 A 显示的是 B 的详情。
    """
    import shutil

    project_id = done_job.result["project_id"]
    root = isolated_data_dir / "projects"
    clone = root / "复制出来的项目"
    shutil.copytree(root / project_id, clone)  # 目录名变了，json 里的 id 还是旧的

    items = client.get("/api/projects").json()["data"]
    ids = [p["id"] for p in items]
    assert len(ids) == len(set(ids)), f"出现了重复 id：{ids}"
    assert "复制出来的项目" in ids
    assert client.get("/api/projects/复制出来的项目").json()["data"]["id"] == "复制出来的项目"
    assert project_store.get_meta("复制出来的项目")["id"] == "复制出来的项目"


# ---------------------------------------------------------------- 版本历史与回滚
def test_initial_snapshot_exists(done_job) -> None:
    """新建项目就有一个可回的起点，不然第一次重刷之后就没退路了。"""
    project_id = done_job.result["project_id"]
    data = client.get(f"/api/projects/{project_id}/history").json()["data"]
    assert len(data["versions"]) == 1
    v = data["versions"][0]
    assert v["version"] == "v001"
    assert v["reason"] == "生成"
    assert v["files"] > 0 and v["lines"] > 0


def test_refresh_snapshots_before_overwriting(done_job, isolated_data_dir) -> None:
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"
    (files / "main.py").write_text("# 用户手改过\n", encoding="utf-8")

    client.post(f"/api/projects/{project_id}/refresh")
    versions = client.get(f"/api/projects/{project_id}/history").json()["data"]["versions"]
    reasons = [v["reason"] for v in versions]
    assert "更新代码前" in reasons

    backup = next(v for v in versions if v["reason"] == "更新代码前")
    detail = client.get(
        f"/api/projects/{project_id}/history/{backup['version']}"
    ).json()["data"]
    assert "main.py" in detail["file_list"]


def test_diff_reports_modified_file(done_job, isolated_data_dir) -> None:
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"
    target = files / "main.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# 手动加一行\n", encoding="utf-8")

    data = client.get(f"/api/projects/{project_id}/history/v001/diff").json()["data"]
    assert data["identical"] is False
    changed = {c["path"]: c for c in data["changes"]}
    assert changed["main.py"]["status"] == "modified"
    assert changed["main.py"]["lines_delta"] >= 1


def test_restore_brings_code_back_but_keeps_user_assets(done_job, isolated_data_dir) -> None:
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"
    original = (files / "main.py").read_text(encoding="utf-8")
    (files / "main.py").write_text("# 改坏了\n", encoding="utf-8")
    (files / ".env").write_text("OPENAI_API_KEY=sk-user\n", encoding="utf-8")
    (files / "knowledge").mkdir(exist_ok=True)
    (files / "knowledge" / "faq.md").write_text("语料\n", encoding="utf-8")

    resp = client.post(f"/api/projects/{project_id}/history/v001/restore")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["backup_version"] and data["backup_version"] != "v001"

    assert (files / "main.py").read_text(encoding="utf-8") == original
    # 用户资产不能被回滚冲掉
    assert (files / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-user\n"
    assert (files / "knowledge" / "faq.md").read_text(encoding="utf-8") == "语料\n"
    # .env 根本不进快照（避免把 Key 复制进历史），所以也不会被回滚覆盖
    assert ".env" not in data["restored"]


def test_restore_is_itself_undoable(done_job, isolated_data_dir) -> None:
    """回滚前自动备份 —— 回滚错了还能回滚回来。"""
    project_id = done_job.result["project_id"]
    files = isolated_data_dir / "projects" / project_id / "files"
    (files / "main.py").write_text("# 手改版本\n", encoding="utf-8")

    data = client.post(f"/api/projects/{project_id}/history/v001/restore").json()["data"]
    back = client.post(
        f"/api/projects/{project_id}/history/{data['backup_version']}/restore"
    )
    assert back.status_code == 200
    assert (files / "main.py").read_text(encoding="utf-8") == "# 手改版本\n"


def test_version_path_traversal_rejected(done_job) -> None:
    project_id = done_job.result["project_id"]
    assert client.get(f"/api/projects/{project_id}/history/..%2F..%2Fetc/diff").status_code in (404, 422)
    assert client.get(f"/api/projects/{project_id}/history/v999/diff").status_code == 404


def test_manual_snapshot(done_job) -> None:
    project_id = done_job.result["project_id"]
    resp = client.post(f"/api/projects/{project_id}/snapshot?note=改之前先存一份")
    assert resp.status_code == 200
    assert resp.json()["data"]["reason"] == "手动"
    versions = client.get(f"/api/projects/{project_id}/history").json()["data"]["versions"]
    assert len(versions) == 2


def test_history_is_pruned(done_job) -> None:
    """快照不能无限长：超过上限删最老的，且删的确实是老的。"""
    project_id = done_job.result["project_id"]
    for i in range(project_store.MAX_SNAPSHOTS + 3):
        client.post(f"/api/projects/{project_id}/snapshot?note=第{i}次")
    versions = client.get(f"/api/projects/{project_id}/history").json()["data"]["versions"]
    assert len(versions) == project_store.MAX_SNAPSHOTS
    assert "v001" not in [v["version"] for v in versions]
