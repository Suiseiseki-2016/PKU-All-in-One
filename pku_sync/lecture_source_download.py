"""Bounded, on-demand Teaching Network attachments for one lecture note.

The catalog refresh only saves metadata.  Downloading happens when a student
actively organizes a lecture, never while browsing or refreshing all courses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import unicodedata
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .pipeline import RecordingJob
from .store import safe_name
from .media import extract_keyframes

logger = logging.getLogger(__name__)

SUPPORTED = frozenset({".pdf", ".pptx", ".docx"})
LOCAL_UPLOAD_SUPPORTED = SUPPORTED | {".md", ".txt"}
MAX_FILES = 6
KEYFRAME_MATCHES_FILENAME = "keyframe-matches.json"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
_LOCAL_UPLOAD_LOCK = threading.RLock()
_ASSIGNMENT_WORDS = ("作业", "考试", "测试", "测验", "提交")


def _override_lecture_ids(override) -> list[str]:
    values = [override] if isinstance(override, str) else override if isinstance(override, list) else []
    return list(dict.fromkeys(value for value in values
                             if isinstance(value, str) and value and value not in {"ignore", "unassigned"}))


def _override_matches_lecture(override, recording_key: str) -> bool:
    return recording_key in _override_lecture_ids(override)


def _local_upload_dir(job: RecordingJob, recording_key: str) -> Path:
    cache_key = hashlib.sha256(recording_key.encode("utf-8")).hexdigest()[:24]
    folder = job.course_dir / "materials" / ".lecture-uploads" / cache_key
    if not folder.resolve().is_relative_to((job.course_dir / "materials").resolve()):
        raise ValueError("本讲课件目录无效。")
    return folder


def _looks_like_text_note(data: bytes) -> bool:
    if not data or b"\x00" in data or data.startswith((b"%PDF-", b"PK\x03\x04", b"MZ")):
        return False
    try:
        return bool(data.decode("utf-8").strip())
    except UnicodeDecodeError:
        return False


def _looks_like_local_upload(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return _looks_like_text_note(path.read_bytes())
    return suffix in SUPPORTED and _looks_like_document(path)


def _verified_local_uploads(job: RecordingJob, recording_key: str) -> list[tuple[dict, Path]]:
    folder = _local_upload_dir(job, recording_key)
    rows = _read_json(folder / "index.json", [])
    verified, total = [], 0
    for row in rows if isinstance(rows, list) else []:
        if (not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("id", "")))
                or not isinstance(row.get("filename"), str)
                or row["filename"] != safe_name(row["filename"])
                or Path(row["filename"]).suffix.lower() not in LOCAL_UPLOAD_SUPPORTED):
            continue
        path = folder / (row["id"] + "-" + row["filename"])
        try:
            size = path.stat().st_size
            if (path.is_symlink() or not path.resolve().is_relative_to(folder.resolve())
                    or not path.is_file() or not 0 < size <= MAX_FILE_BYTES
                    or size != row.get("size") or total + size > MAX_TOTAL_BYTES
                    or hashlib.sha256(path.read_bytes()).hexdigest() != row.get("sha256")
                    or not _looks_like_local_upload(path)):
                continue
        except (OSError, ValueError):
            continue
        verified.append((row, path.resolve()))
        total += size
        if len(verified) >= MAX_FILES:
            break
    return verified


def list_local_lecture_materials(job: RecordingJob, recording_key: str) -> list[dict]:
    with _LOCAL_UPLOAD_LOCK:
        return [{key: row.get(key) for key in ("id", "filename", "size", "uploaded_at")}
                for row, _ in _verified_local_uploads(job, recording_key)]


def local_upload_source_paths(job: RecordingJob, recording_key: str) -> list[Path]:
    with _LOCAL_UPLOAD_LOCK:
        return [path for _, path in _verified_local_uploads(job, recording_key)]


def save_local_lecture_materials(job: RecordingJob, recording_key: str,
                                uploads: list[tuple[str, bytes]]) -> list[dict]:
    with _LOCAL_UPLOAD_LOCK:
        existing = _verified_local_uploads(job, recording_key)
        if not uploads or len(existing) + len(uploads) > MAX_FILES:
            raise ValueError(f"每讲最多上传 {MAX_FILES} 份课件。")
        if sum(row["size"] for row, _ in existing) + sum(len(data) for _, data in uploads) > MAX_TOTAL_BYTES:
            raise ValueError("本讲上传课件总大小不能超过 40 MB。")
        folder = _local_upload_dir(job, recording_key)
        folder.mkdir(parents=True, exist_ok=True)
        created, rows = [], [row for row, _ in existing]
        try:
            for name, data in uploads:
                filename = safe_name(name.replace("\\", "/").rsplit("/", 1)[-1])
                if Path(filename).suffix.lower() not in LOCAL_UPLOAD_SUPPORTED:
                    raise ValueError("仅支持 .pdf、.pptx、.docx、.md、.txt 课件或笔记。")
                if not 0 < len(data) <= MAX_FILE_BYTES:
                    raise ValueError("每份课件大小须在 1 字节至 20 MB 之间。")
                material_id = uuid.uuid4().hex
                path = folder / (material_id + "-" + filename)
                created.append(path)
                path.write_bytes(data)
                if not _looks_like_local_upload(path):
                    raise ValueError("课件内容与文件格式不符。")
                rows.append({"id": material_id, "filename": filename, "size": len(data),
                             "uploaded_at": datetime.now(timezone.utc).isoformat(),
                             "sha256": hashlib.sha256(data).hexdigest()})
            _atomic_json(folder / "index.json", rows)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return list_local_lecture_materials(job, recording_key)


def remove_local_lecture_material(job: RecordingJob, recording_key: str, material_id: str) -> list[dict]:
    with _LOCAL_UPLOAD_LOCK:
        existing = _verified_local_uploads(job, recording_key)
        found = next((path for row, path in existing if row["id"] == material_id), None)
        if found is None:
            raise ValueError("找不到这份本地课件。")
        _atomic_json(_local_upload_dir(job, recording_key) / "index.json",
                     [row for row, _ in existing if row["id"] != material_id])
        found.unlink(missing_ok=True)
        return list_local_lecture_materials(job, recording_key)


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return fallback


def _explicit_date(value: str, day: date) -> bool:
    normalized = unicodedata.normalize("NFKC", value or "")
    # Keep the boundaries between year, month and day.  Removing every
    # non-digit could accidentally combine a week number and a content ID.
    pattern = (rf"(?<!\d){day.year}(?:[-./_年]\s*)?0?{day.month}"
               rf"(?:[-./_月]\s*)?0?{day.day}(?:日)?(?!\d)")
    return re.search(pattern, normalized) is not None


def _single_lecture_that_day(job: RecordingJob) -> bool:
    rows = _read_json(job.course_dir / "recordings" / "index.json", [])
    return isinstance(rows, list) and sum(
        isinstance(row, dict) and str(row.get("recorded_at") or "").startswith(job.recording.date)
        for row in rows
    ) == 1


def _candidate_attachments(job: RecordingJob, recording_key: str) -> list[tuple[dict, dict]]:
    material_dir = job.course_dir / "materials"
    rows = _read_json(material_dir / "index.json", [])
    matches = _read_json(material_dir / "matches.json", {})
    if not isinstance(rows, list):
        return []
    if not isinstance(matches, dict):
        matches = {}
    try:
        day = date.fromisoformat(job.recording.date)
    except ValueError:
        return []
    unique_day = _single_lecture_that_day(job)
    chosen = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        path = str(row.get("path") or "")
        kind = str(row.get("kind") or "")
        if kind in {"作业", "考试", "测试"} or any(word in title for word in _ASSIGNMENT_WORDS):
            continue
        override = matches.get(path)
        manual = _override_matches_lecture(override, recording_key)
        if override is not None and not manual:
            continue
        assets = row.get("attachments") or []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("filename") or "")
            suffix = Path(filename).suffix.lower()
            if not filename or (suffix and suffix not in SUPPORTED):
                continue
            dated = unique_day and any(
                _explicit_date(value, day) for value in (title, path, filename)
            )
            if manual or dated:
                chosen.append((row, asset))
    return chosen


def _safe_webdav_url(url: str) -> str:
    """Accept only the course host's WebDAV attachment paths."""
    if not isinstance(url, str) or "\\" in url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme or parts.netloc:
            if (parts.scheme != "https" or parts.hostname != "course.pku.edu.cn"
                    or parts.port not in (None, 443) or parts.username or parts.password):
                return ""
    except ValueError:
        return ""
    decoded_path = unquote(parts.path)
    if (not decoded_path.startswith("/bbcswebdav/") or "\\" in decoded_path
            or ".." in decoded_path.split("/")):
        return ""
    if parts.fragment:
        return ""
    return url if parts.scheme else "https://course.pku.edu.cn" + url


