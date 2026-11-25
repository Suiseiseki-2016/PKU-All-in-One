"""Place representative recording frames beside local and Notion note sections."""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from ..notion import NotionClient, markdown_to_blocks

_WINDOW = re.compile(r"^(\d+):(\d{2})[–-](\d+):(\d{2})$")
_CAPTION_PREFIX = "课堂画面 "

class NoteFrameError(RuntimeError):
    """The original video could not supply a missing note-window picture."""

def _text(block: dict) -> str:
    kind = block.get("type") or ""
    return "".join(
        part.get("plain_text") or part.get("text", {}).get("content", "")
        for part in (block.get(kind) or {}).get("rich_text") or []
    )

def _seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)

_SMALL_FRAME_CACHE: dict[tuple[str, int, int], tuple | None] = {}
_USELESS_FRAME_CACHE: dict[tuple[str, int, int, bool], bool] = {}
_CONTENT_SIG_CACHE: dict[tuple[str, int, int], tuple | None] = {}
_VISUAL_SCORE_CACHE: dict[tuple[str, int, int], float] = {}
_BOARD_PANEL_SIG_CACHE: dict[tuple[str, int, int], tuple | None] = {}


def _has_tr_pip(gray, *, threshold: float = 14.0) -> bool:
    import numpy as np

    tr_h = float(np.mean(np.abs(gray[44:46, 240:].astype(float) - gray[46:48, 240:].astype(float))))
    tr_v = float(np.mean(np.abs(gray[:45, 238:240].astype(float) - gray[:45, 240:242].astype(float))))
    return tr_h > threshold and tr_v > threshold


def _has_br_pip(gray) -> bool:
    import numpy as np

    if _has_tr_pip(gray, threshold=14.0):
        return False
    br_h = float(np.mean(np.abs(gray[133:135, 240:].astype(float) - gray[135:137, 240:].astype(float))))
    br_v = float(np.mean(np.abs(gray[135:, 238:240].astype(float) - gray[135:, 240:242].astype(float))))
    return br_h > 22.0 and br_v > 22.0


def _detect_desktop_chrome(small, gray, hsv) -> str:
    """Classify Windows Explorer / PowerPoint edit chrome vs clean fullscreen slides."""
    import cv2
    import numpy as np

    bar_dark = float(np.mean(gray[173:180, :] < 65))
    bar_r = float(np.mean(gray[173:180, 260:315] < 70))
    bl_sat = float(np.mean(hsv[173:180, 0:85, 1] > 70))
    above_bar = float(np.median(gray[158:170, :230]))
    if not (bar_dark >= 0.50 and bar_r >= 0.65 and bl_sat >= 0.25 and above_bar >= 170.0):
        return ""

    top_hsv = hsv[0:8, 0:235]
    ppt_org = float(
        np.mean(
            ((top_hsv[:, :, 0] <= 18) | (top_hsv[:, :, 0] >= 168))
            & (top_hsv[:, :, 1] >= 100)
            & (top_hsv[:, :, 2] >= 100)
        )
    )
    if ppt_org >= 0.45:
        return "ppt_edit"

    top_med = float(np.median(gray[0:5, 10:200]))
    rib = gray[7:26, 0:235]
    rib_light = float(np.mean((rib >= 210) & (rib <= 248)))
    top_edges = float(np.mean(cv2.Canny(gray[4:27, 0:200], 45, 135) > 0))
    rib_sat = float(np.mean(hsv[6:25, 0:180, 1] > 70))
    left_pane_white = float(np.mean(gray[35:165, 2:42] > 220))
    if (
        top_med >= 205.0
        and rib_light >= 0.78
        and top_edges >= 0.08
        and rib_sat < 0.08
        and left_pane_white >= 0.70
        and above_bar >= 205.0
    ):
        return "explorer"

    return "taskbar"


def _load_small_frame(path: Path) -> tuple | None:
    """Decode an image once and return cached (small_bgr_320x180, gray_320x180, stat_key)."""
    try:
        st = path.stat()
        stat_key = (str(path), st.st_mtime_ns, st.st_size)
        if stat_key in _SMALL_FRAME_CACHE:
            cached = _SMALL_FRAME_CACHE[stat_key]
            return (*cached, stat_key) if cached is not None else None
        if len(_SMALL_FRAME_CACHE) >= 96:
            for k in list(_SMALL_FRAME_CACHE)[:32]:
                _SMALL_FRAME_CACHE.pop(k, None)
        import cv2
        import numpy as np

        flag = (
            cv2.IMREAD_REDUCED_COLOR_2
            if st.st_size > 40_000 and path.suffix.lower() in {".jpg", ".jpeg"}
            else cv2.IMREAD_COLOR
        )
        frame = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flag)
        if frame is None:
            _SMALL_FRAME_CACHE[stat_key] = None
            return None
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        _SMALL_FRAME_CACHE[stat_key] = (small, gray)
        return (small, gray, stat_key)
    except Exception:
        return None


def _is_useless_visual_frame(path: Path, *, is_board: bool = False) -> bool:
    """Return True when a decoded frame is a blank/dead canvas, desktop wallpaper, or non-board classroom wide-shot."""
    try:
        loaded = _load_small_frame(path)
        if loaded is None:
            return False
        small, gray, stat_key = loaded
        cache_key = (*stat_key, bool(is_board))
        if cache_key in _USELESS_FRAME_CACHE:
            return _USELESS_FRAME_CACHE[cache_key]
        if len(_USELESS_FRAME_CACHE) >= 1024:
            for k in list(_USELESS_FRAME_CACHE)[:256]:
                _USELESS_FRAME_CACHE.pop(k, None)
        import cv2
        import numpy as np
        if (
            float(np.std(gray[:, :236])) < 14.0 and float(np.std(gray[47:, 240:])) < 10.0
        ) or float(np.std(gray)) < 5.0:
            _USELESS_FRAME_CACHE[cache_key] = True
            return True
        if is_board:
            _USELESS_FRAME_CACHE[cache_key] = False
            return False
        has_tr_pip = _has_tr_pip(gray, threshold=14.0)
        has_br_pip = _has_br_pip(gray)
        if has_br_pip:
            _USELESS_FRAME_CACHE[cache_key] = True
            return True
        if detect_wide_classroom_projector_box(small, gray) is not None:
            _USELESS_FRAME_CACHE[cache_key] = False
            return False
        main_gray = gray[:166, :240]
        hist = cv2.calcHist([main_gray], [0], None, [16], [0, 256]).flatten()
        hist = hist / max(float(hist.sum()), 1.0)
        top2_bg = float(np.sort(hist)[-2:].sum())
        if not has_tr_pip and top2_bg < 0.65:
            _USELESS_FRAME_CACHE[cache_key] = True
            return True
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        if _detect_desktop_chrome(small, gray, hsv) == "explorer":
            _USELESS_FRAME_CACHE[cache_key] = True
            return True
        w10_roi = hsv[48:162, 165:238]
        w10_blue = float(
            np.mean(
                (w10_roi[:, :, 0] >= 98)
                & (w10_roi[:, :, 0] <= 113)
                & (w10_roi[:, :, 1] >= 115)
                & (w10_roi[:, :, 2] >= 75)
            )
        )
        ppt_left_sidebar = float(np.mean(main_gray[25:160, :30] > 210))
        if w10_blue > 0.68 and ppt_left_sidebar < 0.55:
            _USELESS_FRAME_CACHE[cache_key] = True
            return True
        sat = hsv[:166, :240, 1]
        sat_high = float(np.mean(sat > 55))
        lap_var = float(cv2.Laplacian(main_gray, cv2.CV_64F).var())
        res = bool(top2_bg < 0.52 and (sat_high > 0.62 or lap_var < 3800.0))
        _USELESS_FRAME_CACHE[cache_key] = res
        return res
    except Exception:
        return False


