"""Persistent SQLite-backed task queue and concurrency limiter for heavy jobs.

Bounds concurrent heavy media/LLM jobs (PyAV/ffmpeg extraction, transcription,
note generation) while queueing excess jobs with queue position feedback and
SQLite WAL persistence across restarts.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_MAX_CONCURRENT_JOBS = int(os.getenv("PKU_MAX_CONCURRENT_JOBS", "2") or "2")
DEFAULT_MAX_CONCURRENT_PER_TENANT = int(os.getenv("PKU_MAX_CONCURRENT_PER_TENANT", "1") or "1")
DEFAULT_DRAIN_MESSAGE = "当前计算节点正在维护升级中，暂不接收新整理任务，请稍候 1–2 分钟后重试。"


class DrainingError(RuntimeError):
    """Raised when the worker queue is in graceful drain mode and rejects new work."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GlobalTaskQueue:
    """Process-wide bounded worker queue with fair tenant scheduling and SQLite WAL persistence."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        max_concurrent: int | None = None,
        max_per_tenant: int | None = None,
        recover_stale: bool = True,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent or DEFAULT_MAX_CONCURRENT_JOBS))
        self.max_per_tenant = max(1, int(max_per_tenant or DEFAULT_MAX_CONCURRENT_PER_TENANT))
        self.db_path = db_path
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._active_ids: list[str] = []
        self._waiting_ids: list[str] = []
        self._active_tenants: dict[str, str] = {}
        self._waiting_entries: list[tuple[str, str]] = []
        self._live_stages: dict[str, str] = {}
        self._draining: bool = False
        self._drain_reason: str = ""
        self._db_initialized = False
        if self.db_path is not None:
            self._init_db(recover_stale=recover_stale)

    def is_draining(self) -> bool:
        with self._lock:
            return self._draining

    def drain_message(self) -> str:
        with self._lock:
            return self._drain_reason or DEFAULT_DRAIN_MESSAGE

    def set_draining(self, enabled: bool, *, reason: str = "") -> dict[str, Any]:
        with self._lock:
            self._draining = bool(enabled)
            self._drain_reason = str(reason or "").strip() if self._draining else ""
        return self.snapshot()

    def has_immediate_slot(self, tenant_id: str = "") -> bool:
        """True when a new task for `tenant_id` can start immediately without queuing."""
        with self._cond:
            if self._draining:
                return False
            if len(self._active_ids) >= self.max_concurrent or self._waiting_ids:
                return False
            if tenant_id:
                active_for_tenant = sum(
                    1 for t in self._active_tenants.values() if t == tenant_id
                )
                if active_for_tenant >= self.max_per_tenant:
                    return False
            return True

    def queued_count(self) -> int:
        with self._cond:
            return len(self._waiting_ids)

    def update_stage(self, task_id: str, stage: str) -> None:
        """Update an active task's live stage text in RAM without writing SQLite on every tick."""
        tid = str(task_id or "").strip()
        clean = str(stage or "").strip()
        if not tid or not clean:
            return
        with self._lock:
            if tid in self._active_ids:
                self._live_stages[tid] = clean

    def configure_db(self, db_path: Path, *, recover_stale: bool = True) -> None:
        with self._lock:
            if self.db_path != db_path:
                self.db_path = db_path
                self._db_initialized = False
            if not self._db_initialized:
                self._init_db(recover_stale=recover_stale)

    def _connect(self) -> sqlite3.Connection | None:
        if self.db_path is None:
            return None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except Exception:
            return None

    def _init_db(self, *, recover_stale: bool = True) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_tasks (
                        task_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        recording_id TEXT NOT NULL DEFAULT '',
                        course_id TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        stage TEXT NOT NULL DEFAULT '',
                        queue_position INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT '',
                        finished_at TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                if recover_stale:
                    # Mark any previously running/queued tasks from a crashed process as interrupted
                    conn.execute(
                        """
                        UPDATE cloud_tasks
                        SET state = 'interrupted',
                            stage = '服务重启导致任务中断，请重新发起',
                            finished_at = ?
                        WHERE state IN ('queued', 'running')
                        """,
                        (_utc_now(),),
                    )
            self._db_initialized = True
        except Exception:
            pass
        finally:
            conn.close()

    def _record_task(
        self,
        task_id: str,
        *,
        tenant_id: str,
        kind: str,
        recording_id: str = "",
        course_id: str = "",
        state: str,
        stage: str = "",
        queue_position: int = 0,
        error: str = "",
    ) -> None:
        conn = self._connect()
        if conn is None:
            return
        now = _utc_now()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT task_id, started_at FROM cloud_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO cloud_tasks (
                            task_id, tenant_id, kind, recording_id, course_id,
                            state, stage, queue_position, created_at, started_at, finished_at, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            tenant_id,
                            kind,
                            recording_id,
                            course_id,
                            state,
                            stage,
                            queue_position,
                            now,
                            now if state == "running" else "",
                            now if state in ("done", "failed", "cancelled") else "",
                            error,
                        ),
                    )
                else:
                    started_at = existing["started_at"] or (now if state == "running" else "")
                    finished_at = now if state in ("done", "failed", "cancelled") else ""
                    conn.execute(
                        """
                        UPDATE cloud_tasks
                        SET state = ?, stage = ?, queue_position = ?,
                            started_at = ?, finished_at = ?, error = ?
                        WHERE task_id = ?
                        """,
                        (state, stage, queue_position, started_at, finished_at, error, task_id),
                    )
        except Exception:
            pass
        finally:
            conn.close()

    def _next_fair_waiting_task_id(self) -> str | None:
        """Pick the next eligible waiting task without letting a bursty tenant starve others."""
        if not self._waiting_entries:
            return None
        tenant_active: dict[str, int] = {}
        for active_tenant in self._active_tenants.values():
            tenant_active[active_tenant] = tenant_active.get(active_tenant, 0) + 1
        for waiting_task_id, waiting_tenant in self._waiting_entries:
            if tenant_active.get(waiting_tenant, 0) == 0:
                return waiting_task_id
        for waiting_task_id, waiting_tenant in self._waiting_entries:
            if tenant_active.get(waiting_tenant, 0) < self.max_per_tenant:
                return waiting_task_id
        return None

    @contextmanager
    def acquire_slot(
        self,
        *,
        tenant_id: str,
        kind: str,
        recording_id: str = "",
        course_id: str = "",
        on_queued: Callable[[int, int], None] | None = None,
        on_started: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        """Acquire a heavy-worker slot using work-conserving fair tenant scheduling."""
        task_id = uuid.uuid4().hex[:12]
        with self._cond:
            if self._draining:
                raise DrainingError(self._drain_reason or DEFAULT_DRAIN_MESSAGE)
            active_for_tenant = sum(1 for t in self._active_tenants.values() if t == tenant_id)
            if (
                len(self._active_ids) < self.max_concurrent
                and not self._waiting_ids
                and active_for_tenant < self.max_per_tenant
            ):
                self._active_ids.append(task_id)
                self._active_tenants[task_id] = tenant_id
                self._record_task(
                    task_id,
                    tenant_id=tenant_id,
                    kind=kind,
                    recording_id=recording_id,
                    course_id=course_id,
                    state="running",
                    stage="正在执行",
                    queue_position=0,
                )
            else:
                self._waiting_ids.append(task_id)
                self._waiting_entries.append((task_id, tenant_id))
                while True:
                    if cancel_check is not None and cancel_check():
                        if task_id in self._waiting_ids:
                            self._waiting_ids.remove(task_id)
                        self._waiting_entries = [
                            (tid, ten) for tid, ten in self._waiting_entries if tid != task_id
                        ]
                        self._cond.notify_all()
                        self._record_task(
                            task_id,
                            tenant_id=tenant_id,
                            kind=kind,
                            recording_id=recording_id,
                            course_id=course_id,
                            state="cancelled",
                            stage="排队已取消",
                        )
                        raise ValueError("任务已取消。")
                    if (
                        len(self._active_ids) < self.max_concurrent
                        and self._waiting_ids
                        and self._next_fair_waiting_task_id() == task_id
                    ):
                        self._waiting_ids.remove(task_id)
                        self._waiting_entries = [
                            (tid, ten) for tid, ten in self._waiting_entries if tid != task_id
                        ]
                        self._active_ids.append(task_id)
                        self._active_tenants[task_id] = tenant_id
                        self._cond.notify_all()
                        break
                    pos = self._waiting_ids.index(task_id) + 1
                    active_count = len(self._active_ids)
                    self._record_task(
                        task_id,
                        tenant_id=tenant_id,
                        kind=kind,
                        recording_id=recording_id,
                        course_id=course_id,
                        state="queued",
                        stage=f"前方有 {pos} 项任务正在处理，排队等待中…",
                        queue_position=pos,
                    )
                    if on_queued is not None:
                        try:
                            on_queued(pos, active_count)
                        except Exception:
                            pass
                    self._cond.wait(timeout=0.5)

        if on_started is not None:
            try:
                on_started()
            except Exception:
                pass
        self._record_task(
            task_id,
            tenant_id=tenant_id,
            kind=kind,
            recording_id=recording_id,
            course_id=course_id,
            state="running",
            stage="正在处理",
            queue_position=0,
        )

        final_state = "done"
        final_error = ""
        try:
            yield task_id
        except Exception as exc:
            final_state = "cancelled" if "取消" in str(exc) else "failed"
            final_error = str(exc)
            raise
        finally:
            with self._cond:
                if task_id in self._active_ids:
                    self._active_ids.remove(task_id)
                self._active_tenants.pop(task_id, None)
                self._live_stages.pop(task_id, None)
                self._cond.notify_all()
            self._record_task(
                task_id,
                tenant_id=tenant_id,
                kind=kind,
                recording_id=recording_id,
                course_id=course_id,
                state=final_state,
                stage="已完成" if final_state == "done" else final_error,
                queue_position=0,
                error=final_error,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_count = len(self._active_ids)
            waiting_count = len(self._waiting_ids)
            draining = self._draining
            drain_reason = self._drain_reason
            live_stages = dict(self._live_stages)
        recent: list[dict[str, Any]] = []
        conn = self._connect()
        if conn is not None:
            try:
                rows = conn.execute(
                    """
                    SELECT task_id, tenant_id, kind, recording_id, course_id,
                           state, stage, queue_position, created_at, started_at, finished_at
                    FROM cloud_tasks
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
                recent = [dict(r) for r in rows]
                for row in recent:
                    if row.get("state") == "running" and row.get("task_id") in live_stages:
                        row["stage"] = live_stages[row["task_id"]]
            except Exception:
                pass
            finally:
                conn.close()
        return {
            "max_concurrent_jobs": self.max_concurrent,
            "max_per_tenant": self.max_per_tenant,
            "active_jobs": active_count,
            "queued_jobs": waiting_count,
            "available_slots": max(0, self.max_concurrent - active_count),
            "draining": draining,
            "drain_reason": drain_reason,
            "db_backed": self.db_path is not None,
            "recent_tasks": recent,
        }


GLOBAL_TASK_QUEUE = GlobalTaskQueue()


def get_global_task_queue(
    data_dir: Path | None = None, *, recover_stale: bool = True
) -> GlobalTaskQueue:
    if data_dir is not None and GLOBAL_TASK_QUEUE.db_path is None:
        root = Path(data_dir).expanduser().resolve()
        GLOBAL_TASK_QUEUE.configure_db(
            root / ".pku_sync_queue.db",
            recover_stale=recover_stale,
        )
    return GLOBAL_TASK_QUEUE


if __name__ == "__main__":
    import json
    from ..config import settings

    q = get_global_task_queue(Path(settings.data_dir), recover_stale=False)
    print(json.dumps(q.snapshot(), ensure_ascii=False, indent=2))
