"""Course Auto-Track Configuration, Selective Background Inspection, and Scheduler.

Allows users to select specific courses to track automatically ("自动追踪").
Tracked courses are periodically inspected in the background (and on page open
when due) for new lecture recordings, courseware/materials, assignments/DDLs,
and announcements, with optional automatic note organization for newly
discovered recordings.
"""

from __future__ import annotations

from contextvars import copy_context
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

AUTO_TRACK_FILENAME = ".auto-track-config.json"
DEFAULT_INTERVAL_MINUTES = 120
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440
MAX_RECENT_EVENTS = 20

_CONFIG_LOCK = threading.RLock()


def auto_track_config_path(root: Path) -> Path:
    return Path(root) / AUTO_TRACK_FILENAME


def _normalize_course_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        cid = str(item or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _normalize_interval(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, val))


def _normalize_strategy(raw: Any) -> str:
    val = str(raw or "").strip().lower()
    return "transcript" if val == "transcript" else "topics"


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "tracked_course_ids": [],
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "auto_organize_new_recordings": True,
        "notes_strategy": "topics",
        "last_checked_at": "",
        "last_check_started_at": "",
        "last_check_status": "idle",
        "last_check_summary": "",
        "last_error": "",
        "recent_events": [],
    }


def _sanitize_config(data: dict[str, Any] | None) -> dict[str, Any]:
    base = _default_config()
    if not isinstance(data, dict):
        return base
    tracked_ids = _normalize_course_ids(data.get("tracked_course_ids"))
    enabled = bool(data.get("enabled", bool(tracked_ids))) and bool(tracked_ids)
    interval = _normalize_interval(data.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
    auto_organize = bool(data.get("auto_organize_new_recordings", True))
    strategy = _normalize_strategy(data.get("notes_strategy", "topics"))
    last_checked_at = str(data.get("last_checked_at") or "").strip()
    last_started_at = str(data.get("last_check_started_at") or "").strip()
    last_status = str(data.get("last_check_status") or "idle").strip()
    if last_status not in {"idle", "running", "done", "partial", "failed"}:
        last_status = "idle"
    last_summary = str(data.get("last_check_summary") or "").strip()
    last_error = str(data.get("last_error") or "").strip()
    raw_events = data.get("recent_events")
    events: list[dict[str, Any]] = []
    if isinstance(raw_events, list):
        for ev in raw_events[:MAX_RECENT_EVENTS]:
            if isinstance(ev, dict):
                events.append(dict(ev))
    return {
        "enabled": enabled,
        "tracked_course_ids": tracked_ids,
        "interval_minutes": interval,
        "auto_organize_new_recordings": auto_organize,
        "notes_strategy": strategy,
        "last_checked_at": last_checked_at,
        "last_check_started_at": last_started_at,
        "last_check_status": last_status,
        "last_check_summary": last_summary,
        "last_error": last_error,
        "recent_events": events,
    }


def load_auto_track_config(root: Path) -> dict[str, Any]:
    path = auto_track_config_path(root)
    with _CONFIG_LOCK:
        if not path.is_file():
            return _default_config()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return _default_config()
        return _sanitize_config(raw)


def save_auto_track_config(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    root = Path(root)
    clean = _sanitize_config(config)
    path = auto_track_config_path(root)
    with _CONFIG_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    return clean


def update_auto_track_config(root: Path, patch: dict[str, Any]) -> dict[str, Any]:
    with _CONFIG_LOCK:
        current = load_auto_track_config(root)
        if "tracked_course_ids" in patch:
            current["tracked_course_ids"] = _normalize_course_ids(patch["tracked_course_ids"])
            if "enabled" not in patch:
                current["enabled"] = bool(current["tracked_course_ids"])
        if "enabled" in patch:
            current["enabled"] = bool(patch["enabled"]) and bool(current["tracked_course_ids"])
        if "interval_minutes" in patch and patch["interval_minutes"] is not None:
            current["interval_minutes"] = _normalize_interval(patch["interval_minutes"])
        if "auto_organize_new_recordings" in patch and patch["auto_organize_new_recordings"] is not None:
            current["auto_organize_new_recordings"] = bool(patch["auto_organize_new_recordings"])
        if "notes_strategy" in patch and patch["notes_strategy"] is not None:
            current["notes_strategy"] = _normalize_strategy(patch["notes_strategy"])
        return save_auto_track_config(root, current)


def toggle_course_auto_track(
    root: Path,
    course_id: str,
    tracked: bool | None = None,
) -> dict[str, Any]:
    cid = str(course_id or "").strip()
    if not cid:
        raise ValueError("课程 ID 不能为空。")
    with _CONFIG_LOCK:
        current = load_auto_track_config(root)
        ids = list(current["tracked_course_ids"])
        already = cid in ids
        target_state = (not already) if tracked is None else bool(tracked)
        if target_state and not already:
            ids.append(cid)
        elif not target_state and already:
            ids = [item for item in ids if item != cid]
        current["tracked_course_ids"] = ids
        current["enabled"] = bool(ids)
        saved = save_auto_track_config(root, current)
        return {**saved, "course_id": cid, "tracked": target_state}


def mark_auto_track_running(root: Path, courses_count: int) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    with _CONFIG_LOCK:
        current = load_auto_track_config(root)
        current["last_check_started_at"] = now_iso
        current["last_check_status"] = "running"
        current["last_error"] = ""
        current["last_check_summary"] = f"正在巡检 {courses_count} 门追踪课程…"
        return save_auto_track_config(root, current)


def heal_stale_auto_track_running(
    root: Path,
    config: dict[str, Any] | None = None,
    *,
    bg_running: bool | None = None,
    max_running_seconds: int = 1800,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Clear a stuck `last_check_status == 'running'` persisted on disk when no background task is active."""
    current = config if isinstance(config, dict) else load_auto_track_config(root)
    if current.get("last_check_status") != "running":
        return current
    if bg_running is True:
        return current
    now_dt = now or datetime.now(timezone.utc)
    is_stale = bg_running is False
    if not is_stale:
        started_raw = str(current.get("last_check_started_at") or "").strip()
        if not started_raw:
            is_stale = True
        else:
            try:
                started_dt = datetime.fromisoformat(started_raw)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                is_stale = (now_dt - started_dt).total_seconds() >= max_running_seconds
            except ValueError:
                is_stale = True
    if not is_stale:
        return current
    with _CONFIG_LOCK:
        fresh = load_auto_track_config(root)
        if fresh.get("last_check_status") != "running":
            return fresh
        events = list(fresh.get("recent_events") or [])
        latest = events[0] if events and isinstance(events[0], dict) else None
        if latest:
            prev_status = str(latest.get("status") or "done")
            fresh["last_check_status"] = prev_status if prev_status in {"done", "partial", "failed"} else "done"
            fresh["last_check_summary"] = str(latest.get("summary") or "")
            if not fresh.get("last_checked_at") and latest.get("timestamp"):
                fresh["last_checked_at"] = str(latest["timestamp"])
        else:
            fresh["last_check_status"] = "idle"
            fresh["last_check_summary"] = ""
        fresh["last_check_started_at"] = ""
        return save_auto_track_config(root, fresh)


def record_auto_track_result(
    root: Path,
    *,
    status: str,
    courses_checked: int,
    added_recordings: int = 0,
    added_materials: int = 0,
    added_assignments: int = 0,
    added_announcements: int = 0,
    auto_organized_count: int = 0,
    error: str = "",
    trigger: str = "auto",
) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    parts: list[str] = []
    if added_recordings > 0:
        parts.append(f"{added_recordings} 节新回放")
    if added_materials > 0:
        parts.append(f"{added_materials} 份新课件")
    if added_assignments > 0:
        parts.append(f"{added_assignments} 项新作业")
    if added_announcements > 0:
        parts.append(f"{added_announcements} 条新通知")
    delta_str = ("发现 " + "、".join(parts)) if parts else "暂无新增内容"
    if auto_organized_count > 0:
        delta_str += f"（已自动启动 {auto_organized_count} 讲笔记整理）"

    if status == "failed":
        summary = error or "自动巡检未能完成，将在下次周期重试"
    elif status == "partial":
        summary = f"已巡检 {courses_checked} 门追踪课程（部分课程需重试）· {delta_str}"
    else:
        summary = f"已巡检 {courses_checked} 门追踪课程 · {delta_str}"

    event = {
        "timestamp": now_iso,
        "trigger": trigger,
        "status": status,
        "courses_checked": courses_checked,
        "added_recordings": added_recordings,
        "added_materials": added_materials,
        "added_assignments": added_assignments,
        "added_announcements": added_announcements,
        "auto_organized_count": auto_organized_count,
        "summary": summary,
    }
    with _CONFIG_LOCK:
        current = load_auto_track_config(root)
        current["last_checked_at"] = now_iso
        current["last_check_started_at"] = ""
        current["last_check_status"] = status if status in {"done", "partial", "failed"} else "done"
        current["last_check_summary"] = summary
        current["last_error"] = error if status == "failed" else ""
        events = list(current.get("recent_events") or [])
        events.insert(0, event)
        current["recent_events"] = events[:MAX_RECENT_EVENTS]
        return save_auto_track_config(root, current)


def is_auto_track_due(
    config: dict[str, Any],
    *,
    min_age_seconds: int | None = None,
    now: datetime | None = None,
    bg_running: bool | None = None,
) -> bool:
    tracked = config.get("tracked_course_ids") or []
    if not config.get("enabled") or not tracked:
        return False
    now_dt = now or datetime.now(timezone.utc)
    if config.get("last_check_status") == "running":
        if bg_running is True:
            return False
        if bg_running is None:
            started_raw = str(config.get("last_check_started_at") or "").strip()
            if started_raw:
                try:
                    started_dt = datetime.fromisoformat(started_raw)
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    if (now_dt - started_dt).total_seconds() < 1800:
                        return False
                except ValueError:
                    pass
    last_raw = str(config.get("last_checked_at") or "").strip()
    if not last_raw:
        return True
    try:
        last_dt = datetime.fromisoformat(last_raw)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    interval_sec = _normalize_interval(config.get("interval_minutes")) * 60
    threshold = interval_sec if min_age_seconds is None else min(interval_sec, max(60, int(min_age_seconds)))
    return (now_dt - last_dt).total_seconds() >= threshold


def build_auto_track_status(
    root: Path,
    courses: list[dict[str, Any]] | None = None,
    *,
    campus_configured: bool = True,
    bg_running: bool | None = None,
) -> dict[str, Any]:
    config = load_auto_track_config(root)
    if config.get("last_check_status") == "running":
        config = heal_stale_auto_track_running(root, config, bg_running=bg_running)
    tracked_set = set(config["tracked_course_ids"])
    course_rows: list[dict[str, Any]] = []
    if courses is not None:
        for c in courses:
            cid = str(c.get("id") or "")
            is_tracked = cid in tracked_set
            c["tracked"] = is_tracked
            if is_tracked:
                course_rows.append({
                    "id": cid,
                    "title": str(c.get("title") or cid),
                    "term": str(c.get("term") or ""),
                    "synced_at": c.get("synced_at") or "",
                    "recordings": int(c.get("recordings") or 0),
                    "last_delta": c.get("last_delta"),
                })

    next_check_at = ""
    if config["enabled"] and config["tracked_course_ids"] and config["last_checked_at"]:
        try:
            last_dt = datetime.fromisoformat(config["last_checked_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_dt = last_dt + timedelta(minutes=config["interval_minutes"])
            next_check_at = next_dt.isoformat()
        except ValueError:
            next_check_at = ""

    return {
        **config,
        "tracked_count": len(config["tracked_course_ids"]),
        "tracked_courses": course_rows,
        "next_check_at": next_check_at,
        "is_due": is_auto_track_due(config, bg_running=bg_running),
        "campus_configured": campus_configured,
    }


class AutoTrackScheduler:
    """Background daemon that periodically inspects workspaces with active course tracking."""

    def __init__(self, app: Any, *, poll_seconds: float = 60.0) -> None:
        self.app = app
        self.poll_seconds = max(10.0, float(poll_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="pku-auto-track-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self.tick_once()
            except Exception:
                logger.warning("auto-track scheduler tick failed", exc_info=True)

    def _iter_candidate_settings(self) -> list[Any]:
        base_settings = getattr(self.app.state, "settings", None)
        if base_settings is None:
            return []
        try:
            root = Path(base_settings.data_dir).expanduser().resolve()
            if auto_track_config_path(root).is_file():
                return [base_settings]
        except Exception:
            pass
        return []

    def tick_once(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Inspect the local workspace and trigger auto-track checks when due."""
        manager = getattr(self.app.state, "recording_manager", None)
        if manager is None:
            return []

        triggered_results: list[dict[str, Any]] = []
        for candidate_settings in self._iter_candidate_settings():
            root = Path(candidate_settings.data_dir)
            if not getattr(candidate_settings, "pku_username", "") or not getattr(candidate_settings, "pku_password", ""):
                continue

            try:
                bg_running = manager.state().get("background_sync", {}).get("state") == "running"
                config = load_auto_track_config(root)
                if config.get("last_check_status") == "running" and not bg_running:
                    config = heal_stale_auto_track_running(root, config, bg_running=False, now=now)
                if not is_auto_track_due(config, now=now, bg_running=bg_running):
                    continue
                res = manager.trigger_auto_track_if_due(
                    trigger="scheduled",
                    now=now,
                )
                if res.get("triggered"):
                    triggered_results.append({
                        "data_dir": str(root),
                        **res,
                    })
            except Exception:
                logger.warning("scheduled auto-track check failed for %s", root, exc_info=True)

        return triggered_results
