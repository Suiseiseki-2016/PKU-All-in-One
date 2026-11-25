"""Local-only step guards and read/write phases for Notion synchronization."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
from pathlib import Path

from . import catalog_snapshots as catalog
from .catalog_schema import fingerprint

_cohort: ContextVar[object | None] = ContextVar("prepared_catalog_projection_cohort", default=None)


class PreparedReadExpired(catalog.CatalogUnavailable):
    """Retained exception type for workspace read refresh boundaries."""


class _LocalCohort:
    def __init__(self, verified_steps: bool = False, tracked_mutations: bool = False) -> None:
        self.members: list = []
        self.started = False
        self.completed = False
        self.verified_steps = verified_steps
        self.tracked_mutations = tracked_mutations

    def begin(self) -> None:
        self.started = True

    def complete(self) -> None:
        self.completed = True


@contextmanager
def prepared_read_phase(settings, envelopes, *, check_cancel=None, max_workers=4):
    yield None


@contextmanager
def prepared_cohort(*, verified_steps: bool = False, tracked_mutations: bool = False):
    group = _LocalCohort(verified_steps=verified_steps, tracked_mutations=tracked_mutations)
    token = _cohort.set(group)
    try:
        yield group
    finally:
        _cohort.reset(token)


@contextmanager
def verified_workspace_step(kind: str):
    if kind not in {
        "home",
        "course",
        "center",
        "source-section",
        "lecture",
        "lecture-placeholder",
        "summary-link",
        "recording-index",
    }:
        raise ValueError(f"Unknown verified workspace step: {kind}")
    yield


def verified_workspace_operation(kind: str):
    def decorate(operation):
        @wraps(operation)
        def verified(*args, **kwargs):
            with verified_workspace_step(kind):
                return operation(*args, **kwargs)

        return verified

    return decorate


def assert_notion_write_phase(method: str, url: str) -> None:
    return None


def _invalidate_prepared_read_phase() -> None:
    return None


def discover(settings) -> bool:
    return False


def owner_key(settings) -> str:
    return fingerprint({"account": "local", "catalog_endpoint": "local://catalog"})


def journal_path(settings, course_id: str, owner: str) -> Path:
    return (
        Path(settings.data_dir)
        / ".projection-receipts"
        / owner
        / (fingerprint({"course": course_id}) + ".json")
    )


def root_key(settings) -> str:
    return fingerprint({"data_root": os.path.normcase(str(Path(settings.data_dir).resolve()))})