def detect_wide_classroom_projector_box(small_bgr, gray) -> tuple[int, int, int, int] | None:
    """Locate a bright projector screen opposite a dim, uniform classroom wall."""
    import cv2
    import numpy as np

    tr_h = float(np.mean(np.abs(gray[44:46, 240:].astype(float) - gray[46:48, 240:].astype(float))))
    tr_v = float(np.mean(np.abs(gray[:45, 238:240].astype(float) - gray[:45, 240:242].astype(float))))
    if tr_h > 12.0 and tr_v > 12.0:
        return None
    if float(np.mean(gray > 170)) > 0.35:
        return None
    work = gray[:110, :]
    mask = (work > 135).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    best_box = None
    best_area = 0
    for x, y, bw, bh, area in stats[1:]:
        if not (
            0.09 <= (bw * bh) / (320.0 * 180.0) <= 0.32
            and area / max(bw * bh, 1) >= 0.72
            and 1.25 <= bw / max(float(bh), 1.0) <= 2.2
            and y <= 28 and bh >= 48 and bw >= 85
        ):
            continue
        wall = (
            gray[max(15, y):min(105, y + bh), 15:max(40, x - 10)]
            if x > 120 else
            gray[max(15, y):min(105, y + bh), min(280, x + bw + 10):305]
        )
        if wall.size and 25.0 <= float(np.mean(wall)) <= 125.0 and float(np.std(wall)) < 32.0:
            if area > best_area:
                best_area = area
                best_box = (int(x), int(y), int(bw), int(bh))
    return best_box


