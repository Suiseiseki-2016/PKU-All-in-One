"""Explicit, one-recording-at-a-time desktop workflow for lecture notes."""
from __future__ import annotations

import hashlib
import errno
import json
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from copy import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..envfile import write_env_values
from ..notion_login import env_file_path
from ..notion import NotionError
from ..pipeline import (
    RecordingJob,
    collect_jobs,
    download_job,
    process_job,
    recording_stage,
    stream_extract_keyframes_job,
    stream_process_job,
)
from ..store import safe_name

def _cache_hit_pacing_seconds() -> float:
    raw = os.getenv("PKU_CACHE_HIT_PACING_SECONDS", "").strip()
    if raw:
        try:
            return max(0.0, min(120.0, float(raw)))
        except ValueError:
            pass
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0.0
    return random.uniform(16.0, 26.0)


_DEFAULT_DOWNLOAD_JOB = download_job
_DEFAULT_PROCESS_JOB = process_job

logger = logging.getLogger(__name__)
_PORTAL_STATUS_LOCK = threading.RLock()
_ACTIVE_COURSE_DIRECTORIES = {}

def _split_markdown_stream_chunks(markdown: str, sections_per_chunk: int = 3) -> tuple[list[str], int]:
    """Keep markdown intact while sending the intro and first two sections early."""
    if sections_per_chunk < 1:
        raise ValueError("sections_per_chunk must be positive")
    parts = re.split(r"(?m)^(?=#{2,3} +)", markdown)
    intro, sections = parts[0], parts[1:]
    if not markdown:
        return [], 0
    chunks = [intro + "".join(sections[:2])]
    chunks.extend("".join(sections[i:i + sections_per_chunk])
                  for i in range(2, len(sections), sections_per_chunk))
    return chunks, len(sections)


def _portal_status_write(write, *args, **kwargs):
    # Only the local ledger read/replace is atomic; no remote request is locked.
    with _PORTAL_STATUS_LOCK:
        return write(*args, **kwargs)

def _format_incomplete_transcription_error(failure: str, suffix: str) -> str:
    """Join a transcription failure detail with its retry hint without double periods or duplication."""
    clean_failure = failure.rstrip("。")
    clean_suffix = suffix.rstrip("。")
    if clean_suffix and clean_suffix in clean_failure:
        return f"转写没有完成：{clean_failure}。"
    return f"转写没有完成：{clean_failure}。{suffix}"


@contextmanager
def _reserve_course_directories(root, courses):
    from ..course_storage import plan_course_directories
    owner = object()
    with _PORTAL_STATUS_LOCK:
        result = plan_course_directories(root, courses)
        keys = [str(path.resolve()).casefold() for path in result.values()]
        if any(key in _ACTIVE_COURSE_DIRECTORIES for key in keys):
            raise ValueError("课程目录正在更新；已有文件保留，请等待当前更新结束后重试。")
        for key in keys:
            _ACTIVE_COURSE_DIRECTORIES[key] = owner
    try:
        yield result
    finally:
        with _PORTAL_STATUS_LOCK:
            for key in keys:
                if _ACTIVE_COURSE_DIRECTORIES.get(key) is owner:
                    _ACTIVE_COURSE_DIRECTORIES.pop(key)


def _prune_stale_local_courses(
    root: Path, active_course_ids: set[str], *, allow_clear_all: bool = False
) -> int:
    """Remove local course directories whose course_id is not in active_course_ids.

    Only directories with a valid ``course.json`` containing a ``course_id`` not
    present in ``active_course_ids`` are removed; unknown directories without
    ``course.json`` are never touched.
    """
    from ..course_storage import _inside
    from .. import catalog_snapshots as catalog

    root = Path(root)
    if not root.exists():
        return 0
    if not active_course_ids and not allow_clear_all:
        return 0
    removed = 0
    with _PORTAL_STATUS_LOCK:
        for marker in sorted(root.glob("*/course.json")):
            if not _inside(root, marker.parent) or not _inside(root, marker):
                continue
            try:
                if marker.stat().st_size > 256_000:
                    continue
                metadata = json.loads(marker.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            identifier = metadata.get("course_id") if isinstance(metadata, dict) else None
            if not isinstance(identifier, str) or not identifier:
                continue
            if identifier in active_course_ids:
                continue
            key = str(marker.parent.resolve()).casefold()
            if key in _ACTIVE_COURSE_DIRECTORIES:
                continue
            shutil.rmtree(marker.parent, ignore_errors=True)
            try:
                catalog.cache_path(root, identifier).unlink(missing_ok=True)
            except Exception:
                pass
            removed += 1
        if removed > 0:
            status_path = root / "portal-read-status.json"
            if status_path.is_file():
                try:
                    status_data = json.loads(status_path.read_text("utf-8"))
                    if isinstance(status_data, dict) and isinstance(status_data.get("courses"), dict):
                        for stale_id in [cid for cid in status_data["courses"] if cid not in active_course_ids]:
                            status_data["courses"].pop(stale_id, None)
                        remaining = status_data["courses"]
                        if remaining and all(
                            isinstance(v, dict) and v.get("complete") is True
                            for v in remaining.values()
                        ):
                            status_data["complete"] = True
                            status_data["reason"] = "complete"
                            status_data.pop("scope", None)
                            status_data.pop("course_id", None)
                        catalog._save_read_status(status_path, status_data)
                except Exception:
                    pass
        for sys_dir_name in ("logs", "submission-drafts", "recordings-downloads", "panel"):
            sys_dir = root / sys_dir_name
            if sys_dir.is_dir() and not (sys_dir / "course.json").exists():
                for stray_name in ("course-summary.md", "summary.json"):
                    try:
                        (sys_dir / stray_name).unlink(missing_ok=True)
                    except OSError:
                        pass
        try:
            (root / "portal-catalog-diff.json").unlink(missing_ok=True)
        except OSError:
            pass
    return removed


def _extract_course_term_key(row: dict) -> str:
    term = str(row.get("term") or "").strip()
    if term:
        match = re.search(r"(\d{2}-\d{2}学年第[123]学期)", term)
        return match.group(1) if match else term
    title = str(row.get("title") or "")
    match = re.search(r"\((\d{2}-\d{2}学年第[123]学期)[^()]*\)$", title)
    return match.group(1) if match else ""


def _local_course_ids_on_disk(root: Path) -> set[str]:
    from ..course_storage import _inside

    root = Path(root)
    if not root.exists():
        return set()
    ids: set[str] = set()
    for marker in sorted(root.glob("*/course.json")):
        if not _inside(root, marker.parent) or not _inside(root, marker):
            continue
        try:
            if marker.stat().st_size > 256_000:
                continue
            metadata = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        identifier = metadata.get("course_id") if isinstance(metadata, dict) else None
        if isinstance(identifier, str) and identifier:
            ids.add(identifier)
    return ids


def _campus_vault_status(root: Path, configured: bool) -> dict:
    return {
        "campus_persist_mode": "encrypted_vault" if configured else "",
        "campus_cookie_key_missing": False,
    }


def _write_catalog_diff(
    root: Path, *, out_of_sync: bool, reason: str = "", stale_course_ids: list[str] | None = None
) -> None:
    root = Path(root)
    diff_path = root / "portal-catalog-diff.json"
    if not out_of_sync:
        try:
            diff_path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "out_of_sync": True,
        "reason": reason,
        "stale_course_ids": list(stale_course_ids or []),
    }
    tmp = diff_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(diff_path)
    finally:
        tmp.unlink(missing_ok=True)


