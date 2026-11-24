"""项目持久化：把"一次构建的产物"落成磁盘上可管理的工程。

为什么要有这一层（对照 job_store 的边界）：
- job_store 是内存任务表，进程重启就没了，只适合"看过程"；
- 这里把**结果**写成真实目录，重启、换浏览器、过一周回来都还在。

磁盘布局（data/projects/<project_id>/）：

    project.json      管理元信息：名字、状态、标签、来源任务、统计
    codegen.json      代码生成的元数据（文件清单、覆盖度、校验），不含正文
    files/<path>      真实的工程文件，可直接浏览/编辑/打包
    design/*.json     需求、架构、组件三份设计产物，便于回溯"为什么这样生成"

不做的事：这里不是数据库，没有事务、没有并发写保护。单用户本地工具够用；
改成多用户/协作时把本模块换成 DB + 对象存储即可，接口保持不变。
"""

from __future__ import annotations

import io
import json
import shutil
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.config import settings
from app.schemas.codegen import CodegenResult

META_FILE = "project.json"
CODEGEN_FILE = "codegen.json"
FILES_DIR = "files"
DESIGN_DIR = "design"
HISTORY_DIR = "history"

# 版本快照：保留最近 10 个，再多就把最老的删掉（每个约几十 KB，别无限长）
MAX_SNAPSHOTS = 10
# 快照里不存的东西：缓存没意义，data/ 是用户运行期产物，可能很大
SNAPSHOT_SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git"}
SNAPSHOT_SKIP_PREFIXES = ("data/",)
SNAPSHOT_SKIP_EXACT = (".env", ".env.local")

MAX_FILE_BYTES = 1 << 20  # 单个文件 1MB 上限：这里放的是脚手架，不是二进制仓库

SUFFIX_KIND = {
    ".py": "python",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "text",
    ".txt": "text",
    ".cfg": "text",
    ".ini": "text",
}


class ProjectError(Exception):
    """项目层的业务错误，路由层翻译成 4xx。"""

    def __init__(self, message: str, *, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------- 基础工具


def _root() -> Path:
    return Path(settings.data_dir) / "projects"


def _now() -> float:
    return time.time()


def _slug(name: str) -> str:
    """目录名：保留中日韩字符（对中文用户来说比一串 hex 可读得多），其余转连字符。"""
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in name.strip()
    )
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug[:40] or "project"


# 生成器给出的英文名常常被洗成 "agent" 这类无信息量的词，
# 这时优先用需求分析产出的 agent_name 做展示名。
GENERIC_NAMES = {"agent", "agent_app", "app", "project", "demo", "untitled", ""}


def _display_name(name: str, requirement: dict[str, Any] | None) -> str:
    cleaned = (name or "").strip()
    if cleaned.lower() not in GENERIC_NAMES:
        return cleaned
    if requirement:
        spec = requirement.get("spec") or requirement
        agent_name = (spec.get("agent_name") or "").strip()
        if agent_name:
            return agent_name
    return cleaned or "未命名 Agent"


def _kind(path: str) -> str:
    return SUFFIX_KIND.get(Path(path).suffix.lower(), "text")


def _lines(content: str) -> int:
    return len(content.splitlines())


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_rel_path(rel: str) -> str:
    """把外部传入的相对路径收敛成安全路径。

    拒绝绝对路径、`..`、盘符和反斜杠穿越 —— 否则 `GET ?path=../../.env`
    就能读到项目目录之外的东西。这是本模块唯一必须严格的地方。
    """
    raw = rel.replace("\\", "/").strip()
    if not raw:
        raise ProjectError("文件路径不能为空", code=422)
    if raw.startswith("/") or ":" in raw:
        raise ProjectError(f"非法文件路径：{rel}", code=422)

    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".", "") for part in parts):
        raise ProjectError(f"非法文件路径：{rel}", code=422)
    return "/".join(parts)


# ---------------------------------------------------------------- 读取