def crop_wide_classroom_projector_bgr(bgr):
    """Crop a large classroom frame to its projector, retaining a small border."""
    if bgr is None or bgr.ndim < 2:
        return bgr
    h, w = bgr.shape[:2]
    if h < 360 or w < 640:
        return bgr
    import cv2

    small = cv2.resize(bgr, (320, 180))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    box = detect_wide_classroom_projector_box(small, gray)
    if box is None:
        return bgr
    x, y, bw, bh = box
    pad_x = int(round(bw * 0.06))
    pad_y = int(round(bh * 0.06))
    x1 = max(0, min(w, int(round((x - pad_x) * w / 320.0))))
    y1 = max(0, min(h, int(round((y - pad_y) * h / 180.0))))
    x2 = max(0, min(w, int(round((x + bw + pad_x) * w / 320.0))))
    y2 = max(0, min(h, int(round((y + bh + pad_y) * h / 180.0))))
    return bgr[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else bgr


def _frame_content_signature(path: Path) -> tuple | None:
    """Compute a PiP-invariant foreground detail and intensity grid for near-duplicate detection."""
    try:
        loaded = _load_small_frame(path)
        if loaded is None:
            return None
        small, g, cache_key = loaded
        if cache_key in _CONTENT_SIG_CACHE:
            return _CONTENT_SIG_CACHE[cache_key]
        if len(_CONTENT_SIG_CACHE) >= 1024:
            for k in list(_CONTENT_SIG_CACHE)[:256]:
                _CONTENT_SIG_CACHE.pop(k, None)
        import cv2
        import numpy as np

        pbox = detect_wide_classroom_projector_box(small, g)
        if pbox is not None:
            x, y, bw, bh = pbox
            roi = g[max(0, y):min(180, y + bh), max(0, x):min(320, x + bw)]
        else:
            roi = g[10:164, 8:236]
        blur = cv2.GaussianBlur(roi, (21, 21), 0)
        detail = np.abs(roi.astype(np.float32) - blur.astype(np.float32))
        grid = cv2.resize(detail, (38, 26), interpolation=cv2.INTER_AREA)
        raw_grid = cv2.resize(roi, (38, 26), interpolation=cv2.INTER_AREA).astype(np.float32)
        res = (grid, raw_grid, float(np.sum(grid)))
        _CONTENT_SIG_CACHE[cache_key] = res
        return res
    except Exception:
        return None


def _are_near_duplicate_frames(sig1: tuple | None, sig2: tuple | None) -> bool:
    """Return True when two frames show the same slide or board state with only minor cursor/pen/PiP changes."""
    if sig1 is None or sig2 is None:
        return False
    import numpy as np

    g1, r1, _ = sig1
    g2, r2, _ = sig2
    fg = (g1 > 4.0) | (g2 > 4.0)
    raw_diff = float(np.mean(np.abs(r1 - r2)))
    if not np.any(fg):
        return raw_diff < 4.0
    fg_diff = float(np.mean(np.abs(g1[fg] - g2[fg])) / max(float(np.mean(np.maximum(g1[fg], g2[fg]))), 1.0))
    iou = float(np.sum((g1 > 4.5) & (g2 > 4.5))) / max(float(np.sum((g1 > 4.5) | (g2 > 4.5))), 1.0)
    return bool(iou > 0.72 and fg_diff < 0.24 and raw_diff < 6.5)


def _board_panel_signature(path: Path) -> tuple | None:
    """Compute a teacher/projector-invariant chalkboard stroke grid for mid-class dedup."""
    try:
        loaded = _load_small_frame(path)
        if loaded is None:
            return None
        _, g, cache_key = loaded
        if cache_key in _BOARD_PANEL_SIG_CACHE:
            return _BOARD_PANEL_SIG_CACHE[cache_key]
        if len(_BOARD_PANEL_SIG_CACHE) >= 1024:
            for k in list(_BOARD_PANEL_SIG_CACHE)[:256]:
                _BOARD_PANEL_SIG_CACHE.pop(k, None)
        import cv2
        import numpy as np

        left_m = float(np.mean(g[25:85, 10:150]))
        right_m = float(np.mean(g[25:85, 165:310]))
        roi = g[25:85, 165:310] if right_m < left_m else g[25:85, 10:150]
        blur = cv2.GaussianBlur(roi, (15, 15), 0)
        chalk = np.clip(roi.astype(np.float32) - blur.astype(np.float32) - 10.0, 0.0, 255.0)
        grid = cv2.resize(chalk, (28, 14), interpolation=cv2.INTER_AREA)
        res = ("right" if right_m < left_m else "left", grid)
        _BOARD_PANEL_SIG_CACHE[cache_key] = res
        return res
    except Exception:
        return None


def _are_same_board_panel(sig1: tuple | None, sig2: tuple | None) -> bool:
    """Return True when two wide classroom frames depict the same unchanged chalkboard."""
    if sig1 is None or sig2 is None or sig1[0] != sig2[0]:
        return False
    import numpy as np

    g1, g2 = sig1[1], sig2[1]
    c1, c2 = g1 > 2.0, g2 > 2.0
    union = float(np.sum(c1 | c2))
    if union <= 0.0:
        return True
    iou = float(np.sum(c1 & c2)) / union
    s1, s2 = float(np.std(g1)), float(np.std(g2))
    corr = float(np.corrcoef(g1.flatten(), g2.flatten())[0, 1]) if s1 > 1e-6 and s2 > 1e-6 else 0.0
    return bool(iou >= 0.55 and corr >= 0.55)


def _visual_score(path: Path) -> float:
    """Prefer legible slide or chalkboard views over blank screens and distant shots."""
    loaded = _load_small_frame(path)
    if loaded is None:
        return 0.0
    small, gray, cache_key = loaded
    if cache_key in _VISUAL_SCORE_CACHE:
        return _VISUAL_SCORE_CACHE[cache_key]
    if len(_VISUAL_SCORE_CACHE) >= 1024:
        for k in list(_VISUAL_SCORE_CACHE)[:256]:
            _VISUAL_SCORE_CACHE.pop(k, None)
    import cv2
    import numpy as np
    from ..board_ocr import is_blackboard_frame

    is_board: bool | None = None
    if _is_useless_visual_frame(path, is_board=False):
        is_board = is_blackboard_frame(path)
        if not is_board or _is_useless_visual_frame(path, is_board=True):
            if cache_key is not None:
                _VISUAL_SCORE_CACHE[cache_key] = 0.25
            return 0.25
    pbox = detect_wide_classroom_projector_box(small, gray)
    if pbox is not None:
        px, py, pw, ph = pbox
        proj_roi = gray[max(0, py):min(180, py + ph), max(0, px):min(320, px + pw)]
        bright = float(np.mean(proj_roi > 185))
        edges = float(np.mean(cv2.Canny(proj_roi, 50, 150) > 0))
        contrast = float(np.std(proj_roi)) / 255
        res = max(1.65, 2 * bright + 6 * edges + contrast)
        if cache_key is not None:
            _VISUAL_SCORE_CACHE[cache_key] = res
        return res
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    chrome = _detect_desktop_chrome(small, gray, hsv)
    has_br_pip = _has_br_pip(gray)
    bright = max(float(np.mean(gray > 185)), float(np.mean(gray[:166, :240] > 185)))
    edges = float(np.mean(cv2.Canny(gray, 50, 150) > 0))
    contrast = float(np.std(gray)) / 255
    slide_score = 2 * bright + 6 * edges + contrast
    if slide_score < 2.2 and is_board is None:
        is_board = is_blackboard_frame(path)
    if slide_score < 2.2 and is_board:
        dark_pixels = gray[(gray >= 18) & (gray <= 155)]
        if dark_pixels.size > 0:
            median = float(np.percentile(dark_pixels, 50))
            chalk = float(np.mean(dark_pixels > median + 25))
        else:
            chalk = 0.0
        board_score = 1.80 + 6 * edges + 3 * chalk + contrast * 0.5
        if bright >= 0.12 or has_br_pip:
            board_score = min(board_score, 2.15)
        res = max(slide_score, board_score)
    else:
        res = slide_score
    if chrome == "ppt_edit":
        penalized = res - 0.85
        res = max(penalized, 1.65) if res >= 2.0 else max(0.25, penalized)
    elif chrome == "taskbar":
        penalized = res - 0.45
        res = max(penalized, 1.65) if res >= 2.0 else max(0.25, penalized)
    if cache_key is not None:
        _VISUAL_SCORE_CACHE[cache_key] = res
    return res

def _frames(directory: Path) -> list[dict]:
    folder = directory / "keyframes"
    try:
        index = json.loads((folder / "index.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for item in index.get("keyframes", []):
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".jpg"):
            continue
        path = folder / name
        if not path.is_file():
            continue
        try:
            stamp = float(item["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        score = _visual_score(path)
        if score < 1.6:
            continue
        rows.append({"path": path, "time": str(item.get("time") or ""),
                     "timestamp": stamp, "score": score})
    return sorted(rows, key=lambda row: row["timestamp"])

def _choose(frames: list[dict], start: int, end: int) -> dict | None:
    midpoint = (start + end) / 2
    inside = [row for row in frames if start <= row["timestamp"] < end]
    if inside:
        return max(inside, key=lambda row: row["score"] -
                   abs(row["timestamp"] - midpoint) / max(end - start, 1) * 0.25)
    before = [row for row in frames if row["timestamp"] < start and start - row["timestamp"] <= 300]
    return before[-1] if before else None

def _sample_window(video: Path, folder: Path, start: float, end: float) -> list[dict]:
    """Seek original frames for a window missed by slide-change extraction.

    The supplemental cache is separate from the original keyframe index.
    Repeated publication reuses the same video/time files without decoding.
    """
    import cv2
    from ..media import _stamp, _write_jpeg

    stat = video.stat()
    signature = hashlib.sha256(
        f"note-window-v1:{video.name}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()[:20]
    stamps = sorted({round(start + (end - start) * fraction, 3)
                     for fraction in (.25, .5, .75)})
    rows = []
    capture = None
    try:
        for stamp in stamps:
            path = folder / f"window-{signature}-{round(stamp * 1000):09d}.jpg"
            if not path.is_file():
                if capture is None:
                    capture = cv2.VideoCapture(str(video))
                    if not capture.isOpened():
                        raise NoteFrameError("无法打开原录像以补充课堂画面；文字笔记已保留。")
                fps = capture.get(cv2.CAP_PROP_FPS)
                if not math.isfinite(fps) or fps <= 0:
                    raise NoteFrameError("原录像帧率无法读取；文字笔记已保留。")
                if not capture.set(cv2.CAP_PROP_POS_FRAMES, round(stamp * fps)):
                    raise NoteFrameError("原录像无法定位到本段时间；文字笔记已保留。")
                ok, frame = capture.read()
                actual = (capture.get(cv2.CAP_PROP_POS_FRAMES) - 1) / fps
                if (not ok or not math.isfinite(actual)
                        or abs(actual - stamp) > max(.1, 1 / fps)):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                staged = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                try:
                    if not _write_jpeg(cv2, staged, frame):
                        raise NoteFrameError("课堂画面保存失败；文字笔记已保留，请重试。")
                    staged.replace(path)
                finally:
                    staged.unlink(missing_ok=True)
            score = _visual_score(path)
            if score >= 1.6:
                rows.append({"path": path, "timestamp": stamp,
                             "time": _stamp(stamp), "score": score})
    finally:
        if capture is not None:
            capture.release()
    return rows

def _window_frame(frames, start, end, directory):
    """Only a saved original note window may trigger local supplemental frames."""
    if directory is not None and not any(start <= row["timestamp"] < end for row in frames):
        video = directory / "video.mp4"
        if video.is_file():
            try:
                sampled = _sample_window(video, directory / "note-frames", start, end)
            except OSError as exc:
                raise NoteFrameError("课堂画面保存失败；文字笔记已保留，请重试。") from exc
            return _choose(sampled, start, end)
    return _choose(frames, start, end)

def _organized_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Use saved original packet times and block positions, not prose matching."""
    from ..transcript_notes import POLICY, _read, _body, fingerprint, report_path

    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") != POLICY or report.get("status") != "organized":
            return None
        body = notes_path.read_text("utf-8")
        if report["note_sha256"] != fingerprint(body):
            return None
        cache = notes_path.parent / ".notes-parts" / POLICY
        records = [_read(cache, key) for key in report["requests"]]
        if not records or any(row is None for row in records):
            return None
        responses = [row["response"] for row in records]
        if _body(report["title"], responses) != body:
            return None
        prefix_count = len(markdown_to_blocks(f"# {report['title']}\n\n"))
        counts = [len(markdown_to_blocks(text)) for text in responses]
        body_count = len(markdown_to_blocks(body))
        if prefix_count + sum(counts) != body_count:
            # A literal unfinished Markdown fence may span responses. Keep
            # the older picture gallery rather than guessing its placement.
            return None
        plain = [block for block in blocks if block.get("type") not in {"image", "video"}]
        if len(plain) < body_offset + body_count:
            return None
        # Publication's own source notices precede the exact submitted body.
        # Existing pictures do not affect these ordered text-block offsets.
        offset = body_offset + prefix_count
        placements = []
        for record, count in zip(records, counts):
            rows = record["input"]["transcript"]
            start = min(float(row["start"]) for row in rows)
            end = max(float(row["end"]) for row in rows)
            frame = _window_frame(frames, start, end, directory)
            if frame is not None and count:
                placements.append((plain[offset]["id"], frame))
            offset += count
        return placements
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def _topic_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Optional heading time labels locate pictures; prose is never graded.

    Missing labels leave the older gallery available. Only the submitted body
    is considered, so a student's appended heading cannot acquire an image.
    """
    from ..transcript_notes import TOPIC_POLICY, _topic_read, _body, fingerprint, report_path

    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") != TOPIC_POLICY or report.get("status") != "organized":
            return None
        body = notes_path.read_text("utf-8")
        cache = notes_path.parent / ".notes-parts" / TOPIC_POLICY
        merged = _topic_read(cache, report["merge_request"])
        if (merged is None or merged["stage"] != "merge" or report["note_sha256"] != fingerprint(body)
                or _body(report["title"], [merged["response"]]) != body):
            return None
        rows = merged["input"]["original_transcript"]
        lower = min(float(row["start"]) for row in rows)
        upper = max(float(row["end"]) for row in rows)
        plain = [b for b in blocks if b.get("type") not in {"image", "video"}]
        count = len(markdown_to_blocks(body))
        if len(plain) < body_offset + count:
            return None
        placements = []
        timed_windows = 0
        for block in plain[body_offset:body_offset + count]:
            if block.get("type") not in {"heading_1", "heading_2", "heading_3"}:
                continue
            label = _text(block).rsplit("[", 1)[-1]
            if not label.endswith("]"):
                continue
            match = _WINDOW.fullmatch(label[:-1])
            if not match:
                continue
            start, end = _seconds(match[1], match[2]), _seconds(match[3], match[4])
            if not lower <= start < end <= upper + 1:
                continue
            timed_windows += 1
            frame = _window_frame(frames, start, end, directory)
            if frame is not None:
                placements.append((block["id"], frame))
        return placements if timed_windows else None
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def _knowledge_placements(blocks, frames, notes_path, body_offset, directory=None):
    """Use verified source intervals and exact body offsets, not model labels."""
    from ..knowledge_workflow import POLICIES, saved_sections
    from ..note_editor import report_path
    try:
        if type(body_offset) is not int or body_offset < 0:
            return None
        report = json.loads(report_path(notes_path).read_text("utf-8"))
        if report.get("policy") not in POLICIES:
            return None
        body, sections = saved_sections(notes_path)
        if notes_path.read_text("utf-8") != body:
            return None
        prefix = len(markdown_to_blocks(f"# {report['title']}\n\n"))
        def text_count(markdown):
            return sum(b.get("type") not in {"image", "video"} for b in markdown_to_blocks(markdown))
        counts = [text_count(s["markdown"]) for s in sections]
        if prefix + sum(counts) != text_count(body):
            return None
        plain = [b for b in blocks if b.get("type") not in {"image", "video"}]
        if len(plain) < body_offset + prefix + sum(counts):
            return None
        offset, placements = body_offset + prefix, []
        for section, count in zip(sections, counts):
            inside = any(section["start"] <= f["timestamp"] < section["end"] for f in frames)
            available_video = directory is not None and (directory / "video.mp4").is_file()
            frame = (_window_frame(frames, section["start"], section["end"], directory)
                     if inside or available_video else None)
            if frame is not None and count:
                placements.append((plain[offset]["id"], frame))
            offset += count
        return placements
    except NoteFrameError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
        return None

def publish_note_images(notion: NotionClient, note_id: str, directory: Path, *, notes_path: Path | None = None,
                        body_offset: int | None = None) -> int:
    """Insert one slide per time window; retries skip captions already present."""
    frames = _frames(directory)
    if not frames and not (directory / "video.mp4").is_file():
        return 0
    blocks = notion.list_children(note_id)
    existing = {
        "".join(part.get("plain_text") or part.get("text", {}).get("content", "")
                for part in (block.get("image") or {}).get("caption") or [])
        for block in blocks if block.get("type") == "image"
    }
    placements = _knowledge_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    if placements is None:
        placements = _organized_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    if placements is None:
        placements = _topic_placements(blocks, frames, notes_path or directory / "notes.md", body_offset, directory)
    organized = placements is not None
    placements = placements or []
    for block in ([] if organized else blocks):
        if block.get("type") != "heading_2":
            continue
        match = _WINDOW.fullmatch(_text(block))
        if not match:
            continue
        start = _seconds(match[1], match[2])
        end = _seconds(match[3], match[4])
        frame = _choose(frames, start, end)
        if frame:
            placements.append((block["id"], frame))
    if not placements and not organized:
        # Notes imported from an earlier writer may lack timed headings.
        placements = [(None, frame) for frame in frames[::max(1, len(frames) // 6)][:6]]
    inserted = 0
    for after, frame in placements:
        caption = f"{_CAPTION_PREFIX}{frame['time']} · 来自课程录像"
        if caption in existing:
            continue
        reference = notion.upload_file(frame["path"])
        image = markdown_to_blocks(f"![{caption}]({reference})")[0]
        notion.append_blocks(note_id, [image], after=after, retry=False)
        existing.add(caption)
        inserted += 1
    return inserted



_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _visual_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _visual_rows(payload, key):
    rows = payload.get(key, []) if isinstance(payload, dict) else payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _local_material(job, name):
    """Resolve a local PDF by filename or catalog title, confined to materials."""
    from ..store import safe_name

    clean_name = str(name or "").strip()
    if not clean_name:
        return None
    local_mat = (job.course_dir / "materials").resolve()
    shared_rec_dir = _shared_seed_recording_dir(job)
    roots: list[tuple[Path, Path, Path]] = [(job.course_dir.resolve(), local_mat, job.directory)]
    if shared_rec_dir is not None:
        shared_course = shared_rec_dir.parent.parent.resolve()
        shared_mat = (shared_course / "materials").resolve()
        if shared_mat != local_mat and shared_mat.is_dir():
            roots.append((shared_course, shared_mat, shared_rec_dir))

    for course_root, folder, rec_dir in roots:
        if not folder.is_dir():
            continue
        candidates: list[Path] = [folder / clean_name]
        target_names: set[str] = {clean_name, safe_name(clean_name)}
        for row in _visual_rows(_visual_json(folder / "index.json", []), "materials"):
            assets = [str(f) for f in (row.get("files") or []) if isinstance(f, str)]
            assets += [str(a.get("filename") or "") for a in (row.get("attachments") or []) if isinstance(a, dict)]
            row_path = str(row.get("path") or "")
            if clean_name in [row.get("title"), row_path, *assets]:
                for f in assets:
                    if not f:
                        continue
                    target_names.add(f)
                    target_names.add(safe_name(f))
                    candidates.append(folder / f)
                    if row_path:
                        candidates.append(folder / safe_name(row_path) / f)
                        candidates.append(folder / row_path / f)
                if row_path:
                    candidates.append(folder / row_path)
        for receipt_path in (rec_dir / "keyframes/keyframe-matches.json", job.directory / "keyframes/keyframe-matches.json"):
            receipt = _visual_json(receipt_path, {})
            for row in _visual_rows(receipt, "matches"):
                if clean_name in (row.get("title"), row.get("filename"), row.get("material_path")):
                    if row.get("filename"):
                        target_names.add(str(row["filename"]))
                        target_names.add(safe_name(str(row["filename"])))
                    if row.get("local_path"):
                        candidates.append(course_root / str(row["local_path"]))
                        candidates.append(job.course_dir / str(row["local_path"]))
        sources_root = folder / ".lecture-sources"
        if sources_root.is_dir():
            for pdf_file in sources_root.rglob("*.pdf"):
                pname = pdf_file.name
                if any(
                    t and (pname == t or pname.endswith("-" + t))
                    for t in target_names
                ):
                    candidates.append(pdf_file)
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(folder) and resolved.is_file() and resolved.suffix.lower() == ".pdf":
                return resolved
    return None


def _slide_cache(job, name, page):
    from ..store import safe_name
    return job.directory / "keyframes/slides-cache" / f"{safe_name(name, 'slide')}_p{page}"


def _saved_visual_windows(job, markdown):
    """Only use saved intervals when their associated note text still matches."""
    from ..knowledge_workflow import saved_sections
    try:
        body, sections = saved_sections(job.directory / "notes.md")
        if body == markdown:
            return sections
    except RuntimeError:
        pass
    from ..transcript_notes import POLICY, _read, report_path, fingerprint
    report = _visual_json(report_path(job.directory / "notes.md"), {})
    if not isinstance(report, dict) or report.get("policy") != POLICY or report.get("note_sha256") != fingerprint(markdown):
        return []
    sections = []
    try:
        for key in report["requests"]:
            row = _read(job.directory / ".notes-parts" / POLICY, key)
            transcript = row["input"]["transcript"]
            sections.append({"markdown": row["response"],
                             "start": min(float(r["start"]) for r in transcript),
                             "end": max(float(r["end"]) for r in transcript)})
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return []
    return sections


_DEFAULT_VISUAL_SCORE = _visual_score
_DEFAULT_SAVED_VISUAL_WINDOWS = _saved_visual_windows


def _recording_identity_for_dir(
    course_dir: Path, rec_dir: Path, recording=None
) -> dict[str, Any]:
    """Resolve strong course/recording identity metadata for cross-tenant isolation."""
    from ..store import safe_name

    course_id = str(getattr(recording, "course_id", "") or "").strip() if recording is not None else ""
    source_id = str(getattr(recording, "source_id", "") or "").strip() if recording is not None else ""
    subject_id = str(getattr(recording, "subject_id", "") or "").strip() if recording is not None else ""
    recorded_at = str(getattr(recording, "recorded_at", "") or "").strip() if recording is not None else ""
    teacher = str(getattr(recording, "teacher", "") or "").strip() if recording is not None else ""
    room = str(getattr(recording, "room", "") or "").strip() if recording is not None else ""

    cmeta = _visual_json(course_dir / "course.json", {})
    if isinstance(cmeta, dict) and not course_id:
        course_id = str(cmeta.get("course_id") or cmeta.get("id") or "").strip()

    rmeta = _visual_json(rec_dir / "recording.json", {})
    if isinstance(rmeta, dict):
        if not course_id:
            course_id = str(rmeta.get("course_id") or "").strip()
        if not source_id:
            source_id = str(rmeta.get("source_id") or "").strip()
        if not subject_id:
            subject_id = str(rmeta.get("subject_id") or "").strip()
        if not recorded_at:
            recorded_at = str(rmeta.get("recorded_at") or "").strip()
        if not teacher:
            teacher = str(rmeta.get("teacher") or "").strip()
        if not room:
            room = str(rmeta.get("room") or "").strip()

    ambiguous_timestamp = False
    raw_index = _visual_json(course_dir / "recordings" / "index.json", [])
    if isinstance(raw_index, list):
        rows = [row for row in raw_index if isinstance(row, dict)]
        slug_matches = []
        for row in rows:
            ra = str(row.get("recorded_at") or "").strip()
            title = str(row.get("title") or "").strip()
            date_part = ra.split(" ")[0] if ra else ""
            base = f"{date_part}_{title}".strip("_")
            if safe_name(base, "recording") == rec_dir.name:
                slug_matches.append(row)
        if len(slug_matches) >= 2:
            ambiguous_timestamp = True
        elif len(slug_matches) == 1:
            row = slug_matches[0]
            if not course_id:
                course_id = str(row.get("course_id") or "").strip()
            if not source_id:
                source_id = str(row.get("source_id") or "").strip()
            if not subject_id:
                subject_id = str(row.get("subject_id") or "").strip()
            if not recorded_at:
                recorded_at = str(row.get("recorded_at") or "").strip()
            if not teacher:
                teacher = str(row.get("teacher") or "").strip()
            if not room:
                room = str(row.get("room") or "").strip()
        if recorded_at:
            count_by_recorded_at = sum(
                1 for row in rows if str(row.get("recorded_at") or "").strip() == recorded_at
            )
            if count_by_recorded_at >= 2:
                ambiguous_timestamp = True

    return {
        "course_id": course_id,
        "source_id": source_id,
        "subject_id": subject_id,
        "recorded_at": recorded_at,
        "teacher": teacher,
        "room": room,
        "ambiguous_timestamp": ambiguous_timestamp,
    }


def _shared_seed_recording_dir(job) -> Path | None:
    """Shared seed recording directories are only used in multi-tenant mode (disabled here)."""
    return None


def hydrate_tenant_job_from_shared(job) -> bool:
    """No-op in the single-user local BYOK client."""
    return False


def prune_unreferenced_keyframes(
    job, visuals: list[dict] | None, markdown: str, *, max_keep: int = 48
) -> int:
    """Prune unreferenced redundant keyframes after processing while keeping cache consistency."""
    if not visuals or max_keep < 1:
        return 0
    folder = job.directory / "keyframes"
    if not folder.is_dir():
        return 0

    image_files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    )
    existing_names = {p.name for p in image_files}
    thumbs_dir = folder / ".thumbs"
    if len(image_files) <= max_keep:
        shutil.rmtree(thumbs_dir, ignore_errors=True)
        return 0

    keep_names: set[str] = set()

    def _add_name(candidate: str) -> None:
        raw = str(candidate or "").strip()
        if not raw:
            return
        name = Path(unquote(raw.split("?", 1)[0].split("#", 1)[0])).name
        if name in existing_names:
            keep_names.add(name)

    for item in visuals:
        if not isinstance(item, dict):
            continue
        _add_name(item.get("frame_file") or "")
        f_url = str(item.get("frame_url") or "")
        if "/frames/" in f_url:
            _add_name(f_url.split("/frames/", 1)[1])

    texts_to_scan = [markdown or ""]
    for note_path in (
        job.directory / "notes.md",
        job.directory / "refined-notes.md",
        job.directory / "refined-notes.json",
    ):
        if note_path.is_file():
            try:
                texts_to_scan.append(note_path.read_text("utf-8", errors="ignore"))
            except OSError:
                pass
    for txt in texts_to_scan:
        if not txt:
            continue
        for match in re.findall(r'/frames/([^)\s"\'?#]+)', txt):
            _add_name(match)
        for _, url in re.findall(r"!\[([^\]]*)\]\(([^\s)]+)\)", txt):
            _add_name(url)

    def _collect_json_refs(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k in existing_names:
                    keep_names.add(k)
                if k in ("file", "frame_file", "filename", "path", "frame_url") and isinstance(v, str):
                    if "/frames/" in v:
                        _add_name(v.split("/frames/", 1)[1])
                    else:
                        _add_name(v)
                else:
                    _collect_json_refs(v)
        elif isinstance(obj, list):
            for entry in obj:
                _collect_json_refs(entry)

    for json_path in (
        job.directory / "board-notes.json",
        folder / "board-notes.json",
        folder / "keyframe-matches.json",
        folder / "slide-alignment.json",
    ):
        if json_path.is_file():
            _collect_json_refs(_visual_json(json_path, None))

    if not keep_names:
        return 0

    index_path = folder / "index.json"
    raw_index = _visual_json(index_path, None) if index_path.is_file() else None
    ordered_names: list[str] = []
    seen_ordered: set[str] = set()
    if raw_index is not None:
        rows = _visual_rows(raw_index, "keyframes")
        timed_rows: list[tuple[float, str]] = []
        for idx, row in enumerate(rows):
            fname = str(row.get("file") or row.get("frame_file") or "").strip()
            if fname in existing_names and fname not in seen_ordered:
                seen_ordered.add(fname)
                try:
                    ts = float(row.get("timestamp", idx))
                except (TypeError, ValueError):
                    ts = float(idx)
                timed_rows.append((ts, fname))
        timed_rows.sort(key=lambda pair: (pair[0], pair[1]))
        ordered_names = [pair[1] for pair in timed_rows]
    for p in image_files:
        if p.name not in seen_ordered:
            ordered_names.append(p.name)

    remaining = [name for name in ordered_names if name not in keep_names]
    slots_left = max_keep - len(keep_names)
    if slots_left > 0 and remaining:
        if len(remaining) <= slots_left:
            keep_names.update(remaining)
        else:
            step = len(remaining) / float(slots_left)
            for i in range(slots_left):
                pick_idx = min(len(remaining) - 1, int(i * step + step * 0.5))
                keep_names.add(remaining[pick_idx])

    removed = 0
    for p in image_files:
        if p.name not in keep_names:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass

    shutil.rmtree(thumbs_dir, ignore_errors=True)

    if removed > 0:
        if raw_index is not None:
            new_index = None
            if isinstance(raw_index, list):
                new_index = [
                    r for r in raw_index
                    if isinstance(r, dict) and str(r.get("file") or r.get("frame_file") or "") in keep_names
                ]
            elif isinstance(raw_index, dict) and isinstance(raw_index.get("keyframes"), list):
                updated_rows = [
                    r for r in raw_index["keyframes"]
                    if isinstance(r, dict) and str(r.get("file") or r.get("frame_file") or "") in keep_names
                ]
                new_index = {**raw_index, "keyframes": updated_rows}
            if new_index is not None:
                tmp_idx = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex[:8]}.tmp")
                try:
                    tmp_idx.write_text(json.dumps(new_index, ensure_ascii=False, indent=1), encoding="utf-8")
                    os.replace(tmp_idx, index_path)
                except OSError:
                    tmp_idx.unlink(missing_ok=True)

        cache_file = folder / ".visual-sections-cache.json"
        new_sig = _visual_cache_signature(job, markdown)
        tmp_cache = cache_file.with_name(f".{cache_file.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp_cache.write_text(
                json.dumps({"sig": new_sig, "visuals": visuals}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_cache, cache_file)
        except OSError:
            tmp_cache.unlink(missing_ok=True)

    return removed


def _visual_cache_signature(
    job, markdown: str, prefix: str = "v6:", *, include_mtime: bool = False
) -> str:
    h = hashlib.sha256()
    h.update(prefix.encode("utf-8"))
    h.update(markdown.encode("utf-8", errors="replace"))
    course_dir = getattr(job, "course_dir", job.directory.parent.parent)
    shared_rec_dir = _shared_seed_recording_dir(job)
    kf_dir = job.directory / "keyframes"
    if (
        shared_rec_dir is not None
        and (not kf_dir.is_dir() or not any(p.suffix.lower() in _IMAGE_SUFFIXES for p in kf_dir.iterdir() if p.is_file()))
        and (shared_rec_dir / "keyframes").is_dir()
    ):
        kf_dir = shared_rec_dir / "keyframes"
    nf_dir = job.directory / "note-frames"
    if shared_rec_dir is not None and not nf_dir.is_dir() and (shared_rec_dir / "note-frames").is_dir():
        nf_dir = shared_rec_dir / "note-frames"
    mat_dir = course_dir / "materials"
    if shared_rec_dir is not None and not mat_dir.is_dir() and (shared_rec_dir.parent.parent / "materials").is_dir():
        mat_dir = shared_rec_dir.parent.parent / "materials"
    for sub in (
        kf_dir,
        nf_dir,
        kf_dir / "slides-cache",
        mat_dir,
    ):
        try:
            if sub.is_dir():
                for p in sorted(sub.iterdir(), key=lambda x: x.name):
                    if p.name.startswith(".visual-sections-cache") or p.name.startswith(".thumbs"):
                        continue
                    try:
                        st = p.stat()
                        if include_mtime:
                            h.update(f"|{p.name}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8"))
                        elif p.suffix.lower() == ".json" and st.st_size <= 65536 and sub.name == "keyframes":
                            h.update(f"|{p.name}:".encode("utf-8") + p.read_bytes())
                        else:
                            h.update(f"|{p.name}:{st.st_size}".encode("utf-8"))
                    except OSError:
                        continue
        except OSError:
            continue
    return h.hexdigest()


def _rewrite_visual_urls(visuals: list[dict], base: str) -> list[dict]:
    out = []
    for item in visuals:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        f_url = str(row.get("frame_url") or "")
        if "/frames/" in f_url:
            row["frame_url"] = f"{base}/frames/{f_url.split('/frames/', 1)[1]}"
        s_url = str(row.get("slide_url") or "")
        if "/slide-page?" in s_url:
            row["slide_url"] = f"{base}/slide-page?{s_url.split('/slide-page?', 1)[1]}"
        out.append(row)
    return out


def build_note_visual_sections(job, course_id: str, recording_key: str, markdown: str, *, fast_only: bool = False) -> list[dict] | None:
    """Link locally available pictures to note headings without a score cutoff."""
    from types import SimpleNamespace
    from urllib.parse import quote
    from ..board_ocr import (
        find_static_leftover_boards,
        is_blackboard_frame,
        load_cached_board_notes,
        transcript_board_summary,
    )

    base = f"/api/campus/courses/{quote(course_id, safe='')}/recordings/{quote(recording_key, safe='')}"
    folder = job.directory / "keyframes"
    shared_rec_dir = _shared_seed_recording_dir(job)
    if (
        shared_rec_dir is not None
        and (not folder.is_dir() or not any(p.suffix.lower() in _IMAGE_SUFFIXES for p in folder.iterdir() if p.is_file()))
        and (shared_rec_dir / "keyframes").is_dir()
    ):
        folder = shared_rec_dir / "keyframes"

    cache_file = folder / ".visual-sections-cache.json"
    sig_key = _visual_cache_signature(job, markdown)
    use_disk_cache = (
        _visual_score is _DEFAULT_VISUAL_SCORE
        and _saved_visual_windows is _DEFAULT_SAVED_VISUAL_WINDOWS
    )
    cache_candidates = [cache_file, job.directory / ".visual-sections-cache.json"]
    if shared_rec_dir is not None:
        cache_candidates.extend([
            shared_rec_dir / "keyframes" / ".visual-sections-cache.json",
            shared_rec_dir / ".visual-sections-cache.json",
        ])
    valid_sigs: set[str] | None = None
    for candidate in dict.fromkeys(cache_candidates):
        if not use_disk_cache or not candidate.is_file():
            continue
        try:
            cached_payload = json.loads(candidate.read_text("utf-8"))
            if isinstance(cached_payload, dict) and isinstance(cached_payload.get("visuals"), list):
                cached_vis = cached_payload["visuals"]
                cached_sig = cached_payload.get("sig", "")
                if valid_sigs is None:
                    rec_json = job.directory / "recording.json"
                    alt_rec_id = ""
                    if rec_json.is_file():
                        try:
                            rd = json.loads(rec_json.read_text("utf-8"))
                            alt_rec_id = rd.get("id") or rd.get("source_id") or ""
                        except Exception:
                            pass
                    sig_jobs = [job]
                    if shared_rec_dir is not None:
                        sig_jobs.append(SimpleNamespace(directory=shared_rec_dir, course_dir=shared_rec_dir.parent.parent))
                    valid_sigs = {sig_key}
                    for sjob in sig_jobs:
                        for prefix in ("v6:", "v5:", "v4:", "v3:"):
                            for mtime in (False, True):
                                valid_sigs.add(_visual_cache_signature(sjob, markdown, prefix, include_mtime=mtime))
                        valid_sigs.add(_visual_cache_signature(sjob, markdown, f"v2:{course_id}:{recording_key}:", include_mtime=True))
                        if alt_rec_id:
                            valid_sigs.add(_visual_cache_signature(sjob, markdown, f"v2:{course_id}:{alt_rec_id}:", include_mtime=True))
                if cached_sig in valid_sigs and (cached_vis or not folder.is_dir() or not any(p.suffix.lower() in _IMAGE_SUFFIXES for p in folder.iterdir() if p.is_file())):
                    if cached_sig != sig_key:
                        try:
                            cache_file.write_text(
                                json.dumps({"sig": sig_key, "visuals": cached_vis}, ensure_ascii=False),
                                encoding="utf-8",
                            )
                        except OSError:
                            pass
                    return _rewrite_visual_urls(cached_vis, base)
        except Exception:
            pass

    if fast_only:
        image_count = 0
        for directory in dict.fromkeys([folder, job.directory / "note-frames"] + (
            [shared_rec_dir / "note-frames"] if shared_rec_dir is not None else []
        )):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.suffix.lower() in _IMAGE_SUFFIXES and path.is_file():
                    image_count += 1
                    if image_count > 15:
                        return None

    static_leftover: set[str] | None = None

    def get_static_leftover() -> set[str]:
        nonlocal static_leftover
        if static_leftover is None:
            static_leftover = find_static_leftover_boards(folder)
        return static_leftover

    search_dirs = [folder, job.directory / "note-frames"]
    if shared_rec_dir is not None:
        search_dirs.append(shared_rec_dir / "note-frames")
    allowed_roots = [job.directory.resolve()]
    if shared_rec_dir is not None:
        allowed_roots.append(shared_rec_dir.resolve())

    frames = []
    for item in _visual_rows(_visual_json(folder / "index.json", {}), "keyframes"):
        name = item.get("file")
        if (not isinstance(name, str) or Path(name).name != name or "\\" in name
                or Path(name).suffix.lower() not in _IMAGE_SUFFIXES):
            continue
        path = next((d / name for d in search_dirs if (d / name).is_file()), None)
        if path is None or not any(path.resolve().is_relative_to(r) for r in allowed_roots):
            continue
        try:
            stamp = float(item.get("timestamp", 0))
            if not math.isfinite(stamp) or stamp < 0:
                continue
        except (ValueError, TypeError):
            continue
        if _is_useless_visual_frame(path, is_board=False):
            is_board = is_blackboard_frame(path) and (name not in get_static_leftover())
            if not is_board or _is_useless_visual_frame(path, is_board=True):
                continue
        frames.append({**item, "file": name, "timestamp": stamp, "path": path})
    frames.sort(key=lambda f: f["timestamp"])
    headings = re.findall(r"^#{2,3} +(.+?)\s*$", markdown, re.M)
    # Container headings such as '课堂重点' have no content of their own.
    if re.search(r"^### +", markdown, re.M):
        headings = [h for h in headings if h not in {"课堂重点", "课程笔记", "本讲总结"}]
    if len(headings) > 1:
        headings = [h for h in headings if "复习建议" not in h]
    if not frames or not headings:
        try:
            if use_disk_cache and folder.is_dir():
                cache_file.write_text(
                    json.dumps({"sig": sig_key, "visuals": []}, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            pass
        return []

    sections = _saved_visual_windows(job, markdown)
    anchors = _visual_rows(_visual_json(folder / "slide-alignment.json", {}), "anchors")
    matches = _visual_rows(_visual_json(folder / "keyframe-matches.json", {}), "matches")
    cached_board_notes = load_cached_board_notes(folder)
    transcript = _visual_json(job.directory / "transcript.json", {})
    materials = None
    base = f"/api/campus/courses/{quote(course_id, safe='')}/recordings/{quote(recording_key, safe='')}"
    visuals = []

    section_bodies: dict[str, str] = {}
    parent_windows: dict[str, tuple[int, int]] = {}
    l2_has_l3: set[str] = set()
    active_l2: str | None = None
    active_l2_window: tuple[int, int] | None = None
    current_head = None
    current_lines: list[str] = []
    for raw_line in markdown.splitlines():
        head_match = re.match(r"^(#{2,3}) +(.+?)\s*$", raw_line)
        if head_match:
            if current_head is not None:
                section_bodies[current_head] = "\n".join(current_lines)
            hashes, head_text = head_match.group(1), head_match.group(2)
            current_head = head_text
            current_lines = []
            if hashes == "##":
                active_l2 = head_text
                m_win = re.search(r"\[(\d+):(\d{2})\s*[–—-]\s*(\d+):(\d{2})\]", head_text)
                active_l2_window = (
                    (_seconds(m_win[1], m_win[2]), _seconds(m_win[3], m_win[4])) if m_win else None
                )
            elif hashes == "###" and active_l2 is not None:
                l2_has_l3.add(active_l2)
                if active_l2_window is not None:
                    parent_windows[head_text] = active_l2_window
        elif current_head is not None:
            current_lines.append(raw_line)
    if current_head is not None:
        section_bodies[current_head] = "\n".join(current_lines)

    if l2_has_l3:
        headings = [h for h in headings if h not in l2_has_l3]
    if not headings:
        return []

    def score(frame):
        if "_preview_score" not in frame:
            try:
                frame["_preview_score"] = _visual_score(frame["path"])
            except (ImportError, OSError, ValueError):
                frame["_preview_score"] = 0
        return frame["_preview_score"]

    def content_sig(frame):
        if "_content_sig" not in frame:
            frame["_content_sig"] = _frame_content_signature(frame["path"])
        return frame["_content_sig"]

    has_timed_headings = bool(sections) or any(
        re.search(r"\[(\d+):(\d{2})\s*[–—-]\s*(\d+):(\d{2})\]", h) for h in headings
    )
    used_files: set[str] = set()
    used_sigs: list[tuple] = []
    used_board_sigs: list[tuple] = []

    def _is_wide_board_frame(f: dict) -> bool:
        return bool(_is_useless_visual_frame(f["path"], is_board=False) and is_blackboard_frame(f["path"]))

    for i, heading in enumerate(headings):
        window = None
        timed = re.search(r"\[(\d+):(\d{2})\s*[–—-]\s*(\d+):(\d{2})\]", heading)
        if timed:
            window = (_seconds(timed[1], timed[2]), _seconds(timed[3], timed[4]))
        else:
            for section in sections:
                if heading in re.findall(r"^#{2,3} +(.+?)\s*$", section["markdown"], re.M):
                    window = (section["start"], section["end"])
                    break
            if window is None and heading in parent_windows:
                window = parent_windows[heading]
        if window:
            candidates = [
                f for f in frames
                if window[0] <= f["timestamp"] < window[1] and f["file"] not in used_files
            ]
            if used_board_sigs:
                fresh_candidates = []
                for f in candidates:
                    if _is_wide_board_frame(f):
                        bsig = _board_panel_signature(f["path"])
                        if bsig is not None and any(_are_same_board_panel(bsig, ubs) for ubs in used_board_sigs):
                            continue
                    fresh_candidates.append(f)
                candidates = fresh_candidates
            inside = [
                f for f in candidates
                if not any(_are_near_duplicate_frames(content_sig(f), us) for us in used_sigs)
            ]
            if not inside and candidates:
                inside = [
                    f for f in candidates
                    if not (used_sigs and _are_near_duplicate_frames(content_sig(f), used_sigs[-1]))
                ] or candidates
            if not inside:
                continue
            frame = max(inside, key=score) if len(inside) > 1 else inside[0]
            used_files.add(frame["file"])
            sig = content_sig(frame)
            if sig is not None:
                used_sigs.append(sig)
        elif has_timed_headings:
            continue
        else:
            # Fewer pictures than topics: attach each once, in source order.
            if len(frames) < len(headings):
                indices = [round(j * (len(headings) - 1) / max(len(frames) - 1, 1)) for j in range(len(frames))]
                if i not in indices:
                    continue
                frame = frames[indices.index(i)]
            else:
                frame = frames[min(len(frames) - 1, i * len(frames) // len(headings))]
        stamp = frame["timestamp"]
        anchor = frame.get("anchor") if isinstance(frame.get("anchor"), dict) else {}
        if not anchor:
            for row in anchors:
                try:
                    start, end = float(row["start"]), float(row["end"])
                    if start <= stamp < end:
                        anchor = row
                        break
                except (KeyError, TypeError, ValueError):
                    continue
        if not anchor and window:
            overlapping = []
            for row in anchors:
                try:
                    overlap = min(window[1], float(row["end"])) - max(window[0], float(row["start"]))
                    if overlap > 0:
                        overlapping.append((overlap, row))
                except (KeyError, TypeError, ValueError):
                    continue
            if overlapping:
                anchor = max(overlapping, key=lambda pair: pair[0])[1]
        title = str(anchor.get("slide_title") or anchor.get("title") or frame.get("slide_title") or frame.get("slide_file") or "")
        page = anchor.get("slide_page") or anchor.get("page") or frame.get("slide_page")
        if not title and matches:
            match = next((r for r in matches if r.get("file") == frame["file"] or r.get("frame_file") == frame["file"]), None)
            if match is None and len(matches) == 1:
                match = matches[0]
            if match:
                title = str(match.get("slide_title") or match.get("title") or match.get("filename") or match.get("material_path") or "")
                pages = match.get("pages") or []
                page = match.get("slide_page") or match.get("page") or (pages[0] if len(pages) == 1 else None)
        if not title:
            if materials is None:
                materials = _fast_lesson_materials(job, recording_key)
            if len(materials) == 1:
                title = materials[0].get("title") or ""
        try:
            page = int(page) if page is not None else None
            if page is not None and page < 1:
                page = None
        except (ValueError, TypeError):
            page = None
        slide_url = ""
        if title and page:
            cache = _slide_cache(job, title, page)
            if any(Path(str(cache) + ext).is_file() for ext in (".jpg", ".png")) or _local_material(job, title):
                slide_url = f"{base}/slide-page?file={quote(title, safe='')}&page={page}"
        time = f"{int(stamp) // 60:02d}:{int(stamp) % 60:02d}"
        loaded_f = _load_small_frame(frame["path"])
        has_wide_proj = loaded_f is not None and detect_wide_classroom_projector_box(loaded_f[0], loaded_f[1]) is not None
        has_tr_pip = loaded_f is not None and _has_tr_pip(loaded_f[1])
        if has_wide_proj and not is_blackboard_frame(frame["path"]):
            is_board = False
        else:
            is_board = is_blackboard_frame(frame["path"]) and (
                frame["file"] not in get_static_leftover()
            )
        if is_board:
            bsig = _board_panel_signature(frame["path"])
            if bsig is not None:
                used_board_sigs.append(bsig)
        cached_entry = cached_board_notes.get(frame["file"])
        if not isinstance(cached_entry, dict) and is_board and cached_board_notes:
            nearby_entries = []
            for entry in cached_board_notes.values():
                if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
                    continue
                try:
                    ets = float(entry.get("timestamp", -1e9))
                except (TypeError, ValueError):
                    continue
                in_win = bool(window and window[0] <= ets < window[1])
                if in_win or abs(ets - stamp) <= 90.0:
                    nearby_entries.append((0 if in_win else 1, abs(ets - stamp), entry))
            if nearby_entries:
                nearby_entries.sort(key=lambda item: (item[0], item[1]))
                cached_entry = nearby_entries[0][2]
        cached_text = str(cached_entry.get("text") or "").strip() if isinstance(cached_entry, dict) else ""
        board_note = str(frame.get("board_note") or cached_text or "").strip()
        if not board_note and is_board:
            board_note = transcript_board_summary(
                transcript, stamp, section_bodies.get(heading, ""), window=window
            )
        if is_board and not slide_url:
            caption = f"课堂板书特写 · {time}"
        elif has_tr_pip and not is_board:
            caption = f"课堂投影画面 · {time}"
        else:
            caption = f"课堂板书/投影画面 · {time}"
        visuals.append({"heading": heading, "timestamp": stamp, "time": time,
                        "frame_url": f"{base}/frames/{quote(frame['file'], safe='')}",
                        "frame_caption": caption,
                        "is_blackboard": is_board,
                        "board_note": board_note,
                        "slide_title": title, "slide_page": page, "slide_url": slide_url,
                        "slide_caption": (f"对应课件：{title} · 第 {page} 页" if page else f"关联课件：{title}") if title else ""})
    try:
        if use_disk_cache and folder.is_dir():
            cache_file.write_text(
                json.dumps({"sig": sig_key, "visuals": visuals}, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception:
        pass
    return visuals


def _fast_lesson_materials(job, recording_key: str) -> list[dict]:
    """Fast local lookup of materials assigned to `recording_key` without invoking full `campus_course` PDF handbook parsing."""
    try:
        from ..lecture_source_download import _override_lecture_ids
        course_dir = getattr(job, "course_dir", job.directory.parent.parent)
        raw_materials = _visual_json(course_dir / "materials" / "index.json", [])
        if not isinstance(raw_materials, list):
            return []
        overrides = _visual_json(course_dir / "materials" / "matches.json", {})
        if not isinstance(overrides, dict):
            overrides = {}
        matched = []
        for item in raw_materials:
            if not isinstance(item, dict):
                continue
            mpath = str(item.get("path") or "")
            ov = overrides.get(mpath)
            if ov in ("ignore", "unassigned"):
                continue
            lids = _override_lecture_ids(ov)
            if lids and recording_key in lids:
                matched.append({"title": str(item.get("title") or "未命名课件")})
        return matched
    except Exception:
        return []


_MD_IMG_LINE = re.compile(r"^(\s*)!\[([^\]]*)\]\(([^\s)]+)\)(\s*)$")


def rewrite_note_markdown_images(job, course_id: str, recording_key: str, markdown: str) -> str:
    """Resolve relative or legacy local image paths in `notes.md` to valid `/frames/` URLs
    or clean callouts when the target local image file is missing on disk.
    """
    if "![" not in markdown:
        return markdown
    from urllib.parse import quote

    base = f"/api/campus/courses/{quote(course_id, safe='')}/recordings/{quote(recording_key, safe='')}"
    folder = job.directory / "keyframes"
    shared_rec_dir = _shared_seed_recording_dir(job)
    if (
        shared_rec_dir is not None
        and (not folder.is_dir() or not any(p.suffix.lower() in _IMAGE_SUFFIXES for p in folder.iterdir() if p.is_file()))
        and (shared_rec_dir / "keyframes").is_dir()
    ):
        folder = shared_rec_dir / "keyframes"

    search_dirs = [folder, job.directory / "note-frames"]
    if shared_rec_dir is not None:
        search_dirs.append(shared_rec_dir / "note-frames")

    indexed_frames: list[tuple[float, str]] | None = None

    def get_indexed_frames() -> list[tuple[float, str]]:
        nonlocal indexed_frames
        if indexed_frames is not None:
            return indexed_frames
        indexed_frames = []
        for item in _visual_rows(_visual_json(folder / "index.json", {}), "keyframes"):
            fname = item.get("file")
            if isinstance(fname, str) and Path(fname).name == fname and any((d / fname).is_file() for d in search_dirs):
                try:
                    indexed_frames.append((float(item.get("timestamp", 0)), fname))
                except (TypeError, ValueError):
                    pass
        if not indexed_frames and folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES:
                    m_ts = re.search(r"_(\d+)m(\d+)s\.", p.name)
                    ts = (int(m_ts.group(1)) * 60 + int(m_ts.group(2))) if m_ts else 0.0
                    indexed_frames.append((float(ts), p.name))
        return indexed_frames

    api_rec_img_re = re.compile(
        r"^/api/campus/courses/[^/]+/recordings/[^/]+/(frames/[^\s]+|slide-page\?[^\s]+)$"
    )
    out_lines = []
    for line in markdown.splitlines():
        m = _MD_IMG_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        lead, alt, raw_url, tail = m.groups()
        m_api = api_rec_img_re.match(raw_url)
        if m_api:
            out_lines.append(f"{lead}![{alt}]({base}/{m_api.group(1)}){tail}")
            continue
        # Keep explicit /api/ or http(s):// or javascript: (for XSS sanitizer test) untouched unless relative
        if raw_url.startswith(("/api/", "http://", "https://", "javascript:", "data:")):
            out_lines.append(line)
            continue
        if raw_url.startswith("/") and not raw_url.startswith("/notion-illustrations"):
            out_lines.append(line)
            continue
        fname = Path(raw_url).name
        if fname and Path(fname).suffix.lower() in _IMAGE_SUFFIXES and any((d / fname).is_file() for d in search_dirs):
            out_lines.append(f"{lead}![{alt}]({base}/frames/{quote(fname, safe='')}){tail}")
            continue
        # Try matching timestamp from filename (e.g. cns0930_2844.jpg -> 2844s) or alt text (e.g. 47:24｜...)
        target_sec: float | None = None
        m_sec = re.search(r"_(\d{2,5})\.(?:jpg|jpeg|png|webp)$", fname, re.I)
        if m_sec:
            target_sec = float(m_sec.group(1))
        else:
            m_alt_time = re.search(r"(\d{1,3}):(\d{2})", alt)
            if m_alt_time:
                target_sec = float(int(m_alt_time.group(1)) * 60 + int(m_alt_time.group(2)))
        avail = get_indexed_frames()
        if target_sec is not None and avail:
            best_ts, best_file = min(avail, key=lambda pair: abs(pair[0] - target_sec))
            out_lines.append(f"{lead}![{alt}]({base}/frames/{quote(best_file, safe='')}){tail}")
            continue
        # If local file does not exist and no keyframes are extracted yet, render a clean visual note callout instead of a broken 404 image
        clean_alt = alt.strip() or "课堂板书与演示画面"
        out_lines.append(f"{lead}> 📌 **课堂图示/板书要点**：{clean_alt}{tail}")
    return "\n".join(out_lines)
