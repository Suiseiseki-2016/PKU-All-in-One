"""Local read-only historical recording provenance for Notion projections."""

from __future__ import annotations

import json
from pathlib import Path

from . import catalog_snapshots as catalog
from .catalog_schema import fingerprint


def verified_history(
    settings,
    course_id: str,
    current_envelope: dict,
    expected_previous_sources=None,
) -> dict:
    root = Path(settings.data_dir)
    cached = read_cache(root, course_id, current_envelope)
    if cached is not None:
        return cached
    current = catalog._envelope(current_envelope, course_id)
    return {
        "schema": 1,
        "account_namespace": "local",
        "account_owner_sha256": fingerprint({"account": "local", "catalog_endpoint": "local://catalog"}),
        "course_id": course_id,
        "current_revision": current["revision"],
        "current_snapshot_sha256": fingerprint(current["snapshot"]),
        "raw_history_sha256": fingerprint({"recordings": []}),
        "recordings": [],
    }


def assert_fresh(settings, course_id: str, current_envelope: dict, value: dict) -> None:
    if not isinstance(value, dict) or value.get("course_id") != course_id:
        raise catalog.CatalogUnavailable("本地历史记录身份不匹配。")


def _cache_path(root: Path, course_id: str) -> Path:
    return (
        Path(root)
        / ".recording-history"
        / "local"
        / (fingerprint({"course": course_id}) + ".json")
    )


def read_cache(root: Path, course_id: str, current_envelope: dict) -> dict | None:
    path = _cache_path(Path(root), course_id)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text("utf-8"))
        payload = loaded.get("payload") if isinstance(loaded, dict) else None
        if isinstance(payload, dict) and payload.get("course_id") == course_id:
            return payload
    except (OSError, ValueError):
        pass
    return None


def write_cache(settings, value: dict) -> None:
    return None