def _looks_like_document(path: Path, suffix: str | None = None) -> bool:
    suffix = (suffix or path.suffix).lower()
    with path.open("rb") as handle:
        magic = handle.read(5)
    if suffix == ".pdf":
        return magic == b"%PDF-"
    if magic[:4] != b"PK\x03\x04":
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(member.file_size for member in archive.infolist()) > 100 * 1024 * 1024:
                return False
            required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
            return required in archive.namelist() and archive.getinfo(required).file_size <= 20 * 1024 * 1024
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _document_suffix(path: Path) -> str:
    """Infer only our supported formats from bytes, never response filenames."""
    with path.open("rb") as handle:
        magic = handle.read(5)
    if magic == b"%PDF-":
        return ".pdf"
    if magic[:4] != b"PK\x03\x04":
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > 5000 or len(names) != len(set(names)) or any(
                    name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                    for name in names):
                return ""
            candidates = [(suffix, core) for suffix, core in
                          ((".pptx", "ppt/presentation.xml"), (".docx", "word/document.xml"))
                          if core in names]
            if len(candidates) != 1:
                return ""
            suffix, core = candidates[0]
            if not _looks_like_document(path, suffix) or "[Content_Types].xml" not in names:
                return ""
            if archive.getinfo("[Content_Types].xml").file_size > 1_000_000:
                return ""
            content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
            prefix = "http://schemas.openxmlformats.org/package/2006/content-types"
            expected_type = ("application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
                             if suffix == ".pptx" else
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml")
            if content_types.tag != "{" + prefix + "}Types" or not any(
                    node.tag == "{" + prefix + "}Override" and node.get("PartName") == "/" + core
                    and node.get("ContentType") == expected_type for node in content_types):
                return ""
            document = ElementTree.fromstring(archive.read(core))
            allowed_roots = ({"{http://schemas.openxmlformats.org/presentationml/2006/main}presentation",
                              "{http://purl.oclc.org/ooxml/presentationml/main}presentation"}
                             if suffix == ".pptx" else
                             {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
                              "{http://purl.oclc.org/ooxml/wordprocessingml/main}document"})
            return suffix if document.tag in allowed_roots else ""
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError, KeyError):
        return ""