def list_projects(*, include_archived: bool = True) -> list[dict[str, Any]]:
    root = _root()
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / META_FILE
        if not meta_path.exists():
            continue
        try:
            meta = _load_json(meta_path)
        except (OSError, json.JSONDecodeError):
            # 手工改坏了某个 project.json 不该让整个列表 500
            continue
        if not include_archived and meta.get("status") == "archived":
            continue
        # 目录名才是 id 的唯一真相：整目录复制过之后，里面的 project.json
        # 还带着旧 id，会让两个项目抢同一个 id（点开 A 显示 B）。
        meta["id"] = child.name
        items.append(meta)
    items.sort(key=lambda m: m.get("updated_at", 0), reverse=True)
    return items


def get_meta(project_id: str) -> dict[str, Any]:
    meta_path = _root() / project_id / META_FILE
    if not meta_path.exists():
        raise ProjectError(f"项目不存在：{project_id}", code=404)
    try:
        meta = _load_json(meta_path)
        meta["id"] = project_id  # 同上：以目录名为准，不信文件里的旧 id
        return meta
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"项目元信息损坏：{exc}", code=500) from exc


def _scan_files(project_id: str) -> list[dict[str, Any]]:
    base = _root() / project_id / FILES_DIR
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        try:
            size = path.stat().st_size
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append(
            {"path": rel, "kind": _kind(rel), "lines": _lines(text), "bytes": size}
        )
    return out


def get_detail(project_id: str) -> dict[str, Any]:
    meta = get_meta(project_id)
    files = _scan_files(project_id)
    design_dir = _root() / project_id / DESIGN_DIR
    codegen: dict[str, Any] = {}
    codegen_path = _root() / project_id / CODEGEN_FILE
    if codegen_path.exists():
        try:
            codegen = _load_json(codegen_path)
        except (OSError, json.JSONDecodeError):
            codegen = {}
    return {
        **meta,
        "file_list": files,
        "tree": codegen.get("tree", ""),
        "warnings": codegen.get("warnings", []),
        "notes": codegen.get("notes", []),
        "has_requirement": (design_dir / "requirement.json").exists(),
        "has_architecture": (design_dir / "architecture.json").exists(),
        "has_components": (design_dir / "components.json").exists(),
        "has_delivery": (design_dir / "delivery.json").exists(),
        "has_tests": (design_dir / "tests.json").exists(),
        # 以磁盘为准重算统计：用户改过文件后，元信息里的旧数字就不作数了
        "files": len(files),
        "lines": sum(f["lines"] for f in files),
    }


def read_file(project_id: str, rel: str) -> dict[str, Any]:
    safe = safe_rel_path(rel)
    target = _root() / project_id / FILES_DIR / safe
    if not target.is_file():
        raise ProjectError(f"文件不存在：{safe}", code=404)
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectError(f"文件不是文本，无法预览：{safe}", code=415) from exc
    return {
        "path": safe,
        "content": content,
        "kind": _kind(safe),
        "lines": _lines(content),
        "editable": True,
    }


def read_design(project_id: str, stage: str) -> dict[str, Any]:
    if stage not in ("requirement", "architecture", "components", "delivery"):
        raise ProjectError(f"未知的设计阶段：{stage}", code=422)
    path = _root() / project_id / DESIGN_DIR / f"{stage}.json"
    if not path.exists():
        raise ProjectError(f"该项目没有 {stage} 产物", code=404)
    return _load_json(path)


# ---------------------------------------------------------------- 写入


def _unique_id(base: str) -> str:
    root = _root()
    for _ in range(20):
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        if not (root / candidate).exists():
            return candidate
    raise ProjectError("生成项目标识失败，请重试", code=500)