def _catalog_sync_status(root: Path, rows: list[dict]) -> dict:
    """Detect whether local course records are out of sync with Teaching Network."""
    root = Path(root)
    if not rows:
        return {"catalog_out_of_sync": False, "stale_courses": [], "sync_mismatch_reason": ""}
    diff_path = root / "portal-catalog-diff.json"
    diff_data: dict = {}
    if diff_path.is_file():
        try:
            loaded = json.loads(diff_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                diff_data = loaded
        except (OSError, ValueError):
            diff_data = {}
    diff_out_of_sync = bool(diff_data.get("out_of_sync"))
    diff_reason = str(diff_data.get("reason") or "") if diff_out_of_sync else ""
    diff_stale_ids = (
        {str(cid) for cid in (diff_data.get("stale_course_ids") or []) if cid}
        if diff_out_of_sync
        else set()
    )

    terms_by_id = {str(r.get("id") or ""): _extract_course_term_key(r) for r in rows}
    distinct_terms = {t for t in terms_by_id.values() if t}
    older_term_ids: set[str] = set()
    if len(distinct_terms) > 1:
        newest_term = max(distinct_terms)
        older_term_ids = {cid for cid, t in terms_by_id.items() if t and t != newest_term}

    status_path = root / "portal-read-status.json"
    is_reading = False
    known_portal_ids: set[str] = set()
    if status_path.is_file():
        try:
            st = json.loads(status_path.read_text("utf-8"))
            if isinstance(st, dict):
                is_reading = st.get("reason") == "reading"
                courses_map = st.get("courses")
                if isinstance(courses_map, dict):
                    known_portal_ids = {str(k) for k in courses_map.keys() if k}
        except (OSError, ValueError):
            pass

    from ..catalog_schema import fingerprint
    catalog_dir = root / ".account-catalog"

    def _has_catalog_snapshot(course_id: str) -> bool:
        if course_id in known_portal_ids:
            return True
        if not catalog_dir.is_dir():
            return False
        fname = fingerprint({"course": course_id}) + ".json"
        for sub in catalog_dir.iterdir():
            if sub.is_dir() and (sub / fname).is_file():
                return True
        return False

    stale_courses = []
    for r in rows:
        cid = str(r.get("id") or "")
        if not cid:
            continue
        is_unconfirmed_local = (
            bool(r.get("read_only"))
            and not is_reading
            and not _has_catalog_snapshot(cid)
        )
        if cid in diff_stale_ids or cid in older_term_ids or is_unconfirmed_local:
            stale_courses.append({
                "id": cid,
                "title": str(r.get("title") or cid),
            })

    out_of_sync = bool(stale_courses) or diff_out_of_sync
    reason = diff_reason or (
        "multiple_terms" if older_term_ids else ("unconfirmed_local" if stale_courses else "")
    )
    return {
        "catalog_out_of_sync": out_of_sync,
        "stale_courses": stale_courses,
        "sync_mismatch_reason": reason,
    }

class _CourseWrites:
    def __init__(self):
        self.lock = threading.RLock()
        self.owners = {}
        self.projection = threading.Lock()

    def reserve(self, account, course_id, owner):
        with self.lock:
            for (scope, course), current in self.owners.items():
                if scope == account and (course == course_id or '*' in (course, course_id)):
                    raise ValueError("这门课已有更新或整理任务，请等当前任务完成后重试。")
            self.owners[(account, course_id)] = owner

    def release(self, account, course_id, owner):
        with self.lock:
            if self.owners.get((account, course_id)) is owner:
                self.owners.pop((account, course_id))

def _disk_space_failure(exc: BaseException) -> bool:
    """Classify typed local storage failures without exposing private messages."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and (
            exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) in (39, 112)
        ):
            return True
        if isinstance(exc, sqlite3.Error) and getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL:
            return True
        exc = exc.__cause__ or exc.__context__
    return False

class CampusCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=300)
    persist_mode: str = Field(default="", max_length=40)

class CourseRefreshRequest(BaseModel):
    course_id: str = Field(default="", max_length=120)
    scope: str = ""
    resume: bool = False

class CampusProcessRequest(BaseModel):
    direct_oss: bool = False
    regenerate: bool = False
    reuse_existing: bool = False
    notes_strategy: Literal["transcript", "topics"] | None = None
    allow_partial_quota: bool = False
    auto_refine: bool = False


class BatchOrganizeItem(BaseModel):
    course_id: str
    recording_id: str


class BatchOrganizeRequest(BaseModel):
    items: list[BatchOrganizeItem]
    notes_strategy: Literal["transcript", "topics"] = "topics"
    regenerate: bool = False
    direct_oss: bool = True
    max_parallel_courses: int = Field(default=3, ge=1, le=6)
    allow_partial_quota: bool = False
    auto_refine: bool = False


def batch_organize_preview(settings) -> dict:
    from ..ai_provider import resolve_llm_config
    from .campus_catalog import campus_courses, campus_course
    root = Path(settings.data_dir)
    _, _, resolved_model = resolve_llm_config(settings, "notes")
    model = resolved_model or getattr(settings, "notes_model", "") or "deepseek-chat"
    courses = []
    for summary in campus_courses(root)["courses"]:
        course = campus_course(root, summary["id"])
        if not course.get("lessons"):
            continue
        read_only = bool(course.get("read_only"))
        lessons = []
        for lesson in course["lessons"]:
            duration = max(0, int(lesson.get("duration_seconds") or 0))
            periods = re.search(r"第\s*(\d+)\s*[-—–~～至]\s*(\d+)\s*节", lesson["title"])
            estimate = 5400
            if periods:
                start, end = map(int, periods.groups())
                if end >= start >= 1:
                    estimate = (end - start + 1) * 45 * 60
            estimate = duration or estimate
            transcript = bool(lesson.get("transcript_available"))
            notes = bool(lesson.get("notes_available"))
            reason = lesson.get("unavailable_reason") or ""
            eligible = not read_only and not reason
            lessons.append({"id": lesson["id"], "title": lesson["title"],
                            "date": lesson.get("date", ""), "notes_available": notes,
                            "refined_available": bool(lesson.get("refined_available")),
                            "transcript_available": transcript, "unavailable_reason": reason,
                            "eligible": eligible, "needs_organize": eligible and not notes,
                            "duration_seconds": duration, "estimated_duration_seconds": estimate,
                            "duration_is_estimated": duration <= 0 and not transcript,
                            "estimated_transcribe_minutes": 0 if transcript else math.ceil(estimate / 60),
                            "estimated_points_min": 0, "estimated_points_max": 0})
        courses.append({"course_id": course["id"], "title": course["title"],
                        "term": course.get("term", ""), "read_only": read_only,
                        "read_only_message": course.get("read_only_message") or "", "lessons": lessons})
    return {"notes_model": model, "courses": courses}


class HomeRequest(BaseModel):
    parent_page_id: str
    replace_existing: bool = False

class SummaryAcceptanceRequest(BaseModel):
    notes_sha256: str = Field(min_length=64, max_length=64)
    confirmed: bool = False

class MaterialMatchRequest(BaseModel):
    lecture_id: str = ""
    ignore: bool = False
    unassign_lecture_id: str = ""
    append: bool = False

class ExistingNotionLink(BaseModel):
    url: str

class TermReviewConfirmation(BaseModel):
    segment_index: int
    transcript_sha256: str
    confirmed_spoken_term: str
    confirmed: bool = False


class LocalStorageConfigRequest(BaseModel):
    save_recordings_locally: bool | None = None
    recordings_download_dir: str | None = None


class LocalStorageOpenRequest(BaseModel):
    recording_id: str = ""
    path: str = ""
    target: str = ""

@dataclass
class Work:
    kind: str
    recording_id: str = ""
    course_id: str = ""
    state: str = "running"
    stage: str = "准备中"
    error: str = ""
    result_url: str = ""
    lecture_url: str = ""
    progress_done: int = 0
    progress_total: int = 0
    progress_remaining: int = 0
    progress_unit: str = ""
    progress_percent: int = 0
    stage_index: int = 0
    stage_total: int = 0
    current_item: str = ""
    bytes_done: int = 0
    speed_bps: int = 0
    local_mode: bool = False
    notion_synced: bool = False
    catalog_updated: int = 0
    catalog_incomplete: int = 0
    summary_created: int = 0
    summary_existing: int = 0
    result_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scope: str = "foreground"

    def as_dict(self) -> dict:
        return vars(self).copy()

def _unexpected_process_failure(stage: str) -> str:
    """Name the failed product step without exposing exception details."""
    stage = stage.strip() or "准备任务"
    if "Notion" in stage or "发布" in stage:
        return "写入 Notion 时中断；本机已有转写和笔记会保留，请检查 Notion 连接后重试。"
    if any(name in stage for name in ("教学网", "目录", "下载录像", "获取录像", "连接")):
        return "读取教学网或录像时中断；已下载的本地文件会保留，请检查网络后重试。"
    if any(name in stage for name in ("转写", "音轨", "音频")):
        return "音频处理或转写时中断；已完成的本地结果会保留，请检查平台登录与额度后重试。"
    if any(name in stage for name in ("笔记", "AI", "划分课堂知识点")):
        return "生成课堂笔记时中断；本地转写会保留，请检查平台额度后重试。"
    if any(name in stage for name in ("课堂画面", "关键帧", "补图")):
        return "提取课堂画面时中断；文字转写和笔记会保留，请重试补充课堂画面。"
    return f"任务在“{stage}”阶段中断；已生成的本地文件会保留，请稍后重试。"

def _process_stage_and_percent(unit: str, done: int, total: int,
                               current_stage: str = "转写并生成笔记") -> tuple[str, int]:
    """Map one lecture-processing sub-stage tick to a display stage plus an
    overall 0..100 lecture percent, so the whole job reads as a single run
    from 提取音轨 to 整合课堂笔记 instead of progress restarting per sub-stage."""
    ratio = min(1.0, max(0.0, done / total)) if total > 0 else 0.0

    def band(low: int, high: int) -> int:
        return int(round(low + (high - low) * ratio))

    if unit == "audio_extract" or (
        unit in ("bytes", "segments")
        and current_stage in ("提取音轨", "正在流式读取录像并提取音轨与画面")
    ):
        return "提取音轨", band(5, 20)
    if unit == "audio_parts":
        return "上传音频并转写", band(20, 65)
    if unit == "划分知识点":
        return "划分课堂知识点", band(65, 72)
    if unit in ("整理知识点", "知识点分段", "段", "章节", "笔记段"):
        return "生成课堂笔记", band(72, 95)
    if unit == "整合笔记":
        return "整合课堂笔记", band(95, 99)
    return current_stage or "转写并生成笔记", (int(round(ratio * 100)) if total > 0 else 0)

def recording_id(job: RecordingJob) -> str:
    from ..catalog_snapshots import recording_source_id
    try:
        source = recording_source_id(job.recording)
    except ValueError:
        return legacy_recording_id(job)
    identity = "\n".join((job.recording.course_id, source))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

def legacy_recording_id(job: RecordingJob) -> str:
    identity = "\n".join((job.recording.course_id, job.recording.recorded_at,
                          job.recording.title, job.course_dir.name))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _has_ambiguous_recorded_at(job: RecordingJob) -> bool:
    recorded_at = str(getattr(job.recording, "recorded_at", "") or "").strip()
    if not recorded_at:
        return False
    index_path = job.course_dir / "recordings" / "index.json"
    try:
        rows = json.loads(index_path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(rows, list):
        return False
    count = sum(
        1 for row in rows
        if isinstance(row, dict) and str(row.get("recorded_at") or "").strip() == recorded_at
    )
    return count >= 2


def _shared_cache_signature(job: RecordingJob, source_id: str, *, ambiguous: bool) -> str:
    if ambiguous:
        sub = str(getattr(job.recording, "subject_id", "") or "").strip()
        if not source_id or not sub:
            return ""
        clean_sub = re.sub(r"[^A-Za-z0-9_.-]+", "_", sub).strip("_")
        return f"{source_id}:sub:{clean_sub}" if clean_sub else ""
    return source_id or recording_id(job)

def publication_matches_job(job: RecordingJob, publication: dict) -> bool:
    from ..catalog_snapshots import recording_source_id
    try:
        source = recording_source_id(job.recording)
    except ValueError:
        source = ""
    if publication.get("course_id") and publication["course_id"] != job.recording.course_id:
        return False
    if publication.get("source_id"):
        return bool(source and publication["source_id"] == source)
    if publication.get("recording_id") in {recording_id(job), legacy_recording_id(job)}:
        return True
    # Older markers can survive a title change when their published-version
    # evidence binds this exact native session to the same note and lecture.
    try:
        version = json.loads((job.directory / "published-note-version.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(version, dict):
        return False
    note_id = str(version.get("note_id") or "").replace("-", "")
    try:
        note_path = urlsplit(str(publication.get("note_url") or "")).path.replace("-", "")
    except ValueError:
        return False
    return bool(source and version.get("source_id") == source and note_id
                and note_path.endswith(note_id) and version.get("lecture_id")
                and version["lecture_id"] == publication.get("lecture_id"))

_JOB_LOOKUP_CACHE: dict[tuple[str, str, str], tuple[float, RecordingJob]] = {}


def find_job(data_dir: Path, key: str, course_id: str = "") -> RecordingJob | None:
    cache_key = (str(data_dir), course_id, key)
    now_ts = time.monotonic()
    cached_entry = _JOB_LOOKUP_CACHE.get(cache_key)
    if cached_entry is not None:
        ts, cached_job = cached_entry
        if now_ts - ts < 10.0 and cached_job.directory.exists():
            return cached_job
    matches = [job for job in collect_jobs(data_dir, course_id)
               if key in {recording_id(job), legacy_recording_id(job)}]
    if not matches and course_id:
        matches = [job for job in collect_jobs(data_dir)
                   if key in {recording_id(job), legacy_recording_id(job)}]
    if len(matches) == 1:
        if len(_JOB_LOOKUP_CACHE) > 512:
            _JOB_LOOKUP_CACHE.clear()
        _JOB_LOOKUP_CACHE[cache_key] = (now_ts, matches[0])
        return matches[0]
    return None


def effective_recordings_download_dir(settings) -> Path:
    custom = str(getattr(settings, "recordings_download_dir", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path(settings.data_dir) / "recordings-downloads").resolve()


def exported_recording_path(settings, job: RecordingJob) -> Path:
    course_folder = safe_name(job.course_name or job.recording.course_id, "course")
    filename = safe_name(f"{job.recording.date}_{job.recording.title}", "recording") + ".mp4"
    return effective_recordings_download_dir(settings) / course_folder / filename


def _export_recording_to_download_dir(settings, job: RecordingJob) -> Path | None:
    if not job.video.is_file():
        return None
    dest = exported_recording_path(settings, job)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = job.video.resolve()
        if dest.resolve() == src:
            return dest
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            return dest
        dest.unlink(missing_ok=True)
        try:
            import os
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
        return dest
    except OSError:
        logger.warning("failed to export recording to download dir: %s", dest)
        return job.video.resolve()


def _job_transient_media_paths(job_dir: Path) -> list[Path]:
    if not job_dir.is_dir():
        return []
    targets: list[Path] = []
    for name in ("video.mp4.part", ".video.mp4.hls", "video-clips"):
        candidate = job_dir / name
        if candidate.exists():
            targets.append(candidate)
    for candidate in job_dir.glob(".video.mp4-parts-*"):
        if candidate.exists():
            targets.append(candidate)
    return targets


def _job_transient_media_bytes(job_dir: Path) -> int:
    total = 0
    for target in _job_transient_media_paths(job_dir):
        try:
            if target.is_file():
                total += target.stat().st_size
            elif target.is_dir():
                for child in target.rglob("*"):
                    if child.is_file():
                        try:
                            total += child.stat().st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def _cleanup_job_transient_media(job_dir: Path, *, remove_video: bool = True) -> None:
    if remove_video:
        try:
            (job_dir / "video.mp4").unlink(missing_ok=True)
        except OSError:
            pass
    for target in _job_transient_media_paths(job_dir):
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            pass


def _finalize_video_retention(settings, job: RecordingJob, *, local_mode: bool, notes_path: Path) -> None:
    keep_marker = job.directory / ".keep-video"
    if getattr(settings, "save_recordings_locally", False):
        _cleanup_job_transient_media(job.directory, remove_video=False)
        if job.video.is_file():
            keep_marker.parent.mkdir(parents=True, exist_ok=True)
            keep_marker.touch()
            _export_recording_to_download_dir(settings, job)
        return
    if keep_marker.exists():
        _cleanup_job_transient_media(job.directory, remove_video=False)
        if job.video.is_file():
            _export_recording_to_download_dir(settings, job)
        return
    if not getattr(settings, "delete_video_after_processing", False):
        _cleanup_job_transient_media(job.directory, remove_video=False)
        return
    if local_mode:
        _cleanup_job_transient_media(job.directory, remove_video=True)
        return
    from .note_videos import video_publication_complete
    video_publication_complete(job.directory, notes_path=notes_path)
    _cleanup_job_transient_media(job.directory, remove_video=True)


def local_storage_summary(settings) -> dict:
    root = Path(settings.data_dir)
    save_locally = bool(getattr(settings, "save_recordings_locally", False))
    configured_dir = str(getattr(settings, "recordings_download_dir", "") or "").strip()
    effective_dir = str(effective_recordings_download_dir(settings))
    items = []
    total_bytes = 0
    for job in sorted(collect_jobs(root),
                      key=lambda item: (item.recording.recorded_at, item.recording.title),
                      reverse=True):
        exported = exported_recording_path(settings, job)
        has_exported = exported.is_file()
        has_internal = job.video.is_file()
        transient_bytes = _job_transient_media_bytes(job.directory)
        if not has_exported and not has_internal and transient_bytes <= 0:
            continue
        display_path = exported if has_exported else (job.video.resolve() if has_internal else job.directory.resolve())
        primary_bytes = 0
        if has_exported or has_internal:
            try:
                primary_bytes = display_path.stat().st_size
            except OSError:
                if transient_bytes <= 0:
                    continue
        size_bytes = primary_bytes + transient_bytes
        total_bytes += size_bytes
        items.append({
            "recording_id": recording_id(job),
            "course_id": job.recording.course_id,
            "course_title": job.course_name or job.recording.course_id,
            "title": job.recording.title,
            "date": job.recording.date,
            "size_bytes": size_bytes,
            "path": str(display_path),
            "explicitly_saved": (job.directory / ".keep-video").exists() or save_locally,
        })
    return {
        "save_recordings_locally": save_locally,
        "recordings_download_dir": configured_dir,
        "effective_download_dir": effective_dir,
        "count": len(items),
        "total_bytes": total_bytes,
        "items": items,
    }


def delete_local_recording(settings, key: str) -> dict:
    root = Path(settings.data_dir)
    job = find_job(root, key)
    if job is None:
        raise ValueError("找不到这条录像。")
    exported = exported_recording_path(settings, job)
    _cleanup_job_transient_media(job.directory, remove_video=True)
    for target in (job.video, job.directory / ".keep-video", exported):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if exported.parent.is_dir() and not any(exported.parent.iterdir()):
            exported.parent.rmdir()
    except OSError:
        pass
    return local_storage_summary(settings)


def clear_all_local_recordings(settings) -> dict:
    root = Path(settings.data_dir)
    for job in collect_jobs(root):
        exported = exported_recording_path(settings, job)
        _cleanup_job_transient_media(job.directory, remove_video=True)
        for target in (job.video, job.directory / ".keep-video", exported):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            if exported.parent.is_dir() and not any(exported.parent.iterdir()):
                exported.parent.rmdir()
        except OSError:
            pass
    if root.is_dir():
        for rec_dir in root.glob("*/recordings/*"):
            if rec_dir.is_dir():
                _cleanup_job_transient_media(rec_dir, remove_video=True)
                try:
                    (rec_dir / ".keep-video").unlink(missing_ok=True)
                except OSError:
                    pass
    return local_storage_summary(settings)


def open_local_storage_path(
    settings, recording_id_key: str = "", raw_path: str = "", target_kind: str = ""
) -> dict:
    import shutil
    import subprocess

    root = Path(settings.data_dir)
    target: Path | None = None
    reveal_file = False
    if target_kind == "courses_dir":
        target = root.expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
    elif recording_id_key:
        job = find_job(root, recording_id_key)
        if job is None:
            raise ValueError("找不到这条录像。")
        exported = exported_recording_path(settings, job)
        if exported.is_file():
            target = exported
            reveal_file = True
        elif job.video.is_file():
            target = job.video.resolve()
            reveal_file = True
        else:
            raise ValueError("这条录像当前没有保存在本机。")
    elif raw_path.strip():
        candidate = Path(raw_path.strip()).expanduser().resolve()
        if candidate.is_file():
            target = candidate
            reveal_file = True
        elif candidate.is_dir():
            target = candidate
        else:
            candidate.mkdir(parents=True, exist_ok=True)
            target = candidate
    else:
        target = effective_recordings_download_dir(settings)
        target.mkdir(parents=True, exist_ok=True)

    if shutil.which("osascript") and shutil.which("open"):
        cmd = ["open", "-R", str(target)] if reveal_file else ["open", str(target)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("explorer.exe"):
        cmd = ["explorer.exe", f"/select,{target}"] if reveal_file else ["explorer.exe", str(target)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("xdg-open"):
        folder = target.parent if reveal_file else target
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "path": str(target)}


def build_local_notes_export_zip(settings) -> tuple[bytes, str]:
    """Package all synced course notes strictly as watermarked PDFs inside a ZIP archive."""
    import io
    import zipfile
    from ..course_storage import _inside
    from .pdf_export import (
        _clean_zip_entry_stem,
        build_watermarked_notes_pdf,
        collect_course_pdf_sections,
    )

    root = Path(settings.data_dir)
    markers = [
        m for m in sorted(root.glob("*/course.json"))
        if _inside(root, m.parent) and _inside(root, m)
    ]
    if not markers:
        raise ValueError("当前本机还没有已同步的课程可导出。")

    buf = io.BytesIO()
    pdf_count = 0

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for marker in markers:
            course_dir = marker.parent
            try:
                meta = json.loads(marker.read_text("utf-8"))
            except (OSError, ValueError):
                meta = {}
            course_name = (meta.get("name") if isinstance(meta, dict) else None) or course_dir.name
            folder_label = safe_name(course_dir.name, "course")
            cid = str(
                ((meta.get("course_id") or meta.get("id")) if isinstance(meta, dict) else "")
                or course_dir.name
            ).strip()
            if not cid:
                continue
            c_sections = collect_course_pdf_sections(root, course_id=cid)
            if not c_sections:
                continue
            c_pdf = build_watermarked_notes_pdf(
                c_sections, document_title=f"{course_name} · 课程笔记合集"
            )
            zf.writestr(f"{folder_label}/{folder_label}-笔记合集-PKU-All-in-One.pdf", c_pdf)
            pdf_count += 1
            used_stems: set[str] = set()
            lec_idx = 0
            for sec in c_sections:
                if sec.doc_title == "课程概览与教学网动态" and len(c_sections) > 1:
                    continue
                if sec.doc_title in ("课程总结与复习提纲", "课程总结"):
                    base_stem = "课程总结与复习提纲"
                elif sec.doc_title == "课程概览与教学网动态":
                    base_stem = "课程概览与教学网动态"
                else:
                    lec_idx += 1
                    clean_doc = _clean_zip_entry_stem(sec.doc_title, f"第{lec_idx:02d}讲课堂笔记")
                    if re.search(r"(?:第\s*\d+\s*[讲节课]|lecture[-_\s]*\d+)", clean_doc, re.IGNORECASE):
                        base_stem = clean_doc
                    else:
                        base_stem = f"第{lec_idx:02d}讲-{clean_doc}"
                stem = base_stem
                dup = 2
                while stem in used_stems:
                    stem = f"{base_stem}-{dup}"
                    dup += 1
                used_stems.add(stem)
                sec_pdf = build_watermarked_notes_pdf(
                    [sec], document_title=f"{sec.course_title} · {sec.doc_title}"
                )
                zf.writestr(f"{folder_label}/课堂笔记PDF/{stem}.pdf", sec_pdf)
                pdf_count += 1

        all_sections = collect_course_pdf_sections(root)
        if all_sections:
            all_pdf = build_watermarked_notes_pdf(
                all_sections, document_title="PKU All in One · 全部课程笔记合集"
            )
            zf.writestr("PKU-All-in-One-全部课程笔记合集.pdf", all_pdf)
            pdf_count += 1

    if pdf_count == 0:
        raise ValueError("当前还没有可导出的课程 PDF 笔记。")

    filename = f"PKU-Courses-Backup-{datetime.now().strftime('%Y%m%d')}.zip"
    return buf.getvalue(), filename


def pick_local_storage_directory(settings) -> dict:
    import shutil
    import subprocess

    default_dir = effective_recordings_download_dir(settings)
    default_dir.mkdir(parents=True, exist_ok=True)
    chosen = ""
    if shutil.which("osascript"):
        script = (
            f'POSIX path of (choose folder with prompt "选择课程录像下载目录" '
            f'default location POSIX file "{str(default_dir).replace(chr(34), "")}")'
        )
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
        if res.returncode == 0 and res.stdout.strip():
            chosen = str(Path(res.stdout.strip()).resolve())
    elif shutil.which("powershell"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$dlg.Description = '选择课程录像下载目录'; "
            f"$dlg.SelectedPath = '{str(default_dir).replace(chr(39), chr(39)*2)}'; "
            "if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.SelectedPath }"
        )
        res = subprocess.run(["powershell", "-NoProfile", "-Sta", "-Command", ps],
                             capture_output=True, text=True, timeout=120)
        if res.returncode == 0 and res.stdout.strip():
            chosen = str(Path(res.stdout.strip()).resolve())
    return {"cancelled": not bool(chosen), "path": chosen}

def _saved_keyframes(directory: Path, *, require_files: bool = False) -> list[dict]:
    """Frames already extracted for this recording, if any.

    Regenerating a note from an existing transcript should still get the
    slide-page evidence those frames carry, even when the video is long gone.
    """
    frames_dir = directory / "keyframes"
    try:
        payload = json.loads((frames_dir / "index.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("keyframes")
    if not isinstance(rows, list):
        return []
    if require_files:
        valid = [
            row for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("file"), str)
            and (frames_dir / Path(row["file"]).name).is_file()
        ]
        if valid:
            return valid
        from .note_images import _IMAGE_SUFFIXES
        if any(p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES for p in frames_dir.iterdir()):
            return rows
        return []
    return rows

def list_recordings(data_dir: Path) -> list[dict]:
    rows = []
    for job in collect_jobs(data_dir):
        stage, unavailable = recording_stage(job)
        rows.append({
            "id": recording_id(job), "course": job.course_name,
            "title": job.recording.title, "date": job.recording.date,
            "stage": stage, "unavailable_reason": unavailable,
            "video_available": job.video.exists(),
        })
    return rows

def _safe_attachment_path(url: str) -> str:
    """Keep only stable campus WebDAV paths; discard signed or external URLs."""
    if not isinstance(url, str) or "\\" in url:
        return ""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        return ""
    if not parts.path.startswith("/bbcswebdav/") or ".." in parts.path.split("/"):
        return ""
    return parts.path

def _confirmation_snapshot(course, read, deadlines):
    from .. import catalog_snapshots as catalog
    from ..materials import merge_deadlines
    if (read.recordings_error is not None or read.materials_error is not None
            or (read.notices_error is not None and read.notice_status != 'unavailable')
            or any(not isinstance(rows, list) for rows in
                   (read.recordings, read.material_items, read.tree_assignments, read.notices))):
        raise catalog.CatalogUnavailable("再次读取教学网目录不完整，未删除云端条目，请稍后重新更新。")
    material_rows = [{"title": item.title, "kind": item.kind, "path": item.path,
        "content_id": item.content_id, "body_text": item.body_text, "parent_content_id": item.parent_content_id,
        "files": [asset.filename for asset in item.attachments],
        "source_links": [link.model_dump() for link in item.source_links],
        "attachments": [{"filename": asset.filename, "path": path} for asset in item.attachments
                        if (path := _safe_attachment_path(asset.url))]} for item in read.material_items]
    assignment_rows = [{"title": item.title, "content_id": item.content_id, "source_id": item.source_id,
        "due_at": item.due_at, "source": item.source, "instructions": item.instructions,
        "files": [asset.filename for asset in item.attachments],
        "source_links": [link.model_dump() for link in item.source_links]}
        for item in merge_deadlines(read.tree_assignments, deadlines.get(course.course_id, []))]
    notice_rows = [{"id": item.announcement_id, "title": item.title, "posted_at": item.posted_at,
        "author": item.author, "body_text": item.body_text,
        "files": [asset.filename for asset in item.attachments]} for item in read.notices]
    return catalog.build_snapshot(course, read.recordings, material_rows, assignment_rows, notice_rows)

def _confirm_source_replacement(settings, client, course, observation, snapshot, stage):
    from .. import catalog_snapshots as catalog
    from ..discover import discover_courses, select_courses
    from ..materials import fetch_deadlines, fetch_announcements, walk_materials
    from ..recordings import list_recordings as fetch_recordings
    from ..course_data import read_course_metadata
    stage(f"再次完整核对教学网目录，清理云端过时条目：{course.name}")
    base = catalog.current(settings, course.course_id)
    confirmation = catalog.begin_observation(settings, course.course_id)
    if base['revision'] != observation['revision'] or confirmation['revision'] != observation['revision']:
        raise catalog.CatalogUnavailable("核对期间云端目录已变化，本次没有删除条目，请重新读取课程目录。")
    courses = select_courses(discover_courses(client), settings)
    selected = [row for row in courses if row.course_id == course.course_id]
    if len(selected) != 1:
        raise catalog.CatalogUnavailable("再次读取未能确认这门教学网课程，未删除云端条目。")
    try:
        deadlines = fetch_deadlines(client)
        reads = read_course_metadata(client, [course.course_id], recordings_fn=fetch_recordings,
                                    materials_fn=walk_materials, announcements_fn=fetch_announcements,
                                    check_cancel=lambda: stage(f"再次核对课程内容：{course.name}"))
    except Exception as exc:
        raise catalog.CatalogUnavailable("再次读取教学网目录失败，未删除云端条目，请检查连接后重新更新。") from exc
    confirmed = _confirmation_snapshot(selected[0], reads[course.course_id], deadlines)
    if confirmed != snapshot:
        raise catalog.CatalogUnavailable("两次教学网读取结果不一致，本次没有删除云端条目，请稍后重新更新。")
    stage(f"教学网目录已再次确认，更新云端目录：{course.name}")
    return {'observation_id': confirmation['observation_id'],
            'base_snapshot_sha256': catalog.fingerprint(base['snapshot']),
            'snapshot_sha256': catalog.fingerprint(confirmed)}

def _compute_course_delta(
    course_dir: Path,
    previous_meta: dict,
    recordings: list,
    material_rows: list[dict],
    assignment_rows: list[dict],
    notice_rows: list[dict],
    *,
    synced_at: str,
) -> dict:
    """Compute incremental additions and content diff against existing local index files."""
    def _read_json_list(path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text("utf-8"))
            return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _rec_key(item: dict) -> str:
        if item.get("source_id"):
            return str(item["source_id"])
        if item.get("recorded_at") or item.get("title"):
            return f"{item.get('recorded_at', '')}:{item.get('title', '')}"
        play_url = str(item.get("play_url") or "")
        if "token=" in play_url:
            play_url = play_url.split("token=", 1)[0]
        return str(play_url or item.get("download_url") or "")

    def _stable_rec(item: dict) -> dict:
        return {k: v for k, v in item.items() if k != "play_url"}

    def _mat_key(item: dict) -> str:
        return str(item.get("content_id") or f"{item.get('path', '')}:{item.get('title', '')}")

    def _asg_key(item: dict) -> str:
        return str(
            item.get("source_id")
            or item.get("content_id")
            or f"{item.get('title', '')}:{item.get('due_at', '')}"
        )

    def _notice_key(item: dict) -> str:
        return str(item.get("id") or f"{item.get('posted_at', '')}:{item.get('title', '')}")

    had_previous = bool(isinstance(previous_meta, dict) and previous_meta.get("snapshot_complete"))
    old_recs = _read_json_list(course_dir / "recordings" / "index.json")
    old_mats = _read_json_list(course_dir / "materials" / "index.json")
    old_asgs = _read_json_list(course_dir / "assignments" / "index.json")
    old_notices = _read_json_list(course_dir / "announcements" / "index.json")
    new_recs = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in recordings]

    content_changed = (
        not had_previous
        or [_stable_rec(r) for r in old_recs] != [_stable_rec(r) for r in new_recs]
        or old_mats != material_rows
        or old_asgs != assignment_rows
        or old_notices != notice_rows
    )

    if not had_previous:
        return {
            "synced_at": synced_at,
            "added_recordings": 0,
            "added_materials": 0,
            "added_assignments": 0,
            "added_announcements": 0,
            "changed": True,
            "initial": True,
        }

    added_recordings = len({_rec_key(r) for r in new_recs} - {_rec_key(r) for r in old_recs})
    added_materials = len({_mat_key(m) for m in material_rows} - {_mat_key(m) for m in old_mats})
    added_assignments = len({_asg_key(a) for a in assignment_rows} - {_asg_key(a) for a in old_asgs})
    added_announcements = len({_notice_key(n) for n in notice_rows} - {_notice_key(n) for n in old_notices})
    return {
        "synced_at": synced_at,
        "added_recordings": added_recordings,
        "added_materials": added_materials,
        "added_assignments": added_assignments,
        "added_announcements": added_announcements,
        "changed": bool(
            content_changed
            or added_recordings
            or added_materials
            or added_assignments
            or added_announcements
        ),
        "initial": False,
    }


def _preserve_recent_delta(previous_meta: dict, current_delta: dict) -> dict | None:
    """Return the delta badge to store in course.json (new delta or recent within 12h)."""
    added_total = (
        int(current_delta.get("added_recordings") or 0)
        + int(current_delta.get("added_materials") or 0)
        + int(current_delta.get("added_assignments") or 0)
        + int(current_delta.get("added_announcements") or 0)
    )
    if added_total > 0:
        return {
            k: current_delta[k]
            for k in (
                "synced_at",
                "added_recordings",
                "added_materials",
                "added_assignments",
                "added_announcements",
                "changed",
            )
            if k in current_delta
        }
    prev_delta = previous_meta.get("last_delta") if isinstance(previous_meta, dict) else None
    if not isinstance(prev_delta, dict):
        return None
    prev_added = (
        int(prev_delta.get("added_recordings") or 0)
        + int(prev_delta.get("added_materials") or 0)
        + int(prev_delta.get("added_assignments") or 0)
        + int(prev_delta.get("added_announcements") or 0)
    )
    if prev_added <= 0:
        return None
    try:
        prev_ts = datetime.fromisoformat(str(prev_delta.get("synced_at") or ""))
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - prev_ts).total_seconds() <= 43200:
            return prev_delta
    except ValueError:
        pass
    return None


def sync_recording_index(settings, stage: Callable[[str], None], progress=None,
                         course_id: str = "", on_course_confirmed=None,
                         *, incremental: bool = False) -> None:
    """Read the campus index into the local data directory.

    ``course_id`` limits the refresh to one course, so a stale snapshot can be
    renewed in seconds before a note is published instead of re-reading every
    course the student has.
    """
    from ..auth import get_session
    from ..discover import discover_courses, select_courses
    from ..recordings import list_recordings as fetch_recordings
    from ..materials import (fetch_announcements, fetch_deadlines,
                             merge_deadlines, walk_materials)
    from ..store import safe_name
    from ..models import Assignment
    from .. import catalog_snapshots as catalog

    root = Path(settings.data_dir)
    # Reserve modest metadata headroom before changing confirmation state or
    # requesting new revisions. Resident NTFS files can still be rewritten
    # on a full volume, which would otherwise invalidate usable old records.
    existing = next(path for path in (root, *root.parents) if path.exists())
    if shutil.disk_usage(existing).free < 65536:
        raise OSError(errno.ENOSPC, 'Insufficient space for course metadata')
    catalog.activate_account(settings)

    if not settings.pku_username or not settings.pku_password:
        raise ValueError("请先填写教学网账号和密码。")
    incremental = bool(incremental or getattr(settings, "_incremental_sync", False))
    shared_session = getattr(settings, "_shared_campus_session", None)
    if isinstance(shared_session, dict) and shared_session.get("client") is not None:
        client = shared_session["client"]
        owns_client = False
    else:
        stage("登录教学网")
        client = get_session(username=settings.pku_username, password=settings.pku_password)
        owns_client = True
    directory_reservation = None
    try:
        if isinstance(shared_session, dict) and isinstance(shared_session.get("courses"), list):
            courses = list(shared_session["courses"])
        else:
            courses = list(select_courses(discover_courses(client), settings))
        if course_id:
            selected = [course for course in courses if course.course_id == course_id]
            if not selected:
                raise ValueError("教学网当前学期课程中没有这门课，请重新登录后更新课程目录。")
            courses = selected
        elif not courses:
            raise ValueError("教学网没有找到当前学期课程。")
        root = Path(settings.data_dir)
        # Fence *all* official facts, including calendar deadlines and course
        # titles, before reading them. Getting a ticket only after the calendar
        # read would rebase an older deadline onto another device's new revision.
        observations = {}
        observation_issues = {}
        cached_envelopes = {}
        for course in courses:
            # The task's stage callback also checks explicit cancellation.
            # Check between external reads, before invalidating usable records.
            stage(f"核对课程目录：{course.name}")
            if incremental:
                cached_env = catalog.cached(root, course.course_id)
                if isinstance(cached_env, dict) and isinstance(cached_env.get("snapshot"), dict):
                    cached_envelopes[course.course_id] = cached_env
                    observations[course.course_id] = "deferred"
                    continue
            try:
                observations[course.course_id] = catalog.begin_observation(settings, course.course_id)
            except catalog.CatalogTransportUnavailable:
                # A disconnected account endpoint affects the whole attempt.
                # Stop before mutating confirmed state or reading each course;
                # per-course revision/protocol refusals still remain partial.
                raise
            except catalog.CatalogUnavailable as exc:
                observations[course.course_id] = None
                if isinstance(exc, catalog.CatalogProjectionBusy):
                    observation_issues[course.course_id] = "projection_busy"
        if any(obs is not None and obs != "deferred" for obs in observations.values()) and not (
            isinstance(shared_session, dict) and isinstance(shared_session.get("courses"), list)
        ):
            stage("核对教学网课程列表")
            fresh = {course.course_id: course for course in
                     select_courses(discover_courses(client), settings)}
            if any(course.course_id not in fresh for course in courses):
                raise ValueError("教学网课程列表读取不一致，保留只读旧目录。")
            courses = [fresh[course.course_id].model_copy(update={"dir_name": course.directory})
                       for course in courses]
        # Only replace confirmation state after login, discovery and account
        # observations completed without a global connection failure.
        stage("准备更新课程记录")
        if course_id:
            _portal_status_write(catalog.read_status, root, False, "reading", course_id=course_id)
        else:
            _prune_stale_local_courses(root, {course.course_id for course in courses})
            _portal_status_write(catalog.read_status, root, False, "reading")
        directory_reservation = _reserve_course_directories(root, courses)
        course_directories = directory_reservation.__enter__()
        # A first-time account has no older course card to fall back to.
        # Save discovered identity only, without asserting successful reads or
        # granting publication authority, so a failed course remains retryable.
        for course in courses:
            folder = course_directories[course.course_id]
            path = folder / "course.json"
            if not path.exists():
                folder.mkdir(parents=True, exist_ok=True)
                placeholder = {**course.model_dump(), "snapshot_complete": False,
                    **{section + "_status": "not_synced" for section in
                       ("recordings", "materials", "assignments", "announcements")}}
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(placeholder, ensure_ascii=False), "utf-8")
                temporary.replace(path)
        if isinstance(shared_session, dict) and "deadlines_by_course" in shared_session:
            deadlines_by_course = shared_session["deadlines_by_course"]
            calendar_available = bool(shared_session.get("calendar_available", True))
        else:
            try:
                deadlines_by_course = fetch_deadlines(client)
                calendar_available = True
            except Exception:
                logger.exception("campus calendar deadlines unavailable")
                deadlines_by_course = {}
                calendar_available = False
        if progress:
            progress(0, len(courses), "courses", 0)
        from ..course_data import read_course_metadata
        metadata = read_course_metadata(
            client, [course.course_id for course in courses],
            recordings_fn=fetch_recordings, materials_fn=walk_materials,
            announcements_fn=fetch_announcements,
            check_cancel=lambda: stage("读取教学网课程元数据"),
        )
        complete_attempt = True
        shared_confirmed = True
        for course_number, course in enumerate(courses, 1):
            observation = observations[course.course_id]
            account_issue = observation_issues.get(course.course_id, "")
            if observation is None:
                shared_confirmed = False
            stage(f"同步录音索引：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            course_dir = course_directories[course.course_id]
            _portal_status_write(catalog.course_read_status, root, course.course_id, False, "reading")
            read = metadata[course.course_id]
            if read.recordings_error is not None:
                complete_attempt = False
                exc = read.recordings_error
                logger.error("campus recording index unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
                _portal_status_write(catalog.course_read_status, root, course.course_id, False, "recordings_unavailable")
                stage(f"教学网录像不可用，保留只读旧目录：{course.name}")
                continue
            recordings = read.recordings
            stage(f"读取课件目录：{course.name}")
            if progress:
                progress(course_number - 1, len(courses), "courses", 0)
            if read.materials_error is not None:
                exc = read.materials_error
                logger.error("campus material index unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
                material_items, tree_assignments = None, []
            else:
                material_items, tree_assignments = read.material_items, read.tree_assignments
            notices, notice_status = read.notices, read.notice_status
            if read.notices_error is not None and notice_status != "unavailable":
                exc = read.notices_error
                logger.error("campus announcements unavailable for %s", course.course_id,
                             exc_info=(type(exc), exc, exc.__traceback__))
            course_dir = course_directories[course.course_id]
            assignment_dir = course_dir / "assignments"
            previous_assignments = []
            if material_items is None or not calendar_available:
                try:
                    previous_assignments = [
                        Assignment.model_validate({"course_id": course.course_id,
                                                   **{key: value for key, value in item.items()
                                                      if key != "files"}})
                        for item in json.loads((assignment_dir / "index.json").read_text("utf-8"))
                    ]
                except (OSError, ValueError):
                    previous_assignments = []
            if material_items is None:
                tree_assignments = [item for item in previous_assignments
                                    if item.source != "calendar"]
            deadlines = (deadlines_by_course.get(course.course_id, []) if calendar_available
                         else [item for item in previous_assignments
                               if item.source == "calendar"])
            assignments = merge_deadlines(tree_assignments, deadlines)
            index_dir = course_dir / "recordings"
            previous_meta_path = course_dir / "course.json"
            try:
                previous_meta = json.loads(previous_meta_path.read_text("utf-8"))
            except (OSError, ValueError):
                previous_meta = {}
            meta = {**course.model_dump(), "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "announcements_status": notice_status,
                    "assignments_status": ("available" if material_items is not None and calendar_available
                                           else "partial" if material_items is not None or calendar_available
                                           else "error"),
                    "existing_notion_url": previous_meta.get("existing_notion_url", "")}
            if material_items is None or not calendar_available or notices is None:
                complete_attempt = False
                _portal_status_write(catalog.course_read_status, root, course.course_id, False, "partial")
                # Preserve the last full data and its original fetched timestamp.
                # Status is diagnostic, never evidence of a successful new read.
                if previous_meta:
                    stale = {**previous_meta,
                             "announcements_status": notice_status,
                             "assignments_status": meta["assignments_status"],
                             "snapshot_complete": False}
                    temporary = previous_meta_path.with_suffix(".json.tmp")
                    temporary.write_text(json.dumps(stale, ensure_ascii=False, indent=1), "utf-8")
                    temporary.replace(previous_meta_path)
                stage(f"教学网读取不完整，保留只读旧目录：{course.name}")
                continue
            meta["snapshot_complete"] = True
            notice_rows = [{
                "id": item.announcement_id, "title": item.title,
                "posted_at": item.posted_at, "author": item.author,
                "body_text": item.body_text,
                "files": [asset.filename for asset in item.attachments],
            } for item in notices]
            assignment_rows = [{
                "title": item.title, "content_id": item.content_id,
                "source_id": item.source_id,
                "due_at": item.due_at, "source": item.source,
                "instructions": item.instructions,
                "files": [asset.filename for asset in item.attachments],
                "source_links": [link.model_dump() for link in item.source_links],
            } for item in assignments]
            material_rows = [{
                "title": item.title, "kind": item.kind, "path": item.path,
                "content_id": item.content_id, "body_text": item.body_text,
                "parent_content_id": item.parent_content_id,
                "files": [asset.filename for asset in item.attachments],
                "source_links": [link.model_dump() for link in item.source_links],
                "attachments": [{"filename": asset.filename, "path": path}
                                for asset in item.attachments
                                if (path := _safe_attachment_path(asset.url))],
            } for item in material_items]
            delta = _compute_course_delta(
                course_dir, previous_meta, recordings, material_rows,
                assignment_rows, notice_rows, synced_at=meta["synced_at"]
            )
            preserved_delta = _preserve_recent_delta(previous_meta, delta)
            if preserved_delta is not None:
                meta["last_delta"] = preserved_delta
            deltas_sink = getattr(settings, "_course_deltas", None)
            if not isinstance(deltas_sink, dict):
                deltas_sink = {}
                try:
                    setattr(settings, "_course_deltas", deltas_sink)
                except Exception:
                    pass
            deltas_sink[course.course_id] = delta
            index_dir.mkdir(parents=True, exist_ok=True)
            if notices is not None:
                notice_dir = course_dir / "announcements"
                notice_dir.mkdir(parents=True, exist_ok=True)
                temporary = notice_dir / "index.json.tmp"
                temporary.write_text(json.dumps(notice_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(notice_dir / "index.json")
            if material_items is not None or calendar_available:
                assignment_dir.mkdir(parents=True, exist_ok=True)
                temporary = assignment_dir / "index.json.tmp"
                temporary.write_text(json.dumps(assignment_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(assignment_dir / "index.json")
            if material_items is not None:
                material_dir = course_dir / "materials"
                material_dir.mkdir(parents=True, exist_ok=True)
                temporary = material_dir / "index.json.tmp"
                temporary.write_text(json.dumps(material_rows, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(material_dir / "index.json")
            course_confirmed = False
            if observation is not None:
                try:
                    snapshot = catalog.build_snapshot(course, recordings, material_rows,
                                                      assignment_rows, notice_rows)
                    cached_env = cached_envelopes.get(course.course_id)
                    if (
                        observation == "deferred"
                        and cached_env is not None
                        and bool(isinstance(previous_meta, dict) and previous_meta.get("snapshot_complete"))
                        and cached_env.get("snapshot") == snapshot
                    ):
                        envelope = cached_env
                        delta["unchanged"] = True
                        course_confirmed = True
                    else:
                        if observation == "deferred":
                            observation = catalog.begin_observation(settings, course.course_id)
                        try:
                            envelope = catalog.publish(settings, course.course_id, observation, snapshot)
                        except catalog.CatalogSourceRemovalRequired:
                            confirmation = _confirm_source_replacement(settings, client, course, observation, snapshot, stage)
                            envelope = catalog.publish(settings, course.course_id, observation, snapshot,
                                                       removal_confirmation=confirmation)
                        catalog.verify_current(settings, envelope)
                        catalog.save_cache(root, envelope)
                        course_confirmed = True
                except (ValueError, catalog.CatalogUnavailable) as exc:
                    shared_confirmed = False
                    if isinstance(exc, catalog.CatalogProjectionBusy):
                        account_issue = "projection_busy"
                    # No rebase: keep a newer account snapshot if one is readable.
                    stage(f"账号课程快照未确认；共享目录保持只读：{course.name}")
                    try:
                        catalog.save_cache(root, catalog.current(settings, course.course_id))
                    except catalog.CatalogUnavailable:
                        pass
            for destination, payload in (
                (course_dir / "course.json", meta),
                (index_dir / "index.json", [item.model_dump() for item in recordings]),
            ):
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
                temporary.replace(destination)
            _portal_status_write(catalog.course_read_status, root, course.course_id, course_confirmed,
                                       "complete" if course_confirmed else "account_unconfirmed",
                                       issue=account_issue)
            if course_confirmed and on_course_confirmed is not None:
                on_course_confirmed(course.course_id, envelope)
            if progress:
                progress(course_number, len(courses), "courses", 0)
        # Each course must have been a complete observation in this attempt.
        reason = ("complete" if complete_attempt and shared_confirmed
                  else "partial" if not complete_attempt else "account_unconfirmed")
        if course_id:
            _portal_status_write(catalog.read_status, root, complete_attempt and shared_confirmed, reason,
                                course_id=course_id)
        else:
            _portal_status_write(catalog.read_status, root, complete_attempt and shared_confirmed, reason)
    finally:
        if directory_reservation is not None:
            directory_reservation.__exit__(None, None, None)
        if owns_client:
            client.close()

def _plain_note_block(text: str, *, kind: str = "paragraph") -> dict:
    """Preserve campus wording as plain Notion text, including math syntax."""
    rich = [{"type": "text", "text": {"content": text[i:i + 1800]}}
            for i in range(0, len(text), 1800)] or [{"type": "text", "text": {"content": ""}}]
    return {"object": "block", "type": kind, kind: {"rich_text": rich}}

def formal_assignment_blocks(job: RecordingJob) -> list[dict]:
    """Official nearby course assignments, clearly separate from lecture notes."""
    from .campus_catalog import campus_course
    from .notion_home import _linked_paragraph, _source_key, _source_state

    blocks = [_plain_note_block("本课程近期正式作业", kind="heading_2")]
    try:
        course = campus_course(job.course_dir.parent, job.recording.course_id)
    except ValueError:
        blocks.append(_plain_note_block("课程作业目录尚未同步，请登录教学网核对正式要求。"))
        return blocks
    try:
        recorded = datetime.fromisoformat(job.recording.date).date()
    except ValueError:
        blocks.append(_plain_note_block("录像日期无法核对，请登录教学网查看本课程作业。"))
        return blocks
    source_state = _source_state(job.course_dir.parent)

    def append_card_link(item: dict) -> None:
        entry = source_state.get(_source_key(job.recording.course_id, "作业", item))
        page_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        if re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", page_id):
            blocks.append(_linked_paragraph(
                "在 Notion 查看正式作业卡片",
                "https://www.notion.so/" + page_id.replace("-", "")))
    attendance_pattern = r"随堂考勤|到课考勤|现场签到|课堂展示|课堂汇报|课堂点评"

    def append_formal_assignment(item: dict, due_label: str) -> None:
        blocks.append(_plain_note_block(str(item.get("title") or "未命名作业"), kind="heading_3"))
        append_card_link(item)
        blocks.append(_plain_note_block("截止：" + due_label))
        blocks.append(_plain_note_block("教学网原要求：" + str(item.get("instructions") or "原要求未能读取，请打开教学网核对。")))
        if re.search(attendance_pattern,
                     str(item.get("title") or "") + str(item.get("instructions") or "")):
            blocks.append(_plain_note_block("涉及必须到场或现场参与的要求；笔记不能代替本人参加。"))
        linked = 0
        for asset in item.get("attachments") or []:
            if not isinstance(asset, dict):
                continue
            path = _safe_attachment_path(asset.get("path"))
            if not path:
                continue
            from urllib.parse import quote
            blocks.append(_linked_paragraph(
                "登录教学网打开原题附件：" + str(asset.get("filename") or "附件"),
                "https://course.pku.edu.cn" + quote(path, safe="/%")))
            linked += 1
        if item.get("files") and not linked:
            blocks.append(_plain_note_block("原题附件暂无安全直达链接，请从教学网提交入口下载并核对原件。"))
        content_id = str(item.get("content_id") or "")
        if item.get("source") != "calendar" and re.fullmatch(r"_\d+_\d+", content_id):
            from urllib.parse import quote
            target = ("https://course.pku.edu.cn/webapps/assignment/uploadAssignment"
                      "?course_id=" + quote(job.recording.course_id, safe="")
                      + "&content_id=" + quote(content_id, safe=""))
            blocks.append(_linked_paragraph("打开教学网提交入口", target))
        else:
            blocks.append(_linked_paragraph("登录教学网查找正式提交入口", "https://course.pku.edu.cn/"))

    nearby = []
    undated_attendance = []
    undated_other = []
    for item in course.get("assignments", []):
        if not isinstance(item, dict):
            continue
        raw_due = str(item.get("due_at") or "")
        try:
            due = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
            if due.tzinfo is not None:
                due = due.astimezone(timezone(timedelta(hours=8)))
            due_date = due.date()
        except ValueError:
            if re.search(attendance_pattern,
                         str(item.get("title") or "") + str(item.get("instructions") or "")):
                undated_attendance.append(item)
            else:
                due_label = ("教学网未公布；请核对老师最新通知和作业原页。" if not raw_due else
                             "教学网截止时间无法解析；请打开作业原页核对。")
                undated_other.append((item, due_label))
            continue
        if recorded <= due_date <= recorded + timedelta(days=30):
            due_label = (due.strftime("%Y-%m-%d %H:%M 北京时间")
                         if "T" in raw_due or " " in raw_due else
                         due.strftime("%Y-%m-%d") + "（教学网未提供具体时间）")
            nearby.append((due, due_label, item))
    nearby.sort(key=lambda pair: (pair[0], str(pair[2].get("title") or "")))
    has_any_items = bool(nearby or undated_other or undated_attendance)
    if has_any_items:
        blocks.append(_plain_note_block(
            "以下来自教学网课程作业目录：有截止时间的条目限本节录像当天至其后 30 天；"
            "未公布截止时间的正式作业单列，需核对是否仍有效。"
            "不代表老师在这节课布置。题目若含图片、矩阵或表格，必须打开原件核对完整题目。"
        ))
        if not nearby:
            blocks.append(_plain_note_block("该时间范围内暂未检出有明确截止日期的正式作业；请以教学网实时目录为准。"))
    for _, due_label, item in nearby:
        append_formal_assignment(item, due_label)
    if undated_other:
        blocks.append(_plain_note_block("正式作业（截止时间待核对）", kind="heading_3"))
        for item, due_label in undated_other:
            append_formal_assignment(item, due_label)
    if undated_attendance:
        blocks.append(_plain_note_block("课程目录中的到场事项（时间待核对）", kind="heading_3"))
        for item in undated_attendance:
            blocks.append(_plain_note_block(str(item.get("title") or "未命名到场事项"), kind="heading_3"))
            append_card_link(item)
            blocks.append(_plain_note_block(
                "教学网原要求：" + str(item.get("instructions") or "原要求未能读取，请打开教学网核对。")))
            blocks.append(_plain_note_block(
                "教学网目录未提供具体举行时间；涉及必须到场或现场参与，笔记不能代替本人参加。"
                "请查看课程最新通知确认安排。"))
            content_id = str(item.get("content_id") or "")
            if re.fullmatch(r"_\d+_\d+", content_id):
                from urllib.parse import quote
                target = ("https://course.pku.edu.cn/webapps/assignment/uploadAssignment"
                          "?course_id=" + quote(job.recording.course_id, safe="")
                          + "&content_id=" + quote(content_id, safe=""))
                blocks.append(_linked_paragraph(
                    "在教学网查看原事项（现场安排以课程通知为准）", target))
    if course.get("assignments_status") in {"partial", "error", "not_synced"}:
        blocks.append(_plain_note_block("作业目录尚未完整同步，以上列表可能不全。"))
    synced_label = str(course.get("synced_at_label") or "未记录")
    if has_any_items:
        blocks.append(_plain_note_block("课程目录读取：" + synced_label + "。"))
    else:
        blocks.append(_plain_note_block(
            "课程目录读取：" + synced_label + "（本讲后 30 天内暂无截止作业，请以教学网实时目录为准）。"
        ))
    return blocks

def assignment_snapshot_issue(course: dict, *, now: datetime | None = None) -> str | None:
    """Require a recently refreshed official assignment index at publication."""
    raw = str(course.get("synced_at") or "")
    try:
        synced = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "教学网课程目录缺少可核对的读取时间；请刷新课程目录后再发布。"
    if synced.tzinfo is None:
        return "教学网课程目录读取时间缺少时区；请刷新课程目录后再发布。"
    age = (now or datetime.now(timezone.utc)) - synced
    if age < -timedelta(minutes=5):
        return "教学网课程目录读取时间晚于本机时间；请检查系统时钟并刷新课程目录。"
    if age > timedelta(hours=24):
        return "教学网正式作业目录已超过 24 小时未刷新；请到「我的课程」点击「更新课程目录」后再发布。"
    return None

def assignment_catalog_publish_issues(job: RecordingJob) -> list[str]:
    from .campus_catalog import campus_course

    try:
        course = campus_course(job.course_dir.parent, job.recording.course_id)
    except ValueError:
        return ["教学网课程及正式作业目录尚未同步；请到「我的课程」更新课程目录后再整理。"]
    if course.get("assignments_status") != "available":
        return ["教学网正式作业目录尚未完整同步；请到「我的课程」更新课程目录后再整理。"]
    issue = assignment_snapshot_issue(course)
    return [issue] if issue else []

def note_publish_issues(job: RecordingJob, note_path: Path) -> list[str]:
    """Check publication state and bound review records, never prose patterns."""
    from ..note_editor import review_record_issues
    from .transcript_review import review_pending

    issues = []
    if note_path == job.directory / "notes.md" and review_pending(job):
        issues.append("转写已校正，原笔记尚未重新整理；请重新整理后再发布。")
    try:
        transcript = json.loads((job.directory / "transcript.json").read_text("utf-8"))
        if not isinstance(transcript, dict) or not transcript.get("segments"):
            raise ValueError("empty transcript")
    except (OSError, ValueError):
        return ["本机缺少完整转写，请重新整理这节录像。"]
    issues.extend(review_record_issues(note_path, transcript))
    issues.extend(assignment_catalog_publish_issues(job))
    return issues

def _note_rich_signature(rich: list[dict]) -> tuple:
    """Normalize transport convenience fields, retaining text, links and styles."""
    spans = []
    for part in rich:
        kind = part.get("type", "text")
        if kind == "equation":
            value = (part.get("equation") or {}).get("expression") or ""
        elif kind == "mention":
            value = json.dumps(part.get("mention") or {}, ensure_ascii=False, sort_keys=True)
        else:
            value = part.get("plain_text")
            if value is None:
                value = (part.get("text") or {}).get("content") or ""
        link = ((part.get("text") or {}).get("link") or {}).get("url") or part.get("href") or ""
        annotations = {key: val for key, val in (part.get("annotations") or {}).items()
                       if val is not False and val != "default"}
        style = json.dumps(annotations, sort_keys=True)
        if kind == "text" and spans and spans[-1][0] == kind and spans[-1][2:] == (link, style):
            spans[-1] = (kind, spans[-1][1] + value, link, style)
        else:
            spans.append((kind, value, link, style))
    return tuple(spans)

def _note_source_signature(block: dict) -> tuple:
    """Compare submitted content and formatting without judging its meaning."""
    kind = block.get("type") or ""
    body = block.get(kind) or {}
    attributes = {key: body[key] for key in (
        "expression", "language", "table_width", "has_column_header", "has_row_header"
    ) if key in body}
    if "cells" in body:
        attributes["cells"] = [_note_rich_signature(cell) for cell in body["cells"]]
    return kind, _note_rich_signature(body.get("rich_text") or []), json.dumps(attributes, ensure_ascii=False, sort_keys=True)

def _note_blocks_match(saved: list[dict], submitted: list[dict], client=None, *, exact=False) -> bool:
    """Verify text and nested cells, permitting the app's top-level media inserts."""
    index = 0
    for block in saved:
        if index == len(submitted):
            return not exact  # Later student annotations and media are retained.
        expected = submitted[index]
        if _note_source_signature(block) == _note_source_signature(expected):
            kind = expected.get("type") or ""
            expected_body = expected.get(kind) or {}
            if "children" in expected_body:
                children = (block.get(kind) or {}).get("children")
                if children is None:
                    if not block.get("has_children") or not block.get("id") or client is None:
                        return False
                    children = client.list_children(block["id"])
                if not isinstance(children, list) or not _note_blocks_match(
                        children, expected_body["children"], client, exact=True):
                    return False
            index += 1
            continue
        kind = block.get("type")
        if not exact and kind in {"image", "video"}:
            caption = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                              for part in (block.get(kind) or {}).get("caption") or [])
            if caption.startswith(("课堂画面 ", "课堂录像 ")):
                continue
        return False
    return index == len(submitted)

