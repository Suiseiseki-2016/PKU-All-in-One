"""In-process runtime dependency, codec selftest, proxy, and resource health checks."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import socket
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REQUIRED_RUNTIME_MODULES: tuple[str, ...] = (
    "av",
    "cv2",
    "numpy",
    "PIL",
    "pypdf",
    "pypdfium2",
    "zhconv",
    "reportlab",
    "oss2",
    "openai",
    "docx",
    "pptx",
)

_VERSION_MODULES: tuple[str, ...] = ("av", "cv2", "pypdf")
_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_key(data_dir: Path | str) -> str:
    path = Path(data_dir)
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _existing_usage_path(data_dir: Path | str) -> Path:
    path = Path(data_dir)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path if path.exists() else Path("/")


def _read_proc_meminfo_mb() -> tuple[int | None, int | None]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None, None
    total_kb: int | None = None
    avail_kb: int | None = None
    try:
        for line in meminfo.read_text("utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    total_kb = int(parts[1])
            elif line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    avail_kb = int(parts[1])
    except OSError:
        return None, None
    return (
        avail_kb // 1024 if avail_kb is not None else None,
        total_kb // 1024 if total_kb is not None else None,
    )


def _run_codec_selftest(loaded: dict[str, Any]) -> tuple[bool, str]:
    av = loaded.get("av")
    cv2 = loaded.get("cv2")
    np = loaded.get("numpy")
    if av is None or cv2 is None or np is None:
        missing = [m for m in ("av", "cv2", "numpy") if m not in loaded]
        return False, f"缺少媒体依赖: {', '.join(missing)}"
    try:
        from .transcription import _MemoryAudioChunkEncoder

        enc = _MemoryAudioChunkEncoder(chunk_seconds=10)
        frame = av.AudioFrame(format="fltp", layout="mono", samples=1024)
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        frame.sample_rate = 16000
        frame.time_base = Fraction(1, 16000)
        frame.pts = 0
        enc.feed(frame)
        parts = enc.finish()
        if not parts:
            return False, "PyAV 音频编码未生成分段"
        part, _offset = parts[0]
        if part.size_bytes() <= 0:
            return False, "PyAV 音频编码输出为空"
        with av.open(io.BytesIO(part.data)) as container:
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
            if audio_stream is None:
                return False, "PyAV 解码未发现音频流"
            decoded = list(container.decode(audio_stream))
            if not decoded:
                return False, "PyAV 音频解码帧为空"
        ok, buf = cv2.imencode(".jpg", np.zeros((16, 16, 3), dtype=np.uint8))
        if not ok or len(buf) <= 0:
            return False, "OpenCV JPEG 编码失败"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _check_campus_proxy() -> tuple[bool, bool | None, str]:
    raw = os.environ.get("PKU_CAMPUS_MEDIA_PROXY", "").strip()
    if not raw:
        return False, None, ""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True, None, ""
    parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
    host = parsed.hostname
    if not host:
        return True, False, f"代理地址无效: {raw}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            pass
        return True, True, ""
    except OSError as exc:
        return True, False, f"{host}:{port} ({type(exc).__name__}: {exc})"


def get_runtime_health(
    data_dir: Path | str,
    *,
    force: bool = False,
    ttl_seconds: float = 60.0,
) -> dict[str, Any]:
    """Run (with short TTL cache when healthy) worker runtime dependency, codec, proxy, and resource checks."""
    key = _cache_key(data_dir)
    now = time.time()
    in_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    if not force and not in_pytest:
        with _HEALTH_LOCK:
            cached = _HEALTH_CACHE.get(key)
            if cached is not None:
                ts, report = cached
                if report.get("ok") and (now - ts) < ttl_seconds:
                    return dict(report)

    loaded: dict[str, Any] = {}
    missing_modules: list[str] = []
    versions: dict[str, str] = {}
    for mod_name in REQUIRED_RUNTIME_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            loaded[mod_name] = mod
            if mod_name in _VERSION_MODULES:
                ver = getattr(mod, "__version__", "")
                if ver:
                    versions[mod_name] = str(ver)
        except Exception:
            missing_modules.append(mod_name)

    if any(m in missing_modules for m in ("av", "cv2", "numpy")):
        codec_selftest_ok = False
        codec_selftest_error = (
            f"缺少媒体依赖: {', '.join(m for m in ('av', 'cv2', 'numpy') if m in missing_modules)}"
        )
    else:
        codec_selftest_ok, codec_selftest_error = _run_codec_selftest(loaded)

    campus_proxy_configured, campus_proxy_ok, campus_proxy_error = _check_campus_proxy()

    usage = shutil.disk_usage(_existing_usage_path(data_dir))
    disk_used_pct = round((usage.used / usage.total) * 100.0, 1) if usage.total > 0 else 0.0
    disk_free_gb = round(usage.free / (1024 ** 3), 2)
    mem_available_mb, mem_total_mb = _read_proc_meminfo_mb()

    issues: list[str] = []
    if missing_modules:
        issues.append(f"缺少运行时依赖: {', '.join(missing_modules)}")
    elif not codec_selftest_ok:
        issues.append(f"媒体编解码自检失败: {codec_selftest_error}")
    if campus_proxy_ok is False:
        issues.append(f"校园网代理不可达: {campus_proxy_error}")
    if disk_used_pct >= 90.0:
        issues.append(f"磁盘使用率过高: {disk_used_pct}%")
    if mem_available_mb is not None and mem_available_mb < 100:
        issues.append(f"可用内存偏低: {mem_available_mb}MB")

    ok = (
        not missing_modules
        and codec_selftest_ok
        and (campus_proxy_ok is not False)
        and disk_used_pct < 94.0
    )

    report: dict[str, Any] = {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "instance_id": os.environ.get("PKU_PANEL_INSTANCE_ID", "").strip(),
        "python_version": sys.version.split()[0],
        "missing_modules": missing_modules,
        "versions": versions,
        "codec_selftest_ok": codec_selftest_ok,
        "codec_selftest_error": codec_selftest_error,
        "campus_proxy_configured": campus_proxy_configured,
        "campus_proxy_ok": campus_proxy_ok,
        "campus_proxy_error": campus_proxy_error,
        "disk_used_pct": disk_used_pct,
        "disk_free_gb": disk_free_gb,
        "mem_available_mb": mem_available_mb,
        "mem_total_mb": mem_total_mb,
        "issues": issues,
        "checked_at": round(now, 3),
    }

    if not in_pytest:
        with _HEALTH_LOCK:
            _HEALTH_CACHE[key] = (now, dict(report))

    return report