def create_project(
    *,
    name: str,
    codegen: CodegenResult | dict[str, Any],
    requirement: dict[str, Any] | None = None,
    architecture: dict[str, Any] | None = None,
    components: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    job_id: str | None = None,
    raw_text: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """从一次代码生成结果创建项目，返回 ProjectSummary 结构。"""
    result = (
        codegen
        if isinstance(codegen, CodegenResult)
        else CodegenResult.model_validate(codegen)
    )
    if not result.files:
        raise ProjectError("生成结果为空，无法保存为项目", code=422)

    project_name_fallback = name or result.project_name
    display_name = _display_name(project_name_fallback, requirement)
    project_id = _unique_id(_slug(display_name))
    project_dir = _root() / project_id
    files_dir = project_dir / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)

    for f in result.files:
        rel = safe_rel_path(f.path)
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")

    for stage, payload in (
        ("requirement", requirement),
        ("architecture", architecture),
        ("components", components),
        ("delivery", delivery or (result.delivery.model_dump() if result.delivery else None)),
    ):
        if payload:
            _dump_json(project_dir / DESIGN_DIR / f"{stage}.json", payload)

    # 元数据里剔除文件正文：正文只存 files/ 一份，避免改完文件后两份对不上
    codegen_meta = result.model_dump()
    codegen_meta["files"] = [
        {"path": f["path"], "kind": f["kind"], "lines": f["lines"], "source": f["source"]}
        for f in codegen_meta["files"]
    ]
    _dump_json(project_dir / CODEGEN_FILE, codegen_meta)

    now = _now()
    meta: dict[str, Any] = {
        "id": project_id,
        "name": display_name,
        "description": description.strip(),
        "status": "active",
        "tags": list(tags or []),
        "job_id": job_id,
        "raw_text": raw_text,
        "agent_name": None,
        "architecture_type": None,
        "complexity_level": None,
        "created_at": now,
        "updated_at": now,
        "files": result.stats.files,
        "lines": result.stats.lines,
        "tests": result.stats.tests,
        "validation_passed": result.validation.passed,
    }
    if requirement:
        spec = requirement.get("spec") or requirement
        meta["agent_name"] = spec.get("agent_name")
    if architecture:
        spec = architecture.get("spec") or architecture
        meta["architecture_type"] = spec.get("architecture_type")
        meta["complexity_level"] = spec.get("complexity_level")

    _dump_json(project_dir / META_FILE, meta)
    # 初始版本也要进历史：不然第一次"更新代码"之后就没有可回的起点了
    try:
        snapshot(project_id, reason="生成", note="初始构建")
    except (OSError, ProjectError):  # pragma: no cover - 快照失败不该让创建失败
        pass
    return meta


