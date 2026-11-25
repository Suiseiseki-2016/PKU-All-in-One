"""Frontend module bundler for the student panel SPA (VAL-META-019/020 compliant).

Maintains clean modular source files under ``pku_sync/panel/static/modules/``:
  - ``01_core_and_recordings.js``: State store, DOM/focus helpers, recording jobs & batch UI
  - ``02_courses_and_reader.js``: Course catalog, lecture workspace, note reader, logic map & quiz
  - ``03_models_and_shell.js``: BYOK model settings, navigation shell & task status helpers
  - ``04_actions_and_bootstrap.js``: Render loop, API actions, event delegation & bootstrap

Compiles them deterministically into ``pku_sync/panel/static/app.js`` so the runtime
and test suite continue to serve a single zero-external-dependency script asset.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
MODULES_DIR = STATIC_DIR / "modules"
APP_JS_PATH = STATIC_DIR / "app.js"

MODULE_SPECS = [
    ("01_core_and_recordings.js", "  function campusExistingNotionControl(course) {"),
    ("02_courses_and_reader.js", "  function ensureModelDraft("),
    ("03_models_and_shell.js", "  function render() {"),
    ("04_actions_and_bootstrap.js", None),
]


def split_monolith_to_modules(source_text: str | None = None) -> list[Path]:
    """Split ``app.js`` into the 4 domain modules at exact top-level function boundaries."""
    text = source_text if source_text is not None else APP_JS_PATH.read_text(encoding="utf-8")
    MODULES_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    remaining = text
    for filename, next_marker in MODULE_SPECS:
        target = MODULES_DIR / filename
        if next_marker is None:
            chunk = remaining
            remaining = ""
        else:
            idx = remaining.find(next_marker)
            if idx < 0:
                raise ValueError(f"Marker not found while splitting frontend modules: {next_marker!r}")
            chunk = remaining[:idx]
            remaining = remaining[idx:]
        target.write_text(chunk, encoding="utf-8")
        written.append(target)
    return written


def build_bundle_from_modules() -> str:
    """Concatenate ``modules/*.js`` in order and write ``app.js`` if changed."""
    parts: list[str] = []
    for filename, _ in MODULE_SPECS:
        mod_path = MODULES_DIR / filename
        if not mod_path.is_file():
            raise FileNotFoundError(f"Missing frontend module: {mod_path}")
        parts.append(mod_path.read_text(encoding="utf-8"))
    combined = "".join(parts)
    if not APP_JS_PATH.is_file() or APP_JS_PATH.read_text(encoding="utf-8") != combined:
        APP_JS_PATH.write_text(combined, encoding="utf-8")
    return combined


def verify_bundle_sync() -> bool:
    """Return True if ``app.js`` is byte-for-byte identical to the concatenated modules."""
    if not APP_JS_PATH.is_file():
        return False
    parts: list[str] = []
    for filename, _ in MODULE_SPECS:
        mod_path = MODULES_DIR / filename
        if not mod_path.is_file():
            return False
        parts.append(mod_path.read_text(encoding="utf-8"))
    return APP_JS_PATH.read_text(encoding="utf-8") == "".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage modular frontend sources for app.js")
    parser.add_argument("--split", action="store_true", help="Split app.js into static/modules/*.js")
    parser.add_argument("--check", action="store_true", help="Verify app.js matches static/modules/*.js")
    args = parser.parse_args(argv)

    if args.split:
        paths = split_monolith_to_modules()
        for p in paths:
            lines = p.read_text(encoding="utf-8").count("\n")
            print(f"Wrote {p.relative_to(STATIC_DIR)} ({lines} lines)")
        return 0

    if args.check:
        ok = verify_bundle_sync()
        if not ok:
            print("ERROR: app.js is out of sync with static/modules/*.js", file=sys.stderr)
            return 1
        digest = hashlib.sha256(APP_JS_PATH.read_bytes()).hexdigest()[:12]
        print(f"OK: app.js matches static/modules/*.js (sha256={digest})")
        return 0

    combined = build_bundle_from_modules()
    digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]
    print(f"Built app.js from {len(MODULE_SPECS)} modules (sha256={digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
