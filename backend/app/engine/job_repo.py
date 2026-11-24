"""任务与事件持久化。

为什么要做（对照 job_store 原来的边界）：
- 原实现全在进程内存里：服务重启 → 任务、事件流、待提交澄清全部消失，
  用户看着 0.03s 提交成功，第二天回来只看到 404；
- 方案里这一层原本是 PostgreSQL + SQLAlchemy。本机没有 DB 服务可依赖，
  所以先用标准库 `sqlite3` 做**同一套仓库接口**：单文件、零依赖、够单机用。
  上 Postgres 时只需要再写一个同接口的实现，job_store 不用改。

写策略（不追求 DB 级事务，追求"重启后还能查"）：
- `jobs.payload` 存整份任务快照（含 steps / result / approval），
  在状态变更时写一次 —— 每一步只有几次，开销可忽略；
- `job_events` 单独一张表按 seq 追加，事件多但不重写整份快照。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ts          REAL NOT NULL,
    step        TEXT,
    level       TEXT NOT NULL,
    message     TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

TERMINAL = ("done", "failed", "cancelled")


class JobRepo:
    """SQLite 仓库。线程安全：一条连接 + 一把锁，写量小，够用。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL：读写不互相挡住；uvicorn 单进程也用得上（测试里多线程）
            self._conn.executescript("PRAGMA journal_mode=WAL;" + _SCHEMA)
            self._conn.commit()

    # ---- 写 ----

    def save(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (id, status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    payload["id"],
                    payload["status"],
                    payload["created_at"],
                    payload["updated_at"],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def append_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO job_events
                    (job_id, seq, ts, step, level, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event["seq"],
                    event["ts"],
                    event.get("step"),
                    event["level"],
                    event["message"],
                ),
            )
            self._conn.commit()

    # ---- 读 ----

    def load_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                out.append(json.loads(row["payload"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def events_of(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, step, level, message FROM job_events "
                "WHERE job_id=? ORDER BY seq",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