def update_meta(
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    meta = get_meta(project_id)
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise ProjectError("项目名不能为空", code=422)
        if len(cleaned) > 80:
            raise ProjectError("项目名太长（最多 80 字）", code=422)
        meta["name"] = cleaned
    if description is not None:
        meta["description"] = description.strip()
    if status is not None:
        if status not in ("active", "archived"):
            raise ProjectError(f"未知状态：{status}", code=422)
        meta["status"] = status
    if tags is not None:
        meta["tags"] = [t.strip() for t in tags if t.strip()][:10]
    meta["updated_at"] = _now()
    _dump_json(_root() / project_id / META_FILE, meta)
    return meta


def write_file(project_id: str, rel: str, content: str) -> dict[str, Any]:
    meta = get_meta(project_id)  # 顺带校验项目存在
    safe = safe_rel_path(rel)
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ProjectError("文件超过 1MB，请在本地编辑", code=413)
    target = _root() / project_id / FILES_DIR / safe
    if not target.is_file():
        raise ProjectError(f"文件不存在：{safe}（暂不支持新增文件）", code=404)
    target.write_text(content, encoding="utf-8")
    meta["updated_at"] = _now()
    _dump_json(_root() / project_id / META_FILE, meta)
    return {
        "path": safe,
        "content": content,
        "kind": _kind(safe),
        "lines": _lines(content),
        "editable": True,
    }


def delete_project(project_id: str) -> dict[str, Any]:
    """彻底删除：整个项目目录连同文件一起从磁盘抹掉，不可恢复。

    这一层刻意不做回收站：既然前端已经明确告诉用户"不可恢复"并要求二次确认，
    再偷偷留一份副本只会让磁盘越用越乱（旧实现把目录挪进 data/.trash/，
    用户以为删了、其实一直占着地方）。
    """
    root = _root()
    # 先校验 id 形状再拼路径：project_id 来自 URL，不能让它变成 ../../
    safe_id = safe_rel_path(project_id)
    if "/" in safe_id:
        raise ProjectError(f"非法项目标识：{project_id}", code=422)
    project_dir = (root / safe_id).resolve()
    if project_dir.parent != root.resolve() or not project_dir.is_dir():
        raise ProjectError(f"项目不存在：{project_id}", code=404)

    removed = sum(1 for p in project_dir.rglob("*") if p.is_file())
    try:
        shutil.rmtree(project_dir)
    except SystemExit as exc:
        # 某些宿主环境（IDE/沙箱）会给 shutil.rmtree 挂钩子做"批量删除保护"，
        # 触发时抛的是 SystemExit 而不是 OSError。不接住的话整个接口会返回
        # 纯文本 500，前端只能看到"响应不是合法 JSON"。
        raise ProjectError(
            "删除被当前运行环境的批量删除保护拦下了（本轮删除文件过多），"
            "请稍后重试，或直接删除 data/projects/ 下对应目录",
            code=409,
        ) from exc
    except OSError as exc:
        raise ProjectError(f"删除失败：{exc}", code=500) from exc
    return {"id": project_id, "deleted": True, "files_removed": removed}


# 重新生成时要保住的东西：这些是用户的真实资产，不能被模板覆盖
KEEP_PREFIXES = ("knowledge/", "data/")
KEEP_EXACT = (".env", ".env.local")


def refresh_project(project_id: str) -> dict[str, Any]:
    """用已存的设计产物重跑一遍模板生成，覆盖 files/ 里的代码。

    存在的意义：代码生成是纯模板（0 token），模板一改老项目就落后了 ——
    以前只能整条链路重跑（要重新调模型、项目名还多一个），现在点一下就把
    最新的模板修复带进老项目。

    保守策略：只覆盖新生成结果里有的路径，**不删任何已有文件**；
    `.env`、`knowledge/`、`data/` 属于用户资产，永远跳过。
    """
    # 延迟导入：agents.code_generator 依赖 templates，避免模块级循环引用
    from app.agents.code_generator import generate_code
    from app.schemas.codegen import CodegenRequest

    meta = get_meta(project_id)
    design_dir = _root() / project_id / DESIGN_DIR
    payloads: dict[str, Any] = {}
    for stage in ("requirement", "architecture", "components"):
        path = design_dir / f"{stage}.json"
        if not path.exists():
            raise ProjectError(
                f"缺少设计产物 {stage}.json，无法重新生成（请整条链路重建）", code=409
            )
        payloads[stage] = _load_json(path)

    payload = CodegenRequest(
        requirement=payloads["requirement"]["spec"],
        architecture=payloads["architecture"]["spec"],
        components=payloads["components"]["plan"],
    )
    result = generate_code(payload)
    if not result.files:
        raise ProjectError("重新生成结果为空，已放弃覆盖", code=500)

    # 覆盖前留底：refresh 会改掉用户手改过的模型代码，回滚点必须拍在写之前
    try:
        snapshot(project_id, reason="更新代码前", note="模板重刷前的自动备份")
    except (OSError, ProjectError):  # pragma: no cover
        pass

    files_dir = _root() / project_id / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    kept: list[str] = []
    for f in result.files:
        rel = safe_rel_path(f.path)
        if rel in KEEP_EXACT or rel.startswith(KEEP_PREFIXES):
            kept.append(rel)
            continue
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # 内容没变就不碰文件：避免把用户手改过的同名文件静默覆盖掉时间戳
        if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == f.content:
            kept.append(rel)
            continue
        target.write_text(f.content, encoding="utf-8")
        written.append(rel)

    _dump_json(design_dir / "delivery.json", result.delivery.model_dump() if result.delivery else {})
    codegen_meta = result.model_dump()
    codegen_meta["files"] = [
        {"path": f["path"], "kind": f["kind"], "lines": f["lines"], "source": f["source"]}
        for f in codegen_meta["files"]
    ]
    _dump_json(_root() / project_id / CODEGEN_FILE, codegen_meta)

    meta["files"] = result.stats.files
    meta["lines"] = result.stats.lines
    meta["tests"] = result.stats.tests
    meta["validation_passed"] = result.validation.passed
    meta["updated_at"] = _now()
    _dump_json(_root() / project_id / META_FILE, meta)

    return {
        "id": project_id,
        "files_written": len(written),
        "files_kept": len(kept),
        "written": written,
        "stats": result.stats.model_dump(),
        "validation_passed": result.validation.passed,
        "issues": [i.model_dump() for i in result.validation.issues],
    }


# ---------------------------------------------------------------- 版本快照


def _history_root(project_id: str) -> Path:
    return _root() / project_id / HISTORY_DIR


def _snapshot_dirs(project_id: str) -> list[Path]:
    root = _history_root(project_id)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists())


