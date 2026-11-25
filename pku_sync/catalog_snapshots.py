"""Account canonical metadata, observation CAS and shared projection permits.

A PUT requires the revision obtained *before* the live portal fetch. A
conflict never retries old data against a newly fetched revision. Unsupported
relay versions and incomplete portal reads permit local reading only.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import re
import os
import secrets
import sqlite3
import time
from pathlib import Path

from .catalog_schema import SECTIONS, FIELDS, fingerprint, validate_snapshot


class CatalogUnavailable(ValueError):
    pass


class CatalogTransportUnavailable(CatalogUnavailable):
    """The account connection failed, rather than rejecting one course."""


class CatalogSourceRemovalRequired(CatalogUnavailable):
    """The portal must be read again before replacing obsolete cloud entries."""


class CatalogProjectionBusy(CatalogUnavailable):
    """An exact service rejection identifies an unresolved shared writer."""

    def __init__(self):
        super().__init__('上次共享目录写入许可尚未释放；已有课程可继续查看。请先等待正在运行的同步结束；若已结束仍出现此提示，需要核对上次 Notion 写入后恢复许可，反复点击更新不会解除。')


def recording_source_id(recording) -> str:
    # Blackboard exposes no permanent ID in its recording list. The campus
    # start timestamp identifies the session, irrespective of title, folder
    # or rotating CAS token. Ambiguous duplicate timestamps fail validation.
    if recording.source_id:
        return recording.source_id
    if not recording.recorded_at:
        raise ValueError("录像缺少教学网讲次时间，无法建立稳定源标识。")
    return "pku:recording:" + fingerprint({"course": recording.course_id,
                                           "start": recording.recorded_at})[:32]


def _envelope(value: dict, course_id: str) -> dict:
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        raise CatalogUnavailable("本地课程快照版本无效；已有缓存保留只读。")
    try:
        snapshot = validate_snapshot(value.get("snapshot"))
    except ValueError as exc:
        raise CatalogUnavailable("本地课程快照不完整。") from exc
    if snapshot["course_id"] != course_id:
        raise CatalogUnavailable("课程快照身份不匹配。")
    return {"revision": revision, "snapshot": snapshot}


def begin_observation(settings, course_id: str) -> dict:
    existing = cached(Path(settings.data_dir), course_id)
    rev = existing["revision"] if isinstance(existing, dict) and isinstance(existing.get("revision"), int) else 0
    return {"revision": rev, "observation_id": secrets.token_hex(16)}


def current(settings, course_id: str) -> dict:
    env = cached(Path(settings.data_dir), course_id)
    if env is None:
        raise CatalogUnavailable("本机尚未缓存该课程目录，请先更新课程目录。")
    return _envelope(env, course_id)


def read_recording_history(
    settings,
    course_id: str,
    current_envelope: dict,
    expected_previous_sources=None,
) -> dict:
    """Fresh read-only historical evidence; never canonical/processing authority."""
    from .recording_history import verified_history

    return verified_history(settings, course_id, current_envelope, expected_previous_sources)


def published_current(settings, course_id: str) -> dict | None:
    """Read local cached course snapshot when available."""
    return cached(Path(settings.data_dir), course_id)


def publish(
    settings,
    course_id: str,
    observation: dict,
    snapshot: dict,
    *,
    removal_confirmation=None,
) -> dict:
    snapshot = validate_snapshot(snapshot)
    if snapshot["course_id"] != course_id:
        raise ValueError("course identity mismatch")
    rev = int((observation or {}).get("revision") or 0) + 1
    envelope = {"revision": rev, "snapshot": snapshot}
    save_cache(Path(settings.data_dir), envelope)
    return _envelope(envelope, course_id)


def verify_current(settings, expected: dict) -> dict:
    course_id = expected["snapshot"]["course_id"]
    _envelope(expected, course_id)
    env = cached(Path(settings.data_dir), course_id)
    if env is not None and env != expected:
        raise CatalogUnavailable("课程快照已更新，请刷新后继续 Notion 同步。")
    return expected


def _read_binding(settings):
    return (
        "local",
        str(getattr(settings, "pku_username", "") or "").strip(),
        os.path.normcase(str(Path(settings.data_dir).resolve())),
    )


def _check_read_worker():
    return None


def read_current_batch(settings, course_ids, *, read=None, check_cancel=None, max_workers=4):
    """Run per-course readers locally, preserving order and explicit None."""
    identifiers = tuple(course_ids)
    if len(set(identifiers)) != len(identifiers) or any(
        not isinstance(cid, str) or not cid for cid in identifiers
    ):
        raise ValueError("invalid or duplicate course identities")
    reader = current if read is None else read
    results = []
    for course_id in identifiers:
        if check_cancel is not None:
            check_cancel()
        value = reader(settings, course_id)
        if value is not None:
            _envelope(value, course_id)
        results.append(value)
    return results


def _replace_local_file(temporary: Path, path: Path) -> None:
    """Wait briefly for Windows readers before replacing atomic local metadata."""
    delays = (0.01, 0.02, 0.04, 0.08, 0.08)
    for attempt in range(len(delays) + 1):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def activate_account(settings) -> None:
    """Ensure local account-catalog directory marker is initialized."""
    namespace = "local"
    root = Path(settings.data_dir)
    path = root / ".account-catalog" / "active-account"
    try:
        if path.read_text("ascii") == namespace:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("active-account." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(namespace, "ascii")
        _replace_local_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cache_path(root: Path, course_id: str) -> Path:
    directory = Path(root) / ".account-catalog"
    try:
        namespace = (directory / "active-account").read_text("ascii").strip() or "local"
    except OSError:
        namespace = "local"
    if namespace not in {"local", "unscoped", "signed-out"} and (
        len(namespace) != 64 or any(c not in "0123456789abcdef" for c in namespace)
    ):
        namespace = "local"
    return directory / namespace / (fingerprint({"course": course_id}) + ".json")


def save_cache(root: Path, envelope: dict) -> None:
    course_id = envelope["snapshot"]["course_id"]
    envelope = _envelope(envelope, course_id)
    path = cache_path(root, course_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temp.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), "utf-8")
        _replace_local_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


_CACHED_ENVELOPE_MEMO: dict[str, tuple[int, int, dict]] = {}


def cached(root: Path, course_id: str) -> dict | None:
    try:
        p = cache_path(root, course_id).resolve()
        st = p.stat()
        key = str(p)
        hit = _CACHED_ENVELOPE_MEMO.get(key)
        if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
            return copy.deepcopy(hit[2])
        env = _envelope(json.loads(p.read_text("utf-8")), course_id)
        if len(_CACHED_ENVELOPE_MEMO) > 256:
            _CACHED_ENVELOPE_MEMO.clear()
        _CACHED_ENVELOPE_MEMO[key] = (st.st_mtime_ns, st.st_size, env)
        return copy.deepcopy(env)
    except (OSError, ValueError):
        return None


def read_status(root: Path, complete: bool, reason: str = "", *, course_id: str = "") -> None:
    path = Path(root) / "portal-read-status.json"
    try:
        previous = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        previous = {}
    if course_id:
        # A selected refresh cannot prove that the whole portal was refreshed.
        # Retain only this account's existing per-course confirmations; otherwise
        # completing one course could relabel another account's cache as usable.
        namespace = cache_path(root, course_id).parent.name
        same_account = (isinstance(previous, dict)
                        and previous.get("account_namespace") == namespace
                        and namespace not in {"unscoped", "signed-out"})
        courses = previous.get("courses", {}) if same_account else {}
        value = {"complete": False, "reason": reason, "scope": "course",
                 "course_id": course_id, "account_namespace": namespace,
                 "courses": courses if isinstance(courses, dict) else {}}
        _save_read_status(path, value)
        return
    value = {"complete": complete, "reason": reason}
    if reason == "reading":
        value["courses"] = {}
    elif reason in {"complete", "partial", "account_unconfirmed"} and "courses" in previous:
        value["courses"] = previous["courses"]
        value["account_namespace"] = previous.get("account_namespace", "unscoped")
    _save_read_status(path, value)


def _save_read_status(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        _replace_local_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def course_read_status(root: Path, course_id: str, complete: bool, reason: str,
                       *, issue: str = "") -> None:
    if issue not in {"", "projection_busy"} or (complete and issue):
        raise ValueError("invalid course diagnostic")
    path = Path(root) / "portal-read-status.json"
    value = json.loads(path.read_text("utf-8"))
    value.setdefault("courses", {})[course_id] = {"complete": complete, "reason": reason}
    if issue:
        # Persist a fixed diagnostic code, never a server body or credential.
        value["courses"][course_id]["issue"] = issue
    value["account_namespace"] = cache_path(root, course_id).parent.name
    _save_read_status(path, value)


def assert_portal_readable(root: Path, course_id: str | None = None) -> None:
    path = Path(root) / "portal-read-status.json"
    if path.exists():
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。") from exc
        if course_id is not None and "courses" in value:
            if value.get("account_namespace") != cache_path(root, course_id).parent.name:
                raise CatalogUnavailable("教学网读取账号已变化；课程缓存保持只读。")
            value = value["courses"].get(course_id, {})
        if value.get("complete") is not True:
            raise CatalogUnavailable("最近一次教学网读取失败或不完整；课程缓存保持只读。")


def projection_readable_course_ids(root: Path, course_ids) -> list[str]:
    """Authorize a batch's complete courses without authorizing its failed ones.

    A partial ledger is usable only with explicit per-course results in the
    current account namespace. Authentication failures, in-progress reads and
    malformed/legacy failure ledgers retain the global fail-closed behavior.
    """
    identifiers = list(course_ids)
    path = Path(root) / "portal-read-status.json"
    if path.exists():
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。") from exc
        if isinstance(value, dict) and value.get("complete") is False and value.get("reason") == "partial":
            statuses = value.get("courses")
            if not isinstance(statuses, dict) or not statuses:
                raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。")
            readable = []
            for cid in identifiers:
                if value.get("account_namespace") != cache_path(root, cid).parent.name:
                    raise CatalogUnavailable("教学网读取账号已变化；课程缓存保持只读。")
                status = statuses.get(cid)
                if not isinstance(status, dict) or type(status.get("complete")) is not bool:
                    raise CatalogUnavailable("教学网读取状态未知；课程缓存保持只读。")
                if status["complete"]:
                    assert_portal_readable(root, cid)
                    readable.append(cid)
            return readable
    assert_portal_readable(root)
    return identifiers


def pull(settings) -> dict:
    """Ensure local workspace catalog namespace is initialized (local-only mode)."""
    activate_account(settings)
    return {"available": True, "status": "local", "courses": 0}


def build_snapshot(course, recordings, materials, assignments, announcements) -> dict:
    groups = {
        "recordings": [{**{k: v for k, v in row.model_dump().items() if k in FIELDS["recordings"]},
                        "source_id": recording_source_id(row)} for row in recordings],
        "materials": materials, "assignments": assignments, "announcements": announcements,
    }
    for section in SECTIONS[1:]:
        groups[section] = [{k: v for k, v in row.items() if k in FIELDS[section]} for row in groups[section]]
        for row in groups[section]:
            source = (row.get("id") if section == "announcements"
                      else row.get("content_id") or row.get("source_id"))
            if not source:
                raise ValueError(f"教学网 {section} 条目缺少稳定源 ID，禁止共享写入。")
            row["source_id"] = f"pku:{course.course_id}:{section}:{source}"
            if "source_links" in row:
                from urllib.parse import urlsplit
                row["source_links"] = [link for link in row["source_links"]
                                       if urlsplit(link.get("url", "")).hostname == "course.pku.edu.cn"
                                       and not urlsplit(link["url"]).query]
    return validate_snapshot({
        "schema": 1, "authority": "course.pku.edu.cn", "course_id": course.course_id,
        "course": {k: getattr(course, k) for k in ("course_id", "name", "code", "term")},
        "sections": {name: {"complete": True, "items": groups[name]} for name in SECTIONS},
    })


def projection_journal_path(settings, course_id: str) -> Path:
    namespace = fingerprint({"session": "local"})
    return Path(settings.data_dir) / ".projection-permits" / namespace / (fingerprint({"course": course_id}) + ".json")


def _save_projection_journal(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        _replace_local_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _projection_journal_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path.with_suffix(".lock.sqlite"), timeout=1)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise CatalogUnavailable("本机另一进程正在更新该课程目录，请等待完成；已有课程可继续查看。") from exc
        yield
    finally:
        connection.rollback()
        connection.close()


@contextmanager
def projection(settings, expected: dict):
    """Serialize local course projection writes using an SQLite journal lock."""
    from . import projection_receipts

    projection_receipts._select_prepared_course(settings, expected)
    course_id = expected["snapshot"]["course_id"]
    path = projection_journal_path(settings, course_id)
    with _projection_journal_lock(path):
        if path.exists():
            raise CatalogUnavailable("上次课程写入许可回执尚未核验，已有记录已保留。请检查网络连接与上次 Notion 写入后恢复许可；不会自动重复写入。")
        if projection_receipts.discover(settings):
            with projection_receipts.projection(settings, expected) as authorized:
                yield authorized
            return
        group = projection_receipts._cohort.get()
        if group is not None and group.started:
            raise CatalogUnavailable("目录正在同步写入中，请等待当前同步完成后再添加课程。")
        verify_current(settings, expected)
        state = {
            "schema": 1,
            "course_id": course_id,
            "revision": expected["revision"],
            "snapshot_sha256": fingerprint(expected["snapshot"]),
            "phase": "writing",
            "local_receipt_id": secrets.token_hex(32),
        }
        _save_projection_journal(path, state)
        entered_writer = False
        try:
            if group is not None:
                group.register(settings, path, state)
            entered_writer = True
            yield expected
        except BaseException:
            if entered_writer and not (group is not None and (not group.started or group.completed or group.known_boundary())):
                state["phase"] = "writing-unknown"
                try:
                    _save_projection_journal(path, state)
                except OSError:
                    pass
            else:
                path.unlink(missing_ok=True)
            raise
        else:
            path.unlink(missing_ok=True)
            from .projection_notion_effects import intent_path

            intent_path(settings, state).unlink(missing_ok=True)
