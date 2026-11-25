"""Blackboard detection, synchronized derivation captioning, and optional VLM formula recognition."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

BOARD_NOTES_FILENAME = "board-notes.json"
MAX_BOARD_OCR_FRAMES = 6

_VISION_SYSTEM = (
    "你是大学课堂板书识别与演算解析助手。老师通常直接在黑板/白板上书写推导而不口头声明‘写板书’。"
    "请直接观察画面中的黑板/白板手写内容，并结合附带的该时刻老师口述上下文，"
    "输出简洁的中文 Markdown 与标准 LaTeX 公式（行内用 $...$），控制在 160 字以内，不要寒暄：\n"
    "1. 准确还原黑板上的手写公式、算式、表格、图示关系或推导步骤；\n"
    "2. 用一句话凝练黑板上这一步演算的思路由来、近似/前提条件与结论。"
)

_INLINE_MATH_RE = re.compile(r"(\$\$[\s\S]+?\$\$|\$[^$\n]+\$)")
_BARE_TRIG_RE = re.compile(
    r"(?<![\\$a-zA-Z])\b(sin|cos|tan|cot|sec|csc|arcsin|arccos|arctan|sinh|cosh|tanh|ln|log|exp)"
    r"\s*\(\s*([a-zA-Z0-9_+\-*/^]+)\s*\)(?![$])"
)


def _beautify_bare_math(text: str) -> str:
    """Wrap bare trig/log expressions like sin(a), cos(a) in inline LaTeX when outside $...$."""
    parts = _INLINE_MATH_RE.split(text)
    for idx in range(0, len(parts), 2):
        parts[idx] = _BARE_TRIG_RE.sub(r"$\\\1(\2)$", parts[idx])
    return "".join(parts)


def _crop_active_board_panel(roi, written_grid, surface_grid, np):
    """Crop to the contiguous board panel containing the written cells, excluding adjacent screens."""
    rh, rw = roi.shape[:2]
    gh, gw = written_grid.shape[:2]
    w_rows, w_cols = np.where(written_grid > 0)
    if len(w_rows) == 0 or len(w_cols) == 0:
        return roi
    r_min, r_max = int(w_rows.min()), int(w_rows.max())
    c_min, c_max = int(w_cols.min()), int(w_cols.max())
    # Expand horizontally up to +2 contiguous board columns along the written row band (stops at projector screens)
    while c_min - 1 >= max(0, int(w_cols.min()) - 2) and float(np.mean(surface_grid[r_min : r_max + 1, c_min - 1])) >= 0.6:
        c_min -= 1
    while c_max + 1 <= min(gw - 1, int(w_cols.max()) + 2) and float(np.mean(surface_grid[r_min : r_max + 1, c_max + 1])) >= 0.6:
        c_max += 1
    while r_min - 1 >= 0 and float(np.mean(surface_grid[r_min - 1, c_min : c_max + 1])) >= 0.5:
        r_min -= 1
    while r_max + 1 < gh and float(np.mean(surface_grid[r_max + 1, c_min : c_max + 1])) >= 0.5:
        r_max += 1

    x0 = max(0, int((c_min / gw - 0.03) * rw))
    x1 = min(rw, int(((c_max + 1) / gw + 0.03) * rw))
    y0 = max(0, int((r_min / gh - 0.04) * rh))
    y1 = min(rh, int(((r_max + 1) / gh + 0.04) * rh))
    cropped = roi[y0:y1, x0:x1]
    return cropped if cropped.size > 0 else roi


def _analyze_board_bgr(bgr, np, cv2) -> tuple[bool, object, str]:
    """Analyze a frame across classroom layouts and board types, returning (is_board, active_roi, kind)."""
    if bgr is None or bgr.size == 0:
        return False, bgr, "empty"
    h, w = bgr.shape[:2]
    small_full = cv2.resize(bgr, (320, 180))
    gray_full = cv2.cvtColor(small_full, cv2.COLOR_BGR2GRAY)
    bg_full = cv2.medianBlur(gray_full, 15)

    # 1. Multi-stream layout parser (tight close-up, L-shape top-right PiP, or wide classroom wall)
    br_panel = gray_full[int(180 * 0.28) : int(180 * 0.92), int(320 * 0.76) : int(320 * 0.99)]
    tr_pip = gray_full[int(180 * 0.02) : int(180 * 0.24), int(320 * 0.76) : int(320 * 0.99)]
    left_main = gray_full[int(180 * 0.05) : int(180 * 0.85), : int(320 * 0.73)]
    has_br_slide_pip = float(np.std(gray_full[int(180 * 0.75) : int(180 * 0.98), int(320 * 0.75) : int(320 * 0.99)])) > 12.0
    h_seam = float(
        np.mean(np.abs(gray_full[45:47, 240:].astype(np.float32) - gray_full[42:44, 240:].astype(np.float32)))
    )
    v_seam = float(
        np.mean(np.abs(gray_full[:45, 240:242].astype(np.float32) - gray_full[:45, 237:239].astype(np.float32)))
    )
    has_tr_pip_box = (float(np.std(br_panel)) < 6.0 and float(np.std(tr_pip)) > 15.0) or (
        h_seam > 16.0 and v_seam > 16.0 and float(np.std(tr_pip)) > 15.0
    )

    is_tight = False
    if h < 220 or w < 360:
        roi = bgr
        is_tight = True
    elif has_tr_pip_box:
        if float(np.mean(left_main < 15)) > 0.75 or float(np.std(left_main)) < 12.0:
            # Reject dead-canvas flicker frames where the tiny PiP contains a bright projector slide
            if float(np.mean(tr_pip[:, : int(tr_pip.shape[1] * 0.48)] > 180)) > 0.22:
                return False, bgr, "dead_canvas_pip"
            roi = bgr[int(h * 0.02) : int(h * 0.25), int(w * 0.75) : int(w * 0.995)]
        else:
            return False, bgr, "dual_stream_slide"
    elif (h >= 360 or w >= 640) and float(
        np.mean(np.abs(gray_full.astype(np.int16) - int(np.median(gray_full))) <= 2)
    ) > 0.88:
        return False, bgr, "placeholder"
    else:
        b_s, g_s, r_s = cv2.split(small_full)
        green_frac = float(
            np.mean((bg_full >= 18) & (bg_full <= 152) & (g_s >= r_s - 2) & (g_s >= b_s - 6))
        )
        slate_frac = float(
            np.mean(
                (bg_full >= 20)
                & (bg_full <= 85)
                & (np.abs(r_s.astype(np.int16) - g_s.astype(np.int16)) <= 8)
                & (np.abs(g_s.astype(np.int16) - b_s.astype(np.int16)) <= 10)
            )
        )
        if green_frac >= 0.65 or slate_frac >= 0.65:
            roi = bgr
            is_tight = True
        else:
            roi = bgr[int(h * 0.13) : int(h * 0.67), int(w * 0.02) : int(w * 0.98)]

    if roi is None or roi.size == 0:
        return False, bgr, "empty"

    roi_r = cv2.resize(roi, (480, 240))
    gray = cv2.cvtColor(roi_r, cv2.COLOR_BGR2GRAY)
    b, g, r = cv2.split(roi_r)
    bg = cv2.medianBlur(gray, 21)
    b_bg, g_bg, r_bg = cv2.medianBlur(b, 21), cv2.medianBlur(g, 21), cv2.medianBlur(r, 21)

    # 3. Multi-surface dark/green chalkboard & dark-slate board detection
    green_board = (
        (bg >= 18)
        & (bg <= 152)
        & (g_bg >= r_bg - 2)
        & (g_bg >= b_bg - 6)
        & ((g_bg.astype(np.int16) - r_bg.astype(np.int16) >= 3) | (bg <= 85))
    )
    dark_slate = (
        (bg >= 20)
        & (bg <= 85)
        & (np.abs(r_bg.astype(np.int16) - g_bg.astype(np.int16)) <= 8)
        & (np.abs(g_bg.astype(np.int16) - b_bg.astype(np.int16)) <= 10)
    )
    board_surface = (green_board | dark_slate).astype(np.uint8)

    # Carve out occluders (shirts/faces/glare blobs > 9px, warm skin tones, deep black shadows) before erosion
    board_raw = board_surface.copy()
    opened = cv2.morphologyEx(gray, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    large_bright = opened.astype(np.int16) > (bg.astype(np.int16) + 14)
    warm_skin = (r.astype(np.int16) - g.astype(np.int16)) > 6
    deep_dark = gray < 16
    board_raw[large_bright | warm_skin | deep_dark] = 0

    ksize = 9 if is_tight else 15
    board_interior = cv2.erode(
        board_raw,
        np.ones((ksize, ksize), np.uint8),
        borderType=cv2.BORDER_CONSTANT,
        borderValue=0,
    )

    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, se)
    diff = gray.astype(np.int16) - bg.astype(np.int16)
    gx = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)

    chalk = (tophat > 12) & (diff > 14) & (board_interior > 0)
    dark_outliers = (diff < -14) & (board_interior > 0)
    diag = (np.abs(gx) > 26) & (np.abs(gy) > 26) & (tophat > 8) & (board_interior > 0)

    gh, gw = 6, 12
    ch, cw = 240 // gh, 480 // gw
    written_grid = np.zeros((gh, gw), dtype=np.uint8)
    surface_grid = np.zeros((gh, gw), dtype=np.uint8)
    for i in range(gh):
        for j in range(gw):
            if float(np.mean(board_surface[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) > 0.55:
                surface_grid[i, j] = 1
            bm = board_interior[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw]
            bm_mean = float(np.mean(bm))
            if bm_mean <= 0.45:
                continue
            cm = float(np.mean(chalk[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
            nm = float(np.mean(dark_outliers[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
            dm = float(np.mean(diag[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
            # Impulse polarity asymmetry: chalk strokes are positive impulses without symmetric dark texture
            if cm > 0.015 and dm > 0.006 and nm < 0.025 and cm > 2.0 * nm:
                written_grid[i, j] = 1

    written_count = int(np.sum(written_grid))
    if written_count >= (1 if is_tight else 2):
        if is_tight:
            return True, roi, "chalk_tight"
        return True, _crop_active_board_panel(roi, written_grid, surface_grid, np), "chalk_panel"

    # 4. Physical matte whiteboard detection (when no chalkboard wall and no HDMI slide PiP)
    has_chalkboard_wall = float(np.mean(board_surface)) > 0.12
    if not is_tight and not has_br_slide_pip and not has_chalkboard_wall:
        wb_surface = (
            (bg >= 158)
            & (bg <= 232)
            & (np.abs(r_bg.astype(np.int16) - g_bg.astype(np.int16)) <= 10)
            & (np.abs(g_bg.astype(np.int16) - b_bg.astype(np.int16)) <= 12)
        ).astype(np.uint8)
        if 0.15 <= float(np.mean(wb_surface)) <= 0.72:
            wb_raw = wb_surface.copy()
            closed = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
            large_dark = closed.astype(np.int16) < (bg.astype(np.int16) - 14)
            wb_raw[large_dark | warm_skin | (gray > 244)] = 0
            wb_interior = cv2.erode(
                wb_raw, np.ones((17, 17), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0
            )
            blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, se)
            marker = (blackhat > 18) & (diff < -18) & (wb_interior > 0)
            bright_outliers = (diff > 16) & (wb_interior > 0)
            wb_diag = (np.abs(gx) > 28) & (np.abs(gy) > 28) & (blackhat > 12) & (wb_interior > 0)
            wb_written = np.zeros((gh, gw), dtype=np.uint8)
            wb_surf_grid = np.zeros((gh, gw), dtype=np.uint8)
            for i in range(gh):
                for j in range(gw):
                    if float(np.mean(wb_surface[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) > 0.55:
                        wb_surf_grid[i, j] = 1
                    bm = wb_interior[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw]
                    bm_mean = float(np.mean(bm))
                    if bm_mean <= 0.50:
                        continue
                    mm = float(np.mean(marker[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
                    pm = float(np.mean(bright_outliers[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
                    dm = float(np.mean(wb_diag[i * ch : (i + 1) * ch, j * cw : (j + 1) * cw])) / bm_mean
                    if mm > 0.018 and dm > 0.008 and pm < 0.015 and mm > 2.5 * pm:
                        wb_written[i, j] = 1
            if int(np.sum(wb_written)) >= 2:
                return True, _crop_active_board_panel(roi, wb_written, wb_surf_grid, np), "whiteboard_panel"

    return False, roi, "none"


def is_blackboard_bgr(bgr) -> bool:
    """Return True when a decoded BGR frame contains a classroom board with handwritten strokes."""
    try:
        import cv2
        import numpy as np

        ok, _, _ = _analyze_board_bgr(bgr, np, cv2)
        return ok
    except Exception:
        return False


_BOARD_FRAME_CACHE: dict[tuple[str, int, int], bool] = {}
_STATIC_LEFTOVER_CACHE: dict[tuple[str, int, int, int], set[str]] = {}


def is_blackboard_frame(path: Path) -> bool:
    """Return True when an image file contains a classroom board with actual handwritten strokes."""
    try:
        st = path.stat()
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
        if cache_key in _BOARD_FRAME_CACHE:
            return _BOARD_FRAME_CACHE[cache_key]
        import cv2
        import numpy as np

        bgr = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            res = False
        else:
            res, _, _ = _analyze_board_bgr(bgr, np, cv2)
            res = bool(res)
        if len(_BOARD_FRAME_CACHE) > 4096:
            _BOARD_FRAME_CACHE.clear()
        _BOARD_FRAME_CACHE[cache_key] = res
        return res
    except Exception:
        return False


def enhance_blackboard_jpeg(path: Path) -> bytes:
    """Crop to the active written board panel, upscale if small, and apply CLAHE + unsharp enhancement."""
    raw = path.read_bytes()
    try:
        import cv2
        import numpy as np

        bgr = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return raw
        _, roi, _ = _analyze_board_bgr(bgr, np, cv2)
        if roi is None or roi.size == 0:
            roi = bgr
        if 0 < roi.shape[1] < 960:
            scale = 960.0 / roi.shape[1]
            roi = cv2.resize(
                roi,
                (int(roi.shape[1] * scale), int(roi.shape[0] * scale)),
                interpolation=cv2.INTER_CUBIC,
            )
        lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
        l_chan, a_chan, b_chan = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
        enhanced_l = clahe.apply(l_chan)
        merged = cv2.cvtColor(cv2.merge((enhanced_l, a_chan, b_chan)), cv2.COLOR_LAB2BGR)
        blurred = cv2.GaussianBlur(merged, (0, 0), 1.2)
        sharpened = cv2.addWeighted(merged, 1.25, blurred, -0.25, 0)
        ok, buf = cv2.imencode(".jpg", sharpened, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        return bytes(buf) if ok else raw
    except Exception:
        return raw


def _char_bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s\W_]+", "", text)
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)} if len(cleaned) >= 2 else set()


_MEANINGFUL_MATH_RE = re.compile(
    r"(=|\\equiv|\\approx|\\le|\\ge|<|>|\+|-|\\times|\\cdot|/|\\frac|\^|_|!|\\pmod|\\to|\\implies|\\sum|\\prod|\\int|\\sqrt|\\log|\\ln|\\sin|\\cos|\\tan|O\s*\()"
)


def _is_meaningful_formula(raw: str) -> bool:
    """Return True for actual equations/expressions rather than bare identifiers or trivial 3-char tokens like $n=0$."""
    inner = raw.strip("$").replace("\n", " ").strip()
    compact = inner.replace(" ", "")
    if not compact or len(inner) > 80:
        return False
    if re.fullmatch(r"[a-zA-Z0-9]+", compact):
        return False
    if len(compact) <= 3 and not re.search(r"(\^|!|\\)", compact):
        return False
    return bool(_MEANINGFUL_MATH_RE.search(inner) or len(compact) >= 5)


def find_static_leftover_boards(keyframes_dir: Path) -> set[str]:
    """Identify wide projector+board frames where the chalkboard never changed across the lecture (previous class leftover)."""
    try:
        all_jpgs = sorted(keyframes_dir.glob("*.jpg"))
        if len(all_jpgs) < 6:
            return set()
        dir_key = (
            str(keyframes_dir),
            len(all_jpgs),
            all_jpgs[0].stat().st_mtime_ns,
            all_jpgs[-1].stat().st_mtime_ns,
        )
        if dir_key in _STATIC_LEFTOVER_CACHE:
            return set(_STATIC_LEFTOVER_CACHE[dir_key])
        import cv2
        import numpy as np

        p0 = None
        for p in all_jpgs:
            m_ts = re.search(r"_(\d+)m(\d+)s", p.name)
            if m_ts and int(m_ts.group(1)) >= 20:
                break
            if is_blackboard_frame(p):
                p0 = p
                break
        if p0 is None:
            _STATIC_LEFTOVER_CACHE[dir_key] = set()
            return set()
        f0 = cv2.imdecode(np.fromfile(str(p0), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if f0 is None:
            return set()
        g0 = cv2.resize(f0, (320, 180))
        # Must also have a bright projector screen in the room
        if float(np.mean(g0 > 190)) < 0.16:
            _STATIC_LEFTOVER_CACHE[dir_key] = set()
            return set()
        p1 = None
        for p in reversed(all_jpgs):
            if p == p0:
                break
            if is_blackboard_frame(p):
                p1 = p
                break
        if p1 is None:
            _STATIC_LEFTOVER_CACHE[dir_key] = set()
            return set()
        f1 = cv2.imdecode(np.fromfile(str(p1), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if f1 is None:
            return set()
        g1 = cv2.resize(f1, (320, 180))
        ul_diff = float(np.mean(np.abs(g0[32:72, 10:145].astype(np.float32) - g1[32:72, 10:145].astype(np.float32))))
        ur_diff = float(np.mean(np.abs(g0[32:72, 165:305].astype(np.float32) - g1[32:72, 165:305].astype(np.float32))))
        is_right_board = float(np.mean(g0[32:72, 165:305])) < float(np.mean(g0[32:72, 10:145]))
        board_diff = ur_diff if is_right_board else ul_diff
        if board_diff < 7.0:
            board_paths = [p for p in all_jpgs if is_blackboard_frame(p)]
            if len(board_paths) >= 3:
                res = {p.name for p in board_paths}
                _STATIC_LEFTOVER_CACHE[dir_key] = res
                return set(res)
        _STATIC_LEFTOVER_CACHE[dir_key] = set()
    except Exception:
        pass
    return set()


def _select_temporally_aligned_sentence(
    section_markdown: str,
    spoken_text: str,
    timestamp: float,
    window: tuple[float, float] | None = None,
) -> str:
    """Select the sentence in section_markdown aligned with timestamp via time ratio, spoken overlap, and math content."""
    if not section_markdown:
        return ""
    sentences: list[str] = []
    for raw_line in section_markdown.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("![") or line.startswith("|"):
            continue
        line = re.sub(r"^[-*+]\s+|\d+\.\s+|^>\s*", "", line).strip()
        for sent in re.split(r"(?<=[。！？；])\s*", line):
            s = sent.strip()
            prose_only = _INLINE_MATH_RE.sub("", s).strip()
            if len(prose_only) >= 16:
                sentences.append(s)
    if not sentences:
        return ""

    target_ratio = 0.5
    if window and len(window) == 2 and window[1] > window[0]:
        target_ratio = max(0.0, min(1.0, (timestamp - window[0]) / (window[1] - window[0])))

    spoken_grams = _char_bigrams(spoken_text)
    best_idx = 0
    best_score = -1e9
    total = len(sentences)
    for idx, sent in enumerate(sentences):
        pos_ratio = idx / max(total - 1, 1) if total > 1 else 0.5
        pos_score = 1.0 - abs(pos_ratio - target_ratio)
        overlap_score = 0.0
        if spoken_grams:
            sent_grams = _char_bigrams(sent)
            if sent_grams:
                overlap_score = len(spoken_grams & sent_grams) / max(len(sent_grams) ** 0.5, 1.0)
        math_bonus = 0.0
        if any(_is_meaningful_formula(m.group(0)) for m in _INLINE_MATH_RE.finditer(sent)) or _BARE_TRIG_RE.search(sent):
            math_bonus += 1.25
        score = overlap_score * 3.0 + pos_score + math_bonus
        if score > best_score:
            best_score = score
            best_idx = idx

    best = _beautify_bare_math(sentences[best_idx])
    best = _INLINE_MATH_RE.sub(
        lambda m: "$" + m.group(0).strip("$").replace("\n", " ").strip() + "$",
        best,
    )
    if len(best) > 180:
        best = best[:178].rstrip("，、；： ") + "…"
    return best


def transcript_board_summary(
    transcript: dict | None,
    timestamp: float,
    section_markdown: str = "",
    *,
    window: tuple[float, float] | None = None,
) -> str:
    """Build a concise derivation/formula note synchronized to the frame's timestamp without keyword lists."""
    raw_formulas = [m.group(0).strip() for m in _INLINE_MATH_RE.finditer(section_markdown or "")]
    formulas = [
        "$" + f.strip("$").replace("\n", " ").strip() + "$"
        for f in raw_formulas
        if _is_meaningful_formula(f)
    ]
    unique_formulas: list[str] = []
    for f in formulas:
        if f not in unique_formulas:
            unique_formulas.append(f)

    segments = (transcript or {}).get("segments") if isinstance(transcript, dict) else None
    spoken_parts: list[str] = []
    if isinstance(segments, list) and segments:
        nearby = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                start = float(seg.get("start", 0))
                end = float(seg.get("end", start))
            except (TypeError, ValueError):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            if start <= timestamp + 30.0 and end >= timestamp - 25.0:
                dist = min(abs(start - timestamp), abs(end - timestamp))
                nearby.append((dist, start, text))
        nearby.sort(key=lambda item: (item[0], item[1]))
        top_segments = sorted(nearby[:3], key=lambda item: item[1])
        spoken_parts = [item[2] for item in top_segments]

    spoken_joined = " ".join(spoken_parts).strip()
    section_prose = _select_temporally_aligned_sentence(
        section_markdown, spoken_joined, timestamp, window=window
    )

    pieces: list[str] = []
    if unique_formulas and (not section_prose or unique_formulas[0] not in section_prose):
        pieces.append("关键式：" + " ， ".join(unique_formulas[:2]))
    if section_prose:
        pieces.append("同期要点：" + section_prose)
    elif spoken_joined:
        joined = _beautify_bare_math(spoken_joined)
        if len(joined) > 160:
            joined = joined[:158].rstrip("，、；： ") + "…"
        pieces.append("同期推导：" + joined)
    return " ｜ ".join(pieces)