def _fetch_document(client, url: str, destination: Path, budget: int) -> Path | None:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not _safe_webdav_url(url):
            return None
        with client.stream("GET", url, follow_redirects=True,
                           headers={"Referer": "https://course.pku.edu.cn/"}) as response:
            response.raise_for_status()
            if not _safe_webdav_url(str(response.url)):
                return None
            length = response.headers.get("content-length")
            if length and int(length) > min(MAX_FILE_BYTES, budget):
                return None
            count = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    count += len(chunk)
                    if count > min(MAX_FILE_BYTES, budget):
                        return None
                    handle.write(chunk)
        suffix = destination.suffix.lower()
        if not suffix:
            suffix = _document_suffix(temporary)
            if not suffix:
                return None
            typed = destination.with_name(destination.name + suffix)
            # Never overwrite an unverified or edited existing inferred source.
            if typed.exists():
                if typed.is_symlink() or not typed.is_file() or typed.stat().st_size > min(MAX_FILE_BYTES, budget):
                    return None
                if hashlib.sha256(typed.read_bytes()).digest() != hashlib.sha256(temporary.read_bytes()).digest():
                    return None
                return typed
        else:
            typed = destination
        if suffix not in SUPPORTED or not _looks_like_document(temporary, suffix):
            return None
        temporary.replace(typed)
        return typed
    except Exception as exc:
        logger.warning("lecture material download failed: %s", type(exc).__name__)
        return None
    finally:
        temporary.unlink(missing_ok=True)


def _fetch_bounded(client, url: str, destination: Path, budget: int) -> bool:
    # Preserve the existing fixed-extension caller contract.
    return _fetch_document(client, url, destination, budget) is not None