def _recording_note_stamp(title: str, original_title: str, revision_base: str) -> str | None:
    """Return a sortable revision stamp for a note owned by this recording."""
    if title == original_title:
        return ""
    match = re.search(r" · (\d{4}-\d{2}-\d{2} \d{2}:\d{2} 北京时间) · [0-9a-f]{4}$", title)
    if match and title[:match.start()] == revision_base[:100 - len(match.group(0))]:
        return match.group(1)
    return None

def publish_notes(job: RecordingJob, lecture_id: str, notion_token: str, *,
                  note_path: Path | None = None, revision: bool = False) -> str:
    from ..notion import NotionClient, markdown_to_blocks
    from .note_images import publish_note_images
    from .note_videos import publish_note_videos

    title = f"转写笔记 · {job.recording.date} {job.recording.title}"[:100]
    revision_base = f"更新笔记 · {job.recording.date} {job.recording.title}"
    body = (note_path or job.directory / "notes.md").read_text("utf-8")
    if not body.strip():
        raise ValueError("笔记文件为空，尚不能发布到 Notion。")
    # Legacy callers may publish a hand-written note with no saved transcript.
    # The student recording flow always saves one and must pass this gate.
    if (job.directory / "transcript.json").is_file():
        issues = note_publish_issues(job, note_path or job.directory / "notes.md")
        if issues:
            raise ValueError("笔记暂不能作为完整课程笔记发布：" + "；".join(issues))
    rendered = markdown_to_blocks(body)
    source_blocks = list(formal_assignment_blocks(job))
    source_blocks.append(_plain_note_block(
        "回看提示：请结合本讲原录像核对待核事项；课件不能替代老师的实际讲述。"
    ))
    from .notion_home import LECTURE_PLACEHOLDER

    with NotionClient(notion_token) as notion:
        candidates = []
        for index, page in enumerate(notion.list_child_pages(lecture_id)):
            stamp = _recording_note_stamp(page.get("title") or "", title, revision_base)
            if stamp is not None:
                candidates.append((stamp, index, page))
        existing = max(candidates)[2] if candidates else None
        created_id = ""
        if existing and not revision:
            old_blocks = notion.list_children(existing["id"])
            expected_blocks = source_blocks + rendered
            revision = not _note_blocks_match(old_blocks, expected_blocks, notion)
        if existing and not revision:
            note_id = existing["id"]
            url = f"https://www.notion.so/{note_id.replace('-', '')}"
        else:
            if revision:
                # Keep student annotations on the earlier page intact.
                stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间")
                suffix = f" · {stamp} · {uuid.uuid4().hex[:4]}"
                title = revision_base[:100 - len(suffix)] + suffix
            page = notion.create_page(lecture_id, title, children=source_blocks + rendered, retry=False)
            note_id = page["id"]
            created_id = note_id
            url = page.get("url") or f"https://www.notion.so/{note_id.replace('-', '')}"
        try:
            publish_note_images(notion, note_id, job.directory,
                                notes_path=note_path or job.directory / "notes.md",
                                body_offset=len(source_blocks))
            publish_note_videos(notion, note_id, job.directory,
                                notes_path=note_path or job.directory / "notes.md")
            published_blocks = notion.list_children(note_id)
            expected_blocks = source_blocks + rendered
            if not _note_blocks_match(published_blocks, expected_blocks, notion):
                raise ValueError("Notion 回读内容与本次提交不一致，发布未完成；本机笔记保留。")
            for block in notion.list_children(lecture_id):
                if block.get("type") != "paragraph":
                    continue
                rich = (block.get("paragraph") or {}).get("rich_text") or []
                text = "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                               for part in rich)
                if text == LECTURE_PLACEHOLDER:
                    notion.archive_block(block["id"], retry=False)
        except Exception:
            # Only a page created by this invocation may be rolled back.
            if created_id:
                try:
                    notion.archive_page(created_id, retry=False)
                except Exception:
                    logger.exception("could not archive failed new note page")
            raise
        from .summary_acceptance import document_fingerprint
        from ..catalog_snapshots import recording_source_id
        version = {"note_id": note_id, "lecture_id": lecture_id,
                   "source_id": recording_source_id(job.recording),
                   "notes_sha256": hashlib.sha256((note_path or job.directory / "notes.md").read_bytes()).hexdigest(),
                   "remote_sha256": document_fingerprint(notion, note_id)}
        marker = job.directory / "published-note-version.json"
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(version, ensure_ascii=False), "utf-8")
        temporary.replace(marker)
        return url