def load_cached_board_notes(keyframes_dir: Path) -> dict[str, dict]:
    """Load persisted board recognition notes keyed by frame filename."""
    path = keyframes_dir / BOARD_NOTES_FILENAME
    try:
        data = json.loads(path.read_text("utf-8"))
        rows = data.get("notes") if isinstance(data, dict) else None
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cached_board_notes(keyframes_dir: Path, notes_map: dict[str, dict]) -> None:
    keyframes_dir.mkdir(parents=True, exist_ok=True)
    path = keyframes_dir / BOARD_NOTES_FILENAME
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"version": 1, "notes": notes_map}, ensure_ascii=False, indent=1),
            "utf-8",
        )
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _default_vision_ask(settings) -> Callable[[bytes, str], str] | None:
    """Return a callable that queries the BYOK multimodal model when configured."""
    if settings is None:
        return None
    from .ai_provider import call_vision, is_vision_configured

    if not is_vision_configured(settings):
        return None

    def ask(image_bytes: bytes, spoken_context: str) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_text = "请识别画面中的课堂板书公式与关键推导。"
        if spoken_context:
            user_text += f"\n老师此时口述参考：{spoken_context}"
        body = call_vision(_VISION_SYSTEM, user_text, [b64], settings, json_mode=False)
        return str(body.get("content") or "").strip()

    return ask