def _source_binding(job, row, asset) -> str:
    value = {"course": job.recording.course_id, "date": job.recording.date,
             "content_id": row.get("content_id"), "title": row.get("title"),
             "path": row.get("path"), "filename": asset.get("filename"),
             "official_asset_path": asset.get("path")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _cached_inferred(base: Path, binding: str, budget: int) -> Path | None:
    receipt = _read_json(base.with_name(base.name + ".source.json"), {})
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "source_sha256", "suffix", "bytes", "sha256"}
            or receipt.get("schema") != 1 or receipt.get("source_sha256") != binding
            or receipt.get("suffix") not in SUPPORTED):
        return None
    typed = base.with_name(base.name + receipt["suffix"])
    try:
        if (not typed.is_file() or typed.is_symlink() or type(receipt.get("bytes")) is not int
                or typed.stat().st_size != receipt["bytes"] or receipt["bytes"] > min(MAX_FILE_BYTES, budget)
                or hashlib.sha256(typed.read_bytes()).hexdigest() != receipt.get("sha256")
                or _document_suffix(typed) != receipt["suffix"]):
            return None
    except OSError:
        return None
    return typed


def _save_inferred_receipt(base: Path, typed: Path, binding: str):
    receipt = {"schema": 1, "source_sha256": binding, "suffix": typed.suffix.lower(),
               "bytes": typed.stat().st_size, "sha256": hashlib.sha256(typed.read_bytes()).hexdigest()}
    target = base.with_name(base.name + ".source.json")
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True), "utf-8")
    temporary.replace(target)


def _keyframe_candidate_attachments(job: RecordingJob, recording_key: str) -> list[tuple[dict, dict]]:
    material_dir = job.course_dir / "materials"
    rows = _read_json(material_dir / "index.json", [])
    overrides = _read_json(material_dir / "matches.json", {})
    overrides = overrides if isinstance(overrides, dict) else {}
    recordings = _read_json(job.course_dir / "recordings" / "index.json", [])
    ordered = sorted((row for row in recordings if isinstance(row, dict)),
                     key=lambda row: (str(row.get("recorded_at", "")), str(row.get("title", "")))) if isinstance(recordings, list) else []
    lesson_order = next((i for i, row in enumerate(ordered, 1)
                         if row.get("recorded_at") == job.recording.recorded_at
                         and row.get("title") == job.recording.title), 1)
    try:
        day = date.fromisoformat(job.recording.date)
    except ValueError:
        day = None
    ranked = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title, path = str(row.get("title") or ""), str(row.get("path") or "")
        override = overrides.get(path)
        if (not path or row.get("kind") in {"作业", "考试", "测试"}
                or any(word in title for word in _ASSIGNMENT_WORDS)
                or (override not in (None, "") and not _override_matches_lecture(override, recording_key))):
            continue
        for asset in row.get("attachments") or []:
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("filename") or "")
            if not filename or Path(filename).suffix.lower() not in {".pdf", ""}:
                continue
            value = unicodedata.normalize("NFKC", path + " " + title + " " + filename)
            number = re.search(r"第\s*(\d+)\s*讲|(?:ch(?:apter)?|week|l|ppt)\s*0*(\d+)", value, re.I)
            if _override_matches_lecture(override, recording_key):
                rank = (0, 0)
            elif day and _explicit_date(value, day):
                rank = (1, 0)
            elif number:
                rank = (2, abs(int(number.group(1) or number.group(2)) - lesson_order))
            else:
                rank = (3, 0)
            ranked.append((rank, row, asset))
    ranked.sort(key=lambda entry: entry[0])
    return [(row, asset) for _, row, asset in ranked[:MAX_FILES]]


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)


def keyframe_source_paths(job: RecordingJob, recording_key: str) -> list[Path]:
    """Only reuse visual evidence still confirmed for this lecture."""
    receipt = _read_json(job.directory / "keyframes" / KEYFRAME_MATCHES_FILENAME, {})
    overrides = _read_json(job.course_dir / "materials" / "matches.json", {})
    if not isinstance(receipt, dict) or not isinstance(overrides, dict):
        return []
    paths = []
    for row in receipt.get("matches", []):
        if not isinstance(row, dict) or not _override_matches_lecture(overrides.get(row.get("material_path")), recording_key):
            continue
        path = (job.course_dir / str(row.get("local_path") or "")).resolve()
        if (path.is_relative_to(job.course_dir.resolve()) and path.is_file()
                and path.suffix.lower() == ".pdf" and _looks_like_document(path, ".pdf")
                and path not in paths):
            paths.append(path)
    return paths


def match_materials_by_keyframes(job: RecordingJob, recording_key: str, settings=None, *, stage=None) -> list[dict]:
    """Per-lecture courseware matching has been retired; keep as a safe no-op."""
    return []


def prepare_lesson_sources(job: RecordingJob, recording_key: str, settings, stage=None) -> tuple[list[Path] | None, list[dict]]:
    """Per-lecture courseware selection/matching has been retired so all classmates share deterministic notes."""
    return [], []