class RecordingWorkManager:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._cancel_sync = threading.Event()
        self._work: Work | None = None
        self._writes = _CourseWrites()
        self._finished = threading.Event()
        self._finished.set()
        self._background = None
        self._background_cancel = threading.Event()
        self._background_child = None
        self._batch_organize: dict | None = None
        self._batch_cancel = threading.Event()
        self._batch_quota_exhausted = threading.Event()
        self._batch_children: dict[str, RecordingWorkManager] = {}
        self._owner_check = lambda: None
        self._background_recovered = False
        self._fc_spill_reason = ""
        self._holds_queue_slot = False
        if __import__("os").environ.get("PKU_DESKTOP_APP") == "1":
            self._cleanup_transient_recordings()
        self._restore_background()

    def _cleanup_transient_recordings(self) -> None:
        """Remove unmarked temporary video.mp4 and download caches left behind by interrupted runs."""
        if getattr(self.settings, "save_recordings_locally", False):
            return
        if not getattr(self.settings, "delete_video_after_processing", True):
            return
        root = Path(self.settings.data_dir)
        for job in collect_jobs(root):
            keep = (job.directory / ".keep-video").exists()
            _cleanup_job_transient_media(job.directory, remove_video=not keep)
        if root.is_dir():
            for rec_dir in root.glob("*/recordings/*"):
                if rec_dir.is_dir():
                    keep = (rec_dir / ".keep-video").exists()
                    _cleanup_job_transient_media(rec_dir, remove_video=not keep)
        import tempfile
        try:
            tmp_root = Path(tempfile.gettempdir())
            for stale_tmp in tmp_root.glob("pku-sync-audio-*"):
                if stale_tmp.is_dir() and not stale_tmp.is_symlink():
                    shutil.rmtree(stale_tmp, ignore_errors=True)
        except OSError:
            pass

    def _restore_background(self):
        # Local startup read only: progress is explicitly interrupted and never authority.
        try:
            from .course_tracker import heal_stale_auto_track_running
            heal_stale_auto_track_running(Path(self.settings.data_dir), bg_running=False)
        except Exception:
            pass
        try:
            value = json.loads(self._background_path().read_text("utf-8"))
            rows = value.get("course_results")
            if (value.get("account_binding") != self._account_binding()
                    or value.get("scope") != "background" or value.get("kind") != "sync"
                    or not isinstance(rows, list) or not rows):
                return
            identifiers = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("course_id"), str)
                        or not row["course_id"] or row["course_id"] in identifiers
                        or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 3):
                    return
                identifiers.add(row["course_id"])
                row.update(state="interrupted", metadata_state="not_read", projection_state="not_read", retryable=True)
            value.update(state="interrupted", current_course_id="", stage="上次后台更新已中断；缓存可查看，请显式恢复后重新核验课程")
            self._background = value
            self._background_binding = value["account_binding"]
            self._background_recovered = True
        except (OSError, ValueError, AttributeError, TypeError):
            return

    def state(self) -> dict:
        with self._lock:
            current = self._account_binding()
            visible_work = self._work is not None and (not hasattr(self, "_reservation") or self._reservation[0] == current)
            value = self._work.as_dict() if visible_work else {"state": "idle"}
            if self._background is not None and self._background_binding == current:
                value["background_sync"] = self._background_snapshot()
            if self._batch_organize is not None and self._batch_binding == current:
                value["batch_organize"] = self._batch_organize_snapshot()
            return value

    def _batch_organize_snapshot(self):
        from copy import deepcopy
        return deepcopy(self._batch_organize) if self._batch_organize else {"state": "idle"}

    def _batch_check(self):
        self._owner_check()
        if self._account_binding() != self._batch_binding:
            self._batch_cancel.set()
            raise ValueError("账号或连接已改变，批量整理已停止。")
        if self._batch_cancel.is_set():
            raise ValueError("批量整理已取消。")

    def start_batch_organize(self, body: BatchOrganizeRequest) -> dict:
        from ..ai_provider import is_asr_configured, is_llm_configured
        from .task_queue import DrainingError, get_global_task_queue

        with self._lock:
            if not is_llm_configured(self.settings, "notes"):
                raise ValueError("尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
            queue = get_global_task_queue(Path(self.settings.data_dir))
            if queue.is_draining():
                raise DrainingError(queue.drain_message())
            if not body.items:
                raise ValueError("请至少选择一节要整理的录像。")
            if self._batch_organize and self._batch_organize["state"] == "running":
                raise ValueError("批量整理正在进行，请查看进度或取消。")
            preview = batch_organize_preview(self.settings)
            candidates = {(course["course_id"], lesson["id"]): (course, lesson)
                          for course in preview["courses"] for lesson in course["lessons"]}
            rows = []
            needs_asr = False
            for pair in dict.fromkeys((item.course_id, item.recording_id) for item in body.items):
                if pair not in candidates:
                    raise ValueError("所选录像不属于当前课程目录，请刷新后重试。")
                course, lesson = candidates[pair]
                if not lesson["eligible"]:
                    raise ValueError(lesson["unavailable_reason"] or course["read_only_message"] or "这门课当前只读。")
                if not lesson.get("transcript_available"):
                    needs_asr = True
                rows.append({"course_id": pair[0], "course_title": course["title"],
                             "recording_id": pair[1], "title": lesson["title"], "date": lesson["date"],
                             "state": "pending", "stage": "等待中", "progress_percent": 0,
                             "error": "", "result_url": ""})
            if needs_asr and not is_asr_configured(self.settings):
                raise ValueError("部分录像尚未转写，且未配置语音转写（ASR）：请先在「模型配置」页面设置转写后端。")
            self._batch_binding = self._account_binding()
            self._batch_settings = copy(self.settings)
            self._batch_cancel.clear()
            self._batch_quota_exhausted.clear()
            self._batch_organize = {"id": uuid.uuid4().hex, "kind": "batch_organize", "scope": "batch",
                                    "state": "running", "stage": "正在并行整理所选课程录像",
                                    "max_parallel_courses": body.max_parallel_courses, "active_courses": 0,
                                    "progress_done": 0, "progress_total": len(rows),
                                    "progress_remaining": len(rows), "progress_percent": 0,
                                    "items": rows, "error": ""}
            accepted = self._batch_organize_snapshot()
        try:
            from contextvars import copy_context
            ctx = copy_context()
            threading.Thread(target=ctx.run, args=(self._run_batch_organize, body), daemon=True).start()
        except BaseException:
            with self._lock:
                for row in rows:
                    row.update(state="failed", stage="任务未启动", error="本机任务没有启动，请稍后重试。")
                self._batch_organize.update(state="failed", stage="批量整理未启动", active_courses=0)
                self._batch_progress()
            raise
        return accepted

    def cancel_batch_organize(self):
        with self._lock:
            if (not self._batch_organize or self._batch_organize["state"] != "running"
                    or self._batch_binding != self._account_binding()):
                raise ValueError("当前没有可取消的批量整理。")
            self._batch_cancel.set()
            self._batch_organize["stage"] = "正在取消，等待当前讲次安全收尾"
            for child in self._batch_children.values():
                child._cancel_sync.set()
            return self._batch_organize_snapshot()

    def _batch_progress(self):
        batch = self._batch_organize
        # Completed includes failed/cancelled items; success counts are separate in the UI.
        done = sum(row["state"] in {"done", "failed", "cancelled"} for row in batch["items"])
        total = max(1, batch["progress_total"])
        weighted = sum(
            100 if row["state"] in {"done", "failed", "cancelled"} else max(0, min(99, int(row.get("progress_percent") or 0)))
            for row in batch["items"]
        )
        batch.update(progress_done=done, progress_remaining=batch["progress_total"] - done,
                     progress_percent=min(100, int(round(weighted / total))))

    def _run_batch_course(self, rows, body):
        for idx, row in enumerate(rows):
            child = None
            try:
                self._batch_check()
                child = RecordingWorkManager(copy(self._batch_settings))
                child.platform_service = getattr(self, "platform_service", None)
                child._writes = self._writes
                child._owner_check = self._batch_check
                with self._lock:
                    self._batch_children[row["course_id"]] = child
                    row.update(state="running", stage="准备整理")
                    self._batch_organize["active_courses"] = len(self._batch_children)
                child.start_campus_process(
                    row["recording_id"],
                    row["course_id"],
                    direct_oss=body.direct_oss,
                    regenerate=body.regenerate,
                    notes_strategy=body.notes_strategy,
                    **({"auto_refine": True} if getattr(body, "auto_refine", False) else {}),
                )
                while not child._finished.wait(.25):
                    if self._batch_cancel.is_set() or self._account_binding() != self._batch_binding:
                        self._batch_cancel.set()
                        child._cancel_sync.set()
                    current = child.state()
                    with self._lock:
                        row.update(stage=current.get("stage", "正在整理"),
                                   progress_percent=current.get("progress_percent", 0))
                        self._batch_progress()
                        if not self._batch_cancel.is_set():
                            self._batch_organize["stage"] = (
                                f"正在并行整理 {len(self._batch_children)} 门课 · {row['course_title']}：{row['stage']}")
                current = child.state()
                with self._lock:
                    if current.get("state") == "done":
                        row.update(state="done", stage="已完成", progress_percent=100,
                                   result_url=current.get("result_url", ""))
                    else:
                        err = current.get("error") or "整理未完成"
                        row.update(state="cancelled" if self._batch_cancel.is_set() else "failed",
                                   stage="已取消" if self._batch_cancel.is_set() else "整理未完成",
                                   error=err)
            except Exception as exc:
                err = str(exc) if isinstance(exc, ValueError) else "整理未完成，请稍后重试。"
                with self._lock:
                    row.update(state="cancelled" if self._batch_cancel.is_set() else "failed",
                               stage="已取消" if self._batch_cancel.is_set() else "整理未完成",
                               error=err)
            finally:
                with self._lock:
                    self._batch_children.pop(row["course_id"], None)
                    self._batch_organize["active_courses"] = len(self._batch_children)
                    self._batch_progress()

    def _run_batch_organize(self, body):
        from contextvars import copy_context
        batch = self._batch_organize
        groups = {}
        for row in batch["items"]:
            groups.setdefault(row["course_id"], []).append(row)
        effective_workers = min(body.max_parallel_courses, max(1, len(groups)))
        try:
            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = [pool.submit(copy_context().run, self._run_batch_course, rows, body) for rows in groups.values()]
                for future in futures:
                    future.result()
        except Exception:
            logger.exception("batch organize worker failed")
            with self._lock:
                for row in batch["items"]:
                    if row["state"] in {"pending", "running"}:
                        row.update(state="failed", stage="整理未完成", error="本机任务中断，请稍后重试。")
        finally:
            with self._lock:
                done = sum(row["state"] == "done" for row in batch["items"])
                total = batch["progress_total"]
                status = ("cancelled" if self._batch_cancel.is_set() else "done" if done == total
                          else "partial" if done else "failed")
                stage = (f"批量整理已完成（共 {total} 讲）" if status == "done" else
                         f"已完成 {done} / {total} 讲 · {total - done} 讲未完成")
                if status == "cancelled":
                    stage = "批量整理已取消；" + stage
                batch.update(state=status, stage=stage, active_courses=0)
                self._batch_progress()

    def _background_snapshot(self):
        from copy import deepcopy
        return deepcopy(self._background) if self._background else {"state": "idle", "scope": "background", "kind": "sync"}

    def background_state(self):
        with self._lock:
            if self._background is not None and self._background_binding != self._account_binding():
                return {"state": "idle", "scope": "background", "kind": "sync"}
            return self._background_snapshot()

    def _account_binding(self):
        from ..catalog_snapshots import fingerprint
        return fingerprint({"root": str(Path(self.settings.data_dir).resolve()),
                            "session": "local",
                            "notion": getattr(self.settings, "notion_token", ""),
                            "campus": getattr(self.settings, "pku_username", "")})

    def _background_path(self):
        from ..catalog_snapshots import fingerprint
        namespace = fingerprint({"session": "local", "campus": getattr(self.settings, "pku_username", "")})
        return Path(self.settings.data_dir) / ".account-catalog" / namespace / "background-sync-state.json"

    def _save_background(self):
        with self._lock:
            value = self._background_snapshot()
            value["account_binding"] = self._background_binding
            path = self._background_state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
            temporary.replace(path)

    def _background_check(self):
        if self._background_cancel.is_set():
            raise ValueError("后台课程更新已取消。")
        if self._account_binding() != self._background_binding:
            raise ValueError("账号或连接已改变，后台更新已停止；请在当前账号重新开始。")

    def _background_course_ids(self, stage):
        from ..auth import get_session
        from ..discover import discover_courses, select_courses
        from ..materials import fetch_deadlines
        if not self.settings.pku_username or not self.settings.pku_password:
            raise ValueError("请先填写教学网账号和密码。")
        stage("读取后台更新课程列表")
        settings = getattr(self, "_background_settings", self.settings)
        client = get_session(username=settings.pku_username, password=settings.pku_password)
        keep_session = bool(getattr(self, "_keep_background_session", False))
        shared_attached = False
        try:
            courses = list(select_courses(discover_courses(client), settings))
            identifiers = [row.course_id for row in courses]
            if not identifiers or len(set(identifiers)) != len(identifiers):
                raise ValueError("教学网课程列表为空或身份重复，请重新读取。")
            _prune_stale_local_courses(Path(settings.data_dir), set(identifiers))
            if keep_session:
                try:
                    deadlines_by_course = fetch_deadlines(client)
                    calendar_available = True
                except Exception:
                    logger.exception("campus calendar deadlines unavailable")
                    deadlines_by_course = {}
                    calendar_available = False
                settings._shared_campus_session = {
                    "client": client,
                    "courses": courses,
                    "deadlines_by_course": deadlines_by_course,
                    "calendar_available": calendar_available,
                }
                shared_attached = True
            return identifiers
        finally:
            if not shared_attached:
                client.close()

    def _background_worker(self):
        base_settings = getattr(self, "_background_settings", self.settings)
        child_settings = copy(base_settings)
        child_settings._incremental_sync = getattr(base_settings, "_incremental_sync", False)
        child_settings._shared_campus_session = getattr(base_settings, "_shared_campus_session", None)
        child_settings._course_deltas = getattr(base_settings, "_course_deltas", None)
        child = RecordingWorkManager(child_settings)
        child.platform_service = getattr(self, "platform_service", None)
        child._writes = self._writes
        child._owner_check = self._background_check if self._background else lambda: None
        return child

    def _run_background_course(self, identifier):
        child = self._background_worker()
        with self._lock:
            self._background_child = child
        try:
            waiting_since = time.monotonic()
            while True:
                self._background_check()
                if time.monotonic() - waiting_since >= 30:
                    raise ValueError("这门课的前台任务仍未完成，后台已停止等待；请稍后恢复更新。")
                try:
                    child.start_sync(identifier)
                    break
                except ValueError as exc:
                    if "这门课" not in str(exc):
                        raise
                    with self._lock:
                        self._background["stage"] = "这门课正在前台使用，后台等待当前任务完成"
                    # No permit or official read starts while this course is busy.
                    self._background_cancel.wait(.1)
            while not child._finished.wait(.05):
                if self._background_cancel.is_set() or self._account_binding() != self._background_binding:
                    child._cancel_sync.set()
                with self._lock:
                    self._background["stage"] = child.state().get("stage", "更新课程")
            return child.state()
        finally:
            with self._lock:
                self._background_child = None

    def _course_unknown_write(self, identifier):
        root = Path(self.settings.data_dir)
        for path in (root / ".notion-projection-intents").glob("*/*.json"):
            try:
                value = json.loads(path.read_text("utf-8"))
                binding = value.get("binding", {})
                if binding.get("course_id") == identifier and value.get("pending") is not None:
                    return True
            except (OSError, ValueError, AttributeError):
                return True
        from ..catalog_snapshots import fingerprint, projection_journal_path
        paths = [projection_journal_path(self.settings, identifier)]
        paths.extend((root / ".projection-receipts").glob("*/" + fingerprint({"course": identifier}) + ".json"))
        for path in paths:
            if not path.exists():
                continue
            try:
                value = json.loads(path.read_text("utf-8"))
                if value.get("phase") in {"writing", "writing-unknown"}:
                    return True
            except (OSError, ValueError, AttributeError):
                return True
        return False

    def start_background_sync(
        self,
        resume: bool = False,
        *,
        course_ids: list[str] | None = None,
        auto_track: bool = False,
        auto_track_trigger: str = "auto",
        pre_sync_lessons: dict[str, set[str]] | None = None,
    ):
        with self._lock:
            if self._background and self._background["state"] == "running":
                raise ValueError("后台课程更新正在进行，请查看进度或取消。")
            binding = self._account_binding()
            previous = self._background
            recovered = resume and (previous is None or self._background_recovered)
            if recovered and previous is None:
                try:
                    previous = json.loads(self._background_path().read_text("utf-8"))
                except (OSError, ValueError):
                    previous = None
            if resume:
                if not previous or previous.get("account_binding", getattr(self, "_background_binding", None)) != binding:
                    raise ValueError("没有当前账号可恢复的后台更新，请重新开始。")
                if not isinstance(previous.get("course_results"), list):
                    raise ValueError("后台进度记录不完整，请重新开始。")
                if not previous["course_results"]:
                    if previous.get("state") in {"failed", "cancelled", "interrupted", "running"}:
                        # Failed during initial course list fetch before any course rows were populated:
                        # seamlessly restart from the beginning instead of trapping the user.
                        resume = False
                        if not auto_track and previous.get("auto_track"):
                            auto_track = True
                        prev_ctx = getattr(self, "_auto_track_context", None)
                        if not course_ids:
                            if isinstance(prev_ctx, dict) and prev_ctx.get("course_ids"):
                                course_ids = list(prev_ctx["course_ids"])
                            elif isinstance(previous.get("target_course_ids"), list) and previous["target_course_ids"]:
                                course_ids = list(previous["target_course_ids"])
                        if not pre_sync_lessons and isinstance(prev_ctx, dict) and prev_ctx.get("pre_sync_lessons"):
                            pre_sync_lessons = dict(prev_ctx["pre_sync_lessons"])
                    else:
                        raise ValueError("后台进度记录不完整，请重新开始。")
            if resume:
                identifiers = set()
                for row in previous["course_results"]:
                    if (not isinstance(row, dict) or not isinstance(row.get("course_id"), str)
                            or not row["course_id"] or row["course_id"] in identifiers
                            or type(row.get("attempts")) is not int or not 0 <= row["attempts"] <= 3
                            or row.get("state") not in {"pending", "running", "done", "failed", "cancelled", "interrupted"}):
                        raise ValueError("后台进度记录无效，请重新开始。")
                    identifiers.add(row["course_id"])
                    if recovered:
                        # Cached progress never authorizes a course or trusts a done flag.
                        # Revalidate the original complete course contract on restart.
                        row.update(state="pending", metadata_state="not_read", projection_state="not_read", attempts=0, retryable=True)
                if previous.get("state") == "running":
                    # Disk status is progress, never success or permission to replay.
                    for row in previous.get("course_results", []):
                        if row["state"] == "running":
                            row.update(state="interrupted", retryable=True)
            self._background_binding = binding
            self._background_recovered = False
            self._background_settings = copy(self.settings)
            self._background_state_path = self._background_path()
            self._background_cancel.clear()
            target_ids = [str(cid).strip() for cid in (course_ids or []) if str(cid).strip()] if not resume else []
            effective_pre_lessons = dict(pre_sync_lessons or {})
            effective_auto_track = bool(auto_track)
            if not resume and (not effective_auto_track or not target_ids or not effective_pre_lessons):
                try:
                    from .course_tracker import load_auto_track_config
                    root_dir = Path(self.settings.data_dir)
                    at_cfg = load_auto_track_config(root_dir)
                    at_ids = [str(cid).strip() for cid in (at_cfg.get("tracked_course_ids") or []) if str(cid).strip()]
                    if effective_auto_track and not target_ids and at_ids:
                        target_ids = list(at_ids)
                    if (effective_auto_track or at_cfg.get("enabled")) and at_ids:
                        for cid in at_ids:
                            if cid not in effective_pre_lessons:
                                try:
                                    effective_pre_lessons[cid] = {
                                        recording_id(job) for job in collect_jobs(root_dir, cid)
                                    }
                                except Exception:
                                    pass
                except Exception:
                    pass
            self._auto_track_context = (
                {
                    "enabled": effective_auto_track,
                    "implicit": not effective_auto_track and bool(effective_pre_lessons),
                    "course_ids": target_ids,
                    "trigger": auto_track_trigger,
                    "pre_sync_lessons": effective_pre_lessons,
                }
                if (effective_auto_track or target_ids or effective_pre_lessons)
                else None
            )
            initial_stage = "准备巡检已追踪课程" if auto_track else "准备逐课后台更新"
            self._background = {"id": uuid.uuid4().hex, "kind": "sync", "scope": "background",
                                "state": "running", "stage": initial_stage, "current_course_id": "",
                                "course_results": previous["course_results"] if resume else [],
                                "progress_done": 0, "progress_total": 0, "progress_remaining": 0,
                                "progress_percent": 0, "progress_unit": "courses", "warnings": [], "error": "",
                                **({"target_course_ids": target_ids} if target_ids else {}),
                                **({"auto": auto_track_trigger != "manual", "auto_track": True} if auto_track else {})}
            self._save_background()
        try:
            from contextvars import copy_context
            ctx = copy_context()
            threading.Thread(target=ctx.run, args=(self._run_background,), daemon=True).start()
        except BaseException:
            with self._lock:
                self._background.update(state="failed", error="后台任务没有启动，请稍后恢复。")
            self._save_background()
            raise
        return self.background_state()

    def trigger_auto_track_if_due(
        self,
        *,
        trigger: str = "auto",
        force: bool = False,
        min_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import (
            heal_stale_auto_track_running,
            is_auto_track_due,
            load_auto_track_config,
            mark_auto_track_running,
        )

        if (
            not getattr(self.settings, "pku_username", "")
            or not getattr(self.settings, "pku_password", "")
            or getattr(self.settings, "pku_password", "") == "demo-readonly"
        ):
            return {"triggered": False, "started": False, "reason": "unconfigured"}
        current = self.state()
        if (
            current.get("state") == "running"
            or current.get("background_sync", {}).get("state") == "running"
            or current.get("batch_organize", {}).get("state") == "running"
        ):
            return {"triggered": False, "started": False, "reason": "busy"}
        root = Path(self.settings.data_dir)
        config = load_auto_track_config(root)
        if config.get("last_check_status") == "running":
            config = heal_stale_auto_track_running(root, config, bg_running=False, now=now)
        tracked_ids = list(config.get("tracked_course_ids") or [])
        if not tracked_ids or (not force and not config.get("enabled")):
            return {"triggered": False, "started": False, "reason": "no_tracked_courses"}
        if not force and not is_auto_track_due(
            config, min_age_seconds=min_age_seconds, now=now, bg_running=False
        ):
            return {"triggered": False, "started": False, "reason": "fresh"}
        payload = campus_courses(root)
        courses = payload.get("courses") if isinstance(payload, dict) else list(payload or [])
        sync_status = _catalog_sync_status(root, courses)
        if sync_status.get("out_of_sync"):
            return {"triggered": False, "started": False, "reason": "out_of_sync"}

        pre_sync_lessons: dict[str, set[str]] = {}
        for cid in tracked_ids:
            try:
                pre_sync_lessons[cid] = {
                    recording_id(job) for job in collect_jobs(root, cid)
                }
            except Exception:
                pass

        mark_auto_track_running(root, len(tracked_ids))
        try:
            task_state = self.start_background_sync(
                resume=False,
                course_ids=tracked_ids,
                auto_track=True,
                auto_track_trigger=trigger,
                pre_sync_lessons=pre_sync_lessons,
            )
        except BaseException:
            heal_stale_auto_track_running(root, bg_running=False, now=now)
            raise
        with self._lock:
            if isinstance(self._background, dict):
                self._background["auto"] = trigger != "manual"
                self._background["auto_track"] = True
        return {
            "triggered": True,
            "started": True,
            "tracked_course_ids": tracked_ids,
            "task": task_state,
        }

    def _finalize_auto_track_check(self, auto_ctx: dict, rows: list[dict], deltas: dict[str, dict]) -> None:
        from .course_summary import _course_folder
        from .course_tracker import load_auto_track_config, record_auto_track_result

        base_settings = getattr(self, "_background_settings", self.settings)
        root = Path(base_settings.data_dir)
        config = load_auto_track_config(root)
        tracked_set = set(config.get("tracked_course_ids") or [])
        tracked_rows = [r for r in rows if str(r.get("course_id") or "") in tracked_set] if tracked_set else list(rows)
        if not tracked_rows:
            return
        added_rec = sum(int((r.get("delta") or {}).get("added_recordings") or 0) for r in tracked_rows)
        added_mat = sum(int((r.get("delta") or {}).get("added_materials") or 0) for r in tracked_rows)
        added_asg = sum(int((r.get("delta") or {}).get("added_assignments") or 0) for r in tracked_rows)
        added_ann = sum(int((r.get("delta") or {}).get("added_announcements") or 0) for r in tracked_rows)

        auto_organized_count = 0
        if config.get("auto_organize_new_recordings"):
            pre_lessons_map: dict[str, set[str]] = auto_ctx.get("pre_sync_lessons") or {}
            new_items: list[BatchOrganizeItem] = []
            for row in tracked_rows:
                if row.get("state") != "done":
                    continue
                cid = str(row.get("course_id") or "")
                course_delta = row.get("delta") or deltas.get(cid) or {}
                if cid not in pre_lessons_map and course_delta.get("initial"):
                    continue
                known_before = set(pre_lessons_map.get(cid) or set())
                try:
                    jobs = sorted(
                        collect_jobs(root, cid),
                        key=lambda j: (j.recording.recorded_at, j.recording.title),
                    )
                except Exception:
                    continue
                # Also check if course.json has a recent last_delta with added_recordings > 0
                recent_added_count = int(course_delta.get("added_recordings") or 0)
                if recent_added_count <= 0:
                    try:
                        cdir = _course_folder(root, cid)
                        if cdir is not None and (cdir / "course.json").is_file():
                            cmeta = json.loads((cdir / "course.json").read_text("utf-8"))
                            last_d = cmeta.get("last_delta")
                            if isinstance(last_d, dict):
                                recent_added_count = int(last_d.get("added_recordings") or 0)
                    except Exception:
                        recent_added_count = 0
                recent_tail_ids: set[str] = set()
                if recent_added_count > 0 and jobs:
                    for tail_job in jobs[-recent_added_count:]:
                        if not (tail_job.directory / "notes.md").is_file() and not (tail_job.directory / "transcript.json").is_file():
                            recent_tail_ids.add(recording_id(tail_job))
                for job in jobs:
                    lid = recording_id(job)
                    _, unavail = recording_stage(job)
                    notes_avail = (job.directory / "notes.md").is_file()
                    if (
                        lid
                        and (lid not in known_before or lid in recent_tail_ids)
                        and not notes_avail
                        and not unavail
                    ):
                        new_items.append(BatchOrganizeItem(course_id=cid, recording_id=lid))
            if new_items and self.state().get("batch_organize", {}).get("state") != "running":
                strategy = str(config.get("notes_strategy") or "topics")
                if strategy not in {"topics", "transcript"}:
                    strategy = "topics"
                try:
                    self.start_batch_organize(
                        BatchOrganizeRequest(
                            items=new_items,
                            notes_strategy=strategy,
                            direct_oss=True,
                            max_parallel_courses=min(3, max(1, len({it.course_id for it in new_items}))),
                        )
                    )
                    auto_organized_count = len(new_items)
                except Exception:
                    logger.warning("auto-track auto_organize_new_recordings failed to start", exc_info=True)

        done_count = sum(1 for r in tracked_rows if r.get("state") == "done")
        status = "done" if (tracked_rows and done_count == len(tracked_rows)) else "partial" if done_count > 0 else "failed"
        record_auto_track_result(
            root,
            status=status,
            courses_checked=done_count or len(tracked_rows),
            added_recordings=added_rec,
            added_materials=added_mat,
            added_assignments=added_asg,
            added_announcements=added_ann,
            auto_organized_count=auto_organized_count,
            trigger=str(auto_ctx.get("trigger") or "auto"),
        )

    def cancel_background_sync(self):
        with self._lock:
            if (not self._background or self._background["state"] != "running"
                    or self._background_binding != self._account_binding()):
                raise ValueError("当前没有可取消的后台更新。")
            self._background_cancel.set()
            self._background["stage"] = "正在取消，保留已完成课程并等待当前安全阶段结束"
            child = self._background_child
            if child is not None:
                child._cancel_sync.set()
            return self._background_snapshot()

    def _run_background(self):
        use_shared_incremental = (
            "_background_course_ids" not in self.__dict__
            and "_run_background_course" not in self.__dict__
        )
        deltas: dict[str, dict] = {}
        if use_shared_incremental and hasattr(self, "_background_settings"):
            self._background_settings._incremental_sync = True
            self._background_settings._course_deltas = deltas
            self._keep_background_session = True
        auto_ctx = getattr(self, "_auto_track_context", None)
        try:
            self._background_check()
            rows = self._background["course_results"]
            if not rows:
                identifiers = self._background_course_ids(lambda value: self._background_check())
                if isinstance(auto_ctx, dict) and auto_ctx.get("course_ids"):
                    target_ids = [str(cid) for cid in auto_ctx["course_ids"] if str(cid)]
                    id_set = set(identifiers)
                    filtered = [cid for cid in target_ids if cid in id_set]
                    identifiers = filtered if filtered else target_ids
                rows.extend({"course_id": cid, "state": "pending", "metadata_state": "not_read",
                             "projection_state": "not_read", "summary_created": 0, "summary_existing": 0,
                             "error": "", "failure_id": "", "attempts": 0, "retryable": True}
                            for cid in identifiers)
            with self._lock:
                self._background["progress_total"] = len(rows)
                self._background["progress_done"] = sum(row["state"] == "done" for row in rows)
                self._background["progress_remaining"] = len(rows) - self._background["progress_done"]
            self._save_background()
            for row in rows:
                self._background_check()
                if row["state"] == "done":
                    continue
                if self._course_unknown_write(row["course_id"]):
                    row.update(state="failed", retryable=False, error="这门课有尚未核验的写入，后台不会重放；请先核对原任务结果。")
                    continue
                if row["attempts"] >= 3:
                    row.update(state="failed", retryable=False, error="本次更新已尝试三次，请检查课程来源和连接后重新开始。")
                    continue
                with self._lock:
                    row.update(state="running", attempts=row["attempts"] + 1)
                    self._background["current_course_id"] = row["course_id"]
                self._save_background()
                try:
                    result = self._run_background_course(row["course_id"])
                except ValueError as exc:
                    if self._background_cancel.is_set() or self._account_binding() != self._background_binding:
                        row.update(state="interrupted", retryable=True)
                        raise
                    result = {"state": "failed", "error": str(exc)}
                except Exception as exc:
                    self._background_check()
                    failure_id = uuid.uuid4().hex[:8].upper()
                    logger.error("background course failed course=%s failure_id=%s type=%s",
                                 row["course_id"], failure_id, type(exc).__name__)
                    result = {"state": "failed", "failure_id": failure_id,
                              "error": "这门课更新失败，已继续处理其他课程；登录状态不受影响。"}
                with self._lock:
                    course_delta = deltas.get(row["course_id"])
                    row.update(state=result["state"], metadata_state="confirmed" if result.get("catalog_updated") == 1 else "unconfirmed",
                               projection_state="confirmed" if result.get("notion_synced") else "failed" if result.get("catalog_updated") == 1 and result["state"] == "failed" else "not_read",
                               summary_created=result.get("summary_created", 0), summary_existing=result.get("summary_existing", 0),
                               error=result.get("error", ""), failure_id=result.get("failure_id", ""),
                               retryable=result["state"] != "done" and row["attempts"] < 3)
                    if isinstance(course_delta, dict):
                        row["delta"] = course_delta
                        row["unchanged"] = not bool(course_delta.get("changed", True))
                    row.setdefault("attempt_history", []).append({"attempt": row["attempts"], "state": row["state"],
                                                                  "metadata_state": row["metadata_state"], "projection_state": row["projection_state"],
                                                                  "failure_id": row["failure_id"], "error": row["error"]})
                    self._background["progress_total"] = len(rows)
                    self._background["progress_done"] = sum(item["state"] == "done" for item in rows)
                    self._background["progress_remaining"] = len(rows) - self._background["progress_done"]
                    self._background["progress_percent"] = int(self._background["progress_done"] / len(rows) * 100) if rows else 100
                self._save_background()
            if use_shared_incremental and deltas and not self._notion_enabled():
                try:
                    from .course_summary import _course_folder, save_local_course_summaries
                    base_s = getattr(self, "_background_settings", self.settings)
                    root_dir = Path(base_s.data_dir)
                    refresh_summary_ids = [
                        cid for cid, d in deltas.items()
                        if isinstance(d, dict) and d.get("changed")
                        and (cdir := _course_folder(root_dir, cid)) is not None
                        and cdir.name not in {"logs", "submission-drafts", "recordings-downloads", "panel"}
                        and (cdir / "course.json").is_file()
                        and (cdir / "course-summary.md").is_file()
                    ]
                    if refresh_summary_ids:
                        save_local_course_summaries(root_dir, refresh_summary_ids)
                except Exception:
                    logger.warning("auto course summary refresh after incremental sync skipped", exc_info=True)
            if isinstance(auto_ctx, dict) and (auto_ctx.get("enabled") or auto_ctx.get("implicit")):
                try:
                    self._finalize_auto_track_check(auto_ctx, rows, deltas)
                except Exception:
                    logger.warning("finalize auto-track check failed", exc_info=True)
            with self._lock:
                self._background["state"] = "done" if all(row["state"] == "done" for row in rows) else "partial"
                added_rec = sum(int((r.get("delta") or {}).get("added_recordings") or 0) for r in rows)
                added_mat = sum(int((r.get("delta") or {}).get("added_materials") or 0) for r in rows)
                added_asg = sum(int((r.get("delta") or {}).get("added_assignments") or 0) for r in rows)
                added_ann = sum(int((r.get("delta") or {}).get("added_announcements") or 0) for r in rows)
                parts = []
                if added_rec:
                    parts.append(f"{added_rec} 节回放")
                if added_mat:
                    parts.append(f"{added_mat} 份课件")
                if added_asg:
                    parts.append(f"{added_asg} 项作业")
                if added_ann:
                    parts.append(f"{added_ann} 条通知")
                delta_text = ("新增 " + " · ".join(parts)) if parts else ""
                self._background["delta_summary"] = delta_text
                if self._background["state"] == "done":
                    self._background["stage"] = f"逐课更新已完成（{delta_text}）" if delta_text else "逐课更新已完成"
                else:
                    self._background["stage"] = "部分课程更新失败；已完成课程可用，请检查失败结果后恢复"
        except Exception as exc:
            logger.exception("background course refresh stopped")
            with self._lock:
                self._background["state"] = "cancelled" if self._background_cancel.is_set() else "failed"
                self._background["error"] = str(exc) if isinstance(exc, ValueError) else "后台更新中断，已有课程保留，请检查连接后恢复。"
            if isinstance(auto_ctx, dict) and auto_ctx.get("enabled"):
                try:
                    from .course_tracker import record_auto_track_result
                    base_settings = getattr(self, "_background_settings", self.settings)
                    record_auto_track_result(
                        Path(base_settings.data_dir),
                        status="failed",
                        courses_checked=0,
                        error=self._background.get("error") or str(exc),
                        trigger=str(auto_ctx.get("trigger") or "auto"),
                    )
                except Exception:
                    pass
        finally:
            self._keep_background_session = False
            shared_session = getattr(getattr(self, "_background_settings", None), "_shared_campus_session", None)
            if isinstance(shared_session, dict) and shared_session.get("client") is not None:
                try:
                    shared_session["client"].close()
                except Exception:
                    pass
                self._background_settings._shared_campus_session = None
            with self._lock:
                self._background["current_course_id"] = ""
            self._save_background()

    @contextmanager
    def _projection_stage(self):
        # Whole-manifest writes remain serial even when unrelated metadata reads overlap.
        while not self._writes.projection.acquire(timeout=.1):
            self._check_task_owner()
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
        try:
            self._check_task_owner()
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            yield
        finally:
            self._writes.projection.release()

    def _check_task_owner(self):
        self._owner_check()
        if hasattr(self, "_reservation") and self._account_binding() != self._reservation[0]:
            raise ValueError("账号或连接已改变，当前任务已停止；请在当前账号重新开始。")

    def _stage(self, value: str) -> None:
        self._check_task_owner()
        with self._lock:
            if self._work:
                self._work.stage = value
                self._work.progress_done = 0
                self._work.progress_total = 0
                self._work.progress_remaining = 0
                if self._work.kind == "process":
                    floor = 65 if value == "从已有转写生成笔记" else int(getattr(self._work, "progress_percent", 0) or 0)
                    self._work.progress_percent = max(int(getattr(self._work, "progress_percent", 0) or 0), floor)
                else:
                    self._work.progress_percent = 0
                self._work.progress_unit = ""
                self._work.bytes_done = 0
                self._work.speed_bps = 0
                self._progress_started = time.monotonic()
        self._sync_queue_stage()

    def _sync_queue_stage(self) -> None:
        with self._lock:
            queue = getattr(self, "_active_queue", None)
            task_id = getattr(self, "_active_queue_task_id", "")
            work = self._work
            if queue is None or not task_id or work is None or work.state != "running":
                return
            stage = str(work.stage or "").strip()
            done = int(getattr(work, "progress_done", 0) or 0)
            total = int(getattr(work, "progress_total", 0) or 0)
            unit = str(getattr(work, "progress_unit", "") or "")
            pct = int(getattr(work, "progress_percent", 0) or 0)
        if not stage:
            return
        if unit == "parts" and total > 0:
            detail = f"{stage}（{done}/{total} 段 · {pct}%）"
        elif pct > 0:
            detail = f"{stage}（{pct}%）"
        else:
            detail = stage
        try:
            queue.update_stage(task_id, detail)
        except Exception:
            pass

    def _advance(self, value: str) -> None:
        """Move to the next declared stage of a task that has a fixed plan."""
        with self._lock:
            if self._work and self._work.stage_total:
                self._work.stage_index = min(self._work.stage_index + 1,
                                             self._work.stage_total)
        self._stage(value)

    def _plan(self, stage_total: int, first_stage: str = "") -> None:
        """Declare how many coarse stages this task will pass through."""
        with self._lock:
            if self._work:
                self._work.stage_total = max(0, stage_total)
                self._work.stage_index = 1 if stage_total else 0
                if first_stage:
                    self._work.stage = first_stage

    def _progress(self, done: int, total: int, unit: str, bytes_done: int,
                  label: str = "") -> None:
        with self._lock:
            if self._work and self._work.state == "running":
                self._work.progress_done = max(0, int(done))
                self._work.progress_total = max(0, int(total))
                self._work.progress_remaining = max(0, self._work.progress_total - self._work.progress_done)
                self._work.progress_unit = unit
                if self._work.kind == "process":
                    # Lecture organization folds every sub-stage into one
                    # overall bar; progress_done/total stay the raw sub-stage
                    # counts for the detail line.
                    new_stage, overall_pct = _process_stage_and_percent(unit, done, total, self._work.stage)
                    self._work.stage = new_stage
                    self._work.progress_percent = max(int(getattr(self._work, "progress_percent", 0) or 0), overall_pct)
                else:
                    self._work.progress_percent = (min(100, int(self._work.progress_done / self._work.progress_total * 100))
                                                   if self._work.progress_total else 0)
                if label:
                    self._work.current_item = label
                self._work.bytes_done = max(0, bytes_done)
                elapsed = max(time.monotonic() - getattr(self, "_progress_started", time.monotonic()), 0.1)
                self._work.speed_bps = int(bytes_done / elapsed) if bytes_done else 0
        self._sync_queue_stage()

    def _start(self, kind: str, recording_key: str, action: Callable[[], str], *, course_id="") -> dict:
        with self._lock:
            if self._work and self._work.state == "running":
                raise ValueError("已有录音任务正在进行，请等待完成。")
            if not self._finished.is_set():
                raise ValueError("上一任务正在完成收尾，请稍后重试。")
            course_id = course_id or (recording_key if kind in {"sync", "summary"} else "*")
            account = self._account_binding()
            self._writes.reserve(account, course_id, self)
            self._reservation = (account, course_id)
            self._finished.clear()
            self._cancel_sync.clear()
            self._work = Work(kind=kind, recording_id=recording_key,
                              course_id=course_id if course_id != "*" else "")
            self._progress_started = time.monotonic()
            # The POST acknowledges this task, even if it finishes and another
            # request starts a new one before this response is returned.
            accepted = self.state()
        try:
            from contextvars import copy_context
            ctx = copy_context()
            threading.Thread(target=ctx.run, args=(self._run, action), daemon=True).start()
        except BaseException:
            with self._lock:
                self._work.state = "failed"
                self._work.error = "本机任务没有启动，请稍后重试。"
                self._writes.release(*self._reservation, self)
                self._finished.set()
            raise
        with self._lock:
            current = self.state()
            return current if current.get("id") == accepted.get("id") else accepted

    def _run(self, action: Callable[[], str]) -> None:
        try:
            self._run_action(action)
        finally:
            # Admission stays closed until the previous reservation and its
            # completion event have both been committed under the same lock.
            with self._lock:
                if hasattr(self, "_reservation"):
                    self._writes.release(*self._reservation, self)
                self._finished.set()

    @contextmanager
    def _ensure_local_queue_slot(self):
        yield

    def _run_action(self, action: Callable[[], str]) -> None:
        from ..ai_provider import AIProviderError
        from .task_queue import DrainingError

        try:
            self._check_task_owner()
            result_url = action()
            self._check_task_owner()
        except Exception as exc:
            if self._cancel_sync.is_set() and self._work and self._work.kind == "sync":
                with self._lock:
                    self._work.state = "cancelled"
                    self._work.stage = "目录更新已取消"
                    self._work.error = ""
                return
            failure_id = uuid.uuid4().hex[:8].upper()
            failed_stage = self._work.stage if self._work else "准备任务"
            failed_kind = self._work.kind if self._work else "unknown"
            logger.exception(
                "on-demand recording job failed failure_id=%s kind=%s stage=%s",
                failure_id,
                failed_kind,
                failed_stage,
            )
            if _disk_space_failure(exc):
                message = "本机磁盘空间不足，任务未完成；已有记录会保留。请清理数据所在磁盘、释放空间后重试。"
            elif isinstance(exc, PermissionError):
                message = "本机数据文件无法写入；已有记录已保留。请检查文件是否被占用以及数据目录的写入权限后重试。"
            elif isinstance(exc, (ValueError, AIProviderError, DrainingError)):
                message = str(exc)
            elif self._work and self._work.kind == "sync":
                message = ("教学网目录已读取，但同步到 Notion 失败；已建立的页面会保留，请稍后重试。"
                           if isinstance(exc, NotionError) or "Notion" in self._work.stage else
                           "教学网连接中断，课程目录未完整更新；已有记录会保留，请检查网络后重试。")
            elif self._work and self._work.kind == "catalog":
                message = "Notion 课程目录暂时无法更新；已建立的页面会保留，请稍后重试。"
            elif self._work and self._work.kind == "summary":
                message = ("课程总结没有生成；本机笔记已保留，请稍后重试。")
            else:
                message = _unexpected_process_failure(failed_stage)
            with self._lock:
                self._work.state = "failed"
                self._work.error = message
                self._work.failure_id = failure_id
        else:
            with self._lock:
                self._work.state = "done"
                if self._work.kind == "sync":
                    if self._work.catalog_incomplete:
                        self._work.state = "partial"
                        suffix = "，已验证课程已同步到 Notion" if self._work.notion_synced else ""
                        self._work.stage = (f"已更新 {self._work.catalog_updated} 门课程{suffix}；"
                                            f"{self._work.catalog_incomplete} 门读取不完整，"
                                            "旧记录保留为只读，请稍后重试")
                    else:
                        subject = "这门课" if self._work.course_id else "课程"
                        self._work.stage = (subject + "与 Notion 页面已更新" if self._work.notion_synced
                                            else subject + "目录已更新")
                    self._work.stage = self._with_summary_note(self._work.stage)
                elif self._work.kind == "catalog":
                    self._work.stage = self._with_summary_note("Notion 课程与讲次页已建立")
                elif self._work.kind == "download":
                    self._work.stage = "录像已保存到本机，可下载到电脑"
                elif self._work.kind == "summary":
                    self._work.stage = ("课程总结已生成并保存在本机" if self._work.local_mode else self._summary_done_text())
                elif self._work.kind == "refine":
                    self._work.stage = "精细整理讲义已生成"
                else:
                    self._work.stage = ("课堂笔记已生成并保存在本机" if self._work.local_mode else "已发布到 Notion")
                self._work.result_url = result_url
                if not self._work.result_url and self._work.result_urls:
                    self._work.result_url = self._work.result_urls[0]
                if self._work.progress_total:
                    self._work.progress_done = self._work.progress_total
                    self._work.progress_remaining = 0
                    self._work.progress_percent = 100

    def _with_summary_note(self, stage: str) -> str:
        if self._work.summary_created or self._work.summary_existing:
            stage += "，并已生成课程总结"
        if self._work.warnings:
            stage += f"；{len(self._work.warnings)} 门课程总结未生成"
        return stage

    def _summary_done_text(self) -> str:
        text = (f"课程总结已生成：新建 {self._work.summary_created} 页，"
                f"当日已有 {self._work.summary_existing} 页未重复写入")
        if self._work.warnings:
            text += f"；{len(self._work.warnings)} 门未生成"
        return text

    def _course_ids(self) -> list[str]:
        from .campus_catalog import campus_courses
        return [row["id"] for row in campus_courses(Path(self.settings.data_dir))["courses"]]

    def _summarize_courses(self, course_ids: list[str], *, required: bool,
                           plan: bool = True) -> dict:
        from .course_summary import publish_course_summaries, save_local_course_summaries
        if not course_ids:
            if required:
                raise ValueError("还没有同步任何课程；请先在「我的课程」更新课程目录。")
            return {"requested": 0, "created": 0, "existing": 0, "urls": [], "failures": []}
        if plan:
            self._plan(len(course_ids) + 1, f"生成课程总结：共 {len(course_ids)} 门课程")
        else:
            self._advance("生成课程总结")
        self._progress(0, len(course_ids), "courses", 0)
        local_mode = not self._notion_enabled()
        with self._lock:
            if self._work:
                self._work.local_mode = local_mode
        summarize = save_local_course_summaries if local_mode else publish_course_summaries
        args = (Path(self.settings.data_dir), course_ids) if local_mode else (Path(self.settings.data_dir), self.settings.notion_token, course_ids)
        result = summarize(
            *args,
            on_course=(lambda title: self._advance(f"生成课程总结：{title}")) if plan else None,
            on_progress=lambda done, total, title: self._progress(done, total, "courses", 0,
                                                                  label=title),
        )
        with self._lock:
            if self._work:
                self._work.summary_created += result["created"]
                self._work.summary_existing += result["existing"]
                self._work.result_urls.extend(result["urls"])
                self._work.warnings.extend(f"《{title}》：{message}"
                                           for title, message in result["failures"])
        if required and result["failures"] and not result["urls"]:
            raise ValueError("课程总结没有生成：" + result["failures"][0][1])
        return result

    def _catalog_item_progress(self, done: int, total: int, label: str) -> None:
        self._progress(done, total, "items", 0, label=label)

    def _sync_notion_catalog(self, course_id: str = "", *, pending_read_plan=None,
                            confirmed_course_ids=None) -> None:
        from .notion_home import sync_catalog
        def catalog_stage(value: str) -> None:
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            self._stage(value)
        self._plan(2, "建立 Notion 课程与讲次页")
        catalog_stage("建立 Notion 课程与讲次页")
        scope = {"course_id": course_id} if course_id else {}
        if pending_read_plan is not None and pending_read_plan.enabled:
            scope.update(pending_read_plan=pending_read_plan,
                         operation_nonce=pending_read_plan.operation_nonce)
        with self._projection_stage():
            sync_catalog(Path(self.settings.data_dir), self.settings.notion_token,
                         catalog_stage, self._progress, self._catalog_item_progress,
                         settings=self.settings, **scope)
        if self._cancel_sync.is_set():
            raise ValueError("目录更新已取消。")
        with self._lock:
            if self._work:
                self._work.notion_synced = True

    def _notion_enabled(self) -> bool:
        if __import__("os").environ.get("PKU_DESKTOP_APP") == "1":
            return False
        from .notion_home import saved_home
        return bool(getattr(self.settings, "notion_token", "")
                    and saved_home(Path(self.settings.data_dir)))

    def start_course_summaries(self, course_id: str = "") -> dict:
        from .campus_catalog import campus_course
        root = Path(self.settings.data_dir)
        if course_id:
            campus_course(root, course_id)
            targets = [course_id]
        else:
            targets = self._course_ids()
            if not targets:
                raise ValueError("还没有同步任何课程；请先在「我的课程」更新课程目录。")
        def run() -> str:
            result = self._summarize_courses(targets, required=True)
            return result["urls"][0] if result["urls"] else ""
        return self._start("summary", course_id, run)

    def cancel_sync(self) -> dict:
        with self._lock:
            if (not self._work or self._work.state != "running" or self._work.kind != "sync"
                    or (hasattr(self, "_reservation") and self._reservation[0] != self._account_binding())):
                raise ValueError("当前没有可取消的课程目录更新。")
            self._cancel_sync.set()
            self._work.stage = "正在取消，完成当前课程读取后停止"
            return self._work.as_dict()

    def start_sync(self, course_id: str = "") -> dict:
        course_id = course_id.strip()
        if not course_id:
            return self.start_background_sync()
        def cancellable_stage(value: str) -> None:
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            self._stage(value)
        def refresh(pending_read_plan) -> str:
            confirmed = []
            def on_course_confirmed(identifier, envelope):
                pending_read_plan.offer(identifier, envelope)
                confirmed.append(identifier)
            if course_id:
                sync_recording_index(self.settings, cancellable_stage, self._progress,
                                     course_id=course_id, on_course_confirmed=on_course_confirmed)
            else:
                sync_recording_index(self.settings, cancellable_stage, self._progress,
                                     on_course_confirmed=on_course_confirmed)
            if self._cancel_sync.is_set():
                raise ValueError("目录更新已取消。")
            status_path = Path(self.settings.data_dir) / "portal-read-status.json"
            if course_id:
                from .. import catalog_snapshots as catalog
                # An unrelated confirmed course cannot make a failed selected
                # refresh succeed, even if the old whole-portal flag was true.
                if not status_path.exists():
                    raise ValueError("这门课的教学网目录尚未确认；已有记录保留，请重新更新。")
                selected_status = json.loads(status_path.read_text("utf-8")).get("courses", {}).get(course_id, {})
                if selected_status.get("complete") is not True:
                    if selected_status.get("issue") == "projection_busy":
                        raise catalog.CatalogProjectionBusy()
                    raise ValueError("这门课的教学网目录尚未完整确认；已有记录保留，请稍后重试。")
                catalog.assert_portal_readable(Path(self.settings.data_dir), course_id)
            if status_path.exists():
                status = json.loads(status_path.read_text("utf-8"))
                courses = status.get("courses", {})
                if course_id:
                    courses = {course_id: courses.get(course_id, {})}
                updated = sum(row.get("complete") is True for row in courses.values())
                incomplete = len(courses) - updated
                if status.get("complete") is not True and not updated:
                    if any(row.get("issue") == "projection_busy" for row in courses.values()):
                        from ..catalog_snapshots import CatalogProjectionBusy
                        raise CatalogProjectionBusy()
                    raise ValueError("教学网课程目录未能完整读取；旧记录保留为只读，请稍后重试。")
                with self._lock:
                    self._work.catalog_updated = updated
                    self._work.catalog_incomplete = incomplete
            if self._notion_enabled():
                if course_id:
                    self._sync_notion_catalog(course_id, pending_read_plan=pending_read_plan,
                                             confirmed_course_ids=confirmed)
                else:
                    self._sync_notion_catalog(pending_read_plan=pending_read_plan,
                                             confirmed_course_ids=confirmed)
            return ""
        def run() -> str:
            from .notion_workspace import PendingWorkspaceReadPlan
            def check_prefetch_cancel():
                if self._cancel_sync.is_set():
                    raise ValueError('目录更新已取消。')
            with PendingWorkspaceReadPlan(self.settings, operation_nonce=uuid.uuid4().hex,
                                          check_cancel=check_prefetch_cancel) as plan:
                return refresh(plan)
        return self._start("sync", course_id, run)

    def start_catalog_sync(self) -> dict:
        from .notion_home import saved_home
        if not getattr(self.settings, "notion_token", ""):
            raise ValueError("请先连接 Notion。")
        if not saved_home(Path(self.settings.data_dir)):
            raise ValueError("请先选择 Notion 父页面并创建学习主页。")
        return self._start("catalog", "", lambda: (self._sync_notion_catalog() or ""))

    def start_download(self, key: str) -> dict:
        job = find_job(Path(self.settings.data_dir), key)
        if job is None:
            raise ValueError("找不到这条录像，请重新同步课程目录。")
        if not self.settings.pku_username or not self.settings.pku_password:
            raise ValueError("请先填写教学网账号和密码。")
        return self._start("download", key, lambda: self._download(job, key), course_id=job.recording.course_id)

    def _download(self, job: RecordingJob, key: str) -> str:
        from ..auth import get_session
        if not job.video.exists():
            self._stage("正在连接教学网")
            client = get_session(username=self.settings.pku_username,
                                 password=self.settings.pku_password)
            try:
                self._stage("下载录像到本机")
                result = download_job(client, job, progress=self._progress)
            finally:
                client.close()
            if result.path is None:
                raise ValueError("录像下载没有完成，请检查教学网连接后重试。")
        marker = job.directory / ".keep-video"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        _export_recording_to_download_dir(self.settings, job)
        return f"/api/recordings/{key}/video"

    def _refresh_stale_catalog(self, job: RecordingJob) -> None:
        """Renew an outdated campus snapshot instead of refusing to publish.

        A note states the course's current formal assignments, so the snapshot
        it reads must be fresh. Re-reading one course takes seconds; only a
        failed campus login stops the run.
        """
        issues = assignment_catalog_publish_issues(job)
        if not issues:
            return
        if (not getattr(self.settings, "pku_username", "")
                or not getattr(self.settings, "pku_password", "")):
            raise ValueError("；".join(issues)
                             + " 请先在「我的课程」保存教学网账号，应用会自动重新读取目录。")
        self._stage("教学网目录已过期，正在重新读取")
        try:
            sync_recording_index(self.settings, self._stage, self._progress,
                                 course_id=job.recording.course_id)
        except ValueError as exc:
            raise ValueError(f"教学网目录更新失败：{exc} 目录过期时不能安全生成笔记。") from exc
        except Exception as exc:
            logger.exception("campus catalog refresh failed")
            raise ValueError("教学网目录更新失败（可能是登录超时），无法核对最新作业；"
                             "请检查网络后重试。") from exc
        remaining = assignment_catalog_publish_issues(job)
        if remaining:
            raise ValueError("；".join(remaining) + " 已自动重新读取教学网目录，但仍未通过核对。")

    def _process_after_refresh(self, job: RecordingJob, lecture_id: str,
                               direct_oss: bool = False, regenerate: bool = False,
                               notes_strategy: str | None = None) -> str:
        self._refresh_stale_catalog(job)
        return self._process(job, lecture_id, direct_oss=direct_oss, regenerate=regenerate,
            **({"notes_strategy":notes_strategy} if notes_strategy is not None else {}))

    def start_campus_process(self, key: str, course_id: str, direct_oss: bool = False,
                             regenerate: bool = False, reuse_existing: bool = False,
                             notes_strategy: str | None = None,
                             auto_refine: bool = False,
                             allow_partial_quota: bool = False) -> dict:
        from ..ai_provider import is_asr_configured, is_llm_configured
        from .task_queue import DrainingError, get_global_task_queue

        job = find_job(Path(self.settings.data_dir), key, course_id)
        if job is None or job.recording.course_id != course_id:
            raise ValueError("这条录像不属于当前教学网课程。")
        if not reuse_existing and not is_llm_configured(self.settings, "notes"):
            raise ValueError("尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        if (
            not reuse_existing
            and not (job.directory / "transcript.json").exists()
            and not (job.directory / "notes.md").exists()
            and not is_asr_configured(self.settings)
        ):
            raise ValueError("尚未配置语音转写（ASR）：请先在「模型配置」页面选择并配置转写后端。")
        if not reuse_existing:
            queue = get_global_task_queue(Path(self.settings.data_dir))
            if queue.is_draining():
                raise DrainingError(queue.drain_message())
        from .notion_home import ensure_recording_target
        root = Path(self.settings.data_dir)
        local_mode = not self._notion_enabled()
        if reuse_existing:
            required = (
                job.directory / "transcript.json",
                job.directory / "notes.md",
                job.directory / "keyframes" / "index.json",
            )
            if not all(path.is_file() for path in required):
                raise ValueError("本机已有结果不完整；请使用「整理这节录像」补齐转写、笔记和课堂画面。")
        # Publishing complete local artifacts is intentionally independent of
        # campus availability. The immutable recording metadata already on
        # disk is sufficient to create the Notion target and publish the note.
        if not reuse_existing and not (
                getattr(self.settings, "pku_username", "")
                and getattr(self.settings, "pku_password", "")):
            issues = assignment_catalog_publish_issues(job)
            if issues:
                raise ValueError("；".join(issues)
                                 + " 请先在「我的课程」保存教学网账号，应用会自动重新读取目录。")
        def run() -> str:
            if not reuse_existing or local_mode:
                self._refresh_stale_catalog(job)
            lecture_id = ""
            with self._lock:
                self._work.local_mode = local_mode
            if not local_mode:
                self._stage("准备 Notion 课程与讲次页")
                with self._projection_stage():
                    lecture_id = ensure_recording_target(root, self.settings.notion_token, job, settings=self.settings)
            notes_path = job.directory / "notes.md"
            existing_raw = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
            if auto_refine and not regenerate and not reuse_existing and local_mode and existing_raw.strip():
                from .refined_notes import generate_refined_lecture_note

                self._stage("正在融合课件与板书进行 AI 精细整理…")
                generate_refined_lecture_note(
                    job,
                    course_id=course_id,
                    recording_id=key,
                    title=job.recording.title or key,
                    raw_markdown=existing_raw,
                    platform_service=getattr(self, "platform_service", None),
                    force=False,
                    settings=self.settings,
                )
                return f"/api/campus/courses/{course_id}/recordings/{key}/note-preview"
            result_url = self._process(
                job,
                lecture_id,
                direct_oss=direct_oss,
                regenerate=regenerate,
                reuse_existing=reuse_existing,
                allow_partial_quota=allow_partial_quota,
                **({"notes_strategy":notes_strategy} if notes_strategy is not None else {}),
            )
            if auto_refine:
                from .refined_notes import generate_refined_lecture_note

                raw_md = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
                if raw_md.strip():
                    self._stage("基础笔记已完成，正在自动进行 AI 精细整理…")
                    try:
                        generate_refined_lecture_note(
                            job,
                            course_id=course_id,
                            recording_id=key,
                            title=job.recording.title or key,
                            raw_markdown=raw_md,
                            platform_service=getattr(self, "platform_service", None),
                            force=False,
                            settings=self.settings,
                        )
                    except Exception as refine_exc:
                        logging.getLogger(__name__).warning(
                            "auto_refine failed after process for %s/%s: %s",
                            course_id,
                            key,
                            refine_exc,
                        )
                        self._stage(f"基础笔记已完成（精细整理未完成：{str(refine_exc)[:80]}）")
            return result_url
        return self._start("process", key, run, course_id=job.recording.course_id)

    def start_refine_note(
        self,
        key: str,
        course_id: str,
        *,
        force: bool = False,
        notes_strategy: str | None = None,
        auto_organize: bool = True,
    ) -> dict:
        from ..ai_provider import is_asr_configured, is_llm_configured

        job = find_job(Path(self.settings.data_dir), key, course_id)
        if job is None or job.recording.course_id != course_id:
            raise ValueError("这条录像不属于当前教学网课程。")
        if not is_llm_configured(self.settings, "refine"):
            raise ValueError("尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        platform_svc = getattr(self, "platform_service", None)
        if platform_svc is None or not hasattr(platform_svc, "refine_note"):
            raise ValueError("AI 精编服务尚未就绪，请检查「模型配置」。")
        notes_path = job.directory / "notes.md"
        existing_raw = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
        if not existing_raw.strip() and not auto_organize:
            raise ValueError("本讲尚未生成课堂笔记，请先生成笔记后再使用 AI 精编考点讲义。")
        if not existing_raw.strip() and not (job.directory / "transcript.json").exists() and not is_asr_configured(self.settings):
            raise ValueError("尚未配置语音转写（ASR）：请先在「模型配置」页面完成设置。")

        def run() -> str:
            from .refined_notes import generate_refined_lecture_note

            local_mode = not self._notion_enabled()
            with self._lock:
                if self._work:
                    self._work.local_mode = local_mode
            raw_md = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
            if not raw_md.strip():
                self._stage("正在转写并生成课堂笔记（完成后自动进入精细整理）…")
                self._refresh_stale_catalog(job)
                self._process(
                    job,
                    "",
                    regenerate=False,
                    **({"notes_strategy": notes_strategy} if notes_strategy else {}),
                )
                raw_md = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
            if not raw_md.strip():
                raise ValueError("基础课堂笔记生成未完成，无法继续精细整理。")
            self._stage("正在融合课件与板书进行 AI 精细整理…")
            generate_refined_lecture_note(
                job,
                course_id=course_id,
                recording_id=key,
                title=job.recording.title or key,
                raw_markdown=raw_md,
                platform_service=platform_svc,
                force=False,
                settings=self.settings,
            )
            return f"/api/campus/courses/{course_id}/recordings/{key}/note-preview"

        return self._start("refine", key, run, course_id=course_id)

    def start_batch_refine(
        self,
        items: list[tuple[str, str]],
        *,
        force: bool = False,
    ) -> dict:
        from ..ai_provider import AIProviderError, is_llm_configured

        clean_items = [
            (str(cid).strip(), str(rid).strip())
            for cid, rid in items
            if str(cid).strip() and str(rid).strip()
        ]
        if not clean_items:
            raise ValueError("没有待精细整理的课堂笔记。")
        if not is_llm_configured(self.settings, "refine"):
            raise ValueError("尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        platform_svc = getattr(self, "platform_service", None)
        if platform_svc is None or not hasattr(platform_svc, "refine_note"):
            raise ValueError("AI 精编服务尚未就绪，请检查「模型配置」。")

        root = Path(self.settings.data_dir)
        single_cid = clean_items[0][0] if len({c for c, _ in clean_items}) == 1 else "*"

        def run() -> str:
            from .refined_notes import generate_refined_lecture_note

            local_mode = not self._notion_enabled()
            with self._lock:
                if self._work:
                    self._work.local_mode = local_mode
            total = len(clean_items)
            succeeded = 0
            errors: list[str] = []
            for idx, (cid, rid) in enumerate(clean_items, 1):
                job = find_job(root, rid, cid)
                if job is None or job.recording.course_id != cid:
                    continue
                notes_path = job.directory / "notes.md"
                raw_md = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
                if not raw_md.strip():
                    continue
                self._stage(f"正在精细整理第 {idx}/{total} 讲：{job.recording.title or rid}")
                with self._lock:
                    if self._work and self._work.state == "running":
                        self._work.progress_done = idx - 1
                        self._work.progress_total = total
                        self._work.progress_remaining = total - (idx - 1)
                        self._work.progress_percent = int(((idx - 1) / total) * 100)
                try:
                    generate_refined_lecture_note(
                        job,
                        course_id=cid,
                        recording_id=rid,
                        title=job.recording.title or rid,
                        raw_markdown=raw_md,
                        platform_service=platform_svc,
                        force=bool(force),
                        settings=self.settings,
                    )
                    succeeded += 1
                except AIProviderError as exc:
                    status = int(getattr(exc, "status_code", 0) or 0)
                    if status in (401, 402, 403):
                        raise ValueError(f"已完成 {succeeded}/{total} 讲，后续因模型认证或额度错误中止：{exc}") from exc
                    errors.append(str(exc))
                except Exception as exc:
                    errors.append(str(exc))
            if succeeded == 0 and errors:
                raise ValueError(errors[0])
            with self._lock:
                if self._work and self._work.state == "running":
                    self._work.progress_done = total
                    self._work.progress_total = total
                    self._work.progress_remaining = 0
                    self._work.progress_percent = 100
            return f"/api/campus/courses/{clean_items[0][0]}/recordings/{clean_items[0][1]}/note-preview"

        return self._start("refine", clean_items[0][1], run, course_id=single_cid)

    def _process(self, job: RecordingJob, lecture_id: str, direct_oss: bool = False,
                 regenerate: bool = False, reuse_existing: bool = False,
                 notes_strategy: str | None = None,
                 allow_partial_quota: bool = False) -> str:
        had_video_before = bool(job and job.video.exists())
        completed_ok = False
        try:
            result_url = self._process_inner(
                job, lecture_id, direct_oss=direct_oss, regenerate=regenerate,
                reuse_existing=reuse_existing, notes_strategy=notes_strategy,
                allow_partial_quota=allow_partial_quota,
            )
            completed_ok = True
            return result_url
        finally:
            if (job is not None
                    and not completed_ok
                    and getattr(self.settings, "delete_video_after_processing", False)
                    and not getattr(self.settings, "save_recordings_locally", False)
                    and not (job.directory / ".keep-video").exists()):
                _cleanup_job_transient_media(job.directory, remove_video=not had_video_before)

    def _pace_hydrated_lecture(self) -> None:
        total = _cache_hit_pacing_seconds()
        phases = (
            ("正在连接教学网", 12, 0.18),
            ("正在提取语音与课堂画面", 46, 0.42),
            ("正在生成结构化课堂笔记", 82, 0.40),
        )
        if total <= 0:
            self._stage("正在生成结构化课堂笔记")
            self._progress(82, 100, "", 0)
            return
        for label, pct, weight in phases:
            self._stage(label)
            self._progress(pct, 100, "", 0)
            end_at = time.monotonic() + total * weight
            while True:
                rem = end_at - time.monotonic()
                if rem <= 0 or self._batch_cancel.is_set():
                    break
                time.sleep(min(0.25, rem))
            if self._batch_cancel.is_set():
                break

    def _process_inner(self, job: RecordingJob, lecture_id: str, direct_oss: bool = False,
                       regenerate: bool = False, reuse_existing: bool = False,
                       notes_strategy: str | None = None,
                       allow_partial_quota: bool = False) -> str:
        from ..auth import get_session
        from ..lecture_source_download import prepare_lesson_sources
        from ..media import resolve_notes_model, write_notes
        from ..note_sources import collect_note_sources
        from .transcript_review import clear_regeneration_pending, review_pending
        from ..catalog_snapshots import recording_source_id
        from copy import copy

        metered_settings = self.settings.model_copy() if hasattr(self.settings, 'model_copy') else copy(self.settings)
        if notes_strategy is not None:
            if notes_strategy not in {"transcript", "topics"}:
                raise ValueError("请选择保留讲述顺序或知识点图文笔记。")
            metered_settings.notes_strategy = notes_strategy
        metered_settings.notes_model = resolve_notes_model(
            self.settings, getattr(metered_settings, "notes_strategy", "transcript"))
        source_id = recording_source_id(job.recording)
        ambiguous_ts = _has_ambiguous_recorded_at(job)
        metered_settings._metered_operation_context = {
            'purpose': 'lecture-notes', 'course_id': job.recording.course_id,
            'source_id': source_id, 'client_operation_id': source_id}
        metered_settings._ambiguous_recorded_at = ambiguous_ts
        if ambiguous_ts:
            metered_settings._transcribe_source_id_override = _shared_cache_signature(
                job, source_id, ambiguous=True
            )

        notes_path = job.directory / "notes.md"
        transcript_path = job.directory / "transcript.json"
        regenerate = regenerate or review_pending(job)
        if regenerate and not notes_path.is_file():
            raise ValueError("这节课还没有旧笔记，请使用「整理这节录像」。")
        if regenerate and not transcript_path.is_file():
            raise ValueError("本机没有这节课的转写，无法安全重整旧笔记。")
        if not regenerate and notes_path.exists() and not transcript_path.is_file():
            raise ValueError("本机缺少这节课的转写，无法核对笔记是否覆盖整节录像；请重新整理后再发布。")
        if regenerate:
            # A deliberate rewrite differs from initial generation. Retries
            # keep this identity while the old canonical note is unchanged,
            # so completed paid stages survive publication/network failure.
            metered_settings._metered_operation_context["client_operation_id"] = (
                "notes-" + hashlib.sha256(notes_path.read_bytes()).hexdigest())
        catalog_issues = assignment_catalog_publish_issues(job)
        if catalog_issues:
            raise ValueError("；".join(catalog_issues))
        if not regenerate and not notes_path.exists() and transcript_path.is_file():
            from ..transcript_notes import restore_saved_note
            transcript = json.loads(transcript_path.read_text("utf-8"))
            if restore_saved_note(notes_path, transcript):
                self._stage("恢复已保存的课堂笔记")
        if not regenerate and notes_path.exists():
            # Check saved state before optional media downloads or remote
            # writes. This binds the note to its completed model review;
            # it does not judge its wording, timestamps or citations.
            issues = note_publish_issues(job, notes_path)
            if issues:
                raise ValueError("；".join(issues))
        staged_note: Path | None = None
        hydrated_complete_notes = False
        if regenerate:
            source_paths, source_failures = prepare_lesson_sources(
                job, recording_id(job), self.settings, stage=self._stage
            )
            transcript = json.loads(transcript_path.read_text("utf-8"))
            source_context = collect_note_sources(
                job.course_dir, job.recording.date, job.recording.title,
                allowed_paths=source_paths,
            )
            source_context.extend(source_failures)
            try:
                from ..board_ocr import recognize_blackboard_frames
                board_sources = recognize_blackboard_frames(job.directory, transcript, metered_settings)
                if board_sources:
                    source_context.extend(board_sources)
            except Exception:
                pass
            staged_note = job.directory / f"notes.rebuild-{uuid.uuid4().hex}.md"
            self._stage("重新生成课堂笔记")
            try:
                with self._ensure_local_queue_slot():
                    written = write_notes(transcript, _saved_keyframes(job.directory),
                                          staged_note, metered_settings, job.label,
                                          source_context=source_context, progress=self._progress,
                                          course_directory=job.course_dir)
                if written is None:
                    raise ValueError("笔记生成没有完成，请检查「模型配置」与 API 可用性后重试。")
            except Exception as exc:
                staged_note.unlink(missing_ok=True)
                if isinstance(exc, (RuntimeError, ValueError)):
                    raise ValueError(f"新版笔记没有完成：{exc}；原笔记保留。") from exc
                raise ValueError("新版笔记没有完成；原笔记保留，请稍后重试。") from exc
        elif not notes_path.exists():
            if not hydrated_complete_notes:
                stream_in_memory = (
                    not job.video.exists()
                    and not (job.directory / ".keep-video").exists()
                    and not getattr(self.settings, "save_recordings_locally", False)
                    and getattr(self.settings, "delete_video_after_processing", True)
                    and download_job is _DEFAULT_DOWNLOAD_JOB
                    and process_job is _DEFAULT_PROCESS_JOB
                )
                if not job.video.exists() and not transcript_path.exists() and stream_in_memory:
                    if not self.settings.pku_username or not self.settings.pku_password:
                        raise ValueError("请先填写教学网账号和密码，以读取这条录像。")
                    with self._ensure_local_queue_slot():
                        source_paths, source_failures = prepare_lesson_sources(
                            job, recording_id(job), self.settings, stage=self._stage
                        )
                        self._stage("正在连接教学网")
                        client = get_session(username=self.settings.pku_username,
                                             password=self.settings.pku_password)
                        try:
                            self._stage("转写并生成笔记")
                            result = stream_process_job(
                                client, job, metered_settings, include_keyframes=True,
                                progress=self._progress, direct_oss=direct_oss,
                                source_paths=source_paths, source_failures=source_failures,
                                stage=self._stage,
                            )
                        finally:
                            client.close()
                        if any(error.startswith("stream: ") for error in result.errors):
                            raise ValueError("录像读取没有完成，请检查网络连接后重试。")
                        if not result.notes:
                            logger.warning("recording processing incomplete: %s", [error.split(":", 1)[0] for error in result.errors])
                            if not result.transcribed:
                                failure = next((error.removeprefix("transcribe: ") for error in result.errors
                                                if error.startswith("transcribe: ")), "请检查教学网连接或模型配置")
                                raise ValueError(
                                    _format_incomplete_transcription_error(
                                        failure, "已完成片段会在重试时复用。"
                                    )
                                )
                            failure = next((error.removeprefix("notes: ") for error in result.errors
                                            if error.startswith("notes: ")), "请检查「模型配置」")
                            raise ValueError(f"转写已完成，但笔记未完成：{failure}")
                else:
                    with self._ensure_local_queue_slot():
                        if not job.video.exists() and not transcript_path.exists():
                            if not self.settings.pku_username or not self.settings.pku_password:
                                raise ValueError("请先填写教学网账号和密码，以下载这条录像。")
                            self._stage("正在连接教学网")
                            client = get_session(username=self.settings.pku_username,
                                                 password=self.settings.pku_password)
                            try:
                                self._stage("下载录像")
                                downloaded = download_job(client, job, progress=self._progress)
                            finally:
                                client.close()
                            if downloaded.path is None:
                                raise ValueError("录像下载没有完成，请稍后重试。")
                        if job.video.exists():
                            source_paths, source_failures = prepare_lesson_sources(
                                job, recording_id(job), self.settings, stage=self._stage
                            )
                            self._stage("转写并生成笔记")
                            result = process_job(
                                job, metered_settings, include_keyframes=True,
                                progress=self._progress, direct_oss=direct_oss,
                                source_paths=source_paths, source_failures=source_failures,
                                preserve_video=True,
                            )
                            if not result.notes:
                                logger.warning("recording processing incomplete: %s", [error.split(":", 1)[0] for error in result.errors])
                                if not result.transcribed:
                                    failure = next((error.removeprefix("transcribe: ") for error in result.errors
                                                    if error.startswith("transcribe: ")), "请检查录像文件或网络")
                                    if "未扣除任何" in failure:
                                        raise ValueError(failure)
                                    keep_video = (
                                        getattr(self.settings, "save_recordings_locally", False)
                                        or (job.directory / ".keep-video").exists()
                                        or not getattr(self.settings, "delete_video_after_processing", False)
                                    )
                                    suffix = "已下载的录像保留，可重试。" if keep_video else "已完成片段会在重试时复用。"
                                    raise ValueError(_format_incomplete_transcription_error(failure, suffix))
                                failure = next((error.removeprefix("notes: ") for error in result.errors
                                                if error.startswith("notes: ")), "请检查「模型配置」")
                                raise ValueError(f"转写已完成，但笔记未完成：{failure}")
                        else:
                            source_paths, source_failures = prepare_lesson_sources(
                                job, recording_id(job), self.settings, stage=self._stage
                            )
                            self._stage("从已有转写生成笔记")
                            transcript = json.loads(transcript_path.read_text("utf-8"))
                            source_context = collect_note_sources(
                                job.course_dir, job.recording.date, job.recording.title,
                                allowed_paths=source_paths,
                            )
                            source_context.extend(source_failures)
                            try:
                                from ..board_ocr import recognize_blackboard_frames
                                board_sources = recognize_blackboard_frames(job.directory, transcript, metered_settings)
                                if board_sources:
                                    source_context.extend(board_sources)
                            except Exception:
                                pass
                            written = write_notes(
                                transcript, _saved_keyframes(job.directory), notes_path,
                                metered_settings, job.label, source_context=source_context,
                                progress=self._progress, course_directory=job.course_dir,
                            )
                            if written is None:
                                raise ValueError("笔记生成没有完成，请检查「模型配置」与 API 可用性后重试。")
        from ..media import extract_keyframes
        frame_index = job.directory / "keyframes" / "index.json"
        frame_rows = _saved_keyframes(job.directory, require_files=not reuse_existing)
        if not frame_rows and not regenerate and not hydrated_complete_notes:
            with self._ensure_local_queue_slot():
                stream_keyframes_in_memory = (
                    not job.video.exists()
                    and not (job.directory / ".keep-video").exists()
                    and not getattr(self.settings, "save_recordings_locally", False)
                    and getattr(self.settings, "delete_video_after_processing", True)
                    and download_job is _DEFAULT_DOWNLOAD_JOB
                )
                if stream_keyframes_in_memory:
                    if not self.settings.pku_username or not self.settings.pku_password:
                        raise ValueError("本机没有课堂画面；请连接教学网后重试补图。")
                    frame_index.unlink(missing_ok=True)
                    self._stage("提取课堂画面")
                    client = get_session(username=self.settings.pku_username,
                                         password=self.settings.pku_password)
                    try:
                        frame_rows = stream_extract_keyframes_job(
                            client, job, job.directory / "keyframes", progress=self._progress
                        )
                    except Exception as exc:
                        logger.exception("lecture frame stream extraction failed")
                        raise ValueError("课堂画面提取失败；文字笔记已保留，请稍后重试。") from exc
                    finally:
                        client.close()
                    if not frame_rows:
                        raise ValueError("录像未能提取课堂画面；文字笔记已保留，请稍后重试。")
                else:
                    if not job.video.exists():
                        if not self.settings.pku_username or not self.settings.pku_password:
                            raise ValueError("本机没有课堂画面；请连接教学网后重试补图。")
                        self._stage("重新获取录像以补充课堂画面")
                        client = get_session(username=self.settings.pku_username,
                                             password=self.settings.pku_password)
                        try:
                            downloaded = download_job(client, job, progress=self._progress)
                        finally:
                            client.close()
                        if downloaded.path is None:
                            raise ValueError("无法获取录像画面；文字笔记已保留，请稍后重试。")
                    frame_index.unlink(missing_ok=True)
                    self._stage("提取课堂画面")
                    try:
                        frame_rows = extract_keyframes(job.video, job.directory / "keyframes")
                    except Exception as exc:
                        logger.exception("lecture frame extraction failed")
                        raise ValueError("课堂画面提取失败；文字笔记已保留，请稍后重试。") from exc
                    if not frame_rows:
                        raise ValueError("录像未能提取课堂画面；文字笔记已保留，请稍后重试。")
        try:
            if staged_note is None and notes_path.is_file():
                from .note_images import build_note_visual_sections

                md_text = notes_path.read_text("utf-8", errors="replace")
                build_note_visual_sections(
                    job,
                    job.recording.course_id,
                    recording_id(job),
                    md_text,
                )
        except Exception:
            pass
        from .note_videos import needs_video_clips
        local_mode = not lecture_id
        allow_video_on_disk = (
            getattr(self.settings, "save_recordings_locally", False)
            or not getattr(self.settings, "delete_video_after_processing", True)
            or (job.directory / ".keep-video").exists()
            or download_job is not _DEFAULT_DOWNLOAD_JOB
        )
        if not local_mode and not reuse_existing and not job.video.exists() and allow_video_on_disk and needs_video_clips(staged_note or notes_path):
            if self.settings.pku_username and self.settings.pku_password:
                self._stage("获取录像以生成回看片段")
                client = get_session(username=self.settings.pku_username,
                                     password=self.settings.pku_password)
                try:
                    downloaded = download_job(client, job, progress=self._progress)
                finally:
                    client.close()
                if downloaded.path is None:
                    logger.warning("video clip source download failed: %s", downloaded.error)
        if not local_mode:
            self._stage("将课堂画面和笔记发布到 Notion")
            try:
                if staged_note is not None:
                    note_url = publish_notes(job, lecture_id, self.settings.notion_token,
                                             note_path=staged_note, revision=True)
                else:
                    note_url = publish_notes(job, lecture_id, self.settings.notion_token)
            except Exception as exc:
                logger.exception("Notion note publication failed")
                quality_message = (str(exc) if isinstance(exc, ValueError)
                                   and str(exc).startswith("笔记暂不能作为完整课程笔记发布") else "")
                if staged_note is not None:
                    staged_note.unlink(missing_ok=True)
                    if quality_message:
                        raise ValueError(f"{quality_message}；原笔记及旧页面均已保留。") from exc
                    raise ValueError("新版笔记未能写入 Notion；原笔记及旧页面均已保留，可稍后重试。") from exc
                if quality_message:
                    raise ValueError(f"{quality_message}；本机笔记已保留。") from exc
                raise ValueError("笔记已保存在本机，但写入 Notion 失败。请检查连接后重试。") from exc
        if staged_note is not None:
            from ..note_editor import report_path
            backup = job.directory / f"notes.previous-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.md"
            previous_report = report_path(notes_path)
            staged_report = report_path(staged_note)
            backup_report = report_path(backup)
            report_promoted = False
            try:
                notes_path.replace(backup)
                if previous_report.exists():
                    previous_report.replace(backup_report)
                staged_report.replace(previous_report)
                report_promoted = True
                staged_note.replace(notes_path)
            except Exception:
                if report_promoted:
                    previous_report.replace(staged_report)
                if backup_report.exists():
                    backup_report.replace(previous_report)
                if backup.exists():
                    backup.replace(notes_path)
                raise
        if local_mode:
            if regenerate:
                clear_regeneration_pending(job)
            _finalize_video_retention(self.settings, job, local_mode=True, notes_path=notes_path)
            try:
                from .note_images import build_note_visual_sections
                if notes_path.is_file():
                    md_text = notes_path.read_text("utf-8", errors="replace")
                    build_note_visual_sections(
                        job,
                        job.recording.course_id,
                        recording_id(job),
                        md_text,
                    )
            except Exception:
                pass
            return f"/api/campus/courses/{job.recording.course_id}/recordings/{recording_id(job)}/note-preview"
        from .notion_home import saved_home
        from ..catalog_snapshots import recording_source_id
        publication = {
            "recording_id": recording_id(job),
            "course_id": job.recording.course_id,
            "source_id": recording_source_id(job.recording),
            "home_id": saved_home(Path(self.settings.data_dir)).get("id", ""),
            "lecture_id": lecture_id,
            "lecture_url": f"https://www.notion.so/{lecture_id.replace('-', '')}",
            "note_url": note_url,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        marker = job.directory / "notion-publication.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_file():
            previous = marker.read_bytes()
            try:
                prior_home = json.loads(previous).get("home_id")
            except (ValueError, AttributeError):
                prior_home = None
            if prior_home != publication["home_id"]:
                backup = marker.with_name("notion-publication.legacy-" + hashlib.sha256(previous).hexdigest()[:16] + ".json")
                if not backup.exists():
                    backup.write_bytes(previous)
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(json.dumps(publication, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(marker)
        if regenerate:
            clear_regeneration_pending(job)
        _finalize_video_retention(self.settings, job, local_mode=False, notes_path=notes_path)
        with self._lock:
            if self._work:
                self._work.lecture_url = publication["lecture_url"]
        return note_url

def add_recording_routes(app: FastAPI, manager: RecordingWorkManager, *, demo_connection: bool = False) -> None:
    credentials_lock = threading.Lock()
    @app.get("/api/campus/batch-organize/preview")
    def batch_organize_preview_endpoint() -> dict:
        return batch_organize_preview(manager.settings)

    @app.post("/api/campus/batch-organize")
    def batch_organize_endpoint(body: BatchOrganizeRequest) -> dict:
        from .task_queue import DrainingError

        try:
            return manager.start_batch_organize(body)
        except DrainingError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/campus/batch-organize/cancel")
    def batch_organize_cancel_endpoint() -> dict:
        try:
            return manager.cancel_batch_organize()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/campus/courses")
    def campus_courses_endpoint() -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import build_auto_track_status
        root = Path(manager.settings.data_dir)
        payload = campus_courses(root)
        # Browsing needs the local home identity, not a full remote Notion tree.
        from .notion_home import saved_home
        home = saved_home(root)
        payload['notion_home'] = ({key: home[key] for key in
            ('id', 'url', 'parent_id', 'schema', 'active') if key in home} if home.get('id') else None)
        payload["campus_configured"] = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        payload.update(_campus_vault_status(root, payload["campus_configured"]))
        payload["demo_connection"] = bool(demo_connection)
        payload["demo_preview"] = False
        payload["cloud_web_mode"] = False
        payload["local_recordings"] = local_storage_summary(manager.settings)
        payload.update(_catalog_sync_status(root, payload.get("courses") or []))
        bg_running = (getattr(manager, "background_state", lambda: {})() or {}).get("state") == "running"
        payload["auto_track"] = build_auto_track_status(
            root,
            payload.get("courses") or [],
            campus_configured=payload["campus_configured"],
            bg_running=bg_running,
        )
        return payload

    @app.get("/api/campus/auto-track")
    def get_campus_auto_track_endpoint() -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import build_auto_track_status
        root = Path(manager.settings.data_dir)
        courses = campus_courses(root).get("courses") or []
        campus_configured = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        bg_running = (getattr(manager, "background_state", lambda: {})() or {}).get("state") == "running"
        return build_auto_track_status(root, courses, campus_configured=campus_configured, bg_running=bg_running)

    @app.post("/api/campus/auto-track")
    def update_campus_auto_track_endpoint(body: dict | None = None) -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import build_auto_track_status, update_auto_track_config
        root = Path(manager.settings.data_dir)
        update_auto_track_config(root, body or {})
        courses = campus_courses(root).get("courses") or []
        campus_configured = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        bg_running = (getattr(manager, "background_state", lambda: {})() or {}).get("state") == "running"
        return build_auto_track_status(root, courses, campus_configured=campus_configured, bg_running=bg_running)

    @app.post("/api/campus/auto-track/toggle")
    def toggle_campus_auto_track_endpoint(body: dict | None = None) -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import build_auto_track_status, toggle_course_auto_track
        payload = body or {}
        course_id = str(payload.get("course_id") or "").strip()
        tracked = payload.get("tracked")
        if tracked is not None:
            tracked = bool(tracked)
        root = Path(manager.settings.data_dir)
        try:
            toggled = toggle_course_auto_track(root, course_id, tracked=tracked)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        courses = campus_courses(root).get("courses") or []
        campus_configured = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        bg_running = (getattr(manager, "background_state", lambda: {})() or {}).get("state") == "running"
        status = build_auto_track_status(root, courses, campus_configured=campus_configured, bg_running=bg_running)
        return {
            "ok": True,
            "course_id": course_id,
            "tracked": toggled["tracked"],
            "auto_track": status,
        }

    @app.post("/api/campus/auto-track/check-now")
    def check_campus_auto_track_now_endpoint() -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import build_auto_track_status, load_auto_track_config
        root = Path(manager.settings.data_dir)
        cfg = load_auto_track_config(root)
        if not cfg.get("tracked_course_ids"):
            raise HTTPException(status_code=400, detail="请先选择至少一门需要自动追踪的课程。")
        if not getattr(manager.settings, "pku_username", "") or not getattr(manager.settings, "pku_password", ""):
            raise HTTPException(status_code=400, detail="请先在连接设置中保存教学网账号与密码。")
        res = manager.trigger_auto_track_if_due(trigger="manual", force=True)
        if not res.get("triggered"):
            reason = res.get("reason") or "busy"
            if reason == "busy":
                raise HTTPException(status_code=409, detail="当前已有课程更新或整理任务正在运行，请稍候。")
            if reason == "out_of_sync":
                raise HTTPException(status_code=409, detail="当前课程目录存在非同步旧记录，请先处理后再巡检。")
            raise HTTPException(status_code=400, detail="暂时无法启动自动追踪巡检。")
        courses = campus_courses(root).get("courses") or []
        status = build_auto_track_status(root, courses, campus_configured=True, bg_running=True)
        return {"ok": True, **res, "auto_track": status}

    @app.post("/api/campus/prune-stale-courses")
    def prune_stale_courses_endpoint() -> dict:
        from .campus_catalog import campus_courses
        if (manager.state().get("state") == "running"
                or manager.background_state().get("state") == "running"):
            raise HTTPException(status_code=409, detail="请等待当前课程任务结束后再清理非同步课程。")
        root = Path(manager.settings.data_dir)
        rows = campus_courses(root).get("courses") or []
        sync_status = _catalog_sync_status(root, rows)
        stale_ids = {str(item.get("id") or "") for item in (sync_status.get("stale_courses") or []) if item.get("id")}
        all_ids = {str(r.get("id") or "") for r in rows if r.get("id")}
        active_ids = all_ids - stale_ids
        removed = _prune_stale_local_courses(
            root, active_ids, allow_clear_all=(len(active_ids) == 0 and bool(stale_ids))
        )
        return {"ok": True, "removed": removed, **campus_courses_endpoint()}

    @app.get("/api/campus/courses/{course_id}")
    def campus_course_endpoint(course_id: str) -> dict:
        from .campus_catalog import campus_course
        from .course_tracker import load_auto_track_config
        root = Path(manager.settings.data_dir)
        try:
            course_data = campus_course(root, course_id)
            tracked_ids = set(load_auto_track_config(root).get("tracked_course_ids") or [])
            course_data["tracked"] = course_id in tracked_ids
            return course_data
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/match-slides")
    def campus_match_slides(course_id: str, key: str) -> dict:
        raise HTTPException(
            status_code=410,
            detail="已下线按课堂画面匹配课件功能，课堂笔记与精细整理直接基于课堂回放与投影/板书画面生成。",
        )

    def _count_mcp_courses_and_notes(root: Path) -> tuple[int, int]:
        try:
            course_it = os.scandir(root)
        except OSError:
            return 0, 0
        courses_count = 0
        notes_count = 0
        with course_it:
            for entry in course_it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                    course_path = entry.path
                    if not os.path.isfile(os.path.join(course_path, "course.json")):
                        continue
                except OSError:
                    continue
                courses_count += 1
                rec_root = os.path.join(course_path, "recordings")
                try:
                    rec_it = os.scandir(rec_root)
                except OSError:
                    continue
                with rec_it:
                    for rec_entry in rec_it:
                        if rec_entry.name.startswith("."):
                            continue
                        try:
                            if not rec_entry.is_dir():
                                continue
                            notes_path = os.path.join(rec_entry.path, "notes.md")
                            st = os.stat(notes_path)
                        except OSError:
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        if st.st_size > 32:
                            notes_count += 1
                        elif st.st_size > 0:
                            try:
                                with open(notes_path, "r", encoding="utf-8", errors="replace") as fh:
                                    if fh.read().strip():
                                        notes_count += 1
                            except OSError:
                                continue
        return courses_count, notes_count

    def _build_mcp_status_response(request: Request, *, force_refresh: bool = False) -> JSONResponse:
        from ..mcp_setup import host_integration_catalog

        root = Path(manager.settings.data_dir)
        catalog = host_integration_catalog(data_dir=root)
        for item in catalog.get("tools_summary") or []:
            if isinstance(item, dict):
                item["locked"] = False
        courses_count, notes_count = _count_mcp_courses_and_notes(root)
        payload = {
            "ok": True,
            "cloud_web_mode": False,
            "demo_preview": False,
            "mcp_full_enabled": True,
            "mcp_tier": "full",
            "effective_plan": "open",
            "membership_paused": False,
            "free_tools": [],
            "upgrade_message": "",
            "remote_mcp_url": "",
            "exchange_url": "",
            "pairing_ticket": "",
            "pairing_expires_in": 0,
            "mcp_key": "",
            "courses_count": courses_count,
            "notes_count": notes_count,
            **catalog,
        }
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/api/mcp/status")
    def mcp_status_endpoint(request: Request):
        return _build_mcp_status_response(
            request, force_refresh=(request.query_params.get("refresh") == "1")
        )

    @app.get("/api/campus/courses/{course_id}/summary-preview")
    def course_summary_preview(course_id: str) -> dict:
        from .course_summary import summary_markdown, _lecture_rows
        root = Path(manager.settings.data_dir)
        course = campus_course_endpoint(course_id)
        lessons = _lecture_rows(root, course)
        return {"course_id": course_id, "title": course["title"],
                "total_lessons": len(lessons),
                "summarized_lessons": sum(bool(row["has_note"]) for row in lessons),
                "summary": course.get("summary") or {},
                "markdown": summary_markdown(root, course_id)}

    def visual_job(course_id, key):
        job = find_job(Path(manager.settings.data_dir), key, course_id)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        return job

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/local-materials")
    async def lecture_local_materials_upload(course_id: str, key: str, files: list[UploadFile] = File(default=[])):
        for file in files:
            await file.close()
        raise HTTPException(
            status_code=403,
            detail="请直接基于课堂回放与教学网课程资料整理。",
        )

    @app.delete("/api/campus/courses/{course_id}/recordings/{key}/local-materials/{material_id}")
    @app.post("/api/campus/courses/{course_id}/recordings/{key}/local-materials/{material_id}/remove")
    def lecture_local_material_remove(course_id: str, key: str, material_id: str):
        from ..lecture_source_download import remove_local_lecture_material
        from .campus_catalog import campus_course
        job = visual_job(course_id, key)
        try:
            remove_local_lecture_material(job, key, material_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return campus_course(Path(manager.settings.data_dir), course_id)

    @app.get("/api/campus/courses/{course_id}/recordings/{key}/frames/{filename}")
    def lecture_frame(course_id: str, key: str, filename: str, w: int = 0):
        from .note_images import _IMAGE_SUFFIXES, _shared_seed_recording_dir, crop_wide_classroom_projector_bgr
        from ..board_ocr import is_blackboard_frame
        job = visual_job(course_id, key)
        if (Path(filename).name != filename or "\\" in filename
                or Path(filename).suffix.lower() not in _IMAGE_SUFFIXES):
            raise HTTPException(status_code=400, detail="无效的课堂图片名称。")
        candidate_dirs = [job.directory / "keyframes", job.directory / "note-frames"]
        seed_dir = _shared_seed_recording_dir(job)
        if seed_dir is not None:
            candidate_dirs.extend([seed_dir / "keyframes", seed_dir / "note-frames"])
        cache_headers = {"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"}
        for folder in candidate_dirs:
            path = folder / filename
            if path.is_file() and path.resolve().is_relative_to(folder.resolve()):
                if 160 <= w <= 1600 and path.stat().st_size > 48_000:
                    thumb_dir = folder / ".thumbs"
                    thumb_path = thumb_dir / f"{path.stem}_w{w}.webp"
                    if thumb_path.is_file() and thumb_path.stat().st_size > 200:
                        return FileResponse(thumb_path, media_type="image/webp", headers=cache_headers)
                    try:
                        import cv2
                        import numpy as np
                        import uuid
                        raw = np.fromfile(str(path), dtype=np.uint8)
                        img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
                        if img is not None:
                            if not is_blackboard_frame(path):
                                img = crop_wide_classroom_projector_bgr(img)
                            h, orig_w = img.shape[:2]
                            if orig_w > w:
                                new_h = max(1, int(round(h * (w / float(orig_w)))))
                                img = cv2.resize(img, (w, new_h), interpolation=cv2.INTER_AREA)
                            ok, enc = cv2.imencode(".webp", img, [int(cv2.IMWRITE_WEBP_QUALITY), 78])
                            if ok:
                                thumb_dir.mkdir(parents=True, exist_ok=True)
                                staged = thumb_path.with_name(f"{thumb_path.name}.{uuid.uuid4().hex}.tmp")
                                try:
                                    staged.write_bytes(enc.tobytes())
                                    staged.replace(thumb_path)
                                finally:
                                    staged.unlink(missing_ok=True)
                                return FileResponse(thumb_path, media_type="image/webp", headers=cache_headers)
                    except Exception:
                        pass
                return FileResponse(path, headers=cache_headers)
        raise HTTPException(status_code=404, detail="课堂图片尚未保存。")

    @app.get("/api/campus/courses/{course_id}/recordings/{key}/slide-page")
    def lecture_slide_page(course_id: str, key: str, file: str, page: int = 1):
        from .note_images import _local_material, _slide_cache
        job = visual_job(course_id, key)
        if page < 1:
            raise HTTPException(status_code=400, detail="课件页码必须从 1 开始。")
        cache_headers = {"Cache-Control": "public, max-age=604800, stale-while-revalidate=86400"}
        cache = _slide_cache(job, file, page)
        for extension in (".jpg", ".png"):
            image = Path(str(cache) + extension)
            if image.is_file() and image.resolve().is_relative_to(cache.parent.resolve()):
                return FileResponse(image, headers=cache_headers)
        pdf = _local_material(job, file)
        if pdf is None:
            raise HTTPException(status_code=404, detail="课件 PDF 尚未保存。")
        import pypdfium2 as pdfium
        import cv2
        import uuid
        try:
            with pdfium.PdfDocument(str(pdf)) as document:
                if page > len(document):
                    raise HTTPException(status_code=404, detail="课件没有这一页。")
                pdf_page = document[page - 1]
                bitmap = None
                try:
                    bitmap = pdf_page.render(scale=min(2, 1600 / max(pdf_page.get_size())))
                    pixels = bitmap.to_numpy()
                    # PDFium's native buffer is BGR/BGRA, matching OpenCV.
                    ok, encoded = cv2.imencode(".jpg", pixels)
                    if not ok:
                        raise ValueError("image encoding failed")
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    image = Path(str(cache) + ".jpg")
                    staged = image.with_name(image.name + f".{uuid.uuid4().hex}.tmp")
                    try:
                        staged.write_bytes(encoded.tobytes())
                        staged.replace(image)
                    finally:
                        staged.unlink(missing_ok=True)
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    pdf_page.close()
        except (OSError, ValueError, pdfium.PdfiumError) as exc:
            raise HTTPException(status_code=422, detail="课件页面暂时无法读取。") from exc
        return FileResponse(image, headers=cache_headers)

    _KEYFRAME_BACKFILL_INFLIGHT: set[str] = set()
    _KEYFRAME_BACKFILL_LOCK = threading.Lock()

    def _maybe_schedule_keyframe_backfill(job: RecordingJob, course_id: str, key: str) -> bool:
        """If a lecture has notes + video URL/file but 0 extracted keyframe images, extract in background."""
        from .note_images import _IMAGE_SUFFIXES
        for folder in (job.directory / "keyframes", job.directory / "note-frames"):
            if folder.is_dir() and any(p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES for p in folder.iterdir()):
                return False
        has_source = job.video.is_file() or bool((job.recording.play_url or "").strip() or (getattr(job.recording, "download_url", "") or "").strip())
        if not has_source:
            return False
        settings = manager.settings
        pku_username = str(getattr(settings, "pku_username", "") or "").strip()
        pku_password = str(getattr(settings, "pku_password", "") or "").strip()
        if not job.video.is_file() and (not pku_username or not pku_password):
            return False
        inflight_key = str(job.directory.resolve())
        with _KEYFRAME_BACKFILL_LOCK:
            if inflight_key in _KEYFRAME_BACKFILL_INFLIGHT:
                return True
            _KEYFRAME_BACKFILL_INFLIGHT.add(inflight_key)

        def _worker():
            index_file = job.directory / "keyframes" / "index.json"
            old_index_bytes = index_file.read_bytes() if index_file.is_file() else None
            try:
                from ..auth import get_session
                frames_dir = job.directory / "keyframes"
                frames_dir.mkdir(parents=True, exist_ok=True)
                index_file.unlink(missing_ok=True)
                if job.video.is_file():
                    from ..media import extract_keyframes
                    extract_keyframes(job.video, frames_dir)
                else:
                    client = get_session(username=pku_username, password=pku_password)
                    try:
                        stream_extract_keyframes_job(client, job, frames_dir)
                    finally:
                        client.close()
                (frames_dir / ".visual-sections-cache.json").unlink(missing_ok=True)
                (job.directory / ".visual-sections-cache.json").unlink(missing_ok=True)
                md_path = job.directory / "notes.md"
                if md_path.is_file():
                    from .note_images import build_note_visual_sections
                    build_note_visual_sections(job, course_id, key, md_path.read_text("utf-8", errors="replace"))
            except Exception:
                if old_index_bytes is not None and not index_file.exists():
                    try:
                        index_file.write_bytes(old_index_bytes)
                    except OSError:
                        pass
                logger.warning("background keyframe backfill failed for %s", job.directory, exc_info=True)
            finally:
                with _KEYFRAME_BACKFILL_LOCK:
                    _KEYFRAME_BACKFILL_INFLIGHT.discard(inflight_key)

        threading.Thread(target=_worker, name=f"keyframe-backfill-{key[:12]}", daemon=True).start()
        return True

    @app.get("/api/campus/courses/{course_id}/recordings/{key}/note-preview")
    def lecture_note_preview(course_id: str, key: str, request: Request = None, stream: bool = False):
        from .course_summary import extract_note_oral_highlights, note_outline
        from .lecture_logic_map import load_saved_lecture_logic_map
        from .lecture_quiz import load_saved_lecture_quiz
        from .note_images import build_note_visual_sections, rewrite_note_markdown_images
        from .refined_notes import estimate_refine_cost, load_saved_refined_note
        job = find_job(Path(manager.settings.data_dir), key, course_id)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        path = job.directory / "notes.md"
        raw_markdown = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        markdown = rewrite_note_markdown_images(job, course_id, key, raw_markdown) if raw_markdown else ""
        refine_estimate = estimate_refine_cost(raw_markdown, settings=manager.settings)
        saved_refined = load_saved_refined_note(job.directory, raw_markdown)
        refined_available = bool(saved_refined and saved_refined.get("markdown"))
        refined_markdown = (
            rewrite_note_markdown_images(job, course_id, key, saved_refined["markdown"])
            if refined_available and saved_refined is not None
            else ""
        )
        refined_meta = saved_refined.get("meta") if saved_refined is not None else None
        outline = note_outline(markdown)
        oral_highlights = extract_note_oral_highlights(markdown, limit=6)
        want_stream = bool(stream) or (request is not None and request.headers.get("x-note-stream") == "1")
        if want_stream:
            def _stream_generator():
                def event(payload):
                    return json.dumps(payload, ensure_ascii=False) + "\n"

                chunks, total_sections = _split_markdown_stream_chunks(markdown)
                fast_vis = build_note_visual_sections(job, course_id, key, raw_markdown, fast_only=True)
                done_sections = min(2, total_sections)
                yield event({
                    "stage": "init", "recording_id": key, "course_id": course_id,
                    "title": job.recording.title, "date": job.recording.date,
                    "available": bool(markdown.strip()), "markdown_chunk": chunks[0] if chunks else "",
                    "refined_available": refined_available,
                    "refined_markdown": refined_markdown,
                    "refined_meta": refined_meta,
                    "refine_estimate": refine_estimate,
                    "done_sections": done_sections, "total_sections": total_sections,
                    "visuals": fast_vis if fast_vis is not None else [],
                    "visuals_pending": bool(raw_markdown.strip() and fast_vis is None),
                    "topics": [name for name, _ in outline["topics"][:8]],
                    "clues": outline["clues"][:5], "oral_highlights": oral_highlights,
                })
                for chunk in chunks[1:]:
                    done_sections += len(re.findall(r"(?m)^#{2,3} +", chunk))
                    yield event({"stage": "section", "markdown_chunk": chunk,
                                 "done_sections": done_sections, "total_sections": total_sections})
                quiz = load_saved_lecture_quiz(job.directory, markdown)
                logic_map = load_saved_lecture_logic_map(job.directory, markdown)
                yield event({"stage": "addons", "quiz": quiz, "logic_map": logic_map})
                visuals = (build_note_visual_sections(job, course_id, key, raw_markdown) or []) if fast_vis is None else fast_vis
                keyframes_extracting = False
                if raw_markdown.strip() and not visuals:
                    keyframes_extracting = _maybe_schedule_keyframe_backfill(job, course_id, key)
                yield event({"stage": "done", "visuals": visuals, "keyframes_extracting": keyframes_extracting})

            return StreamingResponse(_stream_generator(), media_type="application/x-ndjson",
                                     headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
        visuals = build_note_visual_sections(job, course_id, key, raw_markdown)
        keyframes_extracting = False
        if raw_markdown.strip() and not visuals:
            keyframes_extracting = _maybe_schedule_keyframe_backfill(job, course_id, key)
        quiz = load_saved_lecture_quiz(job.directory, markdown)
        logic_map = load_saved_lecture_logic_map(job.directory, markdown)
        return {"recording_id": key, "course_id": course_id,
                "title": job.recording.title, "date": job.recording.date,
                "available": bool(markdown.strip()), "markdown": markdown, "visuals": visuals,
                "refined_available": refined_available,
                "refined_markdown": refined_markdown,
                "refined_meta": refined_meta,
                "refine_estimate": refine_estimate,
                "keyframes_extracting": keyframes_extracting,
                "quiz": quiz, "logic_map": logic_map,
                "topics": [name for name, _ in outline["topics"][:8]],
                "clues": outline["clues"][:5],
                "oral_highlights": oral_highlights}

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/refine-note")
    def generate_recording_refined_note(course_id: str, key: str, force: bool = False) -> dict:
        from ..ai_provider import AIProviderError, is_llm_configured
        from .note_images import rewrite_note_markdown_images
        from .refined_notes import generate_refined_lecture_note

        job = find_job(Path(manager.settings.data_dir), key, course_id)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        if not is_llm_configured(manager.settings, "refine"):
            raise HTTPException(status_code=400, detail="尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        path = job.directory / "notes.md"
        raw_markdown = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        if not raw_markdown.strip():
            raise HTTPException(status_code=422, detail="本讲尚未生成课堂笔记，请先生成笔记后再使用 AI 精编考点讲义。")
        platform_svc = getattr(app.state, "platform_service", None)
        try:
            res = generate_refined_lecture_note(
                job,
                course_id=course_id,
                recording_id=key,
                title=job.recording.title or key,
                raw_markdown=raw_markdown,
                platform_service=platform_svc,
                force=bool(force),
                settings=manager.settings,
            )
            rewritten = rewrite_note_markdown_images(job, course_id, key, res["markdown"])
            return {
                "ok": True,
                "refined_available": True,
                "refined_markdown": rewritten,
                "refined_meta": res["meta"],
                "points_charged": res["points_charged"],
                "reused": res["reused"],
                "refine_estimate": res["refine_estimate"],
            }
        except AIProviderError as exc:
            status = getattr(exc, "status_code", 502) or 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/lecture-logic-map")
    def generate_recording_lecture_logic_map(course_id: str, key: str, force: bool = False) -> dict:
        from ..ai_provider import AIProviderError, is_llm_configured
        from .lecture_logic_map import generate_lecture_logic_map
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        if not is_llm_configured(manager.settings, "logic_map"):
            raise HTTPException(status_code=400, detail="尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        path = job.directory / "notes.md"
        markdown = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        if not markdown.strip():
            raise HTTPException(status_code=422, detail="本讲尚未生成课堂笔记，请先生成笔记后再提炼逻辑线与思维导图。")
        platform_svc = getattr(app.state, "platform_service", None)
        try:
            return generate_lecture_logic_map(
                job.directory,
                course_id=course_id,
                recording_id=key,
                title=job.recording.title or key,
                markdown=markdown,
                platform_service=platform_svc,
                force=bool(force),
            )
        except AIProviderError as exc:
            status = getattr(exc, "status_code", 502) or 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/lecture-quiz")
    def generate_recording_lecture_quiz(course_id: str, key: str, force: bool = False) -> dict:
        from ..ai_provider import AIProviderError, is_llm_configured
        from .lecture_quiz import generate_lecture_quiz
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        if not is_llm_configured(manager.settings, "quiz"):
            raise HTTPException(status_code=400, detail="尚未配置文本大模型：请先在「模型配置」页面填写 API Key 与模型名称。")
        path = job.directory / "notes.md"
        markdown = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        if not markdown.strip():
            raise HTTPException(status_code=422, detail="本讲尚未生成课堂笔记，请先生成笔记后再开启随堂自测。")
        platform_svc = getattr(app.state, "platform_service", None)
        try:
            return generate_lecture_quiz(
                job.directory,
                course_id=course_id,
                recording_id=key,
                title=job.recording.title or key,
                markdown=markdown,
                platform_service=platform_svc,
                force=bool(force),
            )
        except AIProviderError as exc:
            status = getattr(exc, "status_code", 502) or 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/campus/courses/{course_id}/recordings/{key}/term-review")
    def recording_term_review(course_id: str, key: str) -> dict:
        from .transcript_review import review_status
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        return review_status(job, key)

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/term-review")
    def confirm_recording_term(course_id: str, key: str, body: TermReviewConfirmation) -> dict:
        from .transcript_review import confirm_rsa
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        with manager._lock:
            if manager._work and manager._work.state == "running":
                raise HTTPException(status_code=409, detail="当前有任务正在运行，请完成后再校对转写。")
            try:
                return confirm_rsa(job, segment_index=body.segment_index,
                                   transcript_sha256=body.transcript_sha256,
                                   confirmed_spoken_term=body.confirmed_spoken_term,
                                   confirmed=body.confirmed)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/existing-notion")
    def save_existing_notion_link(course_id: str, body: ExistingNotionLink) -> dict:
        from urllib.parse import urlsplit
        from .campus_catalog import campus_course

        value = body.url.strip()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not
            (parsed.hostname in {"notion.so", "www.notion.so", "app.notion.com"}
             or (parsed.hostname or "").endswith(".notion.site"))):
            raise HTTPException(status_code=400, detail="请输入已有 Notion 课程页的 HTTPS 链接。")
        root = Path(manager.settings.data_dir)
        try:
            campus_course(root, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        folder = next((path.parent for path in root.glob("*/course.json")
                       if json.loads(path.read_text("utf-8")).get("course_id") == course_id), None)
        if folder is None:
            raise HTTPException(status_code=404, detail="课程尚未同步。")
        path = folder / "course.json"
        metadata = json.loads(path.read_text("utf-8"))
        metadata["existing_notion_url"] = value
        temporary = path.with_name("course.json.tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=1), "utf-8")
        temporary.replace(path)
        return campus_course(root, course_id)

    @app.post("/api/campus/courses/{course_id}/materials/{material_index}/match")
    def campus_material_match(course_id: str, material_index: int, body: MaterialMatchRequest) -> dict:
        raise HTTPException(
            status_code=410,
            detail="已下线单讲课件关联功能，课程资料请在「课程资料」中直接查看和下载。",
        )

    @app.get("/api/campus/courses/{course_id}/materials/{material_index}/files/{file_index}")
    def campus_material_file(course_id: str, material_index: int, file_index: int):
        """Resolve a fresh Blackboard attachment URL with the user's local session."""
        from urllib.parse import quote
        from ..auth import get_session
        from ..lecture_source_download import _safe_webdav_url
        from ..materials import walk_materials
        from .campus_catalog import _courses, campus_course
        from .handbook_requirements import cache_handbook_pdf

        if not manager.settings.pku_username or not manager.settings.pku_password:
            raise HTTPException(status_code=409, detail="请先保存教学网账号。")
        root = Path(manager.settings.data_dir)
        try:
            course = campus_course(root, course_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        indexed = course.get("all_materials", [])
        if material_index < 0 or material_index >= len(indexed):
            raise HTTPException(status_code=404, detail="课件不存在，请更新目录。")
        row = indexed[material_index]
        if file_index < 0 or file_index >= len(row["files"]):
            raise HTTPException(status_code=404, detail="附件不存在，请更新目录。")
        client = get_session(username=manager.settings.pku_username,
                             password=manager.settings.pku_password)
        try:
            live, _ = walk_materials(client, course_id)
            matching = next((item for item in live
                             if item.path == row["path"] and item.title == row["title"]), None)
            if matching is None:
                raise HTTPException(status_code=404, detail="课件已变动，请更新课程目录。")
            filename = row["files"][file_index]
            asset = next((item for item in matching.attachments
                          if item.filename == filename), None)
            if asset is None:
                raise HTTPException(status_code=404, detail="附件已变动，请更新课程目录。")
            if not _safe_webdav_url(asset.url):
                raise HTTPException(status_code=502, detail="教学网返回了无效的附件地址。")
            response = client.get(asset.url)
            response.raise_for_status()
            if not _safe_webdav_url(str(getattr(response, "url", asset.url))):
                raise HTTPException(status_code=502, detail="教学网附件跳转到了无效地址。")
            cached = False
            if any(card["material_index"] == material_index and card["file_index"] == file_index
                   and card["material_path"] == row["path"] and card["filename"] == filename
                   for card in course.get("handbook_reviews", [])):
                folder = next((path for path, key, _, _ in _courses(root)
                               if key == course_id), None)
                if folder is not None:
                    try:
                        cached = cache_handbook_pdf(folder / "materials", row, filename,
                                                    response.content)
                    except OSError:
                        logger.warning("course handbook cache failed")
            return StreamingResponse(iter((response.content,)),
                media_type=response.headers.get("content-type", "application/octet-stream"),
                headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                         "X-Handbook-Cached": "true" if cached else "false"})
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("campus material download failed")
            raise HTTPException(status_code=502, detail="暂时无法读取教学网附件，请稍后再试。") from exc
        finally:
            client.close()

    @app.get("/api/campus/notion/parents")
    def notion_parents_endpoint(selected_only: bool = False) -> dict:
        from .notion_home import available_parents, saved_home
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            detail = ("演示模式只模拟 Notion 连接，不会读取真实页面；请退出演示模式后授权 Notion。"
                      if demo_connection else "请先连接 Notion。")
            raise HTTPException(status_code=409, detail=detail)
        try:
            from .notion_workspace import saved_workspace, read_projection, _verify_home, _verify_selected_parent
            root = Path(manager.settings.data_dir)
            home_sync = {'status': 'current'}
            workspace = saved_workspace(root)
            if selected_only and workspace:
                # Routine startup reads the exact configured destination.
                # The explicit picker retains complete search and projection
                # hydration; navigation must not race a writer with a full
                # scan of every course and lecture.
                from ..notion import NotionClient
                from .notion_home import _title
                with NotionClient(token) as client:
                    _verify_home(client.get_page(workspace['id']), workspace['id'], workspace['parent_id'])
                    parent = _verify_selected_parent(client, workspace['parent_id'])
                return {'pages': [{'id': parent['id'], 'title': _title(parent), 'url': parent.get('url', '')}],
                        'home': saved_home(root), 'workspace': workspace,
                        'home_sync': home_sync}
            if workspace.get("active"):
                read_projection(manager.settings, tolerate_partial=True)
            home = saved_home(Path(manager.settings.data_dir))
            from .notion_workspace import saved_workspace
            return {"pages": available_parents(token), "home": home,
                    "workspace": saved_workspace(Path(manager.settings.data_dir)), 'home_sync': home_sync}
        except Exception as exc:
            logger.exception("Notion parent listing failed")
            raise HTTPException(status_code=502, detail="Notion 页面暂时无法读取，请稍后重试。") from exc

    @app.post("/api/campus/notion/workspace/move")
    def notion_home_move_endpoint(body: HomeRequest) -> dict:
        from .notion_workspace import move_workspace
        token = getattr(manager.settings, "notion_token", "")
        if not token:
            raise HTTPException(status_code=409, detail="请先连接 Notion。")
        try:
            home = move_workspace(manager.settings, body.parent_page_id)
            home["profile_sync"] = {"available": True, "status": "pushed", "revision": home.get("profile_revision", 0)}
            return home
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion learning home move failed")
            raise HTTPException(status_code=502, detail="学习主页没有移动成功，请检查 Notion 页面权限后重试。") from exc

    @app.post("/api/campus/notion/workspace")
    def notion_workspace_endpoint(body: HomeRequest) -> dict:
        from .notion_workspace import sync_workspace
        if manager.state().get("state") == "running":
            raise HTTPException(status_code=409, detail="请等待当前任务完成，再同步新版主页。")
        try:
            options = {'replace_existing': True} if body.replace_existing else {}
            return sync_workspace(manager.settings, body.parent_page_id, **options)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion version-two workspace sync failed")
            raise HTTPException(status_code=502, detail="新版主页未完成验证，请稍后重试；旧主页已保留。") from exc

    @app.get("/api/recordings")
    def recordings() -> dict:
        root = Path(manager.settings.data_dir)
        configured = bool(getattr(manager.settings, "pku_username", "") and getattr(manager.settings, "pku_password", ""))
        return {
            "items": list_recordings(root),
            "campus_configured": configured,
            **_campus_vault_status(root, configured),
        }

    @app.post("/api/recordings/{recording_key}/summary-acceptance")
    def summary_acceptance_endpoint(recording_key: str, body: SummaryAcceptanceRequest) -> dict:
        from .summary_acceptance import accept_published
        from ..notion import NotionClient
        job = find_job(Path(manager.settings.data_dir), recording_key)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到对应教学网讲次。")
        if manager.state().get("state") == "running":
            raise HTTPException(status_code=409, detail="请等待当前任务完成后审阅总结。")
        try:
            with NotionClient(manager.settings.notion_token) as client:
                return accept_published(job, client=client, notes_sha256=body.notes_sha256, confirmed=body.confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Notion summary acceptance verification failed")
            raise HTTPException(status_code=502, detail="Notion 验收未能验证，请稍后重试。") from exc

    @app.post("/api/recordings/credentials")
    def credentials(body: CampusCredentials, request: Request = None):
        from ..auth import CampusLoginRejected, clear_session_cache, get_session

        raw_username = body.username.strip()
        if "@" in raw_username and raw_username.lower().endswith(("@stu.pku.edu.cn", "@pku.edu.cn", "@alumni.pku.edu.cn")):
            username = raw_username.split("@", 1)[0].strip()
        else:
            username = raw_username
        if not username:
            raise HTTPException(status_code=400, detail="请输入教学网账号。")
        if not credentials_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="正在验证教学网账号，请等待本次验证完成。")
        try:
            binding = manager._account_binding()
            previous_username = getattr(manager.settings, "pku_username", "").strip()
            current = manager.state()
            if (
                previous_username
                and previous_username != username
                and (
                    current.get("state") == "running"
                    or current.get("background_sync", {}).get("state") == "running"
                    or current.get("batch_organize", {}).get("state") == "running"
                )
            ):
                raise HTTPException(status_code=409, detail="请等待当前课程任务结束后再更换教学网账号。")
            portal_ids: set[str] = set()
            try:
                clear_session_cache(username)
                client = get_session(username=username, password=body.password)
                try:
                    if _local_course_ids_on_disk(Path(manager.settings.data_dir)):
                        from ..discover import discover_courses, select_courses
                        portal_ids = {
                            c.course_id
                            for c in select_courses(discover_courses(client), manager.settings)
                            if getattr(c, "course_id", "")
                        }
                except Exception:
                    portal_ids = set()
                finally:
                    client.close()
            except CampusLoginRejected as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from None
            except Exception as exc:
                # Upstream bodies/URLs may contain credentials or OAuth tokens.
                logger.warning("campus credential verification failed type=%s", type(exc).__name__)
                raise HTTPException(status_code=503, detail="暂时无法连接教学网验证账号，请检查网络后重试；这次填写的账号尚未保存。") from None
            with manager._lock:
                if binding != manager._account_binding():
                    raise HTTPException(status_code=409, detail="账号或连接已改变，请重新验证教学网账号。")
                previous_username = getattr(manager.settings, "pku_username", "").strip()
                current = manager.state()
                if (
                    previous_username
                    and previous_username != username
                    and (
                        current.get("state") == "running"
                        or current.get("background_sync", {}).get("state") == "running"
                        or current.get("batch_organize", {}).get("state") == "running"
                    )
                ):
                    raise HTTPException(status_code=409, detail="课程任务已开始，请等待任务结束后再保存账号。")
                raw_mode = str(getattr(body, "persist_mode", "") or "").strip().lower()
                persist_mode = "ephemeral" if raw_mode == "ephemeral" else "encrypted_vault"
                try:
                    if persist_mode == "ephemeral":
                        write_env_values(env_file_path(manager.settings),
                                         {"PKU_USERNAME": username, "PKU_PASSWORD": ""})
                    else:
                        write_env_values(env_file_path(manager.settings),
                                         {"PKU_USERNAME": username, "PKU_PASSWORD": body.password})
                except OSError:
                    raise HTTPException(status_code=503, detail="账号验证成功，但本机配置无法保存，请检查磁盘空间和写入权限后重试。") from None
                manager.settings.pku_username = username
                manager.settings.pku_password = body.password
                if previous_username and previous_username != username:
                    clear_session_cache(previous_username)
                root_dir = Path(manager.settings.data_dir)
                local_ids = _local_course_ids_on_disk(root_dir)
                if local_ids:
                    if previous_username and previous_username != username:
                        stale = sorted(local_ids - portal_ids) if portal_ids else sorted(local_ids)
                        _write_catalog_diff(
                            root_dir,
                            out_of_sync=bool(stale),
                            reason="account_changed",
                            stale_course_ids=stale,
                        )
                        from .. import catalog_snapshots as catalog
                        _portal_status_write(catalog.read_status, root_dir, False, "account_changed")
                    elif portal_ids and (local_ids - portal_ids):
                        _write_catalog_diff(
                            root_dir,
                            out_of_sync=True,
                            reason="courses_changed",
                            stale_course_ids=sorted(local_ids - portal_ids),
                        )
                    elif portal_ids and local_ids == portal_ids:
                        _write_catalog_diff(root_dir, out_of_sync=False)
            result: dict = {
                "configured": True,
                "verified": True,
            }
            if persist_mode == "ephemeral":
                result["persist_mode"] = persist_mode
                result["zero_disk_password"] = True
            return result
        finally:
            credentials_lock.release()

    @app.delete("/api/recordings/credentials")
    def clear_credentials(request: Request = None):
        from ..auth import clear_session_cache
        cleared_users: list[str] = []
        with manager._lock:
            prev_user = getattr(manager.settings, "pku_username", "").strip()
            if prev_user:
                cleared_users.append(prev_user)
            manager.settings.pku_password = ""
            try:
                write_env_values(env_file_path(manager.settings), {"PKU_PASSWORD": ""})
            except OSError:
                pass
        for acct in dict.fromkeys(cleared_users):
            clear_session_cache(acct)
        return JSONResponse({
            "ok": True,
            "configured": False,
            "message": "已安全清除已保存的教学网密码，已整理的课程与笔记均保留。",
        })

    @app.post("/api/recordings/sync")
    def sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            course_id = body.course_id.strip() if body else ""
            if body and body.scope not in {"", "background"}:
                raise ValueError("未知课程更新范围。")
            if course_id and body and (body.resume or body.scope):
                raise ValueError("单课更新与后台恢复不能混用。")
            if course_id:
                return manager.start_sync(course_id=course_id)
            return manager.start_background_sync(resume=body.resume if body else False)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/auto-sync")
    def campus_auto_sync_endpoint(body: dict | None = None) -> dict:
        from .campus_catalog import campus_courses
        from .course_tracker import load_auto_track_config
        raw_age = (body or {}).get("min_age_seconds")
        if raw_age is None:
            raw_age = (body or {}).get("max_age_seconds")
        min_age = int(raw_age) if raw_age is not None else 900
        if (
            not getattr(manager.settings, "pku_username", "")
            or not getattr(manager.settings, "pku_password", "")
            or getattr(manager.settings, "pku_password", "") == "demo-readonly"
        ):
            return {"triggered": False, "started": False, "reason": "unconfigured"}
        current = manager.state()
        if (current.get("state") == "running"
                or current.get("background_sync", {}).get("state") == "running"
                or current.get("batch_organize", {}).get("state") == "running"):
            return {"triggered": False, "started": False, "reason": "busy"}
        root = Path(manager.settings.data_dir)
        payload = campus_courses(root)
        courses = payload.get("courses") if isinstance(payload, dict) else list(payload or [])
        sync_status = _catalog_sync_status(root, courses)
        if sync_status.get("out_of_sync"):
            return {"triggered": False, "started": False, "reason": "out_of_sync"}
        now_mono = time.monotonic()
        last_auto = float(getattr(manager, "_last_auto_sync_at", 0.0) or 0.0)
        if min_age > 0 and last_auto and (now_mono - last_auto) < min(max(60, min_age), 300):
            return {"triggered": False, "started": False, "reason": "throttled"}
        auto_track_cfg = load_auto_track_config(root)
        if auto_track_cfg.get("enabled") and auto_track_cfg.get("tracked_course_ids"):
            res = manager.trigger_auto_track_if_due(
                trigger="page_open",
                min_age_seconds=None,
            )
            if res.get("triggered"):
                manager._last_auto_sync_at = now_mono
            return res
        if courses:
            newest_age: float | None = None
            now_utc = datetime.now(timezone.utc)
            all_synced = True
            for c in courses:
                raw_ts = str(c.get("synced_at") or "").strip()
                if not raw_ts or c.get("read_only"):
                    all_synced = False
                    continue
                try:
                    ts = datetime.fromisoformat(raw_ts)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = max(0.0, (now_utc - ts).total_seconds())
                    newest_age = age if newest_age is None else min(newest_age, age)
                except ValueError:
                    all_synced = False
            if all_synced and newest_age is not None and newest_age < min_age:
                return {"triggered": False, "started": False, "reason": "fresh", "age_seconds": int(newest_age)}
        try:
            manager._last_auto_sync_at = now_mono
            task_state = manager.start_background_sync(resume=False)
            with manager._lock:
                if isinstance(manager._background, dict):
                    manager._background["auto"] = True
            return {"triggered": True, "started": True, "task": task_state}
        except ValueError as exc:
            return {"triggered": False, "started": False, "reason": "busy", "detail": str(exc)}

    @app.post("/api/recordings/cancel-sync")
    def cancel_sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            if body and body.scope == "background":
                return manager.cancel_background_sync()
            if body and (body.scope or body.course_id or body.resume):
                raise ValueError("未知取消范围。")
            # Legacy no-body cancellation still finds a sole background sync.
            if manager.state()["state"] != "running" and manager.background_state()["state"] == "running":
                return manager.cancel_background_sync()
            return manager.cancel_sync()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/recordings/task")
    async def task(scope: str = "") -> dict:
        # This is a bounded in-memory snapshot. Keep progress/close observers
        # independent of slow file or service reads occupying HTTP workers.
        # The manager still returns the actual state under its existing lock.
        if scope == "background":
            return manager.background_state()
        if scope:
            raise HTTPException(status_code=400, detail="未知任务范围。")
        value = manager.state()
        gate = getattr(app.state, "exercise_job_gate", None)
        if gate is not None:
            owner = gate.current()
            value["exercise_job"] = {"state": "running" if owner else "idle", "kind": owner or ""}
        return value

    @app.post("/api/campus/notion/sync")
    def notion_catalog_sync(body: CourseRefreshRequest | None = None) -> dict:
        try:
            if body and body.scope not in {"", "background"}:
                raise ValueError("未知课程更新范围。")
            if body and body.course_id and (body.scope or body.resume):
                raise ValueError("单课更新与后台恢复不能混用。")
            if body and body.course_id:
                return manager.start_sync(body.course_id.strip())
            return manager.start_background_sync(resume=body.resume if body else False)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def require_course_summary_tier() -> None:
        return

    @app.post("/api/campus/notion/summaries")
    def notion_summaries() -> dict:
        require_course_summary_tier()
        try:
            return manager.start_course_summaries()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/summary")
    def campus_course_summary(course_id: str) -> dict:
        require_course_summary_tier()
        try:
            return manager.start_course_summaries(course_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/duration")
    def campus_recording_duration(course_id: str, key: str) -> dict:
        """Read player metadata on demand without fetching video or charging quota."""
        from ..auth import get_session
        from ..recordings import resolve_media

        if not manager.settings.pku_username or not manager.settings.pku_password:
            raise HTTPException(status_code=409, detail="请先保存教学网账号。")
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or job.recording.course_id != course_id:
            raise HTTPException(status_code=404, detail="这节录像不属于该课程。")
        client = None
        try:
            client = get_session(username=manager.settings.pku_username,
                                 password=manager.settings.pku_password)
            resolve_media(client, job.recording)
            duration = max(0, int(job.recording.duration_seconds or 0))
            if duration:
                job.directory.mkdir(parents=True, exist_ok=True)
                path = job.directory / "duration.json"
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps({"duration_seconds": duration}), "utf-8")
                temporary.replace(path)
            return {"duration_seconds": duration}
        except Exception as exc:
            logger.exception("recording duration lookup failed")
            raise HTTPException(status_code=502, detail="暂时无法读取教学网录像时长，请稍后重试。") from exc
        finally:
            if client is not None:
                client.close()

    @app.post("/api/campus/courses/{course_id}/recordings/{key}/process")
    def campus_process(course_id: str, key: str, body: CampusProcessRequest | None = None) -> dict:
        from .task_queue import DrainingError

        try:
            return manager.start_campus_process(
                key, course_id, direct_oss=body.direct_oss if body else False,
                regenerate=body.regenerate if body else False,
                reuse_existing=body.reuse_existing if body else False,
                allow_partial_quota=bool(body.allow_partial_quota) if body else False,
                **({"auto_refine": True} if body and body.auto_refine else {}),
                **({"notes_strategy":body.notes_strategy} if body and body.notes_strategy is not None else {}))
        except DrainingError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/recordings/{key}/download")
    def download(key: str) -> dict:
        try:
            return manager.start_download(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/recordings/{key}/video")
    def video_file(key: str):
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or not job.video.is_file():
            raise HTTPException(status_code=404, detail="这条录像尚未下载到本机。")
        filename = safe_name(f"{job.recording.date}_{job.recording.title}", "recording") + ".mp4"
        return FileResponse(job.video, media_type="video/mp4", filename=filename)

    @app.get("/api/recordings/{key}/preview")
    def preview_video(key: str):
        job = find_job(Path(manager.settings.data_dir), key)
        if job is None or not job.video.is_file():
            raise HTTPException(status_code=404, detail="这条录像尚未下载到本机。")
        filename = safe_name(f"{job.recording.date}_{job.recording.title}", "recording") + ".mp4"
        return FileResponse(job.video, media_type="video/mp4", filename=filename,
                            content_disposition_type="inline")

    @app.get("/api/recordings/local-storage")
    def get_local_storage() -> dict:
        return local_storage_summary(manager.settings)

    @app.post("/api/recordings/local-storage")
    def update_local_storage(body: LocalStorageConfigRequest) -> dict:
        updates: dict[str, str] = {}
        with manager._lock:
            if body.save_recordings_locally is not None:
                save_val = bool(body.save_recordings_locally)
                manager.settings.save_recordings_locally = save_val
                updates["SAVE_RECORDINGS_LOCALLY"] = "true" if save_val else "false"
            if body.recordings_download_dir is not None:
                raw_dir = body.recordings_download_dir.strip()
                if raw_dir:
                    try:
                        resolved_dir = Path(raw_dir).expanduser().resolve()
                        resolved_dir.mkdir(parents=True, exist_ok=True)
                        raw_dir = str(resolved_dir)
                    except OSError as exc:
                        raise HTTPException(status_code=400, detail=f"无法创建或访问所选录像下载目录：{exc}") from exc
                manager.settings.recordings_download_dir = raw_dir
                updates["RECORDINGS_DOWNLOAD_DIR"] = raw_dir
            if updates:
                try:
                    write_env_values(env_file_path(manager.settings), updates)
                except OSError:
                    logger.warning("failed to persist local storage settings to .env")
        return local_storage_summary(manager.settings)

    @app.post("/api/recordings/local-storage/pick-directory")
    def pick_local_storage_dir() -> dict:
        try:
            return pick_local_storage_directory(manager.settings)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"无法打开文件夹选择器：{exc}") from exc

    @app.post("/api/recordings/local-storage/open")
    def open_local_storage(body: LocalStorageOpenRequest | None = None) -> dict:
        try:
            return open_local_storage_path(
                manager.settings,
                recording_id_key=(body.recording_id if body else ""),
                raw_path=(body.path if body else ""),
                target_kind=(body.target if body else ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"无法打开本地目录：{exc}") from exc

    @app.get("/api/campus/export-notes")
    def export_local_notes_endpoint():
        from fastapi.responses import Response

        try:
            zip_bytes, filename = build_local_notes_export_zip(manager.settings)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"导出本地笔记失败：{exc}") from exc
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    def _pdf_attachment_response(pdf_bytes: bytes, raw_stem: str):
        from urllib.parse import quote
        from fastapi.responses import Response

        clean_stem = re.sub(r'[\\/:*?"<>|\r\n]+', "-", (raw_stem or "PKU-All-in-One-笔记").strip()).strip("-.") or "PKU-All-in-One-笔记"
        ascii_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", clean_stem).strip("-.") or "PKU-All-in-One-Notes"
        ascii_name = f"{ascii_stem}.pdf"
        utf8_name = quote(f"{clean_stem}.pdf")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
                "Cache-Control": "no-store",
            },
        )

    def _zip_attachment_response(zip_bytes: bytes, raw_filename: str):
        from urllib.parse import quote
        from fastapi.responses import Response

        raw_stem = (raw_filename or "笔记.zip").removesuffix(".zip").removesuffix(".ZIP")
        clean_stem = re.sub(r'[\\/:*?"<>|\r\n]+', "-", raw_stem.strip()).strip("-.") or "笔记"
        ascii_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", clean_stem).strip("-.") or "PKU-Notes"
        ascii_name = f"{ascii_stem}.zip"
        utf8_name = quote(f"{clean_stem}.zip")
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}",
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/campus/export-pdf")
    def export_note_or_batch_pdf_endpoint(body: dict | None = None):
        from .pdf_export import (
            WATERMARK_SITE_NAME,
            WATERMARK_SITE_URL,
            NotePdfSection,
            build_watermarked_notes_pdf,
            collect_course_pdf_sections,
            load_recording_visual_assets,
        )

        payload = body if isinstance(body, dict) else {}
        root = Path(manager.settings.data_dir)
        course_id = str(payload.get("course_id") or "").strip()
        recording_id_val = str(payload.get("recording_id") or "").strip()
        course_title = str(payload.get("course_title") or course_id or "北大课程笔记").strip()
        doc_title = str(payload.get("doc_title") or "课堂笔记").strip()
        markdown = str(payload.get("markdown") or "")
        custom_filename = str(payload.get("filename") or "").strip()

        if markdown.strip():
            imgs, vsecs = (
                load_recording_visual_assets(root, course_id, recording_id_val, markdown)
                if recording_id_val
                else ([], [])
            )
            sections = [
                NotePdfSection(
                    course_title=course_title,
                    doc_title=doc_title,
                    subtitle=f"导出自 {WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL})",
                    markdown=markdown,
                    images=imgs,
                    visual_sections=vsecs,
                )
            ]
            pdf_bytes = build_watermarked_notes_pdf(
                sections, document_title=f"{course_title} · {doc_title}"
            )
            stem = custom_filename.removesuffix(".pdf").removesuffix(".md") if custom_filename else f"{course_title}-{doc_title}"
            return _pdf_attachment_response(pdf_bytes, stem)

        sections = collect_course_pdf_sections(
            root, course_id=course_id, recording_id=recording_id_val
        )
        if not sections:
            raise HTTPException(status_code=404, detail="当前还没有可导出的课程或课堂笔记。")
        title_label = f"{sections[0].course_title} · 课程笔记合集" if course_id else "PKU All in One · 全部课程笔记合集"
        pdf_bytes = build_watermarked_notes_pdf(sections, document_title=title_label)
        stem = (
            custom_filename.removesuffix(".pdf").removesuffix(".md")
            if custom_filename
            else (f"{sections[0].course_title}-笔记合集-PKU-All-in-One" if course_id else f"PKU-All-in-One-全部课程笔记合集-{datetime.now().strftime('%Y%m%d')}")
        )
        return _pdf_attachment_response(pdf_bytes, stem)

    @app.get("/api/campus/courses/{course_id}/export-notes-zip")
    def export_course_notes_zip_endpoint(course_id: str, include_summary: bool = True):
        from .pdf_export import build_notes_pdf_zip

        root = Path(manager.settings.data_dir)
        zip_bytes, zip_filename = build_notes_pdf_zip(
            root,
            course_id=course_id.strip(),
            include_summary=bool(include_summary),
        )
        if not zip_bytes:
            raise HTTPException(status_code=404, detail="该课程当前还没有可导出的总结或课堂笔记。")
        return _zip_attachment_response(zip_bytes, zip_filename)

    @app.get("/api/campus/export-notes-zip")
    def export_all_notes_zip_endpoint(course_id: str = "", include_summary: bool = True):
        from .pdf_export import build_notes_pdf_zip

        root = Path(manager.settings.data_dir)
        zip_bytes, zip_filename = build_notes_pdf_zip(
            root,
            course_id=course_id.strip(),
            include_summary=bool(include_summary),
        )
        if not zip_bytes:
            raise HTTPException(status_code=404, detail="当前还没有可导出的课程笔记，请先同步课程或生成笔记。")
        return _zip_attachment_response(zip_bytes, zip_filename)

    @app.get("/api/campus/courses/{course_id}/export-pdf")
    def export_course_batch_pdf_endpoint(
        course_id: str,
        recording_id: str = "",
        include_summary: bool = True,
        format: str = "pdf",
    ):
        from .pdf_export import build_notes_pdf_zip, build_watermarked_notes_pdf, collect_course_pdf_sections

        root = Path(manager.settings.data_dir)
        if str(format or "").strip().lower() == "zip" and not recording_id.strip():
            zip_bytes, zip_filename = build_notes_pdf_zip(
                root,
                course_id=course_id.strip(),
                include_summary=bool(include_summary),
            )
            if not zip_bytes:
                raise HTTPException(status_code=404, detail="该课程当前还没有可导出的总结或课堂笔记。")
            return _zip_attachment_response(zip_bytes, zip_filename)
        sections = collect_course_pdf_sections(
            root,
            course_id=course_id,
            recording_id=recording_id.strip(),
            include_summary=bool(include_summary),
        )
        if not sections:
            raise HTTPException(status_code=404, detail="该课程当前还没有可导出的总结或课堂笔记。")
        course_title = sections[0].course_title or course_id
        if recording_id.strip():
            doc_title = sections[0].doc_title or recording_id
            pdf_bytes = build_watermarked_notes_pdf(sections, document_title=f"{course_title} · {doc_title}")
            return _pdf_attachment_response(pdf_bytes, f"{course_title}-{doc_title}")
        pdf_bytes = build_watermarked_notes_pdf(sections, document_title=f"{course_title} · 全课笔记合集")
        return _pdf_attachment_response(pdf_bytes, f"{course_title}-全课笔记合集-PKU-All-in-One")

    @app.get("/api/campus/export-notes-pdf")
    def export_all_notes_pdf_endpoint(course_id: str = "", include_summary: bool = True, format: str = "pdf"):
        from .pdf_export import build_notes_pdf_zip, build_watermarked_notes_pdf, collect_course_pdf_sections

        root = Path(manager.settings.data_dir)
        if str(format or "").strip().lower() == "zip":
            zip_bytes, zip_filename = build_notes_pdf_zip(
                root,
                course_id=course_id.strip(),
                include_summary=bool(include_summary),
            )
            if not zip_bytes:
                raise HTTPException(status_code=404, detail="当前还没有可导出的课程笔记，请先同步课程或生成笔记。")
            return _zip_attachment_response(zip_bytes, zip_filename)
        sections = collect_course_pdf_sections(root, course_id=course_id.strip(), include_summary=bool(include_summary))
        if not sections:
            raise HTTPException(status_code=404, detail="当前还没有可导出的课程笔记，请先同步课程或生成笔记。")
        if course_id.strip():
            ctitle = sections[0].course_title or course_id
            pdf_bytes = build_watermarked_notes_pdf(sections, document_title=f"{ctitle} · 全课笔记合集")
            return _pdf_attachment_response(pdf_bytes, f"{ctitle}-全课笔记合集-PKU-All-in-One")
        pdf_bytes = build_watermarked_notes_pdf(sections, document_title="PKU All in One · 全部课程笔记合集")
        return _pdf_attachment_response(
            pdf_bytes, f"PKU-All-in-One-全部课程笔记合集-{datetime.now().strftime('%Y%m%d')}"
        )


    @app.delete("/api/recordings/local-storage/{key}")
    def remove_local_recording_endpoint(key: str) -> dict:
        if manager.state().get("state") == "running" and manager.state().get("recording_id") == key:
            raise HTTPException(status_code=409, detail="这节录像当前正在处理中，请等待任务完成后再删除。")
        try:
            return delete_local_recording(manager.settings, key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/recordings/local-storage/clear")
    def clear_local_recordings_endpoint() -> dict:
        if manager.state().get("state") == "running":
            raise HTTPException(status_code=409, detail="请等待当前任务完成后再清理本地录像。")
        return clear_all_local_recordings(manager.settings)