def recognize_blackboard_frames(
    job_dir: Path,
    transcript: dict | None = None,
    settings=None,
    *,
    vision_ask: Callable[[bytes, str], str] | None = None,
    max_frames: int = MAX_BOARD_OCR_FRAMES,
) -> list[dict]:
    """Recognize formulas/text on pure blackboard keyframes and return courseware-compatible rows."""
    keyframes_dir = Path(job_dir) / "keyframes"
    index_path = keyframes_dir / "index.json"
    if not index_path.is_file():
        return []
    try:
        raw_index = json.loads(index_path.read_text("utf-8"))
        keyframes = raw_index.get("keyframes", []) if isinstance(raw_index, dict) else []
    except (OSError, ValueError):
        return []
    if not isinstance(keyframes, list) or not keyframes:
        return []

    matched_files: set[str] = set()
    try:
        matches_data = json.loads((keyframes_dir / "keyframe-matches.json").read_text("utf-8"))
        for row in (matches_data.get("matches") or []) if isinstance(matches_data, dict) else []:
            if isinstance(row, dict):
                fname = row.get("file") or row.get("frame_file")
                if isinstance(fname, str) and fname:
                    matched_files.add(fname)
    except (OSError, ValueError):
        pass

    cached = load_cached_board_notes(keyframes_dir)
    ask_fn = vision_ask or _default_vision_ask(settings)
    static_leftover = find_static_leftover_boards(keyframes_dir)
    candidates: list[tuple[float, dict, Path, str]] = []

    for item in keyframes:
        if not isinstance(item, dict):
            continue
        name = item.get("file")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in matched_files
            or name in static_leftover
        ):
            continue
        path = keyframes_dir / name
        if not path.is_file():
            continue
        is_board = bool(item.get("board")) or is_blackboard_frame(path)
        if not is_board:
            continue
        try:
            stamp = float(item.get("timestamp", 0.0))
        except (TypeError, ValueError):
            stamp = 0.0
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        candidates.append((stamp, item, path, digest))

    if not candidates:
        return []

    candidates.sort(key=lambda row: row[0])
    if len(candidates) > max_frames and max_frames > 0:
        selected: list[tuple[float, dict, Path, str]] = []
        n = len(candidates)
        for b_idx in range(max_frames):
            lo = (b_idx * n) // max_frames
            hi = max(lo + 1, ((b_idx + 1) * n) // max_frames)
            bucket = candidates[lo:hi]
            # In the first bucket, prefer frames after 30s to avoid pre-class leftover boards
            in_class = [c for c in bucket if c[0] >= 30.0]
            pool = in_class if in_class else bucket
            selected.append(pool[len(pool) // 2])
    else:
        selected = candidates[:max_frames]

    updated = False
    sources: list[dict] = []
    for stamp, item, path, digest in selected:
        name = item["file"]
        time_label = str(item.get("time") or f"{int(stamp) // 60:02d}:{int(stamp) % 60:02d}")
        entry = cached.get(name)
        if isinstance(entry, dict) and entry.get("sha256") == digest and entry.get("text"):
            text = str(entry["text"]).strip()
        elif ask_fn is not None:
            spoken = transcript_board_summary(transcript, stamp)
            try:
                enhanced = enhance_blackboard_jpeg(path)
                text = str(ask_fn(enhanced, spoken) or "").strip()
            except Exception as exc:
                logger.warning("blackboard vision recognition skipped for %s: %s", name, type(exc).__name__)
                text = ""
            if text:
                cached[name] = {
                    "file": name,
                    "timestamp": stamp,
                    "time": time_label,
                    "sha256": digest,
                    "text": text,
                }
                updated = True
        else:
            text = ""

        if text:
            sources.append(
                {
                    "title": "课堂板书识别",
                    "locator": f"板书画面 {time_label}",
                    "path": f"keyframes/{name}",
                    "text": text,
                    "status": "readable",
                }
            )

    if updated:
        _save_cached_board_notes(keyframes_dir, cached)
    return sources