def _copy_files(src: Path, dst: Path) -> list[str]:
    """把 src 下该进快照的文件拷到 dst，返回相对路径清单。"""
    out: list[str] = []
    if not src.is_dir():
        return out
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if any(part in SNAPSHOT_SKIP_DIRS for part in PurePosixPath(rel).parts):
            continue
        if rel in SNAPSHOT_SKIP_EXACT or rel.startswith(SNAPSHOT_SKIP_PREFIXES):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        out.append(rel)
    return out


def _count(files_dir: Path, rels: list[str]) -> tuple[int, int]:
    lines = 0
    for rel in rels:
        try:
            lines += _lines((files_dir / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return len(rels), lines


def snapshot(project_id: str, *, reason: str, note: str = "") -> dict[str, Any]:
    """给当前 files/ 拍一张快照。

    什么时候拍：初始生成、模板重刷、AI 自动修复、回滚前。**不**在每次手改文件时拍
    —— 那会把历史塞满无意义的小版本，反而找不到关键节点。
    """
    get_meta(project_id)  # 校验存在
    root = _history_root(project_id)
    root.mkdir(parents=True, exist_ok=True)

    existing = _snapshot_dirs(project_id)
    seq = 1
    if existing:
        try:
            seq = max(int(p.name.lstrip("v")) for p in existing) + 1
        except ValueError:
            seq = len(existing) + 1
    version = f"v{seq:03d}"
    target = root / version
    files_dir = _root() / project_id / FILES_DIR
    rels = _copy_files(files_dir, target / "files")
    files, lines = _count(target / "files", rels)

    codegen = _root() / project_id / CODEGEN_FILE
    if codegen.exists():
        shutil.copy2(codegen, target / CODEGEN_FILE)

    meta = {
        "version": version,
        "created_at": _now(),
        "reason": reason,
        "note": note,
        "files": files,
        "lines": lines,
        "file_list": rels,
    }
    _dump_json(target / "meta.json", meta)

    _prune_history(project_id, keep=MAX_SNAPSHOTS)
    return meta


def _prune_history(project_id: str, *, keep: int) -> list[str]:
    dirs = _snapshot_dirs(project_id)
    if len(dirs) <= keep:
        return []
    removed: list[str] = []
    for path in dirs[: len(dirs) - keep]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


def list_history(project_id: str) -> list[dict[str, Any]]:
    get_meta(project_id)
    items: list[dict[str, Any]] = []
    for path in _snapshot_dirs(project_id):
        try:
            meta = _load_json(path / "meta.json")
        except (OSError, json.JSONDecodeError):
            continue
        meta.pop("file_list", None)  # 列表接口不带全量清单，详情才给
        items.append(meta)
    items.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return items


def _snapshot_path(project_id: str, version: str) -> Path:
    safe = safe_rel_path(version)
    if "/" in safe or not safe.startswith("v"):
        raise ProjectError(f"非法版本号：{version}", code=422)
    path = _history_root(project_id) / safe
    if not path.is_dir():
        raise ProjectError(f"版本不存在：{version}", code=404)
    return path


def read_version(project_id: str, version: str) -> dict[str, Any]:
    path = _snapshot_path(project_id, version)
    meta = _load_json(path / "meta.json")
    return meta


def diff_version(project_id: str, version: str) -> dict[str, Any]:
    """快照 vs 当前，逐文件给 added / removed / modified。"""
    path = _snapshot_path(project_id, version)
    snap_files = path / FILES_DIR
    cur_files = _root() / project_id / FILES_DIR

    snap_rels = {
        p.relative_to(snap_files).as_posix()
        for p in snap_files.rglob("*")
        if p.is_file()
    } if snap_files.is_dir() else set()
    cur_rels = {
        p.relative_to(cur_files).as_posix()
        for p in cur_files.rglob("*")
        if p.is_file()
        and not any(part in SNAPSHOT_SKIP_DIRS for part in PurePosixPath(p.relative_to(cur_files).as_posix()).parts)
    } if cur_files.is_dir() else set()

    changes: list[dict[str, Any]] = []
    for rel in sorted(snap_rels & cur_rels):
        try:
            old = (snap_files / rel).read_text(encoding="utf-8")
            new = (cur_files / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if old != new:
            changes.append(
                {
                    "path": rel,
                    "status": "modified",
                    "lines_delta": _lines(new) - _lines(old),
                }
            )
    for rel in sorted(cur_rels - snap_rels):
        changes.append({"path": rel, "status": "added", "lines_delta": 0})
    for rel in sorted(snap_rels - cur_rels):
        changes.append({"path": rel, "status": "removed", "lines_delta": 0})

    counts = {"added": 0, "removed": 0, "modified": 0}
    for c in changes:
        counts[c["status"]] += 1
    return {
        "version": version,
        "changes": changes,
        **counts,
        "identical": not changes,
    }


def restore_version(project_id: str, version: str, *, note: str = "") -> dict[str, Any]:
    """把 files/ 恢复到某个快照。

    两条规矩，都是为了不把用户的东西弄丢：
    1. **回滚前先给当前状态拍一张快照**（reason=restore），所以回滚本身可撤销；
    2. 只覆盖/新增快照里有的文件，**不删除**当前多出来的文件；`.env`、`knowledge/`、
       `data/` 一律不动 —— 那里面是用户的 Key 和语料。
    """
    get_meta(project_id)
    path = _snapshot_path(project_id, version)
    snap_files = path / FILES_DIR
    if not snap_files.is_dir():
        raise ProjectError(f"版本 {version} 没有文件内容", code=409)

    backup = snapshot(project_id, reason="restore", note=f"回滚到 {version} 前的自动备份")

    files_dir = _root() / project_id / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    kept: list[str] = []
    for src in sorted(snap_files.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(snap_files).as_posix()
        if rel in KEEP_EXACT or rel.startswith(KEEP_PREFIXES):
            kept.append(rel)
            continue
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        restored.append(rel)

    meta = get_meta(project_id)
    files, lines = _count(files_dir, [f["path"] for f in _scan_files(project_id)])
    meta["files"] = files
    meta["lines"] = lines
    meta["updated_at"] = _now()
    _dump_json(_root() / project_id / META_FILE, meta)

    codegen = path / CODEGEN_FILE
    if codegen.exists():
        shutil.copy2(codegen, _root() / project_id / CODEGEN_FILE)

    return {
        "id": project_id,
        "restored_from": version,
        "restored": restored,
        "kept": kept,
        "backup_version": backup["version"],
        "files": files,
        "lines": lines,
        "note": "只覆盖不回删；回滚前的状态已自动备份，可再回滚回来",
    }


def zip_bytes(project_id: str) -> tuple[bytes, str]:
    meta = get_meta(project_id)
    base = _root() / project_id / FILES_DIR
    if not base.exists():
        raise ProjectError("该项目没有任何文件", code=404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                arc = f"{project_id}/{path.relative_to(base).as_posix()}"
                zf.write(path, arc)
    return buffer.getvalue(), meta.get("name") or project_id
