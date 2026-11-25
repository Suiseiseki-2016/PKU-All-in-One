"""Zero-external-dependency frontend JS static, bundle, CSP, and DOM guards."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _extract_inline_scripts(html_text: str) -> list[str]:
    scripts: list[str] = []
    for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html_text, flags=re.S | re.I):
        if "application/ld+json" in attrs.lower():
            continue
        if "src=" in attrs.lower():
            continue
        code = body.strip()
        if code:
            scripts.append(code)
    return scripts


def _run_node_check(node_bin: str, label: str, code: str) -> list[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=True) as fh:
        fh.write(code)
        fh.flush()
        proc = subprocess.run([node_bin, "--check", fh.name], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return [f"{label}: node --check failed:\n{err}"]
    return []


def run_all_checks() -> list[str]:
    errors: list[str] = []

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 1. Verify student SPA bundle matches static/modules/*.js
    from pku_sync.panel.bundle_frontend import APP_JS_PATH, verify_bundle_sync
    from pku_sync.panel.student_ui import student_page

    if not verify_bundle_sync():
        errors.append("pku_sync/panel/static/app.js is out of sync with static/modules/*.js; run bundle_frontend")

    app_js = APP_JS_PATH.read_text(encoding="utf-8")
    page_html = student_page()

    # 2. Verify zero external URLs or inline event handlers / forbidden CSP constructs
    for dangerous in ("eval(", "new Function", "document.write("):
        if dangerous in app_js:
            errors.append(f"app.js contains forbidden construct: {dangerous}")
    inline_handlers = re.findall(r"""\son[a-z]+\s*=""", page_html, flags=re.I)
    if inline_handlers:
        errors.append(f"student_ui.py shell contains inline event handlers: {inline_handlers}")
    for label, text in (("student_ui", page_html), ("app.js", app_js)):
        if "http://" in text or "https://" in text:
            errors.append(f"{label} contains literal http(s):// external URL")

    # 3. Node syntax check on bundled app.js and inline scripts in student_ui
    node_bin = shutil.which("node")
    if node_bin:
        errors.extend(_run_node_check(node_bin, "app.js", app_js))
        for idx, snippet in enumerate(_extract_inline_scripts(page_html)):
            errors.extend(_run_node_check(node_bin, f"student_ui#script-{idx}", snippet))

    return errors


def main() -> int:
    errors = run_all_checks()
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("OK: all frontend JS bundle, CSP, external-asset, and syntax checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
