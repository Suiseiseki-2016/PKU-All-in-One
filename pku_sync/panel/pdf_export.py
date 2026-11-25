"""Promotional PDF generator for PKU All in One course notes and batch exports.

Generates standard ISO-32000 PDF 1.4 documents with:
- Embedded subsetted CJK TrueType font (`wqy-microhei.ttc` / system CJK fallback) via ReportLab so Chinese,
  English, Greek/math symbols, and punctuation render crisply across all PDF viewers (Chrome, macOS Preview,
  iOS, WeChat, Edge, Android)
- Inline rich Markdown formatting (`**bold**` emphasis, `` `inline code` `` pills, timestamp badges,
  numbered lists, nested bullet lists, blockquotes, and bordered multi-column tables)
- Automatic extraction and top-of-note presentation of **老师口述重点与通知提示** (exam review points,
  quiz passwords, deadlines, and oral announcements)
- Section-aligned classroom slide & blackboard keyframes (`build_note_visual_sections`) placed directly
  inside their matching lecture sections
- Clean header banner, footer bar, subtle background brand watermark, and summary card
  (`PKU All in One`)
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from reportlab import rl_config
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

rl_config.useA85 = 0

WATERMARK_SITE_NAME = "PKU All in One"
WATERMARK_SITE_URL = "Open Source BYOK"
WATERMARK_PROMO_TAGLINE = "PKU All in One · 北大教学网 AI 课堂笔记、口述考点提取与 MCP 学习工作台"
WATERMARK_FOOTER_LEFT = "本笔记由 PKU All in One 自动生成 · "


@dataclass
class NotePdfSection:
    course_title: str
    doc_title: str
    markdown: str
    subtitle: str = ""
    images: list[tuple[str, bytes]] = field(default_factory=list)
    visual_sections: list[dict] = field(default_factory=list)
    oral_highlights: list[dict] = field(default_factory=list)


_LATEX_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "varsigma": "ς",
    "tau": "τ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "ne": "≠",
    "neq": "≠",
    "approx": "≈",
    "cong": "≅",
    "equiv": "≡",
    "sim": "∼",
    "simeq": "≈",
    "propto": "∝",
    "times": "×",
    "cdot": "·",
    "div": "÷",
    "pm": "±",
    "mp": "±",
    "to": "→",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "Leftarrow": "⇐",
    "implies": "⇒",
    "impliedby": "⇐",
    "iff": "⇔",
    "leftrightarrow": "↔",
    "Leftrightarrow": "⇔",
    "maps": "↦",
    "mapsto": "↦",
    "in": "∈",
    "notin": "∉",
    "ni": "∋",
    "subset": "⊂",
    "subseteq": "⊆",
    "supset": "⊃",
    "supseteq": "⊇",
    "cup": "∪",
    "cap": "∩",
    "setminus": "\\",
    "emptyset": "∅",
    "varnothing": "∅",
    "forall": "∀",
    "exists": "∃",
    "nexists": "∄",
    "neg": "¬",
    "lnot": "¬",
    "land": "∧",
    "lor": "∨",
    "oplus": "⊕",
    "bigoplus": "⊕",
    "otimes": "⊗",
    "bigotimes": "⊗",
    "perp": "⊥",
    "parallel": "∥",
    "infty": "∞",
    "partial": "∂",
    "nabla": "∇",
    "sum": "∑",
    "prod": "∏",
    "coprod": "∐",
    "int": "∫",
    "oint": "∮",
    "dots": "…",
    "cdots": "…",
    "ldots": "…",
    "vdots": "…",
    "ddots": "…",
    "mid": "∣",
    "nmid": "∤",
    "lfloor": "⌊",
    "rfloor": "⌋",
    "lceil": "⌈",
    "rceil": "⌉",
    "langle": "〈",
    "rangle": "〉",
    "triangle": "△",
    "angle": "∠",
    "circ": "°",
    "degree": "°",
    "prime": "′",
    "gcd": "gcd",
    "lcm": "lcm",
    "mod": "mod",
    "bmod": "mod",
    "sin": "sin",
    "cos": "cos",
    "tan": "tan",
    "cot": "cot",
    "sec": "sec",
    "csc": "csc",
    "log": "log",
    "ln": "ln",
    "exp": "exp",
    "lim": "lim",
    "max": "max",
    "min": "min",
    "sup": "sup",
    "inf": "inf",
    "det": "det",
    "dim": "dim",
    "ker": "ker",
    "rank": "rank",
}

_UNICODE_SUB_MAP = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ᵢᵣᵤᵥₐₑₒₓₖₚₛₜ", "0123456789+-=()iruvaeoxkpst")
_UNICODE_SUP_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", "0123456789+-=()ni")
_UNICODE_SUB_RE = re.compile(r"([₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ᵢᵣᵤᵥₐₑₒₓₖₚₛₜ]+)")
_UNICODE_SUP_RE = re.compile(r"([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ]+)")
_MATH_FONT_FALLBACK_TRANS = str.maketrans({
    "≃": "≈",
    "∓": "±",
    "∖": "\\",
    "⋯": "…",
    "⋮": "…",
    "⋱": "…",
    "∤": "|",
    "⟨": "〈",
    "⟩": "〉",
    "∘": "°",
    "≢": "≠",
    "➔": "→",
    "⟹": "⇒",
    "⟸": "⇐",
    "✦": "•",
    "\u20d7": "",
    "\u0302": "",
    "\ufe0f": "",
})

_SMP_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u269F\u26A1-\u26FF\u2700-\u2725\u2727-\u27BF]+")
_REGISTERED_FONT_NAME: str | None = None


def _ensure_cjk_font() -> str:
    """Register and return the embedded TrueType CJK font name for ReportLab."""
    global _REGISTERED_FONT_NAME
    if _REGISTERED_FONT_NAME and _REGISTERED_FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return _REGISTERED_FONT_NAME

    here = Path(__file__).resolve().parent
    candidates: list[tuple[Path, int | None]] = [
        (here / "wqy-microhei.ttc", 0),
        (Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"), 0),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 0),
        (Path("/System/Library/Fonts/STHeiti Light.ttc"), 0),
        (Path("/System/Library/Fonts/PingFang.ttc"), 0),
        (Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 0),
        (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"), None),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), None),
    ]
    for path, sub_idx in candidates:
        if not path.is_file():
            continue
        try:
            font_name = "PKUCJK"
            if sub_idx is not None:
                pdfmetrics.registerFont(TTFont(font_name, str(path), subfontIndex=sub_idx))
            else:
                pdfmetrics.registerFont(TTFont(font_name, str(path)))
            _REGISTERED_FONT_NAME = font_name
            return font_name
        except Exception:
            continue

    _REGISTERED_FONT_NAME = "Helvetica"
    return "Helvetica"


def _normalize_pdf_text(text: str) -> str:
    s = (
        str(text or "")
        .replace("\u2022", "·")
        .replace("\u2013", "-")
        .replace("\u2014", "—")
        .replace("\u00a0", " ")
        .replace("\u26a0\ufe0f", "[注意] ")
        .replace("\u26a0", "[注意] ")
        .translate(_MATH_FONT_FALLBACK_TRANS)
    )
    return _SMP_EMOJI_RE.sub("", s)


def _normalize_code_text(text: str) -> str:
    s = _normalize_pdf_text(text)
    s = _UNICODE_SUP_RE.sub(lambda m: "^" + m.group(0).translate(_UNICODE_SUP_MAP), s)
    s = _UNICODE_SUB_RE.sub(lambda m: m.group(0).translate(_UNICODE_SUB_MAP), s)
    return s


def _clean_math(text: str, *, preserve_inline_markers: bool = False) -> str:
    s = str(text or "")
    s = re.sub(r"<mention-date\s+start=\"(\d{4}-\d{2}-\d{2})\"\s*/>", r"\1", s)
    s = re.sub(r"<mention-page\s+url=\"(https?://[^\s\"<>]+)\"\s*/>", r"关联页面 (\1)", s)
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[课堂画面：\1]", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", s)
    s = re.sub(r"\\not\s*\\equiv", "≠", s)
    s = re.sub(r"\\not\s*=", "≠", s)
    s = re.sub(r"\\(?:d|t)?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\binom\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"C(\1, \2)", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", s)
    s = re.sub(r"\\pmod\s*\{([^{}]*)\}", r"(mod \1)", s)
    s = re.sub(r"\\pmod\s+([A-Za-z0-9+\-α-ωΑ-Ω]+)", r"(mod \1)", s)
    s = re.sub(r"\\(?:mathbf|mathrm|mathit|text|textbf|textit|mathcal|mathbb|overline|bar|hat|vec|tilde)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\([A-Za-z]+)", lambda m: _LATEX_SYMBOLS.get(m.group(1), m.group(1)), s)
    s = s.replace("$$", "").replace("$", "").replace("\\(", "").replace("\\)", "")
    if not preserve_inline_markers:
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)
    s = s.replace("\\{", "{").replace("\\}", "}").replace("\\\\", " ; ")
    s = s.translate(_UNICODE_SUB_MAP).translate(_UNICODE_SUP_MAP)
    return _normalize_pdf_text(s).strip()


@dataclass
class _InlineSpan:
    text: str
    bold: bool = False
    code: bool = False
    math: bool = False
    script: str = ""  # "", "sub", "sup"
    overline: bool = False
    frac: tuple[str, str] | None = None
    binom: tuple[str, str] | None = None
    highlight: bool = False
    badge_kind: str = ""


_SPAN_MATH_DELIM_RE = re.compile(
    r"\$\$([\s\S]+?)\$\$"
    r"|\\\[([\s\S]+?)\\\]"
    r"|\\\(([\s\S]+?)\\\)"
    r"|(?<!\\)\$([^\s$](?:[^$]*?[^\s$])?)\$"
    r"|(\\?\bbegin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix)\}[\s\S]*?\\?\bend\{\6\})"
)
_BARE_MATH_RE = re.compile(
    r"\\(?:frac|dfrac|tfrac|binom|overline|bar|sqrt|mathbb|mathbf|mathrm|mathit|text)\b"
    r"(?:\s*\[[^\]]*\])?(?:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})+"
    r"|[A-Za-z0-9)\]][_^]\{[^{}]+\}"
    r"|(?<![A-Za-z0-9_])[A-Za-z0-9)\]][_^][A-Za-z0-9](?![A-Za-z0-9_])"
    r"|\\(?:pmod|bmod)\s*(?:\{[^{}]+\}|[A-Za-z0-9+\-α-ωΑ-Ω]+)"
    r"|\\(?:alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|"
    r"mu|nu|xi|pi|rho|sigma|varsigma|tau|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|"
    r"Sigma|Phi|Psi|Omega|leq?|geq?|neq?|approx|cong|equiv|sim|simeq|propto|times|cdot|div|pm|mp|"
    r"to|rightarrow|leftarrow|Rightarrow|Leftarrow|implies|impliedby|iff|leftrightarrow|Leftrightarrow|"
    r"maps|mapsto|in|notin|subset|subseteq|supset|supseteq|cup|cap|setminus|emptyset|varnothing|"
    r"forall|exists|nexists|neg|lnot|land|lor|oplus|otimes|perp|parallel|infty|partial|nabla|sum|"
    r"prod|int|oint|dots|cdots|ldots|vdots|ddots|lfloor|rfloor|lceil|rceil|langle|rangle|triangle|"
    r"angle|circ|degree|prime)\b"
)
_UNICODE_ANY_SCRIPT_RE = re.compile(
    r"([₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ᵢᵣᵤᵥₐₑₒₓₖₚₛₜ]+)|([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ]+)"
)


def _merge_math_spans(spans: list[_InlineSpan]) -> list[_InlineSpan]:
    merged: list[_InlineSpan] = []
    for sp in spans:
        if (
            merged
            and not sp.badge_kind
            and not merged[-1].badge_kind
            and sp.frac is None
            and sp.binom is None
            and merged[-1].frac is None
            and merged[-1].binom is None
            and merged[-1].bold == sp.bold
            and merged[-1].code == sp.code
            and merged[-1].math == sp.math
            and merged[-1].script == sp.script
            and merged[-1].overline == sp.overline
            and merged[-1].highlight == sp.highlight
        ):
            merged[-1].text += sp.text
        elif sp.text or sp.frac is not None or sp.binom is not None or sp.badge_kind:
            merged.append(sp)
    return merged


def _split_unicode_scripts(
    text: str, *, bold: bool = False, math: bool = False, overline: bool = False
) -> list[_InlineSpan]:
    clean = _normalize_pdf_text(text)
    if not clean:
        return []
    out: list[_InlineSpan] = []
    pos = 0
    for m in _UNICODE_ANY_SCRIPT_RE.finditer(clean):
        if m.start() > pos:
            out.append(_InlineSpan(clean[pos : m.start()], bold=bold, math=math, overline=overline))
        if m.group(1):
            out.append(
                _InlineSpan(
                    m.group(1).translate(_UNICODE_SUB_MAP),
                    bold=bold,
                    math=math,
                    script="sub",
                    overline=overline,
                )
            )
        elif m.group(2):
            out.append(
                _InlineSpan(
                    m.group(2).translate(_UNICODE_SUP_MAP),
                    bold=bold,
                    math=math,
                    script="sup",
                    overline=overline,
                )
            )
        pos = m.end()
    if pos < len(clean):
        out.append(_InlineSpan(clean[pos:], bold=bold, math=math, overline=overline))
    return out


def _parse_math_spans(tex: str, *, default_bold: bool = False, _depth: int = 0) -> list[_InlineSpan]:
    s = str(tex or "").strip()
    if not s:
        return []
    s = re.sub(r"\\left\s*\\\}", "}", s)
    s = re.sub(r"\\left\s*\\\{", "{", s)
    s = re.sub(r"\\right\s*\\\}", "}", s)
    s = re.sub(r"\\right\s*\\\{", "{", s)
    s = re.sub(r"\\(?:left|right)\s*\\\|", "‖", s)
    s = re.sub(r"\\(?:left|right)\s*\.", "", s)
    s = re.sub(r"\\(?:left|right)\s*([()\[\]|])", r"\1", s)
    s = re.sub(r"\\not\s*\\equiv", "≠", s)
    s = re.sub(r"\\not\s*=", "≠", s)
    s = re.sub(r"\\(?:quad|qquad)", "  ", s)
    s = re.sub(r"\\[,;:]", " ", s)
    s = s.replace("\\!", "")
    s = re.sub(r"\r?\n", " ", s)

    n = len(s)

    def read_arg(pos: int) -> tuple[str, int]:
        while pos < n and s[pos] == " ":
            pos += 1
        if pos >= n:
            return "", pos
        if s[pos] == "{":
            depth = 1
            start = pos + 1
            pos += 1
            while pos < n and depth > 0:
                if s[pos] == "\\" and pos + 1 < n:
                    pos += 2
                    continue
                if s[pos] == "{":
                    depth += 1
                elif s[pos] == "}":
                    depth -= 1
                pos += 1
            end_inner = pos - 1 if depth == 0 else pos
            return s[start:end_inner], pos
        if s[pos] == "\\":
            m_cmd = re.match(r"\\[A-Za-z]+", s[pos:])
            if m_cmd:
                return m_cmd.group(0), pos + len(m_cmd.group(0))
            return s[pos : min(n, pos + 2)], min(n, pos + 2)
        return s[pos], pos + 1

    out: list[_InlineSpan] = []

    def emit_plain(chunk: str) -> None:
        if chunk:
            out.extend(_split_unicode_scripts(chunk, bold=default_bold, math=True))

    i = 0
    buf: list[str] = []

    def flush_buf() -> None:
        if buf:
            emit_plain("".join(buf))
            buf.clear()

    while i < n:
        ch = s[i]
        if ch == "\\":
            m_cmd = re.match(r"\\([A-Za-z]+)", s[i:])
            if m_cmd:
                flush_buf()
                cmd = m_cmd.group(1)
                pos = i + len(m_cmd.group(0))
                if cmd in ("frac", "dfrac", "tfrac"):
                    num, pos = read_arg(pos)
                    den, pos = read_arg(pos)
                    if _depth == 0:
                        out.append(
                            _InlineSpan(
                                "",
                                bold=default_bold,
                                math=True,
                                frac=(num.strip(), den.strip()),
                            )
                        )
                    else:
                        out.extend(_parse_math_spans(f"({num})/({den})", default_bold=default_bold, _depth=_depth))
                    i = pos
                    continue
                if cmd == "binom":
                    top_s, pos = read_arg(pos)
                    bot_s, pos = read_arg(pos)
                    if _depth == 0:
                        out.append(
                            _InlineSpan(
                                "",
                                bold=default_bold,
                                math=True,
                                binom=(top_s.strip(), bot_s.strip()),
                            )
                        )
                    else:
                        out.extend(_parse_math_spans(f"C({top_s}, {bot_s})", default_bold=default_bold, _depth=_depth))
                    i = pos
                    continue
                if cmd == "sqrt":
                    while pos < n and s[pos] == " ":
                        pos += 1
                    if pos < n and s[pos] == "[":
                        end_b = s.find("]", pos + 1)
                        if end_b > pos:
                            root_idx = s[pos + 1 : end_b].strip()
                            for sp in _parse_math_spans(root_idx, default_bold=default_bold, _depth=_depth + 1):
                                sp.script = "sup"
                                out.append(sp)
                            pos = end_b + 1
                    inner, pos = read_arg(pos)
                    emit_plain("√(")
                    out.extend(_parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1))
                    emit_plain(")")
                    i = pos
                    continue
                if cmd in ("overline", "bar", "hat", "vec"):
                    inner, pos = read_arg(pos)
                    sub_spans = _parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1)
                    for sp in sub_spans:
                        sp.overline = True
                    out.extend(sub_spans)
                    i = pos
                    continue
                if cmd == "tilde":
                    inner, pos = read_arg(pos)
                    sub_spans = _parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1)
                    if len(sub_spans) == 1 and len(sub_spans[0].text) == 1 and sub_spans[0].frac is None:
                        sub_spans[0].text += "\u0303"
                    else:
                        for sp in sub_spans:
                            sp.overline = True
                    out.extend(sub_spans)
                    i = pos
                    continue
                if cmd == "mathbb":
                    inner, pos = read_arg(pos)
                    out.append(_InlineSpan(_normalize_pdf_text(inner), bold=True, math=True))
                    i = pos
                    continue
                if cmd in ("mathbf", "boldsymbol", "textbf"):
                    inner, pos = read_arg(pos)
                    out.extend(_parse_math_spans(inner, default_bold=True, _depth=_depth + 1))
                    i = pos
                    continue
                if cmd in ("text", "textrm", "mathrm", "operatorname", "mathit", "emph", "mathcal", "textit"):
                    inner, pos = read_arg(pos)
                    out.extend(_parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1))
                    i = pos
                    continue
                if cmd == "pmod":
                    while pos < n and s[pos] == " ":
                        pos += 1
                    if pos < n and s[pos] == "{":
                        inner, pos = read_arg(pos)
                    else:
                        m_pm = re.match(r"([A-Za-z0-9+\-α-ωΑ-Ω]+)", s[pos:])
                        inner = m_pm.group(1) if m_pm else ""
                        pos += len(inner)
                    emit_plain(" (mod ")
                    out.extend(_parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1))
                    emit_plain(")")
                    i = pos
                    continue
                if cmd in ("bmod", "mod") and pos < n and s[pos] in (" ", "{"):
                    while pos < n and s[pos] == " ":
                        pos += 1
                    if pos < n and s[pos] == "{":
                        inner, pos = read_arg(pos)
                        emit_plain(" mod ")
                        out.extend(_parse_math_spans(inner, default_bold=default_bold, _depth=_depth + 1))
                        i = pos
                        continue
                emit_plain(_LATEX_SYMBOLS.get(cmd, cmd))
                i = pos
                continue
            if i + 1 < n and s[i + 1] in "{}%_&#|":
                flush_buf()
                emit_plain("‖" if s[i + 1] == "|" else s[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue

        if ch in ("^", "_") and i + 1 < n:
            flush_buf()
            kind = "sup" if ch == "^" else "sub"
            arg, pos = read_arg(i + 1)
            for sp in _parse_math_spans(arg, default_bold=default_bold, _depth=_depth + 1):
                if not sp.script:
                    sp.script = kind
                sp.math = True
                out.append(sp)
            i = pos
            continue

        if ch in ("{", "}"):
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush_buf()
    return _merge_math_spans(out)


def _parse_inline_spans(text: str, *, default_bold: bool = False) -> list[_InlineSpan]:
    raw = str(text or "")
    if not raw.strip():
        return []

    raw = re.sub(r"<mention-date\s+start=\"(\d{4}-\d{2}-\d{2})\"\s*/>", r"\1", raw)
    raw = re.sub(r"<mention-page\s+url=\"(https?://[^\s\"<>]+)\"\s*/>", r"关联页面 (\1)", raw)
    raw = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[课堂画面：\1]", raw)
    raw = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", raw)

    code_store: list[str] = []
    math_store: list[str] = []
    badge_store: list[tuple[str, str]] = []
    hl_store: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        code_store.append(m.group(1))
        return f"\x00C{len(code_store) - 1}\x00"

    def _stash_math_delim(m: re.Match[str]) -> str:
        tex = next((g for g in m.groups()[:5] if g is not None), "")
        math_store.append(tex)
        return f"\x00M{len(math_store) - 1}\x00"

    def _stash_bare_math(m: re.Match[str]) -> str:
        math_store.append(m.group(0))
        return f"\x00M{len(math_store) - 1}\x00"

    def _stash_exam_badge(m: re.Match[str]) -> str:
        b = m.group(1)
        kind = (
            "must"
            if b == "必考"
            else "high"
            if b in ("高频考点", "高频")
            else "trap"
            if "易错" in b
            else "formula"
            if b == "核心公式"
            else "opt"
        )
        badge_store.append((kind, b))
        return f"\x00B{len(badge_store) - 1}\x00"

    def _stash_cw_badge(m: re.Match[str]) -> str:
        cw_name = (m.group(1) or "").strip()
        pnum = m.group(2)
        label = f"{cw_name} · P.{pnum}" if cw_name else f"课件 P.{pnum}"
        badge_store.append(("cw", label))
        return f"\x00B{len(badge_store) - 1}\x00"

    def _stash_highlight(m: re.Match[str]) -> str:
        hl_store.append(m.group(1))
        return f"\x00H{len(hl_store) - 1}\x00"

    tokenized = re.sub(r"`([^`]+)`", _stash_code, raw)
    tokenized = _SPAN_MATH_DELIM_RE.sub(_stash_math_delim, tokenized)
    tokenized = _BARE_MATH_RE.sub(_stash_bare_math, tokenized)
    tokenized = re.sub(r"\[(必考|高频考点|高频|易错陷阱|易错|核心公式|选学)\]", _stash_exam_badge, tokenized)
    tokenized = re.sub(r"\[课件(?:[：:]\s*([^\]]+?))?\s*[Pp第]\.?\s*(\d+)\s*页?\]", _stash_cw_badge, tokenized)
    tokenized = re.sub(r"\*\*==(?!=)([^\n]{1,140}?)==\*\*", _stash_highlight, tokenized)
    tokenized = re.sub(r"==(?!=)([^\n]{1,140}?)==", _stash_highlight, tokenized)

    token_re = re.compile(r"(\x00[CMBH]\d+\x00|\*\*[^*\n]+\*\*|(?<!\*)\*[^*\s][^*\n]*?[^*\s]\*(?!\*)|(?<!\*)\*[^*\s]\*(?!\*))")

    def expand_segment(seg: str, *, seg_bold: bool, seg_hl: bool = False) -> list[_InlineSpan]:
        res: list[_InlineSpan] = []
        pos = 0
        for m in token_re.finditer(seg):
            if m.start() > pos:
                plain_part = seg[pos : m.start()].replace("**", "")
                for sp in _split_unicode_scripts(plain_part, bold=seg_bold):
                    if seg_hl:
                        sp.highlight = True
                    res.append(sp)
            tok = m.group(0)
            if tok.startswith("\x00C") and tok.endswith("\x00"):
                idx = int(tok[2:-1])
                res.append(_InlineSpan(_normalize_code_text(code_store[idx]), bold=seg_bold, code=True, highlight=seg_hl))
            elif tok.startswith("\x00M") and tok.endswith("\x00"):
                idx = int(tok[2:-1])
                for sp in _parse_math_spans(math_store[idx], default_bold=seg_bold):
                    if seg_hl:
                        sp.highlight = True
                    res.append(sp)
            elif tok.startswith("\x00B") and tok.endswith("\x00"):
                idx = int(tok[2:-1])
                bkind, blabel = badge_store[idx]
                res.append(_InlineSpan(_normalize_pdf_text(blabel), bold=True, badge_kind=bkind))
            elif tok.startswith("\x00H") and tok.endswith("\x00"):
                idx = int(tok[2:-1])
                res.extend(expand_segment(hl_store[idx], seg_bold=True, seg_hl=True))
            elif tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                res.extend(expand_segment(tok[2:-2], seg_bold=True, seg_hl=seg_hl))
            elif tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
                res.extend(expand_segment(tok[1:-1], seg_bold=seg_bold, seg_hl=seg_hl))
            pos = m.end()
        if pos < len(seg):
            tail_part = seg[pos:].replace("**", "")
            for sp in _split_unicode_scripts(tail_part, bold=seg_bold):
                if seg_hl:
                    sp.highlight = True
                res.append(sp)
        return res

    spans = _merge_math_spans(expand_segment(tokenized, seg_bold=default_bold))
    return spans or [_InlineSpan(_clean_math(raw), bold=default_bold)]


_NO_START_PUNCT = set("。，、；：！？）》】”’.,;:!?)]}%'")
_CHAR_WIDTH_CACHE: dict[tuple[str, float], dict[str, float]] = {}


def _text_width(text: str, font_name: str, font_size: float) -> float:
    sz = round(font_size, 2)
    w_map = _CHAR_WIDTH_CACHE.setdefault((font_name, sz), {})
    total = 0.0
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\t":
            ch = "  "
        cw = w_map.get(ch)
        if cw is None:
            cw = pdfmetrics.stringWidth(ch, font_name, sz)
            w_map[ch] = cw
        total += cw
    return total


def _span_width(sp: _InlineSpan, font_name: str, font_size: float) -> float:
    if sp.badge_kind:
        return _text_width(sp.text, font_name, font_size * 0.82) + 8.5
    if sp.frac is not None:
        fsz = round(font_size * 0.76, 2)
        nw = sum(_span_width(ns, font_name, fsz) for ns in _parse_math_spans(sp.frac[0], default_bold=sp.bold, _depth=1))
        dw = sum(_span_width(ds, font_name, fsz) for ds in _parse_math_spans(sp.frac[1], default_bold=sp.bold, _depth=1))
        return max(nw, dw, font_size * 0.8) + 6.0
    if sp.binom is not None:
        fsz = round(font_size * 0.74, 2)
        tw = sum(_span_width(ts, font_name, fsz) for ts in _parse_math_spans(sp.binom[0], default_bold=sp.bold, _depth=1))
        bw = sum(_span_width(bs, font_name, fsz) for bs in _parse_math_spans(sp.binom[1], default_bold=sp.bold, _depth=1))
        return max(tw, bw, font_size * 0.8) + 11.0
    if sp.script in ("sub", "sup"):
        return _text_width(sp.text, font_name, font_size * 0.72) + 0.5
    if sp.code:
        return _text_width(sp.text, font_name, font_size * 0.92) + 3.5
    return _text_width(sp.text, font_name, font_size)


def _has_tall_span(spans: list[_InlineSpan]) -> bool:
    return any(sp.frac is not None or sp.binom is not None for sp in spans)


def _spans_line_height(spans: list[_InlineSpan], base_lh: float, extra: float = 7.0) -> float:
    return base_lh + (extra if _has_tall_span(spans) else 0.0)


def _wrap_spans(
    spans: list[_InlineSpan],
    font_name: str,
    font_size: float,
    max_width: float,
) -> list[list[_InlineSpan]]:
    """Wrap styled inline spans into lines that fit within `max_width` points."""
    if not spans:
        return [[]]
    lines: list[list[_InlineSpan]] = []
    cur_line: list[_InlineSpan] = []
    cur_w = 0.0

    def append_char(ch: str, bold: bool, code: bool, math: bool, highlight: bool, w: float) -> None:
        nonlocal cur_w
        if (
            cur_line
            and not cur_line[-1].badge_kind
            and cur_line[-1].frac is None
            and cur_line[-1].binom is None
            and not cur_line[-1].script
            and not cur_line[-1].overline
            and cur_line[-1].bold == bold
            and cur_line[-1].code == code
            and cur_line[-1].math == math
            and cur_line[-1].highlight == highlight
        ):
            cur_line[-1].text += ch
        else:
            cur_line.append(_InlineSpan(ch, bold=bold, code=code, math=math, highlight=highlight))
        cur_w += w

    for sp in spans:
        if sp.badge_kind or sp.frac is not None or sp.binom is not None or sp.script or sp.overline:
            sw = _span_width(sp, font_name, font_size) + (2.5 if sp.badge_kind else 0.0)
            if cur_w + sw > max_width and cur_line:
                lines.append(cur_line)
                cur_line = []
                cur_w = 0.0
            cur_line.append(sp)
            cur_w += sw
            continue
        sz = round(font_size * 0.92 if sp.code else font_size, 2)
        w_map = _CHAR_WIDTH_CACHE.setdefault((font_name, sz), {})
        first_in_code = sp.code
        tokens = re.findall(r"[A-Za-z0-9_.\-/+%]+|\s+|\S", sp.text)
        for tok in tokens:
            if tok == "\r":
                continue
            if "\n" in tok:
                for ch in tok:
                    if ch == "\r":
                        continue
                    if ch == "\n":
                        lines.append(cur_line)
                        cur_line = []
                        cur_w = 0.0
                    elif ch == "\t":
                        tw = w_map.get(" ") or pdfmetrics.stringWidth(" ", font_name, sz)
                        append_char("  ", sp.bold, sp.code, sp.math, sp.highlight, tw * 2)
                continue
            if re.fullmatch(r"[A-Za-z0-9_.\-/+%]{2,}", tok):
                tok_w = sum(
                    w_map.get(c) or w_map.setdefault(c, pdfmetrics.stringWidth(c, font_name, sz))
                    for c in tok
                )
                pad = 3.5 if first_in_code else 0.0
                if cur_w + tok_w + pad <= max_width or tok_w > max_width * 0.85:
                    if cur_w + tok_w + pad <= max_width:
                        for idx_c, ch in enumerate(tok):
                            cw = w_map[ch]
                            append_char(
                                ch,
                                sp.bold,
                                sp.code,
                                sp.math,
                                sp.highlight,
                                cw + (3.5 if first_in_code and idx_c == 0 else 0.0),
                            )
                        first_in_code = False
                        continue
                else:
                    if cur_line:
                        lines.append(cur_line)
                        cur_line = []
                        cur_w = 0.0
                    for idx_c, ch in enumerate(tok):
                        cw = w_map[ch]
                        append_char(
                            ch,
                            sp.bold,
                            sp.code,
                            sp.math,
                            sp.highlight,
                            cw + (3.5 if first_in_code and idx_c == 0 else 0.0),
                        )
                    first_in_code = False
                    continue

            for ch in tok:
                if ch == "\r":
                    continue
                if ch == "\t":
                    ch = "  "
                cw = w_map.get(ch)
                if cw is None:
                    cw = pdfmetrics.stringWidth(ch, font_name, sz)
                    w_map[ch] = cw
                pad = 3.5 if first_in_code else 0.0
                if cur_w + cw + pad > max_width and cur_line and ch not in _NO_START_PUNCT:
                    lines.append(cur_line)
                    cur_line = []
                    cur_w = 0.0
                    if ch == " ":
                        continue
                append_char(ch, sp.bold, sp.code, sp.math, sp.highlight, cw + (3.5 if first_in_code else 0.0))
                first_in_code = False

    if cur_line or not lines:
        lines.append(cur_line)
    return lines


def _draw_spans_line(
    c: canvas.Canvas,
    spans: list[_InlineSpan],
    x: float,
    y: float,
    font_name: str,
    font_size: float,
    rgb: tuple[float, float, float],
    *,
    bold_rgb: tuple[float, float, float] | None = None,
    math_rgb: tuple[float, float, float] | None = None,
) -> float:
    """Draw a single line of styled spans at (x, y) and return final x."""
    cur_x = x
    for sp in spans:
        if sp.badge_kind:
            _BADGE_STYLES = {
                "must": ((0.996, 0.949, 0.949), (0.996, 0.792, 0.792), (0.608, 0.110, 0.110)),
                "high": ((1.000, 0.984, 0.922), (0.992, 0.902, 0.541), (0.573, 0.251, 0.055)),
                "trap": ((1.000, 0.969, 0.929), (0.996, 0.843, 0.667), (0.761, 0.255, 0.047)),
                "formula": ((0.933, 0.949, 1.000), (0.780, 0.824, 0.996), (0.216, 0.188, 0.639)),
                "cw": ((0.933, 0.949, 1.000), (0.780, 0.824, 0.996), (0.216, 0.188, 0.639)),
                "opt": ((0.953, 0.957, 0.965), (0.898, 0.906, 0.922), (0.294, 0.333, 0.388)),
            }
            bg_c, bd_c, fg_c = _BADGE_STYLES.get(sp.badge_kind, _BADGE_STYLES["opt"])
            bsz = font_size * 0.82
            tw_b = _text_width(sp.text, font_name, bsz)
            bw_b = tw_b + 8.5
            bh_b = font_size + 1.8
            c.saveState()
            c.setFillColorRGB(*bg_c)
            c.setStrokeColorRGB(*bd_c)
            c.setLineWidth(0.5)
            c.roundRect(cur_x, y - 2.2, bw_b, bh_b, 2.5, fill=1, stroke=1)
            c.setFont(font_name, bsz)
            c.setFillColorRGB(*fg_c)
            c.setStrokeColorRGB(*fg_c)
            c.setLineWidth(max(0.24, bsz * 0.03))
            tb = c.beginText(cur_x + 4.25, y + font_size * 0.08)
            tb.setFont(font_name, bsz)
            tb.setTextRenderMode(2)
            tb.textOut(sp.text)
            c.drawText(tb)
            c.restoreState()
            cur_x += bw_b + 2.5
            continue

        if sp.frac is not None:
            fsz = font_size * 0.76
            num_spans = _parse_math_spans(sp.frac[0], default_bold=sp.bold, _depth=1)
            den_spans = _parse_math_spans(sp.frac[1], default_bold=sp.bold, _depth=1)
            nw = sum(_span_width(ns, font_name, fsz) for ns in num_spans)
            dw = sum(_span_width(ds, font_name, fsz) for ds in den_spans)
            box_w = max(nw, dw, font_size * 0.8) + 6.0
            bar_y = y + font_size * 0.30
            mcol = (bold_rgb or rgb) if sp.bold else (math_rgb or rgb)
            _draw_spans_line(
                c,
                num_spans,
                cur_x + (box_w - nw) / 2.0,
                bar_y + 2.2,
                font_name,
                fsz,
                mcol,
                bold_rgb=bold_rgb,
                math_rgb=math_rgb,
            )
            c.saveState()
            c.setStrokeColorRGB(*mcol)
            c.setLineWidth(max(0.55, font_size * 0.055))
            c.line(cur_x + 1.8, bar_y, cur_x + box_w - 1.8, bar_y)
            c.restoreState()
            _draw_spans_line(
                c,
                den_spans,
                cur_x + (box_w - dw) / 2.0,
                bar_y - fsz * 0.95 - 1.2,
                font_name,
                fsz,
                mcol,
                bold_rgb=bold_rgb,
                math_rgb=math_rgb,
            )
            cur_x += box_w
            continue

        if sp.binom is not None:
            fsz = font_size * 0.74
            top_spans = _parse_math_spans(sp.binom[0], default_bold=sp.bold, _depth=1)
            bot_spans = _parse_math_spans(sp.binom[1], default_bold=sp.bold, _depth=1)
            tw = sum(_span_width(ts, font_name, fsz) for ts in top_spans)
            bw = sum(_span_width(bs, font_name, fsz) for bs in bot_spans)
            box_w = max(tw, bw, font_size * 0.8) + 11.0
            mid_y = y + font_size * 0.30
            half_h = font_size * 0.65
            mcol = (bold_rgb or rgb) if sp.bold else (math_rgb or rgb)
            c.saveState()
            c.setStrokeColorRGB(*mcol)
            c.setLineWidth(0.7)
            lx = cur_x + 4.2
            rx = cur_x + box_w - 4.2
            p = c.beginPath()
            p.moveTo(lx, mid_y + half_h)
            p.curveTo(lx - 3.2, mid_y + half_h * 0.45, lx - 3.2, mid_y - half_h * 0.45, lx, mid_y - half_h)
            p.moveTo(rx, mid_y + half_h)
            p.curveTo(rx + 3.2, mid_y + half_h * 0.45, rx + 3.2, mid_y - half_h * 0.45, rx, mid_y - half_h)
            c.drawPath(p, stroke=1, fill=0)
            c.restoreState()
            _draw_spans_line(
                c,
                top_spans,
                cur_x + (box_w - tw) / 2.0,
                mid_y + 1.8,
                font_name,
                fsz,
                mcol,
                bold_rgb=bold_rgb,
                math_rgb=math_rgb,
            )
            _draw_spans_line(
                c,
                bot_spans,
                cur_x + (box_w - bw) / 2.0,
                mid_y - fsz * 0.95 - 0.8,
                font_name,
                fsz,
                mcol,
                bold_rgb=bold_rgb,
                math_rgb=math_rgb,
            )
            cur_x += box_w
            continue

        if not sp.text:
            continue

        if sp.script in ("sub", "sup"):
            sz = font_size * 0.72
            draw_y = y + font_size * 0.34 if sp.script == "sup" else y - font_size * 0.20
        elif sp.code:
            sz = font_size * 0.92
            draw_y = y
        else:
            sz = font_size
            draw_y = y

        w = _text_width(sp.text, font_name, sz)
        if sp.highlight and not sp.code:
            c.saveState()
            c.setFillColorRGB(0.996, 0.945, 0.686)
            c.rect(cur_x - 0.5, draw_y - 1.8, w + 1.0, font_size * 1.02, fill=1, stroke=0)
            c.restoreState()
        if sp.code:
            c.saveState()
            c.setFillColorRGB(0.935, 0.96, 0.945)
            c.setStrokeColorRGB(0.80, 0.87, 0.82)
            c.setLineWidth(0.4)
            c.roundRect(cur_x - 1.5, draw_y - 2.2, w + 3.0, font_size + 1.8, 2.0, fill=1, stroke=1)
            c.restoreState()
            cur_x += 1.5

        c.saveState()
        c.setFont(font_name, sz)
        if sp.bold:
            color = bold_rgb or rgb
        elif sp.code:
            color = (0.10, 0.30, 0.21)
        elif sp.math and math_rgb is not None:
            color = math_rgb
        else:
            color = rgb
        c.setFillColorRGB(*color)
        if sp.bold:
            c.setStrokeColorRGB(*color)
            c.setLineWidth(max(0.28, sz * 0.032))
            t = c.beginText(cur_x, draw_y)
            t.setFont(font_name, sz)
            t.setTextRenderMode(2)
            t.textOut(sp.text)
            c.drawText(t)
        else:
            c.drawString(cur_x, draw_y, sp.text)
        if sp.overline:
            c.setStrokeColorRGB(*color)
            c.setLineWidth(max(0.45, sz * 0.055))
            c.line(cur_x + 0.3, draw_y + sz * 0.88, cur_x + max(w - 0.3, 2.0), draw_y + sz * 0.88)
        c.restoreState()
        cur_x += w + (0.5 if sp.script else (2.0 if sp.code else 0.0))
    return cur_x


_PDF_CACHE_VERSION = "v2-bin-opt"
_PDF_JPEG_OPT_CACHE: OrderedDict[tuple[int, bytes], bytes] = OrderedDict()
_PDF_JPEG_OPT_LOCK = threading.Lock()
_PDF_JPEG_OPT_BYTES = 0
_PDF_JPEG_OPT_MAX_ITEMS = 96
_PDF_JPEG_OPT_MAX_BYTES = 12 * 1024 * 1024

_PDF_DOC_CACHE: OrderedDict[str, bytes] = OrderedDict()
_PDF_DOC_CACHE_LOCK = threading.Lock()
_PDF_DOC_CACHE_BYTES = 0
_PDF_DOC_CACHE_MAX_ITEMS = 16
_PDF_DOC_CACHE_MAX_BYTES = 24 * 1024 * 1024
_PDF_DOC_CACHE_SINGLE_MAX_BYTES = 12 * 1024 * 1024


def _jpeg_info(data: bytes) -> tuple[int, int, str] | None:
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    idx = 2
    n = len(data)
    while idx + 9 < n:
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker == 0xFF:
            idx += 1
            continue
        if marker in (0xD8, 0xD9):
            idx += 2
            continue
        seg_len = (data[idx + 2] << 8) + data[idx + 3]
        if seg_len < 2 or idx + 2 + seg_len > n:
            break
        if marker in (0xC0, 0xC1, 0xC2) and idx + 10 <= n:
            height = (data[idx + 5] << 8) + data[idx + 6]
            width = (data[idx + 7] << 8) + data[idx + 8]
            comps = data[idx + 9]
            cs = "/DeviceGray" if comps == 1 else "/DeviceRGB"
            if width > 0 and height > 0:
                return width, height, cs
        idx += 2 + seg_len
    return None


def _optimize_pdf_jpeg(data: bytes, *, max_dim: int = 1120, quality: int = 76) -> bytes:
    """Downscale and recompress oversized JPEGs for faster, lighter PDF export with bounded LRU caching."""
    global _PDF_JPEG_OPT_BYTES
    jinfo = _jpeg_info(data)
    if jinfo is None:
        return data
    iw, ih, _ = jinfo
    if iw <= max_dim and ih <= max_dim and len(data) <= 120_000:
        return data

    cache_key = (len(data), hashlib.blake2b(data, digest_size=12).digest())
    with _PDF_JPEG_OPT_LOCK:
        cached = _PDF_JPEG_OPT_CACHE.get(cache_key)
        if cached is not None:
            _PDF_JPEG_OPT_CACHE.move_to_end(cache_key)
            return cached

    encoded: bytes | None = None
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            ok, enc = cv2.imencode(
                ".jpg",
                img,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(quality), int(cv2.IMWRITE_JPEG_OPTIMIZE), 1],
            )
            if ok:
                encoded = enc.tobytes()
    except Exception:
        encoded = None

    if encoded is None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as pil_img:
                if pil_img.mode not in ("RGB", "L"):
                    pil_img = pil_img.convert("RGB")
                w, h = pil_img.size
                if max(w, h) > max_dim:
                    scale = max_dim / float(max(w, h))
                    nw = max(1, int(round(w * scale)))
                    nh = max(1, int(round(h * scale)))
                    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                    pil_img = pil_img.resize((nw, nh), resampling)
                out_buf = io.BytesIO()
                pil_img.save(out_buf, format="JPEG", quality=int(quality), optimize=True)
                encoded = out_buf.getvalue()
        except Exception:
            encoded = None

    result = data
    if encoded and len(encoded) < len(data) and _jpeg_info(encoded) is not None:
        result = encoded

    if len(result) <= 2 * 1024 * 1024:
        with _PDF_JPEG_OPT_LOCK:
            if cache_key not in _PDF_JPEG_OPT_CACHE:
                while (
                    len(_PDF_JPEG_OPT_CACHE) >= _PDF_JPEG_OPT_MAX_ITEMS
                    or (_PDF_JPEG_OPT_BYTES + len(result) > _PDF_JPEG_OPT_MAX_BYTES and _PDF_JPEG_OPT_CACHE)
                ):
                    _, evicted = _PDF_JPEG_OPT_CACHE.popitem(last=False)
                    _PDF_JPEG_OPT_BYTES = max(0, _PDF_JPEG_OPT_BYTES - len(evicted))
                _PDF_JPEG_OPT_CACHE[cache_key] = result
                _PDF_JPEG_OPT_BYTES += len(result)
            else:
                _PDF_JPEG_OPT_CACHE.move_to_end(cache_key)
    return result


def _pdf_document_cache_key(sections: list[NotePdfSection], document_title: str) -> str:
    h = hashlib.sha256()
    h.update(_PDF_CACHE_VERSION.encode("utf-8"))
    h.update(b"\0")
    h.update((document_title or "").encode("utf-8", errors="replace"))
    for sec in sections:
        h.update(b"\nS\0")
        h.update((sec.course_title or "").encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update((sec.doc_title or "").encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update((sec.subtitle or "").encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update((sec.markdown or "").encode("utf-8", errors="replace"))
        for name, img_bytes in sec.images or []:
            h.update(b"\nI\0")
            h.update(str(name or "").encode("utf-8", errors="replace"))
            h.update(f":{len(img_bytes or b'')}:".encode("ascii"))
            if img_bytes:
                h.update(hashlib.blake2b(img_bytes, digest_size=12).digest())
        if sec.visual_sections:
            h.update(b"\nV\0")
            h.update(json.dumps(sec.visual_sections, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
        if sec.oral_highlights:
            h.update(b"\nO\0")
            h.update(json.dumps(sec.oral_highlights, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()


_HEADING_TS_RE = re.compile(r"\s*\[(\d{1,3}:\d{2}(?::\d{2})?(?:\s*[-–~—]\s*\d{1,3}:\d{2}(?::\d{2})?)?)\]\s*$")


def _norm_heading_key(text: str) -> str:
    s = _HEADING_TS_RE.sub("", str(text or "")).strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"[\s·•:：,，。！？!?\-—_()（）\[\]【】\"'“”‘’/\\]+", "", s).lower()
    return s


@dataclass
class _VisualCard:
    caption: str
    board_note: str
    image_bytes: bytes
    is_blackboard: bool = False


@dataclass
class _DiagramGraph:
    mode: str  # 'dag' or 'protocol'
    direction: str = "LR"  # 'LR' or 'TD'
    nodes: list[dict] = field(default_factory=list)  # [{'id': str, 'label': str, 'shape': str}]
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # [(src_id, dst_id, label)]
    protocol_rows: list[list[str]] = field(default_factory=list)  # [[src, dst, message]]


@dataclass
class _DocBlock:
    kind: str
    # kinds: 'doc_header', 'oral_box', 'h1', 'h2', 'h3', 'paragraph', 'list_item',
    #        'quote', 'code', 'diagram', 'math_block', 'hr', 'table', 'image_card', 'spacer', 'pagebreak', 'promo_card'
    text: str = ""
    raw_text: str = ""
    subtitle: str = ""
    badge: str = ""
    bullet_marker: str = ""
    indent: float = 0.0
    rows: list[list[str]] = field(default_factory=list)
    oral_items: list[dict] = field(default_factory=list)
    visual: _VisualCard | None = None
    diagram: _DiagramGraph | None = None


def _clean_mermaid_label(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if len(text) >= 2 and (
        (text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")
    ):
        text = text[1:-1].strip()
    text = re.sub(r"<br\s*/?>|\\n", " ", text, flags=re.I)
    text = re.sub(
        r"\$\$([^$]+?)\$\$|\$([^$\n]+?)\$",
        lambda m: _clean_math(m.group(1) or m.group(2) or ""),
        text,
    )
    text = text.replace("`", "")
    return re.sub(r"\s{2,}", " ", text).strip()


def _split_mermaid_statements(raw_lines: list[str]) -> list[str]:
    """Join multi-line quoted/bracketed Mermaid statements and split unquoted `;` cleanly."""
    merged: list[str] = []
    buf: list[str] = []
    quote_ch = ""
    depth = 0

    for raw_ln in raw_lines:
        ln = raw_ln.rstrip("\r")
        stripped = ln.strip()
        if not buf and (not stripped or stripped.startswith("%%")):
            continue
        buf.append(stripped)
        ci = 0
        while ci < len(stripped):
            ch = stripped[ci]
            if quote_ch:
                if ch == "\\" and ci + 1 < len(stripped):
                    ci += 2
                    continue
                if ch == quote_ch:
                    quote_ch = ""
            else:
                if ch == '"':
                    quote_ch = '"'
                elif ch == "'" and (ci == 0 or stripped[ci - 1] in "([{|: \t"):
                    quote_ch = "'"
                elif ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth = max(0, depth - 1)
            ci += 1
        if not quote_ch and depth == 0:
            merged.append(" ".join(buf).strip())
            buf = []
    if buf:
        merged.append(" ".join(buf).strip())

    out: list[str] = []
    for stmt in merged:
        cur: list[str] = []
        qch = ""
        dp = 0
        i = 0
        while i < len(stmt):
            c = stmt[i]
            if qch:
                if c == "\\" and i + 1 < len(stmt):
                    cur.append(stmt[i : i + 2])
                    i += 2
                    continue
                if c == qch:
                    qch = ""
                cur.append(c)
            else:
                if c == '"':
                    qch = '"'
                    cur.append(c)
                elif c == "'" and (i == 0 or stmt[i - 1] in "([{|: \t"):
                    qch = "'"
                    cur.append(c)
                elif c in "([{":
                    dp += 1
                    cur.append(c)
                elif c in ")]}":
                    dp = max(0, dp - 1)
                    cur.append(c)
                elif c == ";" and dp == 0:
                    piece = "".join(cur).strip()
                    if piece:
                        out.append(piece)
                    cur = []
                else:
                    cur.append(c)
            i += 1
        tail = "".join(cur).strip()
        if tail:
            out.append(tail)
    return out


_ARROW_PIPE_RE = re.compile(
    r"^(?:<)?(?:---|-->|===|==>|-\.-|-\.->|-..\->|--x|-\)|--\)|->>|-->>|->|--)\s*\|([^|\n]*)\|"
)
_ARROW_INLINE_DASH_RE = re.compile(r'^--\s+(?:"([^"\n]+)"|([^->|\n]+?))\s+-->')
_ARROW_INLINE_DOT_RE = re.compile(r'^-\.\s*(?:"([^"\n]+)"|([^.\n]+?))\s*\.->')
_ARROW_INLINE_EQ_RE = re.compile(r'^==\s*(?:"([^"\n]+)"|([^=\n]+?))\s*==>')
_ARROW_PLAIN_RE = re.compile(r"^(?:<-->|--->|---|===>|==>|<==>|-\.\.->|-\.->|->>|-->>|-->|->|→|➔|⟹|⇒)")


def _split_mermaid_chain(stmt: str) -> list:
    """Split a statement into alternating [group_str, (op, label), group_str, ...]."""
    parts: list = []
    cur: list[str] = []
    qch = ""
    dp = 0
    i = 0
    n = len(stmt)
    while i < n:
        ch = stmt[i]
        if qch:
            if ch == "\\" and i + 1 < n:
                cur.append(stmt[i : i + 2])
                i += 2
                continue
            if ch == qch:
                qch = ""
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            qch = '"'
            cur.append(ch)
            i += 1
            continue
        if ch == "'" and (i == 0 or stmt[i - 1] in "([{|: \t"):
            qch = "'"
            cur.append(ch)
            i += 1
            continue
        if ch in "([{":
            dp += 1
            cur.append(ch)
            i += 1
            continue
        if ch in ")]}":
            dp = max(0, dp - 1)
            cur.append(ch)
            i += 1
            continue
        if dp == 0:
            rest = stmt[i:]
            prev_ok = i == 0 or stmt[i - 1].isspace() or stmt[i - 1] in ")]}\"*`" or stmt[i - 1].isalnum() or ord(stmt[i - 1]) > 127
            if prev_ok:
                m_pipe = _ARROW_PIPE_RE.match(rest)
                if m_pipe:
                    lhs = "".join(cur).strip()
                    if lhs:
                        parts.append(lhs)
                    parts.append((m_pipe.group(0), _clean_mermaid_label(m_pipe.group(1))))
                    cur = []
                    i += len(m_pipe.group(0))
                    continue
                for inl_re in (_ARROW_INLINE_DASH_RE, _ARROW_INLINE_DOT_RE, _ARROW_INLINE_EQ_RE):
                    m_inl = inl_re.match(rest)
                    if m_inl and (i == 0 or stmt[i - 1].isspace() or stmt[i - 1] in ")]}\"*"):
                        lhs = "".join(cur).strip()
                        if lhs:
                            parts.append(lhs)
                        lbl = _clean_mermaid_label(m_inl.group(1) or m_inl.group(2) or "")
                        parts.append((m_inl.group(0), lbl))
                        cur = []
                        i += len(m_inl.group(0))
                        continue
                else:
                    m_plain = _ARROW_PLAIN_RE.match(rest)
                    if m_plain:
                        op = m_plain.group(0)
                        boundary_ok = (op in ("→", "➔", "⟹", "⇒")) or (
                            (i == 0 or stmt[i - 1].isspace() or stmt[i - 1] in ")]}\"*`" or stmt[i - 1].isalnum())
                        )
                        if boundary_ok:
                            lhs = "".join(cur).strip()
                            if lhs:
                                parts.append(lhs)
                            parts.append((op, ""))
                            cur = []
                            i += len(op)
                            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_ampersand_group(group_str: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    qch = ""
    dp = 0
    i = 0
    while i < len(group_str):
        ch = group_str[i]
        if qch:
            if ch == "\\" and i + 1 < len(group_str):
                cur.append(group_str[i : i + 2])
                i += 2
                continue
            if ch == qch:
                qch = ""
            cur.append(ch)
        else:
            if ch == '"':
                qch = '"'
                cur.append(ch)
            elif ch == "'" and (i == 0 or group_str[i - 1] in "([{|: \t"):
                qch = "'"
                cur.append(ch)
            elif ch in "([{":
                dp += 1
                cur.append(ch)
            elif ch in ")]}":
                dp = max(0, dp - 1)
                cur.append(ch)
            elif ch == "&" and dp == 0:
                piece = "".join(cur).strip()
                if piece:
                    out.append(piece)
                cur = []
            else:
                cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _parse_node_spec(raw_tok: str) -> tuple[str | None, str | None, str | None]:
    """Parse Mermaid node token into (node_id, label, shape). Empty label/shape means bare reference."""
    tok = re.sub(r":::[A-Za-z0-9_-]+$", "", (raw_tok or "").strip()).strip()
    if not tok:
        return None, None, None
    if tok == "[*]":
        return "__state_star__", "起止状态", "pill"

    m_id_br = re.match(r"^([A-Za-z_][A-Za-z0-9_-]{0,31})\s*([\[\(\{].*)$", tok, re.S)
    if m_id_br:
        nid = m_id_br.group(1)
        body = m_id_br.group(2).strip()
        shape = "rect"
        inner = body
        pairs = (
            ("([", "])", "pill"),
            ("(((", ")))", "pill"),
            ("((", "))", "pill"),
            ("[(", ")]", "rect"),
            ("[[", "]]", "rect"),
            ("{{", "}}", "diamond"),
            ("[/", "/]", "rect"),
            ("[\\", "\\]", "rect"),
            ("[/", "\\]", "rect"),
            ("[", "]", "rect"),
            ("{", "}", "diamond"),
            ("(", ")", "round"),
        )
        matched_wrapper = False
        for op_s, cl_s, shp in pairs:
            if body.startswith(op_s) and body.endswith(cl_s) and len(body) > len(op_s) + len(cl_s):
                shape = shp
                inner = body[len(op_s) : len(body) - len(cl_s)]
                matched_wrapper = True
                break
        if matched_wrapper:
            return nid, _clean_mermaid_label(inner), shape

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", tok):
        return tok, None, None

    return None, _clean_mermaid_label(tok), "rect"


def _parse_diagram_block(raw_lines: list[str], code_lang: str = "") -> _DocBlock | None:
    """Parse Mermaid (`flowchart`/`graph`/`sequenceDiagram`/`stateDiagram`/`mindmap`) or legacy `diagram` fences."""
    non_empty = [ln.rstrip("\r") for ln in raw_lines if ln.strip() and not ln.strip().startswith("%%")]
    if not non_empty:
        return None

    head_line = non_empty[0].strip()
    has_seq_header = bool(re.match(r"^sequenceDiagram\b", head_line, re.I))
    has_mindmap_header = bool(re.match(r"^mindmap\b", head_line, re.I))
    has_state_header = bool(re.match(r"^stateDiagram(?:-v2)?\b", head_line, re.I))
    m_flow_hdr = re.match(r"^(?:flowchart|graph)(?:\s+(TD|TB|BT|LR|RL))?\b", head_line, re.I)
    has_flow_header = bool(m_flow_hdr)

    # 1. Sequence / Protocol diagrams
    if has_seq_header or (
        not has_flow_header
        and not has_mindmap_header
        and not has_state_header
        and (
            any(re.search(r"->>|-->>", ln) for ln in non_empty)
            or sum(
                1
                for ln in non_empty
                if re.match(r"^[^\s\[\]{}()]+?\s*(?:→|->|-->)\s*[^\s\[\]{}()]+?\s*[:：]\s*.+$", ln.strip())
            )
            >= max(1, len(non_empty) // 2)
        )
    ):
        alias_map: dict[str, str] = {}
        proto_rows: list[list[str]] = []
        seq_arrow_re = re.compile(
            r"^\s*([^\s:：]+?)\s*(?:<<?-->>|<<?->>|->>|-->>|-->|->|--x|-x|-\)|--\)|→)[+-]?\s*([^\s:：]+?)\s*[:：]\s*(.+?)\s*$"
        )
        for ln in non_empty:
            st = ln.strip()
            if has_seq_header and re.match(r"^sequenceDiagram\b", st, re.I):
                continue
            m_part = re.match(r"^(?:participant|actor)\s+([^\s]+)(?:\s+as\s+(.+))?$", st, re.I)
            if m_part:
                pid = m_part.group(1).strip()
                pname = _clean_mermaid_label(m_part.group(2) or pid)
                alias_map[pid] = pname
                continue
            m_note = re.match(r"^Note\s+(?:over|left of|right of)\s+[^:：]+[:：]\s*(.+)$", st, re.I)
            if m_note:
                proto_rows.append(["说明", "", _clean_mermaid_label(m_note.group(1))])
                continue
            if re.match(r"^(?:loop|alt|else|opt|par|and|critical|option|break|rect|end|autonumber|activate|deactivate|box|title)\b", st, re.I):
                if re.match(r"^(?:alt|else|loop|opt)\s+(.+)$", st, re.I):
                    cond_txt = _clean_mermaid_label(re.sub(r"^(?:alt|else|loop|opt)\s+", "", st, flags=re.I))
                    if cond_txt:
                        proto_rows.append(["条件", "", cond_txt])
                continue
            m_arr = seq_arrow_re.match(st)
            if m_arr:
                src = alias_map.get(m_arr.group(1).strip(), m_arr.group(1).strip())
                dst = alias_map.get(m_arr.group(2).strip(), m_arr.group(2).strip())
                msg = _clean_mermaid_label(m_arr.group(3))
                proto_rows.append([src, dst, msg])
            else:
                cleaned_fallback = _clean_mermaid_label(st)
                if cleaned_fallback:
                    m_actor = re.match(r"^([^:：→]{1,22})[:：]\s*(.+)$", cleaned_fallback)
                    if m_actor:
                        proto_rows.append(
                            [
                                alias_map.get(m_actor.group(1).strip(), m_actor.group(1).strip()),
                                "",
                                m_actor.group(2).strip(),
                            ]
                        )
                    else:
                        proto_rows.append(["", "", cleaned_fallback])
        if proto_rows:
            return _DocBlock(
                kind="diagram",
                rows=proto_rows,
                subtitle="protocol",
                diagram=_DiagramGraph(mode="protocol", protocol_rows=proto_rows),
            )
        return None

    # 2. Mindmap diagram
    if has_mindmap_header:
        mm_nodes: list[dict] = []
        mm_edges: list[tuple[str, str, str]] = []
        stack: list[tuple[int, str]] = []
        seq_idx = 0
        for ln in non_empty[1:]:
            st = ln.strip()
            if not st or st.startswith("::icon") or st.startswith(":::"):
                continue
            indent = len(ln) - len(ln.lstrip(" \t"))
            clean_lbl = re.sub(r"^[A-Za-z0-9_-]*\s*(?:\(\(|\(\[|\{\{|\(|\[)", "", st)
            clean_lbl = re.sub(r"(?:\)\)|\]\)|\}\}|\)|\])$", "", clean_lbl)
            clean_lbl = _clean_mermaid_label(clean_lbl or st)
            nid = f"m_{seq_idx}"
            seq_idx += 1
            mm_nodes.append({"id": nid, "label": clean_lbl, "shape": "pill" if seq_idx == 1 else "round"})
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                mm_edges.append((stack[-1][1], nid, ""))
            stack.append((indent, nid))
        if mm_nodes:
            return _DocBlock(
                kind="diagram",
                rows=[[nd["label"]] for nd in mm_nodes],
                diagram=_DiagramGraph(mode="dag", direction="LR", nodes=mm_nodes, edges=mm_edges),
            )
        return None

    # 3. Flowchart / Graph / StateDiagram / Legacy Diagram
    direction = "LR"
    if m_flow_hdr and m_flow_hdr.group(1):
        d_tok = m_flow_hdr.group(1).upper()
        direction = "TD" if d_tok in ("TD", "TB", "BT") else "LR"
    elif has_state_header:
        direction = "LR"

    stmts = _split_mermaid_statements(raw_lines)
    node_map: dict[str, dict] = {}
    node_order: list[str] = []
    txt_to_id: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    edge_set: set[tuple[str, str, str]] = set()
    synthetic_counter = 0

    def _register_node(raw_tok: str) -> str | None:
        nonlocal synthetic_counter
        nid, lbl, shp = _parse_node_spec(raw_tok)
        if nid is not None:
            if nid not in node_map:
                node_map[nid] = {
                    "id": nid,
                    "label": lbl if lbl is not None else nid,
                    "shape": shp or "rect",
                }
                node_order.append(nid)
            else:
                if lbl is not None:
                    node_map[nid]["label"] = lbl
                if shp is not None:
                    node_map[nid]["shape"] = shp
            return nid
        if lbl:
            key = re.sub(r"\s+", " ", lbl).strip()
            if key in txt_to_id:
                return txt_to_id[key]
            synthetic_counter += 1
            sid = f"__n_{synthetic_counter}"
            txt_to_id[key] = sid
            node_map[sid] = {"id": sid, "label": lbl, "shape": shp or "rect"}
            node_order.append(sid)
            return sid
        return None

    for stmt in stmts:
        st = stmt.strip()
        if not st or st.startswith("%%"):
            continue
        if re.match(
            r"^(?:flowchart|graph|stateDiagram(?:-v2)?|classDef|class|style|linkStyle|click|direction|subgraph|end|accTitle|accDescr)\b",
            st,
            re.I,
        ):
            if re.match(r"^direction\s+(TD|TB|BT|LR|RL)\b", st, re.I):
                d2 = st.split()[1].upper()
                direction = "TD" if d2 in ("TD", "TB", "BT") else "LR"
            continue

        # StateDiagram colon description: `StateId : Description` (when no arrow in statement)
        if has_state_header and "-->" not in st and "->" not in st:
            m_st_desc = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+)$", st)
            if m_st_desc:
                sid = m_st_desc.group(1).strip()
                desc = _clean_mermaid_label(m_st_desc.group(2))
                if sid not in node_map:
                    node_map[sid] = {"id": sid, "label": desc, "shape": "round"}
                    node_order.append(sid)
                else:
                    node_map[sid]["label"] = desc
                continue

        chain = _split_mermaid_chain(st)
        if len(chain) >= 3:
            # Handle trailing `: label` on last stateDiagram transition (e.g. `A --> B : cond`)
            last_item = chain[-1]
            if isinstance(last_item, str) and ":" in last_item and not re.search(r'["\[\(\{].*:', last_item):
                m_tail = re.match(r"^([A-Za-z0-9_\-\[\]\*\(\)\{\}\s]+?)\s*:\s*(.+)$", last_item)
                if m_tail and has_state_header:
                    chain[-1] = m_tail.group(1).strip()
                    op_prev, old_lbl = chain[-2]
                    if not old_lbl:
                        chain[-2] = (op_prev, _clean_mermaid_label(m_tail.group(2)))

            idx_p = 0
            while idx_p + 2 < len(chain):
                lhs_raw = chain[idx_p]
                edge_info = chain[idx_p + 1]
                rhs_raw = chain[idx_p + 2]
                edge_lbl = edge_info[1] if isinstance(edge_info, tuple) else ""
                lhs_ids = [_register_node(t) for t in _split_ampersand_group(str(lhs_raw))]
                rhs_ids = [_register_node(t) for t in _split_ampersand_group(str(rhs_raw))]
                for u in lhs_ids:
                    for v in rhs_ids:
                        if u and v and u != v:
                            ekey = (u, v, edge_lbl)
                            if ekey not in edge_set:
                                edge_set.add(ekey)
                                edges.append(ekey)
                idx_p += 2
        elif chain and isinstance(chain[0], str):
            for t in _split_ampersand_group(chain[0]):
                _register_node(t)

    if not node_order:
        return None

    # Standalone multi-line numbered/step cards without arrows (e.g. legacy 4-step list) -> TD sequential chain
    if not edges and len(node_order) >= 2:
        direction = "TD"
        for idx_k in range(len(node_order) - 1):
            edges.append((node_order[idx_k], node_order[idx_k + 1], ""))

    ordered_nodes = [node_map[nid] for nid in node_order]
    fallback_rows = [[nd["label"]] for nd in ordered_nodes]
    return _DocBlock(
        kind="diagram",
        rows=fallback_rows,
        subtitle="",
        diagram=_DiagramGraph(mode="dag", direction=direction, nodes=ordered_nodes, edges=edges),
    )


def _assign_dag_layers(diag: _DiagramGraph) -> tuple[str, list[list[dict]], list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Compute cycle-safe topological ranks, smart orientation, dummy waypoints, and crossing-minimized layer ordering."""
    node_lookup: dict[str, dict] = {
        nd["id"]: {"id": nd["id"], "label": nd["label"], "shape": nd["shape"], "is_dummy": False, "orig_idx": i}
        for i, nd in enumerate(diag.nodes)
    }
    adj_all: dict[str, list[tuple[str, str]]] = {nid: [] for nid in node_lookup}
    for u, v, lbl in diag.edges:
        if u in node_lookup and v in node_lookup and u != v:
            adj_all[u].append((v, lbl))

    # Break cycles safely via DFS back-edge detection
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
    dag_edges: list[tuple[str, str, str]] = []
    back_edges: list[tuple[str, str, str]] = []

    def _dfs(u: str) -> None:
        state[u] = 1
        for v, lbl in adj_all[u]:
            st_v = state.get(v, 0)
            if st_v == 1:
                back_edges.append((u, v, lbl))
            else:
                dag_edges.append((u, v, lbl))
                if st_v == 0:
                    _dfs(v)
        state[u] = 2

    for nd in diag.nodes:
        if state.get(nd["id"], 0) == 0:
            _dfs(nd["id"])

    # Compute longest-path topological rank
    in_deg: dict[str, int] = {nid: 0 for nid in node_lookup}
    out_dag: dict[str, list[tuple[str, str]]] = {nid: [] for nid in node_lookup}
    in_dag: dict[str, list[str]] = {nid: [] for nid in node_lookup}
    for u, v, lbl in dag_edges:
        out_dag[u].append((v, lbl))
        in_dag[v].append(u)
        in_deg[v] += 1

    rank: dict[str, int] = {nid: 0 for nid in node_lookup}
    q = [nd["id"] for nd in diag.nodes if in_deg[nd["id"]] == 0]
    in_deg_rem = dict(in_deg)
    topo: list[str] = []
    while q:
        u = q.pop(0)
        topo.append(u)
        for v, _ in out_dag[u]:
            rank[v] = max(rank[v], rank[u] + 1)
            in_deg_rem[v] -= 1
            if in_deg_rem[v] == 0:
                q.append(v)

    # Pull slack root nodes closer to their shallowest child (`B --> C` when C is rank 2 -> B gets rank 1)
    for u in reversed(topo):
        if in_deg[u] == 0 and out_dag[u]:
            min_child_r = min(rank[v] for v, _ in out_dag[u])
            if min_child_r > rank[u] + 1:
                rank[u] = min_child_r - 1

    max_rank = max(rank.values(), default=0)
    raw_layers_count = [0] * (max_rank + 1)
    for nid, r in rank.items():
        raw_layers_count[r] += 1

    direction = diag.direction
    num_ranks = max_rank + 1
    max_per_layer = max(raw_layers_count, default=1)
    if direction == "LR" and num_ranks >= 6:
        direction = "TD"
    elif direction == "TD" and max_per_layer >= 6 and num_ranks <= 4:
        direction = "LR"

    # Insert dummy waypoints for long span edges (`rank[v] - rank[u] > 1`)
    rendered_edges: list[tuple[str, str, str]] = []
    dummy_idx = 0
    for u, v, lbl in dag_edges:
        du, dv = rank[u], rank[v]
        if dv - du <= 1:
            rendered_edges.append((u, v, lbl))
        else:
            prev_id = u
            for step_r in range(du + 1, dv):
                dummy_idx += 1
                did = f"__dum_{dummy_idx}"
                node_lookup[did] = {
                    "id": did,
                    "label": "",
                    "shape": "dummy",
                    "is_dummy": True,
                    "orig_idx": 10000 + dummy_idx,
                }
                rank[did] = step_r
                rendered_edges.append((prev_id, did, lbl if step_r == du + 1 else ""))
                prev_id = did
            rendered_edges.append((prev_id, v, ""))

    layers: list[list[dict]] = [[] for _ in range(num_ranks)]
    for nid, nd in node_lookup.items():
        layers[rank[nid]].append(nd)
    for r in range(num_ranks):
        layers[r].sort(key=lambda item: item["orig_idx"])

    # Crossing reduction via barycenter ordering (top-down + bottom-up + top-down)
    parents_of: dict[str, list[str]] = {nid: [] for nid in node_lookup}
    children_of: dict[str, list[str]] = {nid: [] for nid in node_lookup}
    for u, v, _ in rendered_edges:
        children_of[u].append(v)
        parents_of[v].append(u)

    for _sweep in range(2):
        for r in range(1, num_ranks):
            pos_prev = {nd["id"]: idx for idx, nd in enumerate(layers[r - 1])}
            layers[r].sort(
                key=lambda nd: (
                    sum(pos_prev[p] for p in parents_of[nd["id"]] if p in pos_prev)
                    / max(1, sum(1 for p in parents_of[nd["id"]] if p in pos_prev))
                    if any(p in pos_prev for p in parents_of[nd["id"]])
                    else float(nd["orig_idx"]),
                    nd["orig_idx"],
                )
            )
        for r in range(num_ranks - 2, -1, -1):
            pos_next = {nd["id"]: idx for idx, nd in enumerate(layers[r + 1])}
            layers[r].sort(
                key=lambda nd: (
                    sum(pos_next[c] for c in children_of[nd["id"]] if c in pos_next)
                    / max(1, sum(1 for c in children_of[nd["id"]] if c in pos_next))
                    if any(c in pos_next for c in children_of[nd["id"]])
                    else float(nd["orig_idx"]),
                    nd["orig_idx"],
                )
            )

    return direction, layers, rendered_edges, back_edges


def _prepare_dag_layout(diag: _DiagramGraph, font_name: str, diag_w: float) -> dict:
    """Compute exact 2D node boxes and Bezier edges inside local `[0, diag_w] x [0, graph_h]` coordinates."""
    direction, layers, rendered_edges, back_edges = _assign_dag_layers(diag)
    num_ranks = len(layers)

    def _est_text_w(txt: str, fsz: float = 8.2) -> float:
        plain = re.sub(r"[*_`\$]+", "", txt or "")
        return pdfmetrics.stringWidth(plain, font_name, fsz)

    edge_labels_between: dict[int, list[str]] = {r: [] for r in range(max(0, num_ranks - 1))}
    node_rank_map = {nd["id"]: r for r, layer in enumerate(layers) for nd in layer}
    for u, v, lbl in rendered_edges:
        if lbl:
            ru = node_rank_map.get(u, 0)
            edge_labels_between.setdefault(ru, []).append(lbl)

    parents_of: dict[str, list[str]] = {nd["id"]: [] for layer in layers for nd in layer}
    children_of: dict[str, list[str]] = {nd["id"]: [] for layer in layers for nd in layer}
    for u, v, _ in rendered_edges:
        children_of[u].append(v)
        parents_of[v].append(u)

    boxes: dict[str, dict] = {}
    graph_h = 0.0

    if direction == "TD":
        row_hs: list[float] = []
        row_gaps: list[float] = []
        cur_y = 0.0
        for r, layer in enumerate(layers):
            dummies = sum(1 for nd in layer if nd["is_dummy"])
            reals = [nd for nd in layer if not nd["is_dummy"]]
            gap_x = 8.5
            n_total = len(layer)
            avail_w = max(80.0, diag_w - dummies * 14.0 - gap_x * max(0, n_total - 1))
            if reals:
                nat_ws = [
                    max(
                        62.0,
                        min(
                            220.0 if len(reals) >= 2 else min(280.0, diag_w * 0.72),
                            _est_text_w(nd["label"], 8.2) + (26.0 if nd["shape"] == "diamond" else 20.0),
                        ),
                    )
                    for nd in reals
                ]
                sum_nat = sum(nat_ws) or 1.0
                if sum_nat <= avail_w:
                    real_ws = nat_ws
                else:
                    scale = avail_w / sum_nat
                    min_rw = min(64.0, avail_w / max(1, len(reals)) * 0.72)
                    real_ws = [max(46.0, nw * scale) for nw in nat_ws]
                    deficits = sum(max(0.0, min_rw - w) for w in real_ws)
                    if deficits > 0.0:
                        surplus = sum(max(0.0, w - min_rw) for w in real_ws) or 1.0
                        real_ws = [
                            min_rw if w < min_rw else w - deficits * ((w - min_rw) / surplus)
                            for w in real_ws
                        ]
            else:
                real_ws = []

            rw_iter = iter(real_ws)
            row_items: list[dict] = []
            max_bh = 22.0
            for nd in layer:
                if nd["is_dummy"]:
                    item = {"nd": nd, "w": 12.0, "h": 4.0, "lines": [], "lhs": []}
                else:
                    bw = next(rw_iter)
                    x_pad_inner = 18.0 if nd["shape"] == "diamond" else 12.0
                    spans = _parse_inline_spans(nd["label"], default_bold=(r == 0 and num_ranks > 1))
                    wlines = _wrap_spans(spans, font_name, 8.2, max(36.0, bw - x_pad_inner))
                    lhs = [_spans_line_height(wl, 11.4, extra=4.5) for wl in wlines]
                    bh = max(21.0, sum(lhs) + 9.0)
                    max_bh = max(max_bh, bh)
                    item = {"nd": nd, "w": bw, "h": bh, "lines": wlines, "lhs": lhs}
                row_items.append(item)

            total_row_w = sum(it["w"] for it in row_items) + gap_x * max(0, len(row_items) - 1)
            cx = max(0.0, (diag_w - total_row_w) / 2.0)
            for it in row_items:
                bh = it["h"] if not it["nd"]["is_dummy"] else 4.0
                boxes[it["nd"]["id"]] = {
                    **it["nd"],
                    "rank": r,
                    "x": cx,
                    "y": cur_y + (max_bh - bh) / 2.0,
                    "w": it["w"],
                    "h": bh,
                    "lines": it["lines"],
                    "lhs": it["lhs"],
                }
                cx += it["w"] + gap_x
            row_hs.append(max_bh)
            rgap = 26.0 if edge_labels_between.get(r) else 18.0
            row_gaps.append(rgap)
            cur_y += max_bh + (rgap if r < num_ranks - 1 else 0.0)
        graph_h = cur_y

        # Barycenter horizontal X alignment (bottom-up then top-down) for centered trees
        for _align_pass in range(2):
            r_seq = range(num_ranks - 2, -1, -1) if _align_pass == 0 else range(1, num_ranks)
            for r in r_seq:
                layer = layers[r]
                desired: list[float] = []
                for nd in layer:
                    bx = boxes[nd["id"]]
                    nbrs = children_of[nd["id"]] if _align_pass == 0 else parents_of[nd["id"]]
                    valid_nbrs = [n for n in nbrs if n in boxes]
                    if valid_nbrs:
                        des_cx = sum(boxes[n]["x"] + boxes[n]["w"] / 2.0 for n in valid_nbrs) / float(len(valid_nbrs))
                    else:
                        des_cx = bx["x"] + bx["w"] / 2.0
                    desired.append(des_cx)
                # Pack left-to-right near desired centers without overlapping
                gap_x = 8.5
                xs_new: list[float] = []
                for idx_n, (nd, des_cx) in enumerate(zip(layer, desired)):
                    bw = boxes[nd["id"]]["w"]
                    left_bound = (xs_new[-1] + boxes[layer[idx_n - 1]["id"]]["w"] + gap_x) if idx_n > 0 else 0.0
                    x_want = max(left_bound, min(diag_w - bw, des_cx - bw / 2.0))
                    xs_new.append(x_want)
                # Push back from right edge if needed
                for idx_n in range(len(layer) - 1, -1, -1):
                    bw = boxes[layer[idx_n]["id"]]["w"]
                    right_bound = (xs_new[idx_n + 1] - gap_x - bw) if idx_n + 1 < len(layer) else (diag_w - bw)
                    if xs_new[idx_n] > right_bound:
                        xs_new[idx_n] = max(0.0, right_bound)
                for nd, nx in zip(layer, xs_new):
                    boxes[nd["id"]]["x"] = nx
    else:
        # 'LR' horizontal layered layout (columns = ranks)
        col_gaps: list[float] = []
        for c in range(max(0, num_ranks - 1)):
            lbls = edge_labels_between.get(c, [])
            if lbls:
                max_lw = max(_est_text_w(lb, 7.3) for lb in lbls)
                col_gaps.append(max(26.0, min(62.0, max_lw + 18.0)))
            else:
                col_gaps.append(18.0)

        avail_cols_w = max(90.0, diag_w - sum(col_gaps))
        col_nats: list[float] = []
        for layer in layers:
            reals = [nd for nd in layer if not nd["is_dummy"]]
            if not reals:
                col_nats.append(14.0)
            else:
                cw_est = max(
                    _est_text_w(nd["label"], 8.1) + (24.0 if nd["shape"] == "diamond" else 18.0)
                    for nd in reals
                )
                col_nats.append(max(58.0, min(175.0, cw_est)))

        sum_nats = sum(col_nats) or 1.0
        if sum_nats <= avail_cols_w:
            col_ws = col_nats
        else:
            scale = avail_cols_w / sum_nats
            min_cw = min(56.0, avail_cols_w / max(1, num_ranks) * 0.72)
            col_ws = [
                12.0 if all(nd["is_dummy"] for nd in layers[c]) else max(44.0, col_nats[c] * scale)
                for c in range(num_ranks)
            ]
            deficits = sum(max(0.0, min_cw - w) for c, w in enumerate(col_ws) if not all(nd["is_dummy"] for nd in layers[c]))
            if deficits > 0.0:
                surplus = sum(max(0.0, w - min_cw) for c, w in enumerate(col_ws) if not all(nd["is_dummy"] for nd in layers[c])) or 1.0
                col_ws = [
                    w
                    if all(nd["is_dummy"] for nd in layers[c])
                    else (min_cw if w < min_cw else w - deficits * ((w - min_cw) / surplus))
                    for c, w in enumerate(col_ws)
                ]

        total_cols_w = sum(col_ws) + sum(col_gaps)
        col_xs: list[float] = [max(0.0, (diag_w - total_cols_w) / 2.0)]
        for c_i in range(num_ranks - 1):
            col_xs.append(col_xs[-1] + col_ws[c_i] + col_gaps[c_i])

        v_gap = 10.0
        col_totals: list[float] = []
        for c_i, layer in enumerate(layers):
            cw = col_ws[c_i]
            cur_y = 0.0
            for idx_n, nd in enumerate(layer):
                if nd["is_dummy"]:
                    bh = 4.0
                    lines, lhs = [], []
                else:
                    x_pad_inner = 18.0 if nd["shape"] == "diamond" else 11.0
                    spans = _parse_inline_spans(nd["label"], default_bold=(c_i == 0 and num_ranks > 1))
                    lines = _wrap_spans(spans, font_name, 8.1, max(36.0, cw - x_pad_inner))
                    lhs = [_spans_line_height(wl, 11.4, extra=4.5) for wl in lines]
                    bh = max(21.0, sum(lhs) + 9.0)
                boxes[nd["id"]] = {
                    **nd,
                    "rank": c_i,
                    "x": col_xs[c_i],
                    "y": cur_y,
                    "w": cw,
                    "h": bh,
                    "lines": lines,
                    "lhs": lhs,
                }
                cur_y += bh + (v_gap if idx_n < len(layer) - 1 else 0.0)
            col_totals.append(cur_y)

        graph_h = max(col_totals, default=22.0)
        # Center shorter columns first
        for c_i, layer in enumerate(layers):
            offset = max(0.0, (graph_h - col_totals[c_i]) / 2.0)
            if offset > 0.0:
                for nd in layer:
                    boxes[nd["id"]]["y"] += offset

        # Relax Y positions toward connected neighbors starting from the tallest column
        anchor_col = max(range(num_ranks), key=lambda idx_c: col_totals[idx_c])
        order_cols = list(range(anchor_col - 1, -1, -1)) + list(range(anchor_col + 1, num_ranks))
        for c_i in order_cols:
            layer = layers[c_i]
            use_right = c_i < anchor_col
            desired_ys: list[float] = []
            for nd in layer:
                bx = boxes[nd["id"]]
                nbrs = [n for n in (children_of[nd["id"]] if use_right else parents_of[nd["id"]]) if n in boxes]
                if nbrs:
                    des_cy = sum(boxes[n]["y"] + boxes[n]["h"] / 2.0 for n in nbrs) / float(len(nbrs))
                    desired_ys.append(des_cy)
                else:
                    desired_ys.append(bx["y"] + bx["h"] / 2.0)
            ys_new: list[float] = []
            for idx_n, (nd, des_cy) in enumerate(zip(layer, desired_ys)):
                bh = boxes[nd["id"]]["h"]
                top_bound = (ys_new[-1] + boxes[layer[idx_n - 1]["id"]]["h"] + v_gap) if idx_n > 0 else 0.0
                y_want = max(top_bound, min(graph_h - bh, des_cy - bh / 2.0))
                ys_new.append(y_want)
            for idx_n in range(len(layer) - 1, -1, -1):
                bh = boxes[layer[idx_n]["id"]]["h"]
                bot_bound = (ys_new[idx_n + 1] - v_gap - bh) if idx_n + 1 < len(layer) else (graph_h - bh)
                if ys_new[idx_n] > bot_bound:
                    ys_new[idx_n] = max(0.0, bot_bound)
            for nd, ny in zip(layer, ys_new):
                boxes[nd["id"]]["y"] = ny

    return {
        "direction": direction,
        "boxes": boxes,
        "rendered_edges": rendered_edges,
        "back_edges": back_edges,
        "graph_h": max(24.0, graph_h),
    }


def _draw_arrowhead(
    c: canvas.Canvas,
    tx: float,
    ty: float,
    direction: str,
    rgb: tuple[float, float, float] = (0.094, 0.361, 0.239),
) -> None:
    c.setFillColorRGB(*rgb)
    pth = c.beginPath()
    if direction == "TD":
        pth.moveTo(tx, ty)
        pth.lineTo(tx - 2.9, ty + 5.2)
        pth.lineTo(tx + 2.9, ty + 5.2)
    else:
        pth.moveTo(tx, ty)
        pth.lineTo(tx - 5.2, ty + 2.9)
        pth.lineTo(tx - 5.2, ty - 2.9)
    pth.close()
    c.drawPath(pth, stroke=0, fill=1)


def _draw_dag_diagram_card(
    c: canvas.Canvas,
    top_fy: float,
    total_h: float,
    left_x: float,
    content_w: float,
    inner_x: float,
    diag_w: float,
    hdr_h: float,
    font_name: str,
    layout: dict,
    title_txt: str = "知识流程与结构图解",
) -> None:
    c.saveState()
    c.setFillColorRGB(0.985, 0.992, 0.989)
    c.setStrokeColorRGB(0.79, 0.87, 0.83)
    c.setLineWidth(0.55)
    c.roundRect(left_x, top_fy - total_h, content_w, total_h, 4.0, fill=1, stroke=1)
    c.setFillColorRGB(0.935, 0.968, 0.952)
    c.roundRect(left_x, top_fy - hdr_h, content_w, hdr_h, 4.0, fill=1, stroke=0)
    c.setStrokeColorRGB(0.80, 0.88, 0.84)
    c.line(left_x, top_fy - hdr_h, left_x + content_w, top_fy - hdr_h)
    _draw_spans_line(
        c,
        [_InlineSpan(title_txt, bold=True)],
        left_x + 11.0,
        top_fy - 13.5,
        font_name,
        8.8,
        (0.094, 0.361, 0.239),
    )

    direction = layout["direction"]
    boxes = layout["boxes"]
    origin_y = top_fy - hdr_h - 10.0

    def _to_canvas(lx: float, ly: float) -> tuple[float, float]:
        return inner_x + lx, origin_y - ly

    # 1. Draw DAG edges & arrowheads first
    edge_rgb = (0.14, 0.40, 0.28)
    label_queue: list[tuple[float, float, str]] = []
    for u_id, v_id, lbl in layout["rendered_edges"]:
        bu = boxes.get(u_id)
        bv = boxes.get(v_id)
        if not bu or not bv:
            continue
        c.setStrokeColorRGB(*edge_rgb)
        c.setLineWidth(0.95)
        if direction == "TD":
            x1, y1 = _to_canvas(
                bu["x"] + bu["w"] / 2.0,
                bu["y"] + (bu["h"] / 2.0 if bu["is_dummy"] else bu["h"]),
            )
            x2, y2 = _to_canvas(
                bv["x"] + bv["w"] / 2.0,
                bv["y"] + (bv["h"] / 2.0 if bv["is_dummy"] else 0.0),
            )
            my = (y1 + y2) / 2.0
            y_end = y2 if bv["is_dummy"] else y2 + 4.4
            c.bezier(x1, y1, x1, my, x2, my, x2, y_end)
            if not bv["is_dummy"]:
                _draw_arrowhead(c, x2, y2, "TD", edge_rgb)
            if lbl:
                label_queue.append(((x1 + x2) / 2.0, my, lbl))
        else:
            x1, y1 = _to_canvas(
                bu["x"] + (bu["w"] / 2.0 if bu["is_dummy"] else bu["w"]),
                bu["y"] + bu["h"] / 2.0,
            )
            x2, y2 = _to_canvas(
                bv["x"] + (bv["w"] / 2.0 if bv["is_dummy"] else 0.0),
                bv["y"] + bv["h"] / 2.0,
            )
            mx = (x1 + x2) / 2.0
            x_end = x2 if bv["is_dummy"] else x2 - 4.4
            c.bezier(x1, y1, mx, y1, mx, y2, x_end, y2)
            if not bv["is_dummy"]:
                _draw_arrowhead(c, x2, y2, "LR", edge_rgb)
            if lbl:
                label_queue.append((mx, (y1 + y2) / 2.0, lbl))

    # Draw cycle back-edges subtly dashed
    for u_id, v_id, lbl in layout.get("back_edges", []):
        bu, bv = boxes.get(u_id), boxes.get(v_id)
        if not bu or not bv:
            continue
        c.saveState()
        c.setDash([3.0, 2.5])
        c.setStrokeColorRGB(0.45, 0.56, 0.50)
        c.setLineWidth(0.8)
        x1, y1 = _to_canvas(bu["x"] + bu["w"] / 2.0, bu["y"])
        x2, y2 = _to_canvas(bv["x"] + bv["w"] / 2.0, bv["y"])
        ctrl_y = max(y1, y2) + 12.0
        c.bezier(x1, y1, x1, ctrl_y, x2, ctrl_y, x2, y2 + 4.0)
        c.restoreState()
        _draw_arrowhead(c, x2, y2, "TD", (0.45, 0.56, 0.50))

    # 2. Draw visible node boxes & centered/wrapped labels
    for nd in boxes.values():
        if nd["is_dummy"]:
            continue
        nx, ny_top = _to_canvas(nd["x"], nd["y"])
        bw, bh = nd["w"], nd["h"]
        ny_bot = ny_top - bh
        shp = nd["shape"]
        if shp == "diamond":
            c.setFillColorRGB(0.996, 0.974, 0.916)
            c.setStrokeColorRGB(0.86, 0.68, 0.34)
        elif nd["rank"] == 0 and len({b["rank"] for b in boxes.values()}) > 1:
            c.setFillColorRGB(0.915, 0.962, 0.938)
            c.setStrokeColorRGB(0.55, 0.76, 0.65)
        elif shp == "pill":
            c.setFillColorRGB(0.928, 0.968, 0.948)
            c.setStrokeColorRGB(0.60, 0.79, 0.70)
        else:
            c.setFillColorRGB(0.997, 0.999, 0.998)
            c.setStrokeColorRGB(0.68, 0.82, 0.75)
        c.setLineWidth(0.7)

        if shp == "diamond":
            ch_dx = min(8.0, bw * 0.14)
            mid_y = (ny_top + ny_bot) / 2.0
            pth_h = c.beginPath()
            pth_h.moveTo(nx, mid_y)
            pth_h.lineTo(nx + ch_dx, ny_top)
            pth_h.lineTo(nx + bw - ch_dx, ny_top)
            pth_h.lineTo(nx + bw, mid_y)
            pth_h.lineTo(nx + bw - ch_dx, ny_bot)
            pth_h.lineTo(nx + ch_dx, ny_bot)
            pth_h.close()
            c.drawPath(pth_h, stroke=1, fill=1)
        else:
            rad = min(10.0, bh / 2.0) if shp == "pill" else (5.5 if shp == "round" else 3.5)
            c.roundRect(nx, ny_bot, bw, bh, rad, fill=1, stroke=1)

        text_h = sum(nd["lhs"])
        ty_top = ny_top - max(4.2, (bh - text_h) / 2.0)
        txt_rgb = (0.46, 0.25, 0.04) if shp == "diamond" else (0.08, 0.24, 0.17)
        for nl, nlh in zip(nd["lines"], nd["lhs"]):
            ly = ty_top - (11.2 if _has_tall_span(nl) else 8.6)
            if len(nd["lines"]) == 1:
                lw = _math_spans_total_width(nl, font_name, 8.2)
                tx = nx + max(4.5, (bw - lw) / 2.0)
            else:
                tx = nx + (9.0 if shp == "diamond" else 6.0)
            _draw_spans_line(
                c,
                nl,
                tx,
                ly,
                font_name,
                8.2,
                txt_rgb,
                bold_rgb=(0.40, 0.20, 0.02) if shp == "diamond" else (0.06, 0.26, 0.17),
                math_rgb=(0.08, 0.28, 0.20),
            )
            ty_top -= nlh

    # 3. Draw edge label pills on top of edges cleanly
    for lx, ly, lbl in label_queue:
        lw = pdfmetrics.stringWidth(lbl, font_name, 7.2) + 8.0
        lh = 11.5
        c.setFillColorRGB(1.0, 1.0, 1.0)
        c.setStrokeColorRGB(0.68, 0.82, 0.75)
        c.setLineWidth(0.55)
        c.roundRect(lx - lw / 2.0, ly - lh / 2.0, lw, lh, 3.0, fill=1, stroke=1)
        _draw_spans_line(c, [_InlineSpan(lbl, bold=True)], lx - lw / 2.0 + 4.0, ly - 2.6, font_name, 7.2, (0.094, 0.361, 0.239))

    c.restoreState()


def _build_visual_lookup(section: NotePdfSection) -> tuple[dict[str, _VisualCard], set[str]]:
    """Map normalized section heading -> representative _VisualCard (up to 10 distinct frames per lecture)."""
    img_map: dict[str, bytes] = {}
    for name, raw_b in section.images or []:
        if raw_b and _jpeg_info(raw_b) is not None:
            img_map[name] = raw_b
            img_map[Path(name).name] = raw_b

    if not section.visual_sections or not img_map:
        return {}, set()

    candidates: list[tuple[str, _VisualCard, float]] = []
    seen_files: set[str] = set()
    for vs in section.visual_sections:
        if not isinstance(vs, dict):
            continue
        heading = str(vs.get("heading") or "").strip()
        furl = str(vs.get("frame_url") or "").strip()
        if not heading or not furl:
            continue
        fname = Path(furl.split("?")[0]).name
        if not fname or fname in seen_files:
            continue
        raw_b = img_map.get(fname) or img_map.get(f"keyframes/{fname}")
        if not raw_b:
            continue
        seen_files.add(fname)
        ts = float(vs.get("timestamp") or 0.0)
        caption = _clean_math(str(vs.get("frame_caption") or f"课堂画面 · {vs.get('time') or ''}"))
        bnote = _clean_math(str(vs.get("board_note") or ""))
        is_bb = bool(vs.get("is_blackboard"))
        candidates.append((_norm_heading_key(heading), _VisualCard(caption, bnote, raw_b, is_bb), ts))

    if not candidates:
        return {}, set()

    # Select up to 8 well-spaced keyframes across the lecture, preferring crisp slide captures
    max_cards = 8
    non_bb = [idx for idx, (_, card, _) in enumerate(candidates) if not card.is_blackboard]
    pool_indices = non_bb if len(non_bb) >= 4 else list(range(len(candidates)))
    if len(pool_indices) <= max_cards:
        chosen = [candidates[i] for i in pool_indices]
    else:
        chosen_indices: list[int] = []
        step = len(pool_indices) / float(max_cards)
        for i in range(max_cards):
            pick = pool_indices[min(len(pool_indices) - 1, int(round(i * step)))]
            if pick not in chosen_indices:
                chosen_indices.append(pick)
        chosen = [candidates[i] for i in chosen_indices]

    lookup: dict[str, _VisualCard] = {}
    for hkey, card, _ in chosen:
        if hkey and hkey not in lookup:
            lookup[hkey] = card
    return lookup, seen_files


def _strip_course_decorations(text: str) -> str:
    s = re.sub(r"\([^()]*\)|（[^（）]*）", "", str(text or ""))
    s = re.sub(r"第\s*\d+\s*[节讲]课?", "", s)
    return _norm_heading_key(s)


def _parse_section_to_blocks(section: NotePdfSection) -> list[_DocBlock]:
    blocks: list[_DocBlock] = []
    blocks.append(
        _DocBlock(
            kind="doc_header",
            text=f"{section.course_title} · {section.doc_title}",
            subtitle=section.subtitle,
        )
    )

    # Extract or use provided teacher oral highlights (exam points, announcements, quiz alerts)
    oral_list = list(section.oral_highlights or [])
    if not oral_list and (section.markdown or "").strip() and "本讲核心考点速览" not in (section.markdown or ""):
        try:
            from .course_summary import extract_note_oral_highlights

            oral_list = extract_note_oral_highlights(section.markdown, limit=8)
        except Exception:
            oral_list = []
    if oral_list:
        blocks.append(_DocBlock(kind="oral_box", oral_items=oral_list[:8]))

    img_lookup = {name: data for name, data in (section.images or []) if data}
    for name, data in list(img_lookup.items()):
        img_lookup[Path(name).name] = data

    visual_by_heading, _ = _build_visual_lookup(section)
    embedded_any_image = False

    lines = (section.markdown or "").splitlines()
    i = 0
    n = len(lines)
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    pending_visual: _VisualCard | None = None

    def flush_pending_visual() -> None:
        nonlocal pending_visual, embedded_any_image
        if pending_visual is not None:
            blocks.append(_DocBlock(kind="image_card", visual=pending_visual))
            embedded_any_image = True
            pending_visual = None

    while i < n:
        raw_line = lines[i]
        stripped = raw_line.strip()

        if stripped.startswith("```"):
            if in_code:
                if code_buf:
                    first_non_empty = next(
                        (ln.strip() for ln in code_buf if ln.strip() and not ln.strip().startswith("%%")),
                        "",
                    )
                    looks_like_mermaid = bool(
                        re.match(
                            r"^(?:flowchart|graph|sequenceDiagram|stateDiagram(?:-v2)?|mindmap)\b",
                            first_non_empty,
                            re.I,
                        )
                    )
                    if code_lang in ("diagram", "mermaid", "flowchart") or (not code_lang and looks_like_mermaid):
                        diag_blk = _parse_diagram_block(code_buf, code_lang)
                        if diag_blk is not None:
                            blocks.append(diag_blk)
                    else:
                        blocks.append(_DocBlock(kind="code", text="\n".join(code_buf)))
                    code_buf = []
                in_code = False
                code_lang = ""
            else:
                in_code = True
                code_lang = stripped[3:].strip().lower()
                code_buf = []
            i += 1
            continue

        if in_code:
            code_buf.append(raw_line.rstrip("\r"))
            i += 1
            continue

        if not stripped:
            if blocks and blocks[-1].kind != "spacer":
                blocks.append(_DocBlock(kind="spacer"))
            i += 1
            continue

        # Block display math (`$$...$$`, `\[...\]`, or standalone `\begin{cases|aligned|...}...\end{...}`)
        m_dm_single = re.match(r"^\$\$(.+)\$\$\s*([，。；,;：:]?)$", stripped)
        if m_dm_single and "$$" not in m_dm_single.group(1):
            expr = m_dm_single.group(1).strip()
            if expr:
                blocks.append(_DocBlock(kind="math_block", text=expr, subtitle=m_dm_single.group(2)))
            i += 1
            continue

        m_bm_single = re.match(r"^\\\[(.+)\\\]\s*([，。；,;：:]?)$", stripped)
        if m_bm_single and "\\]" not in m_bm_single.group(1):
            expr = m_bm_single.group(1).strip()
            if expr:
                blocks.append(_DocBlock(kind="math_block", text=expr, subtitle=m_bm_single.group(2)))
            i += 1
            continue

        if stripped.startswith("$$") and "$$" not in stripped[2:]:
            math_lines: list[str] = [stripped[2:].strip()]
            j = i + 1
            found_close = False
            tail_punct = ""
            while j < n and j - i <= 50:
                cand = lines[j].strip()
                m_close = re.match(r"^(.*?)\$\$\s*([，。；,;：:]?)$", cand)
                if m_close:
                    if m_close.group(1).strip():
                        math_lines.append(m_close.group(1).strip())
                    tail_punct = m_close.group(2)
                    found_close = True
                    break
                math_lines.append(cand)
                j += 1
            if found_close:
                expr = "\n".join(ln for ln in math_lines if ln).strip()
                if expr:
                    blocks.append(_DocBlock(kind="math_block", text=expr, subtitle=tail_punct))
                i = j + 1
                continue

        if stripped.startswith("\\[") and "\\]" not in stripped[2:]:
            math_lines = [stripped[2:].strip()]
            j = i + 1
            found_close = False
            tail_punct = ""
            while j < n and j - i <= 50:
                cand = lines[j].strip()
                m_close = re.match(r"^(.*?)\\\]\s*([，。；,;：:]?)$", cand)
                if m_close:
                    if m_close.group(1).strip():
                        math_lines.append(m_close.group(1).strip())
                    tail_punct = m_close.group(2)
                    found_close = True
                    break
                math_lines.append(cand)
                j += 1
            if found_close:
                expr = "\n".join(ln for ln in math_lines if ln).strip()
                if expr:
                    blocks.append(_DocBlock(kind="math_block", text=expr, subtitle=tail_punct))
                i = j + 1
                continue

        m_env_start = re.match(
            r"^\\begin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix|gather\*?)\}",
            stripped,
        )
        if m_env_start:
            env_name = m_env_start.group(1)
            end_tag = f"\\end{{{env_name}}}"
            end_idx = stripped.find(end_tag)
            if end_idx != -1:
                tail = stripped[end_idx + len(end_tag) :].strip()
                if not tail or re.fullmatch(r"[，。；,;：:]", tail):
                    blocks.append(
                        _DocBlock(kind="math_block", text=stripped[: end_idx + len(end_tag)].strip(), subtitle=tail)
                    )
                    i += 1
                    continue
            else:
                env_lines = [stripped]
                j = i + 1
                found_close = False
                tail_punct = ""
                while j < n and j - i <= 50:
                    cand = lines[j].strip()
                    pos_end = cand.find(end_tag)
                    if pos_end != -1:
                        tail = cand[pos_end + len(end_tag) :].strip()
                        if not tail or re.fullmatch(r"[，。；,;：:]", tail):
                            env_lines.append(cand[: pos_end + len(end_tag)].strip())
                            tail_punct = tail
                            found_close = True
                        break
                    env_lines.append(cand)
                    j += 1
                if found_close:
                    blocks.append(_DocBlock(kind="math_block", text="\n".join(env_lines).strip(), subtitle=tail_punct))
                    i = j + 1
                    continue

        # Check for markdown table block
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            table_rows: list[list[str]] = []
            while i < n:
                row_s = lines[i].strip()
                if not (row_s.startswith("|") and row_s.endswith("|")):
                    break
                if re.match(r"^\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?$", row_s):
                    i += 1
                    continue
                inner = row_s[1:-1] if len(row_s) >= 2 else ""
                cells: list[str] = []
                cur_cell: list[str] = []
                in_bt = False
                in_math = False
                ci = 0
                while ci < len(inner):
                    ch = inner[ci]
                    if ch == "\\" and ci + 1 < len(inner) and inner[ci + 1] == "|":
                        cur_cell.append("|")
                        ci += 2
                        continue
                    if ch == "`":
                        in_bt = not in_bt
                        cur_cell.append(ch)
                        ci += 1
                        continue
                    if ch == "$" and not in_bt:
                        in_math = not in_math
                        cur_cell.append(ch)
                        ci += 1
                        continue
                    if ch == "|" and not in_bt and not in_math:
                        cells.append("".join(cur_cell).strip())
                        cur_cell = []
                        ci += 1
                        continue
                    cur_cell.append(ch)
                    ci += 1
                cells.append("".join(cur_cell).strip())
                if any(cells):
                    table_rows.append(cells)
                i += 1
            if table_rows:
                blocks.append(_DocBlock(kind="table", rows=table_rows))
            continue

        # Horizontal rule (`---`, `***`, `___`)
        if re.match(r"^(?:[-*_]\s*){3,}$", stripped):
            if "本讲核心考点速览" not in (section.markdown or ""):
                blocks.append(_DocBlock(kind="hr"))
            i += 1
            continue

        # Standalone Markdown image
        img_match = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
        if img_match:
            caption = _clean_math(img_match.group(1) or "课堂关键画面")
            ref_path = img_match.group(2).strip()
            ref_base = Path(ref_path.split("?")[0]).name
            img_data = img_lookup.get(ref_path) or img_lookup.get(ref_base) or b""
            if img_data and _jpeg_info(img_data) is not None:
                blocks.append(
                    _DocBlock(
                        kind="image_card",
                        visual=_VisualCard(caption=caption, board_note="", image_bytes=img_data),
                    )
                )
                embedded_any_image = True
            i += 1
            continue

        # Headings
        if stripped.startswith("# "):
            flush_pending_visual()
            raw_h = stripped[2:].strip()
            ts_m = _HEADING_TS_RE.search(raw_h)
            badge = ts_m.group(1) if ts_m else ""
            raw_no_ts = _HEADING_TS_RE.sub("", raw_h).strip()
            hdr1 = _clean_math(raw_no_ts)
            hdr1_base = re.sub(r"\s*·\s*精编(?:考点)?讲义\s*$", "", hdr1).strip()
            norm_h1 = _norm_heading_key(hdr1_base or hdr1)
            norm_doc = _norm_heading_key(f"{section.course_title}{section.doc_title}")
            norm_doc_only = _norm_heading_key(section.doc_title)
            stripped_h1 = _strip_course_decorations(hdr1_base or hdr1)
            stripped_doc = _strip_course_decorations(f"{section.course_title}{section.doc_title}")
            if (
                norm_h1 not in (norm_doc, norm_doc_only)
                and stripped_h1 != stripped_doc
                and (hdr1_base or hdr1) not in (section.doc_title, section.course_title)
            ):
                blocks.append(_DocBlock(kind="h1", text=hdr1, raw_text=raw_no_ts, badge=badge))
                pending_visual = visual_by_heading.pop(norm_h1, None)
            i += 1
            continue

        if stripped.startswith("## "):
            flush_pending_visual()
            raw_h = stripped[3:].strip()
            ts_m = _HEADING_TS_RE.search(raw_h)
            badge = ts_m.group(1) if ts_m else ""
            raw_no_ts = _HEADING_TS_RE.sub("", raw_h).strip()
            hdr2 = _clean_math(raw_no_ts)
            norm_h2 = _norm_heading_key(hdr2)
            blocks.append(_DocBlock(kind="h2", text=hdr2, raw_text=raw_no_ts, badge=badge))
            pending_visual = visual_by_heading.pop(norm_h2, None)
            i += 1
            continue

        if stripped.startswith("### ") or stripped.startswith("#### "):
            raw_h = re.sub(r"^#{3,6}\s*", "", stripped).strip()
            ts_m = _HEADING_TS_RE.search(raw_h)
            badge = ts_m.group(1) if ts_m else ""
            raw_no_ts = _HEADING_TS_RE.sub("", raw_h).strip()
            hdr3 = _clean_math(raw_no_ts)
            norm_h3 = _norm_heading_key(hdr3)
            # Avoid duplicate H3 immediately repeating H2 title
            prev_non_spacer = next((b for b in reversed(blocks) if b.kind != "spacer"), None)
            if prev_non_spacer and prev_non_spacer.kind == "h2" and _norm_heading_key(prev_non_spacer.text) == norm_h3:
                if pending_visual is None:
                    pending_visual = visual_by_heading.pop(norm_h3, None)
                i += 1
                continue
            flush_pending_visual()
            blocks.append(_DocBlock(kind="h3", text=hdr3, raw_text=raw_no_ts, badge=badge))
            if pending_visual is None:
                pending_visual = visual_by_heading.pop(norm_h3, None)
            i += 1
            continue

        if stripped.startswith(">"):
            q_seg: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                qline = lines[i].strip().lstrip(">").strip()
                qline = re.sub(r"^\[!logic\]\s*", "**【逻辑主线】** ", qline, flags=re.I)
                qline = re.sub(r"^\[!exam\]\s*", "**【核心考点】** ", qline, flags=re.I)
                qline = re.sub(r"^\[!(?:trap|contrast)\]\s*", "**【易错边界】** ", qline, flags=re.I)
                qline = re.sub(r"^\[!intuition\]\s*", "**【直觉思路】** ", qline, flags=re.I)
                qline = re.sub(r"^\[!(?:theorem|proof|example)\]\s*", "**【核心精讲】** ", qline, flags=re.I)
                is_new_callout = bool(re.match(r"^(?:\*\*\s*)?【[^】]{1,18}】", qline))
                if not qline:
                    if q_seg:
                        blocks.append(_DocBlock(kind="quote", text="\n".join(q_seg)))
                        q_seg = []
                elif q_seg and is_new_callout:
                    blocks.append(_DocBlock(kind="quote", text="\n".join(q_seg)))
                    q_seg = [qline]
                else:
                    q_seg.append(qline)
                i += 1
            if q_seg:
                blocks.append(_DocBlock(kind="quote", text="\n".join(q_seg)))
            continue

        # Unordered bullet list
        m_bul = re.match(r"^(\s*)[-*+]\s+(.*)$", raw_line)
        if m_bul:
            leading = len(m_bul.group(1).expandtabs(2))
            level = min(3, leading // 2)
            indent = 12.0 + level * 12.0
            marker = "•" if level == 0 else "◦" if level == 1 else "▪"
            blocks.append(
                _DocBlock(kind="list_item", text=m_bul.group(2).strip(), bullet_marker=marker, indent=indent)
            )
            i += 1
            continue

        # Ordered numbered list (`1. ...`, `2) ...`)
        m_num = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", raw_line)
        if m_num:
            leading = len(m_num.group(1).expandtabs(2))
            level = min(3, leading // 2)
            indent = 14.0 + level * 12.0
            blocks.append(
                _DocBlock(
                    kind="list_item",
                    text=m_num.group(3).strip(),
                    bullet_marker=m_num.group(2),
                    indent=indent,
                )
            )
            i += 1
            continue

        # Regular paragraph
        blocks.append(_DocBlock(kind="paragraph", text=stripped))
        if pending_visual is not None:
            flush_pending_visual()
        i += 1

    flush_pending_visual()

    # Fallback: if no section-matched visuals were embedded but keyframes exist, select up to 3 spaced frames
    if not embedded_any_image and section.images:
        unique_imgs: list[tuple[str, bytes]] = []
        seen_names: set[str] = set()
        for name, raw_b in section.images:
            base = Path(name).name
            if base in seen_names or not raw_b or _jpeg_info(raw_b) is None:
                continue
            seen_names.add(base)
            unique_imgs.append((base, raw_b))
        if unique_imgs:
            # Pick frames from middle/across the list rather than the first 4 title frames
            if len(unique_imgs) <= 3:
                picks = unique_imgs
            else:
                idxs = [len(unique_imgs) // 4, len(unique_imgs) // 2, (len(unique_imgs) * 3) // 4]
                picks = [unique_imgs[k] for k in dict.fromkeys(idxs)]
            blocks.append(_DocBlock(kind="h2", text="课堂板书与课件关键画面精选"))
            for base, raw_b in picks:
                ts_m = re.search(r"(\d{2,3})m(\d{2})s", base)
                ts_label = f"{int(ts_m.group(1)):02d}:{ts_m.group(2)}" if ts_m else base
                blocks.append(
                    _DocBlock(
                        kind="image_card",
                        visual=_VisualCard(
                            caption=f"课堂关键画面 · {ts_label}",
                            board_note="",
                            image_bytes=raw_b,
                        ),
                    )
                )

    return blocks


def _draw_page_chrome(
    c: canvas.Canvas,
    font_name: str,
    page_num: int,
    total_pages: int,
    doc_header_right: str,
) -> None:
    """Draw background watermark, top promotional header bar, and bottom page footer."""
    c.saveState()
    # Subtle diagonal brand watermark in background (ultra-light neutral pine-gray)
    c.setFillColorRGB(0.964, 0.971, 0.968)
    c.setFont("Helvetica-Bold", 15.0)
    for x_pos, y_pos in ((135.0, 275.0), (135.0, 555.0)):
        c.saveState()
        c.translate(x_pos, y_pos)
        c.rotate(27.0)
        c.drawString(0, 0, "PKU All in One  -  Open Source")
        c.restoreState()

    # Top promotional banner (Academic Pine harmony)
    c.setFillColorRGB(0.955, 0.974, 0.965)
    c.rect(44.0, 800.0, 507.0, 20.0, fill=1, stroke=0)
    c.setStrokeColorRGB(0.094, 0.361, 0.239)
    c.setLineWidth(0.75)
    c.line(44.0, 800.0, 551.0, 800.0)

    _draw_spans_line(
        c,
        [_InlineSpan(WATERMARK_PROMO_TAGLINE, bold=True)],
        50.0,
        806.2,
        font_name,
        8.2,
        (0.075, 0.290, 0.192),
    )
    c.setFont("Helvetica-Bold", 8.2)
    c.setFillColorRGB(0.094, 0.361, 0.239)
    c.drawRightString(545.0, 806.2, WATERMARK_SITE_URL)

    # Bottom footer bar
    c.setStrokeColorRGB(0.83, 0.87, 0.85)
    c.setLineWidth(0.55)
    c.line(44.0, 44.0, 551.0, 44.0)

    _draw_spans_line(
        c,
        [_InlineSpan(f"{WATERMARK_FOOTER_LEFT}{WATERMARK_SITE_URL}")],
        44.0,
        31.0,
        font_name,
        7.8,
        (0.41, 0.45, 0.43),
    )
    c.setFont(font_name, 7.8)
    c.setFillColorRGB(0.41, 0.45, 0.43)
    c.drawRightString(551.0, 31.0, f"第 {page_num} / {total_pages} 页")
    c.restoreState()


def _math_spans_total_width(spans: list[_InlineSpan], font_name: str, font_size: float) -> float:
    return sum(_span_width(sp, font_name, font_size) for sp in spans)


def _math_row_extents(spans_list: list[list[_InlineSpan]], fsz: float) -> tuple[float, float]:
    """Return (above_baseline, below_baseline) vertical extents in points for one math row."""
    if any(_has_tall_span(sp_row) for sp_row in spans_list):
        return (fsz * 1.22 + 3.2, fsz * 0.72 + 2.8)
    has_sup_or_bar = any(sp.overline or sp.script == "sup" for sp_row in spans_list for sp in sp_row)
    above = fsz * (1.04 if has_sup_or_bar else 0.94) + 2.0
    below = fsz * 0.38 + 2.0
    return (above, below)


def _strip_array_col_spec(body: str) -> str:
    s = body.lstrip()
    if s.startswith("{"):
        depth = 0
        for idx, ch in enumerate(s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[idx + 1 :].lstrip()
    return body


def _draw_math_delimiter(
    c: canvas.Canvas,
    delim: str,
    x: float,
    top_y: float,
    bot_y: float,
    color: tuple[float, float, float],
) -> None:
    """Draw tall vector math delimiter (`{`, `(`, `)`, `[`, `]`, `|`) spanning [bot_y, top_y]."""
    c.saveState()
    c.setStrokeColorRGB(*color)
    c.setLineWidth(0.95)
    c.setLineCap(1)
    c.setLineJoin(1)
    mid_y = (top_y + bot_y) / 2.0
    half_h = max(4.0, (top_y - bot_y) / 2.0)
    if delim == "{":
        rx = x + 7.5
        cx = x + 4.4
        lx = x + 1.0
        p = c.beginPath()
        p.moveTo(rx, top_y)
        p.curveTo(cx, top_y, cx, top_y - half_h * 0.35, cx, mid_y + 2.8)
        p.curveTo(cx, mid_y + 0.5, lx, mid_y, lx, mid_y)
        p.curveTo(lx, mid_y, cx, mid_y - 0.5, cx, mid_y - 2.8)
        p.curveTo(cx, bot_y + half_h * 0.35, cx, bot_y, rx, bot_y)
        c.drawPath(p, stroke=1, fill=0)
    elif delim == "(":
        p = c.beginPath()
        p.moveTo(x + 6.5, top_y)
        p.curveTo(x + 1.4, top_y - half_h * 0.45, x + 1.4, bot_y + half_h * 0.45, x + 6.5, bot_y)
        c.drawPath(p, stroke=1, fill=0)
    elif delim == ")":
        p = c.beginPath()
        p.moveTo(x + 1.5, top_y)
        p.curveTo(x + 6.6, top_y - half_h * 0.45, x + 6.6, bot_y + half_h * 0.45, x + 1.5, bot_y)
        c.drawPath(p, stroke=1, fill=0)
    elif delim == "[":
        p = c.beginPath()
        p.moveTo(x + 5.8, top_y)
        p.lineTo(x + 2.0, top_y)
        p.lineTo(x + 2.0, bot_y)
        p.lineTo(x + 5.8, bot_y)
        c.drawPath(p, stroke=1, fill=0)
    elif delim == "]":
        p = c.beginPath()
        p.moveTo(x + 1.2, top_y)
        p.lineTo(x + 5.0, top_y)
        p.lineTo(x + 5.0, bot_y)
        p.lineTo(x + 1.2, bot_y)
        c.drawPath(p, stroke=1, fill=0)
    elif delim == "|":
        c.line(x + 2.6, top_y, x + 2.6, bot_y)
    c.restoreState()


def _prepare_math_block(
    expr: str,
    subtitle: str,
    font_name: str,
    avail_w: float,
) -> dict:
    """Parse and measure a display math block (environments, multi-row systems, or wrapped formula)."""
    clean_expr = re.sub(r"\\(?:nonumber|notag|label\{[^}]*\})", "", expr).strip()
    m_env = re.search(
        r"\\begin\{(cases|aligned|align\*?|array|matrix|pmatrix|bmatrix|vmatrix|gather\*?)\}([\s\S]*?)\\end\{\1\}",
        clean_expr,
    )
    if m_env:
        env = m_env.group(1)
        prefix_raw = clean_expr[: m_env.start()].strip()
        body_raw = m_env.group(2).strip()
        if env == "array":
            body_raw = _strip_array_col_spec(body_raw)
        suffix_raw = (clean_expr[m_env.end() :].strip() + subtitle).strip()
        raw_rows = re.split(r"\\\\(?:\[[^\]]*\])?", body_raw)
        rows_cells_raw = [re.split(r"(?<!\\)&", r.strip()) for r in raw_rows if r.strip()]
        if not rows_cells_raw:
            rows_cells_raw = [[""]]

        delim_map = {
            "cases": ("{", "", 12.0, 0.0),
            "pmatrix": ("(", ")", 10.5, 10.5),
            "bmatrix": ("[", "]", 9.0, 9.0),
            "vmatrix": ("|", "|", 7.5, 7.5),
        }
        ldelim, rdelim, ldelim_w, rdelim_w = delim_map.get(env, ("", "", 0.0, 0.0))

        def _measure_env(fsz: float) -> tuple[list[_InlineSpan], float, list[_InlineSpan], float, list[list[list[_InlineSpan]]], list[float], float]:
            p_sp = _parse_math_spans(prefix_raw) if prefix_raw else []
            s_sp = _parse_math_spans(suffix_raw) if suffix_raw else []
            pw = (_math_spans_total_width(p_sp, font_name, fsz) + 6.0) if p_sp else 0.0
            sw = (_math_spans_total_width(s_sp, font_name, fsz) + 4.0) if s_sp else 0.0
            ncols = max(len(r) for r in rows_cells_raw)
            col_ws = [0.0] * ncols
            parsed_rows: list[list[list[_InlineSpan]]] = []
            for r in rows_cells_raw:
                prow: list[list[_InlineSpan]] = []
                for c_idx in range(ncols):
                    cell_s = r[c_idx].strip() if c_idx < len(r) else ""
                    csp = _parse_math_spans(cell_s) if cell_s else []
                    cw = _math_spans_total_width(csp, font_name, fsz)
                    col_ws[c_idx] = max(col_ws[c_idx], cw)
                    prow.append(csp)
                parsed_rows.append(prow)
            cgap = 12.0 if env in ("cases", "aligned", "align", "align*") else 10.0
            grid_w = sum(col_ws) + max(0, ncols - 1) * cgap
            tot_w = pw + ldelim_w + grid_w + rdelim_w + sw
            return p_sp, pw, s_sp, sw, parsed_rows, col_ws, tot_w

        fsz = 10.0
        p_sp, pw, s_sp, sw, parsed_rows, col_ws, tot_w = _measure_env(fsz)
        if tot_w > avail_w and tot_w > 0:
            fsz = max(7.8, 10.0 * (avail_w / tot_w))
            p_sp, pw, s_sp, sw, parsed_rows, col_ws, tot_w = _measure_env(fsz)

        cgap = 12.0 if env in ("cases", "aligned", "align", "align*") else 10.0
        extents = [_math_row_extents(r, fsz) for r in parsed_rows]
        row_gap = 4.8
        pad_y = 8.0
        box_h = pad_y * 2.0 + sum(a + b for a, b in extents) + max(0, len(extents) - 1) * row_gap
        return {
            "mode": "env",
            "env": env,
            "fsz": fsz,
            "prefix_spans": p_sp,
            "prefix_w": pw,
            "suffix_spans": s_sp,
            "suffix_w": sw,
            "rows": parsed_rows,
            "col_widths": col_ws,
            "col_gap": cgap,
            "ldelim": ldelim,
            "rdelim": rdelim,
            "ldelim_w": ldelim_w,
            "rdelim_w": rdelim_w,
            "total_w": tot_w,
            "extents": extents,
            "row_gap": row_gap,
            "pad_y": pad_y,
            "box_h": box_h,
        }

    # Non-environment block math: explicit `\\` rows or single wrapped formula
    tail = subtitle.strip()
    if "\\\\" in clean_expr:
        raw_lines = [ln.strip() for ln in re.split(r"\\\\(?:\[[^\]]*\])?", clean_expr) if ln.strip()]
        if tail and raw_lines:
            raw_lines[-1] = f"{raw_lines[-1]} {tail}"
        fsz = 10.0
        parsed_lines = [_parse_math_spans(re.sub(r"\s*\n\s*", " ", ln)) for ln in raw_lines]
        max_lw = max((_math_spans_total_width(ln, font_name, fsz) for ln in parsed_lines), default=0.0)
        if max_lw > avail_w and max_lw > 0:
            fsz = max(8.2, 10.0 * (avail_w / max_lw))
    else:
        one_line = re.sub(r"\s*\n\s*", " ", clean_expr).strip()
        if tail:
            one_line = f"{one_line} {tail}"
        spans = _parse_math_spans(one_line)
        fsz = 10.2
        w = _math_spans_total_width(spans, font_name, fsz)
        if w <= avail_w:
            parsed_lines = [spans]
        elif w * (8.5 / 10.2) <= avail_w:
            fsz = max(8.5, 10.2 * (avail_w / w))
            parsed_lines = [spans]
        else:
            fsz = 8.8
            parsed_lines = _wrap_spans(spans, font_name, fsz, avail_w)

    extents = [_math_row_extents([ln], fsz) for ln in parsed_lines]
    row_gap = 4.2
    pad_y = 8.0
    box_h = pad_y * 2.0 + sum(a + b for a, b in extents) + max(0, len(extents) - 1) * row_gap
    return {
        "mode": "lines",
        "fsz": fsz,
        "lines": parsed_lines,
        "extents": extents,
        "row_gap": row_gap,
        "pad_y": pad_y,
        "box_h": box_h,
    }


def _draw_math_block_card(
    c: canvas.Canvas,
    layout: dict,
    left_x: float,
    content_w: float,
    top_y: float,
    font_name: str,
) -> None:
    """Render a prepared display math card with centered formulas / matrices / cases."""
    box_h = layout["box_h"]
    fsz = layout["fsz"]
    extents = layout["extents"]
    row_gap = layout["row_gap"]
    pad_y = layout["pad_y"]
    base_col = (0.08, 0.22, 0.17)
    bold_col = (0.05, 0.15, 0.11)
    math_col = (0.08, 0.24, 0.18)

    c.saveState()
    c.setFillColorRGB(0.976, 0.986, 0.982)
    c.setStrokeColorRGB(0.80, 0.87, 0.84)
    c.setLineWidth(0.55)
    c.roundRect(left_x, top_y - box_h, content_w, box_h, 3.5, fill=1, stroke=1)
    c.setFillColorRGB(0.09, 0.34, 0.25)
    c.roundRect(left_x, top_y - box_h, 2.8, box_h, 1.5, fill=1, stroke=0)

    baselines: list[float] = []
    cy = top_y - pad_y - extents[0][0]
    baselines.append(cy)
    for idx_r in range(1, len(extents)):
        cy -= extents[idx_r - 1][1] + row_gap + extents[idx_r][0]
        baselines.append(cy)

    if layout["mode"] == "env":
        tot_w = layout["total_w"]
        start_x = left_x + max(12.0, (content_w - tot_w) / 2.0)
        grid_top_y = baselines[0] + extents[0][0] - 1.5
        grid_bot_y = baselines[-1] - extents[-1][1] + 1.5
        mid_baseline = (grid_top_y + grid_bot_y) / 2.0 - fsz * 0.30

        cur_x = start_x
        if layout["prefix_spans"]:
            _draw_spans_line(
                c,
                layout["prefix_spans"],
                cur_x,
                mid_baseline,
                font_name,
                fsz,
                base_col,
                bold_rgb=bold_col,
                math_rgb=math_col,
            )
            cur_x += layout["prefix_w"]

        if layout["ldelim"]:
            _draw_math_delimiter(c, layout["ldelim"], cur_x, grid_top_y, grid_bot_y, math_col)
            cur_x += layout["ldelim_w"]

        grid_x = cur_x
        col_ws = layout["col_widths"]
        cgap = layout["col_gap"]
        env = layout["env"]
        for r_idx, row_cells in enumerate(layout["rows"]):
            ry = baselines[r_idx]
            cx = grid_x
            for c_idx, cell_spans in enumerate(row_cells):
                cw_col = col_ws[c_idx]
                cell_w = _math_spans_total_width(cell_spans, font_name, fsz)
                if env in ("aligned", "align", "align*") and c_idx == 0 and len(col_ws) >= 2:
                    draw_x = cx + (cw_col - cell_w)
                elif env in ("matrix", "pmatrix", "bmatrix", "vmatrix", "array", "gather", "gather*"):
                    draw_x = cx + (cw_col - cell_w) / 2.0
                else:
                    draw_x = cx
                _draw_spans_line(
                    c,
                    cell_spans,
                    draw_x,
                    ry,
                    font_name,
                    fsz,
                    base_col,
                    bold_rgb=bold_col,
                    math_rgb=math_col,
                )
                cx += cw_col + cgap

        grid_w = sum(col_ws) + max(0, len(col_ws) - 1) * cgap
        cur_x = grid_x + grid_w
        if layout["rdelim"]:
            _draw_math_delimiter(c, layout["rdelim"], cur_x + 2.5, grid_top_y, grid_bot_y, math_col)
            cur_x += layout["rdelim_w"]

        if layout["suffix_spans"]:
            _draw_spans_line(
                c,
                layout["suffix_spans"],
                cur_x + 4.0,
                mid_baseline,
                font_name,
                fsz,
                base_col,
                bold_rgb=bold_col,
                math_rgb=math_col,
            )
    else:
        for l_idx, line_spans in enumerate(layout["lines"]):
            lw = _math_spans_total_width(line_spans, font_name, fsz)
            lx = left_x + max(12.0, (content_w - lw) / 2.0)
            _draw_spans_line(
                c,
                line_spans,
                lx,
                baselines[l_idx],
                font_name,
                fsz,
                base_col,
                bold_rgb=bold_col,
                math_rgb=math_col,
            )
    c.restoreState()


def build_watermarked_notes_pdf(
    sections: Iterable[NotePdfSection],
    *,
    document_title: str = "PKU All in One 课堂笔记",
) -> bytes:
    """Compile one or more NotePdfSection objects into a crisp, embedded-font PDF 1.4 binary."""
    global _PDF_DOC_CACHE_BYTES
    rl_config.useA85 = 0
    font_name = _ensure_cjk_font()
    section_list = list(sections)
    if not section_list:
        section_list = [
            NotePdfSection(
                course_title="PKU All in One",
                doc_title="课堂笔记",
                markdown="暂无笔记内容。",
            )
        ]

    doc_cache_key = _pdf_document_cache_key(section_list, document_title)
    with _PDF_DOC_CACHE_LOCK:
        cached_pdf = _PDF_DOC_CACHE.get(doc_cache_key)
        if cached_pdf is not None:
            _PDF_DOC_CACHE.move_to_end(doc_cache_key)
            return cached_pdf

    all_blocks: list[_DocBlock] = []
    for idx, sec in enumerate(section_list):
        if idx > 0:
            all_blocks.append(_DocBlock(kind="pagebreak"))
        all_blocks.extend(_parse_section_to_blocks(sec))

    all_blocks.append(_DocBlock(kind="spacer"))
    all_blocks.append(_DocBlock(kind="promo_card"))

    left_x = 46.0
    right_x = 549.0
    content_w = right_x - left_x
    top_y = 782.0
    bottom_y = 56.0

    # Record drawing callbacks per page so we know `total_pages` before drawing page footers
    pages_ops: list[list] = []
    cur_ops: list = []
    y = top_y

    def new_page() -> None:
        nonlocal cur_ops, y
        pages_ops.append(cur_ops)
        cur_ops = []
        y = top_y

    def ensure_space(needed_h: float) -> None:
        nonlocal y
        if y - needed_h < bottom_y and cur_ops:
            new_page()

    for blk in all_blocks:
        if blk.kind == "pagebreak":
            if cur_ops:
                new_page()
            continue

        if blk.kind == "spacer":
            y -= 5.5
            if y < bottom_y and cur_ops:
                new_page()
            continue

        if blk.kind == "hr":
            ensure_space(10.0)
            y -= 3.0
            hy_hr = y

            def _draw_hr(c: canvas.Canvas, line_y: float = hy_hr) -> None:
                c.saveState()
                c.setStrokeColorRGB(0.84, 0.85, 0.88)
                c.setLineWidth(0.55)
                c.line(left_x, line_y, right_x, line_y)
                c.restoreState()

            cur_ops.append(_draw_hr)
            y -= 5.0
            continue

        if blk.kind == "math_block":
            math_layout = _prepare_math_block(blk.text, blk.subtitle, font_name, content_w - 28.0)
            ensure_space(math_layout["box_h"] + 4.0)
            my = y

            def _draw_math_blk(c: canvas.Canvas, top_my: float = my, ml: dict = math_layout) -> None:
                _draw_math_block_card(c, ml, left_x, content_w, top_my, font_name)

            cur_ops.append(_draw_math_blk)
            y -= math_layout["box_h"] + 5.0
            continue

        if blk.kind == "doc_header":
            title_spans = _parse_inline_spans(blk.text, default_bold=True)
            title_lines = _wrap_spans(title_spans, font_name, 14.5, content_w - 18.0)
            sub_lines: list[list[_InlineSpan]] = []
            if blk.subtitle:
                sub_lines = _wrap_spans(_parse_inline_spans(blk.subtitle), font_name, 8.8, content_w - 18.0)
            box_h = len(title_lines) * 20.0 + (len(sub_lines) * 13.5 + 4.0 if sub_lines else 0.0) + 16.0
            ensure_space(box_h + 8.0)
            top_box_y = y

            def _draw_header(
                c: canvas.Canvas,
                by: float = top_box_y,
                bh: float = box_h,
                tlines: list = title_lines,
                slines: list = sub_lines,
            ) -> None:
                c.saveState()
                c.setFillColorRGB(0.962, 0.980, 0.971)
                c.setStrokeColorRGB(0.76, 0.85, 0.80)
                c.setLineWidth(0.6)
                c.roundRect(left_x, by - bh, content_w, bh, 4.5, fill=1, stroke=1)
                c.setFillColorRGB(0.094, 0.361, 0.239)
                c.roundRect(left_x, by - bh, 4.5, bh, 2.0, fill=1, stroke=0)
                cy = by - 19.0
                for tl in tlines:
                    _draw_spans_line(c, tl, left_x + 13.0, cy, font_name, 14.5, (0.065, 0.255, 0.168))
                    cy -= 20.0
                if slines:
                    cy -= 1.0
                    for sl in slines:
                        _draw_spans_line(c, sl, left_x + 13.0, cy, font_name, 8.8, (0.36, 0.42, 0.40))
                        cy -= 13.5
                c.restoreState()

            cur_ops.append(_draw_header)
            y -= box_h + 10.0
            continue

        if blk.kind == "oral_box" and blk.oral_items:
            prepared_oral: list[list[list[_InlineSpan]]] = []
            oral_heights: list[list[float]] = []
            total_oral_h = 0.0
            for item in blk.oral_items:
                if isinstance(item, dict):
                    cat = str(item.get("category") or "课堂重点").strip()
                    txt = str(item.get("text") or "").strip()
                    spans = [_InlineSpan(f"【{cat}】", bold=True)] + _parse_inline_spans(txt)
                else:
                    txt = str(item or "").strip()
                    if re.search(r"期中|期末|考试|考场|题型|判断改错|填空|名词解释|问答", txt):
                        tag = "【考试与题型提醒】"
                    elif re.search(r"作业|截止|DDL|提交|小测|签到", txt):
                        tag = "【课程通知与要求】"
                    else:
                        tag = "【老师口述考点】"
                    spans = [_InlineSpan(tag, bold=True)] + _parse_inline_spans(txt)
                wrapped = _wrap_spans(spans, font_name, 9.2, content_w - 28.0)
                lhs = [_spans_line_height(wl, 13.8, extra=8.0) for wl in wrapped]
                prepared_oral.append(wrapped)
                oral_heights.append(lhs)
                total_oral_h += sum(lhs)

            box_h = 24.0 + total_oral_h + len(prepared_oral) * 2.5 + 8.0
            ensure_space(min(box_h, 240.0))
            box_top = y

            def _draw_oral(
                c: canvas.Canvas,
                by: float = box_top,
                bh: float = box_h,
                items_wrapped: list = prepared_oral,
                items_lhs: list = oral_heights,
            ) -> None:
                c.saveState()
                c.setFillColorRGB(1.0, 0.976, 0.955)
                c.setStrokeColorRGB(0.90, 0.68, 0.52)
                c.setLineWidth(0.7)
                c.roundRect(left_x, by - bh, content_w, bh, 4.5, fill=1, stroke=1)
                c.setFillColorRGB(0.72, 0.14, 0.10)
                c.roundRect(left_x, by - bh, 4.0, bh, 2.0, fill=1, stroke=0)
                _draw_spans_line(
                    c,
                    [_InlineSpan("老师口述重点与通知提示（考试范围 / 题型提醒 / 课堂强调）", bold=True)],
                    left_x + 12.0,
                    by - 16.0,
                    font_name,
                    10.0,
                    (0.65, 0.10, 0.08),
                )
                cy_top = by - 21.0
                for wlines, lhs in zip(items_wrapped, items_lhs):
                    first_off = 13.5 if _has_tall_span(wlines[0]) else 10.0
                    c.setFillColorRGB(0.72, 0.14, 0.10)
                    c.circle(left_x + 14.5, cy_top - first_off + 3.0, 1.8, fill=1, stroke=0)
                    for wl, l_h in zip(wlines, lhs):
                        tall = _has_tall_span(wl)
                        ly = cy_top - (13.5 if tall else 10.0)
                        _draw_spans_line(
                            c,
                            wl,
                            left_x + 21.0,
                            ly,
                            font_name,
                            9.2,
                            (0.18, 0.16, 0.15),
                            bold_rgb=(0.62, 0.08, 0.08),
                            math_rgb=(0.10, 0.26, 0.20),
                        )
                        cy_top -= l_h
                    cy_top -= 2.5
                c.restoreState()

            cur_ops.append(_draw_oral)
            y -= box_h + 10.0
            continue

        if blk.kind in ("h1", "h2", "h3"):
            sz = 12.8 if blk.kind == "h1" else 11.4 if blk.kind == "h2" else 10.2
            lh = 18.5 if blk.kind == "h1" else 16.8 if blk.kind == "h2" else 15.0
            badge_w = (pdfmetrics.stringWidth(blk.badge, font_name, 8.0) + 14.0) if blk.badge else 0.0
            raw_heading = re.sub(
                r"^[\s\u200d\ufe0f\U00010000-\U0010ffff⚠🎯🔍🧠📌✨💡]+",
                "",
                (blk.raw_text or blk.text).strip(),
            ).strip()
            chap_pill = ""
            pill_rgb: tuple[float, float, float] = (0.094, 0.361, 0.239)
            heading_body = raw_heading
            if blk.kind == "h2":
                cn_nums = {
                    "一": "01", "二": "02", "三": "03", "四": "04", "五": "05",
                    "六": "06", "七": "07", "八": "08", "九": "09", "十": "10",
                    "十一": "11", "十二": "12", "十三": "13", "十四": "14", "十五": "15",
                    "十六": "16", "十七": "17", "十八": "18", "十九": "19", "二十": "20",
                }
                exam_m = re.match(r"^(?:\[注意\]\s*)?(考前必看.*)$", heading_body)
                if heading_body.startswith("本讲核心考点速览"):
                    chap_pill = "导读"
                elif exam_m:
                    chap_pill = "自检"
                    pill_rgb = (0.851, 0.467, 0.024)
                    heading_body = exam_m.group(1).strip()
                else:
                    ch_m = re.match(
                        r"^(?:([一二三四五六七八九十]+)\s*[、.．]\s*|(\d{1,2})\s*[、.．]\s+|第\s*([一二三四五六七八九十\d]{1,3})\s*[章讲节部分][：:\s、]*)(.+)$",
                        raw_heading,
                    )
                    if ch_m:
                        raw_num = (ch_m.group(1) or ch_m.group(2) or ch_m.group(3) or "").strip()
                        rest_body = (ch_m.group(4) or "").strip()
                        if raw_num and rest_body:
                            if raw_num.isdigit():
                                chap_pill = f"{int(raw_num):02d}"
                            else:
                                chap_pill = cn_nums.get(raw_num, raw_num)
                            heading_body = rest_body
            pill_w = (
                max(21.0, pdfmetrics.stringWidth(chap_pill, font_name, 8.4) + 10.0)
                if chap_pill
                else 0.0
            )
            left_pad_h = (pill_w + 7.5) if chap_pill else (7.5 if blk.kind == "h2" else 0.0)
            h_spans = _parse_inline_spans(heading_body, default_bold=True)
            h_lines = _wrap_spans(h_spans, font_name, sz, max(80.0, content_w - badge_w - left_pad_h - 6.0))
            h_lhs = [_spans_line_height(hl, lh, extra=7.5) for hl in h_lines]
            block_h = sum(h_lhs) + (10.0 if blk.kind in ("h1", "h2") else 4.0)
            # Keep headings from being orphaned at the bottom of a page
            ensure_space(block_h + 28.0)
            y -= 5.0 if blk.kind in ("h1", "h2") else 2.5
            hy = y
            b_kind = blk.kind
            b_badge = blk.badge
            b_pill = chap_pill
            b_pill_w = pill_w
            b_pill_rgb = pill_rgb
            b_left_pad = left_pad_h

            def _draw_heading(
                c: canvas.Canvas,
                top_hy: float = hy,
                lines_h: list = h_lines,
                lhs_h: list = h_lhs,
                kind_h: str = b_kind,
                badge_str: str = b_badge,
                pill_str: str = b_pill,
                p_w: float = b_pill_w,
                p_rgb: tuple[float, float, float] = b_pill_rgb,
                x_pad: float = b_left_pad,
                fsize: float = sz,
            ) -> None:
                c.saveState()
                color = (
                    (0.075, 0.290, 0.192)
                    if kind_h in ("h1", "h2")
                    else (0.12, 0.20, 0.22)
                )
                first_extra = 3.5 if _has_tall_span(lines_h[0]) else 0.0
                cy = top_hy - fsize - first_extra
                if kind_h == "h2":
                    if pill_str:
                        c.setFillColorRGB(*p_rgb)
                        c.roundRect(left_x, cy - 2.6, p_w, fsize + 2.8, 3.2, fill=1, stroke=0)
                        c.setFillColorRGB(1.0, 1.0, 1.0)
                        c.setFont(font_name, 8.4)
                        c.drawCentredString(left_x + p_w / 2.0, cy + 0.8, pill_str)
                    else:
                        c.setFillColorRGB(0.094, 0.361, 0.239)
                        c.roundRect(left_x, cy - 2.0, 3.2, fsize + 2.5, 1.2, fill=1, stroke=0)
                x_start = left_x + x_pad
                rule_y = cy
                for idx_l, (hl, line_h) in enumerate(zip(lines_h, lhs_h)):
                    end_x = _draw_spans_line(c, hl, x_start, cy, font_name, fsize, color, math_rgb=color)
                    if idx_l == 0 and badge_str:
                        bw = pdfmetrics.stringWidth(badge_str, font_name, 7.8) + 8.0
                        bx = min(right_x - bw, end_x + 8.0)
                        c.setFillColorRGB(0.93, 0.96, 0.945)
                        c.setStrokeColorRGB(0.74, 0.84, 0.79)
                        c.setLineWidth(0.4)
                        c.roundRect(bx, cy - 1.5, bw, 11.5, 2.5, fill=1, stroke=1)
                        _draw_spans_line(
                            c,
                            [_InlineSpan(badge_str)],
                            bx + 4.0,
                            cy + 1.0,
                            font_name,
                            7.8,
                            (0.14, 0.35, 0.25),
                        )
                    rule_y = cy
                    cy -= line_h
                if kind_h == "h1":
                    c.setStrokeColorRGB(0.75, 0.85, 0.80)
                    c.setLineWidth(0.7)
                    c.line(left_x, rule_y - 4.8, right_x, rule_y - 4.8)
                elif kind_h == "h2":
                    c.setStrokeColorRGB(0.83, 0.89, 0.86)
                    c.setLineWidth(0.5)
                    c.line(left_x, rule_y - 4.5, right_x, rule_y - 4.5)
                c.restoreState()

            cur_ops.append(_draw_heading)
            y -= sum(h_lhs) + (6.5 if blk.kind in ("h1", "h2") else 2.5)
            continue

        if blk.kind == "paragraph":
            spans = _parse_inline_spans(blk.text)
            wlines = _wrap_spans(spans, font_name, 9.6, content_w)
            for wl in wlines:
                lh_line = _spans_line_height(wl, 14.6, extra=8.5)
                ensure_space(lh_line)
                ly = y - (14.2 if _has_tall_span(wl) else 10.0)
                cur_ops.append(
                    lambda c, line_spans=wl, py=ly: _draw_spans_line(
                        c,
                        line_spans,
                        left_x,
                        py,
                        font_name,
                        9.6,
                        (0.13, 0.15, 0.18),
                        bold_rgb=(0.06, 0.08, 0.12),
                        math_rgb=(0.10, 0.26, 0.20),
                    )
                )
                y -= lh_line
            y -= 2.0
            continue

        if blk.kind == "list_item":
            marker = blk.bullet_marker or "•"
            indent = blk.indent
            marker_w = max(11.0, pdfmetrics.stringWidth(marker, font_name, 9.5) + 4.0)
            text_x = left_x + indent + marker_w
            spans = _parse_inline_spans(blk.text)
            wlines = _wrap_spans(spans, font_name, 9.5, right_x - text_x)
            for idx_l, wl in enumerate(wlines):
                lh_line = _spans_line_height(wl, 14.4, extra=8.5)
                ensure_space(lh_line)
                ly = y - (14.0 if _has_tall_span(wl) else 10.0)
                is_first = idx_l == 0
                cur_ops.append(
                    lambda c, line_spans=wl, py=ly, first=is_first, mx=left_x + indent, tx=text_x, mk=marker: (
                        _draw_spans_line(
                            c,
                            [_InlineSpan(mk, bold=True)],
                            mx,
                            py,
                            font_name,
                            9.2,
                            (0.52, 0.06, 0.10),
                        )
                        if first
                        else None,
                        _draw_spans_line(
                            c,
                            line_spans,
                            tx,
                            py,
                            font_name,
                            9.5,
                            (0.13, 0.15, 0.18),
                            bold_rgb=(0.06, 0.08, 0.12),
                            math_rgb=(0.10, 0.26, 0.20),
                        ),
                    )
                )
                y -= lh_line
            y -= 1.2
            continue

        if blk.kind == "quote":
            spans = _parse_inline_spans(blk.text)
            wlines = _wrap_spans(spans, font_name, 9.2, content_w - 26.0)
            q_lhs = [_spans_line_height(ql, 13.8, extra=8.0) for ql in wlines]
            box_h = sum(q_lhs) + 12.0
            ensure_space(box_h)
            qy = y

            q_head_probe = re.sub(r"[*_`【】\[\]\s]+", " ", (blk.text or "")[:36])
            callout_themes = (
                (
                    ("考点", "高频", "必考", "核心要点", "重点", "速览"),
                    (0.996, 0.976, 0.918),
                    (0.955, 0.875, 0.675),
                    (0.851, 0.467, 0.024),
                    (0.675, 0.325, 0.020),
                    (0.240, 0.200, 0.140),
                ),
                (
                    ("直觉", "核心思想", "类比", "思路", "理解", "几何意义", "物理意义"),
                    (0.945, 0.980, 0.961),
                    (0.755, 0.895, 0.825),
                    (0.059, 0.525, 0.333),
                    (0.045, 0.365, 0.225),
                    (0.145, 0.235, 0.195),
                ),
                (
                    ("核心公式", "推导", "证明", "计算", "算法", "复杂度"),
                    (0.949, 0.965, 0.996),
                    (0.790, 0.855, 0.960),
                    (0.220, 0.365, 0.780),
                    (0.145, 0.245, 0.580),
                    (0.165, 0.205, 0.295),
                ),
                (
                    ("易错", "混淆", "边界", "反例", "避坑", "注意", "陷阱", "局限"),
                    (0.996, 0.953, 0.953),
                    (0.945, 0.815, 0.815),
                    (0.800, 0.165, 0.196),
                    (0.620, 0.100, 0.130),
                    (0.270, 0.165, 0.175),
                ),
                (
                    ("定义", "定理", "引理", "命题", "性质", "推论", "公理", "概念"),
                    (0.941, 0.976, 0.984),
                    (0.755, 0.885, 0.920),
                    (0.055, 0.455, 0.565),
                    (0.040, 0.325, 0.415),
                    (0.145, 0.225, 0.255),
                ),
            )
            chosen_theme = (
                (0.966, 0.976, 0.972),
                (0.840, 0.880, 0.865),
                (0.220, 0.380, 0.310),
                (0.094, 0.361, 0.239),
                (0.220, 0.260, 0.250),
            )
            for kws, bg_c, bd_c, bar_c, bld_c, txt_c in callout_themes:
                if any(kw in q_head_probe for kw in kws):
                    chosen_theme = (bg_c, bd_c, bar_c, bld_c, txt_c)
                    break

            def _draw_quote(
                c: canvas.Canvas,
                top_qy: float = qy,
                bh: float = box_h,
                qlines: list = wlines,
                lhs_q: list = q_lhs,
                theme: tuple = chosen_theme,
            ) -> None:
                bg_c, bd_c, bar_c, bld_c, txt_c = theme
                c.saveState()
                c.setFillColorRGB(*bg_c)
                c.setStrokeColorRGB(*bd_c)
                c.setLineWidth(0.45)
                c.roundRect(left_x + 2.0, top_qy - bh, content_w - 2.0, bh, 3.5, fill=1, stroke=1)
                c.setFillColorRGB(*bar_c)
                c.roundRect(left_x + 2.0, top_qy - bh, 3.2, bh, 1.5, fill=1, stroke=0)
                cy_top = top_qy - 4.0
                for ql, l_h in zip(qlines, lhs_q):
                    ly = cy_top - (13.4 if _has_tall_span(ql) else 9.5)
                    _draw_spans_line(
                        c,
                        ql,
                        left_x + 13.5,
                        ly,
                        font_name,
                        9.2,
                        txt_c,
                        bold_rgb=bld_c,
                        math_rgb=(0.08, 0.28, 0.20),
                    )
                    cy_top -= l_h
                c.restoreState()

            cur_ops.append(_draw_quote)
            y -= box_h + 5.0
            continue

        if blk.kind == "diagram" and (blk.diagram is not None or blk.rows):
            is_protocol = (
                blk.diagram.mode == "protocol"
                if blk.diagram is not None
                else blk.subtitle == "protocol"
            )
            diag_title = "协议交互与步骤流程" if is_protocol else "知识流程与结构图解"
            inner_x = left_x + 11.0
            diag_w = content_w - 22.0
            hdr_h = 20.0

            def _draw_vec_arrow(
                c: canvas.Canvas,
                x1: float,
                x2: float,
                ay: float,
                rgb: tuple[float, float, float] = (0.094, 0.361, 0.239),
            ) -> None:
                if x2 <= x1 + 4.0:
                    return
                c.setStrokeColorRGB(*rgb)
                c.setFillColorRGB(*rgb)
                c.setLineWidth(1.0)
                c.line(x1, ay, x2 - 4.2, ay)
                pth = c.beginPath()
                pth.moveTo(x2, ay)
                pth.lineTo(x2 - 5.2, ay + 2.8)
                pth.lineTo(x2 - 5.2, ay - 2.8)
                pth.close()
                c.drawPath(pth, stroke=0, fill=1)

            if is_protocol:
                proto_rows = blk.diagram.protocol_rows if blk.diagram is not None else blk.rows
                step_cards: list[dict] = []
                for idx_s, row in enumerate(proto_rows, start=1):
                    src = row[0] if len(row) > 0 else ""
                    dst = row[1] if len(row) > 1 else ""
                    msg = row[2] if len(row) > 2 else (row[0] if len(row) == 1 else "")
                    step_badge = f"{idx_s:02d}"
                    has_actor_hdr = bool(src or dst)
                    msg_lines = _wrap_spans(
                        _parse_inline_spans(msg),
                        font_name,
                        8.6,
                        max(100.0, diag_w - (24.0 if has_actor_hdr else 40.0)),
                    )
                    msg_lhs = [_spans_line_height(ml, 12.6, extra=6.0) for ml in msg_lines]
                    sh = (21.0 if has_actor_hdr else 5.0) + max(13.0, sum(msg_lhs)) + 8.0
                    step_cards.append(
                        {
                            "badge": step_badge,
                            "src": src,
                            "dst": dst,
                            "msg_lines": msg_lines,
                            "msg_lhs": msg_lhs,
                            "h": sh,
                        }
                    )
                total_diag_h = hdr_h + 10.0 + sum(sc["h"] + 5.0 for sc in step_cards)
                if total_diag_h <= 560.0:
                    ensure_space(total_diag_h + 4.0)
                    dy_top = y

                    def _draw_proto_card(
                        c: canvas.Canvas,
                        top_dy: float = dy_top,
                        total_h: float = total_diag_h,
                        cards: list = step_cards,
                        title_txt: str = diag_title,
                    ) -> None:
                        c.saveState()
                        c.setFillColorRGB(0.985, 0.992, 0.989)
                        c.setStrokeColorRGB(0.79, 0.87, 0.83)
                        c.setLineWidth(0.55)
                        c.roundRect(left_x, top_dy - total_h, content_w, total_h, 4.0, fill=1, stroke=1)
                        c.setFillColorRGB(0.935, 0.968, 0.952)
                        c.roundRect(left_x, top_dy - hdr_h, content_w, hdr_h, 4.0, fill=1, stroke=0)
                        c.setStrokeColorRGB(0.80, 0.88, 0.84)
                        c.line(left_x, top_dy - hdr_h, right_x, top_dy - hdr_h)
                        _draw_spans_line(
                            c,
                            [_InlineSpan(title_txt, bold=True)],
                            left_x + 11.0,
                            top_dy - 13.5,
                            font_name,
                            8.8,
                            (0.094, 0.361, 0.239),
                        )
                        cur_y = top_dy - hdr_h - 7.0
                        for sc in cards:
                            sh = sc["h"]
                            c.setFillColorRGB(1.0, 1.0, 1.0)
                            c.setStrokeColorRGB(0.85, 0.90, 0.88)
                            c.setLineWidth(0.45)
                            c.roundRect(inner_x, cur_y - sh, diag_w, sh, 3.0, fill=1, stroke=1)
                            bx = inner_x + 7.0
                            by_pill = cur_y - 16.5
                            c.setFillColorRGB(0.094, 0.361, 0.239)
                            c.roundRect(bx, by_pill, 17.5, 11.5, 2.5, fill=1, stroke=0)
                            c.setFillColorRGB(1.0, 1.0, 1.0)
                            c.setFont(font_name, 7.6)
                            c.drawCentredString(bx + 8.75, by_pill + 2.8, sc["badge"])
                            bx += 23.0
                            if sc["src"] or sc["dst"]:
                                if sc["src"]:
                                    sw = min(180.0, pdfmetrics.stringWidth(sc["src"], font_name, 8.2) + 12.0)
                                    c.setFillColorRGB(0.92, 0.965, 0.942)
                                    c.setStrokeColorRGB(0.65, 0.82, 0.74)
                                    c.setLineWidth(0.5)
                                    c.roundRect(bx, by_pill - 0.5, sw, 12.5, 3.0, fill=1, stroke=1)
                                    _draw_spans_line(
                                        c,
                                        [_InlineSpan(sc["src"], bold=True)],
                                        bx + 6.0,
                                        by_pill + 2.6,
                                        font_name,
                                        8.0,
                                        (0.08, 0.30, 0.20),
                                    )
                                    bx += sw
                                if sc["src"] and sc["dst"]:
                                    _draw_vec_arrow(c, bx + 4.0, bx + 24.0, by_pill + 5.7)
                                    bx += 28.0
                                if sc["dst"]:
                                    dw_p = min(180.0, pdfmetrics.stringWidth(sc["dst"], font_name, 8.2) + 12.0)
                                    c.setFillColorRGB(0.93, 0.955, 0.992)
                                    c.setStrokeColorRGB(0.70, 0.80, 0.93)
                                    c.setLineWidth(0.5)
                                    c.roundRect(bx, by_pill - 0.5, dw_p, 12.5, 3.0, fill=1, stroke=1)
                                    _draw_spans_line(
                                        c,
                                        [_InlineSpan(sc["dst"], bold=True)],
                                        bx + 6.0,
                                        by_pill + 2.6,
                                        font_name,
                                        8.0,
                                        (0.14, 0.26, 0.50),
                                    )
                                msg_top = cur_y - 21.0
                                msg_x = inner_x + 12.0
                            else:
                                msg_top = cur_y - 5.0
                                msg_x = bx
                            for ml, mlh in zip(sc["msg_lines"], sc["msg_lhs"]):
                                ly = msg_top - (12.2 if _has_tall_span(ml) else 9.0)
                                _draw_spans_line(
                                    c,
                                    ml,
                                    msg_x,
                                    ly,
                                    font_name,
                                    8.6,
                                    (0.18, 0.22, 0.24),
                                    bold_rgb=(0.06, 0.24, 0.16),
                                    math_rgb=(0.08, 0.28, 0.20),
                                )
                                msg_top -= mlh
                            cur_y -= sh + 5.0
                        c.restoreState()

                    cur_ops.append(_draw_proto_card)
                    y -= total_diag_h + 6.0
                else:
                    ensure_space(hdr_h + step_cards[0]["h"] + 12.0)
                    y_hdr = y
                    cur_ops.append(
                        lambda c, hy_t=y_hdr, tt=diag_title: (
                            c.saveState(),
                            c.setFillColorRGB(0.935, 0.968, 0.952),
                            c.setStrokeColorRGB(0.79, 0.87, 0.83),
                            c.roundRect(left_x, hy_t - hdr_h, content_w, hdr_h, 3.5, fill=1, stroke=1),
                            _draw_spans_line(
                                c, [_InlineSpan(tt, bold=True)], left_x + 11.0, hy_t - 13.5, font_name, 8.8, (0.094, 0.361, 0.239)
                            ),
                            c.restoreState(),
                        )
                    )
                    y -= hdr_h + 5.0
                    for sc in step_cards:
                        sh = sc["h"]
                        ensure_space(sh + 5.0)
                        sy_t = y

                        def _draw_one_step(c: canvas.Canvas, cur_y: float = sy_t, step_d: dict = sc) -> None:
                            s_h = step_d["h"]
                            c.saveState()
                            c.setFillColorRGB(0.992, 0.996, 0.994)
                            c.setStrokeColorRGB(0.84, 0.89, 0.87)
                            c.setLineWidth(0.45)
                            c.roundRect(left_x, cur_y - s_h, content_w, s_h, 3.0, fill=1, stroke=1)
                            bx = left_x + 8.0
                            by_p = cur_y - 16.0
                            c.setFillColorRGB(0.094, 0.361, 0.239)
                            c.roundRect(bx, by_p, 17.5, 11.5, 2.5, fill=1, stroke=0)
                            c.setFillColorRGB(1.0, 1.0, 1.0)
                            c.setFont(font_name, 7.6)
                            c.drawCentredString(bx + 8.75, by_p + 2.8, step_d["badge"])
                            msg_top = cur_y - (20.0 if (step_d["src"] or step_d["dst"]) else 5.0)
                            msg_x = left_x + (12.0 if (step_d["src"] or step_d["dst"]) else 31.0)
                            if step_d["src"] or step_d["dst"]:
                                actor_txt = f"{step_d['src']} → {step_d['dst']}" if (step_d["src"] and step_d["dst"]) else (step_d["src"] or step_d["dst"])
                                _draw_spans_line(
                                    c, [_InlineSpan(actor_txt, bold=True)], bx + 23.0, by_p + 2.6, font_name, 8.2, (0.08, 0.30, 0.20)
                                )
                            for ml, mlh in zip(step_d["msg_lines"], step_d["msg_lhs"]):
                                ly = msg_top - (12.2 if _has_tall_span(ml) else 9.0)
                                _draw_spans_line(c, ml, msg_x, ly, font_name, 8.6, (0.18, 0.22, 0.24), math_rgb=(0.08, 0.28, 0.20))
                                msg_top -= mlh
                            c.restoreState()

                        cur_ops.append(_draw_one_step)
                        y -= sh + 4.5
                continue

            dag_graph = blk.diagram or _DiagramGraph(
                mode="dag",
                direction="LR",
                nodes=[{"id": f"n{idx}", "label": " · ".join(r), "shape": "rect"} for idx, r in enumerate(blk.rows)],
                edges=[(f"n{idx}", f"n{idx + 1}", "") for idx in range(len(blk.rows) - 1)],
            )
            dag_layout = _prepare_dag_layout(dag_graph, font_name, diag_w)
            total_flow_h = hdr_h + 20.0 + dag_layout["graph_h"]
            ensure_space(min(total_flow_h + 4.0, 620.0))
            fy_top = y
            cur_ops.append(
                lambda c, top_fy=fy_top, total_h=total_flow_h, dl=dag_layout, tt=diag_title: _draw_dag_diagram_card(
                    c, top_fy, total_h, left_x, content_w, inner_x, diag_w, hdr_h, font_name, dl, tt
                )
            )
            y -= total_flow_h + 6.0
            continue

        if blk.kind == "code":
            raw_code_lines = blk.text.splitlines() or [""]
            wrapped_code: list[list[_InlineSpan]] = []
            for rl in raw_code_lines:
                wrapped_code.extend(_wrap_spans([_InlineSpan(_normalize_code_text(rl))], font_name, 8.4, content_w - 20.0))
            lh = 12.2
            box_h = len(wrapped_code) * lh + 10.0
            ensure_space(min(box_h, 260.0))
            cy_top = y

            def _draw_code(c: canvas.Canvas, top_cy: float = cy_top, bh: float = box_h, clines: list = wrapped_code) -> None:
                c.saveState()
                c.setFillColorRGB(0.955, 0.962, 0.972)
                c.setStrokeColorRGB(0.84, 0.86, 0.90)
                c.setLineWidth(0.5)
                c.roundRect(left_x, top_cy - bh, content_w, bh, 3.5, fill=1, stroke=1)
                cy = top_cy - 12.0
                for cl in clines:
                    _draw_spans_line(c, cl, left_x + 10.0, cy, font_name, 8.4, (0.18, 0.22, 0.30))
                    cy -= lh
                c.restoreState()

            cur_ops.append(_draw_code)
            y -= box_h + 4.0
            continue

        if blk.kind == "table" and blk.rows:
            num_cols = max(len(r) for r in blk.rows)
            if num_cols > 0:
                pad_x = 6.5
                col_scores: list[float] = []
                for c_idx in range(num_cols):
                    col_lens: list[float] = []
                    for r in blk.rows:
                        cell_s = r[c_idx] if c_idx < len(r) else ""
                        plain = re.sub(
                            r"[*_`\$=]+|\[(?:必考|高频考点|易错|核心公式|课件\s*P\.\d+(?:\s*[-–~]\s*P?\.?\d+)?)\]",
                            "",
                            cell_s or "",
                        ).strip()
                        eff_l = sum(1.8 if ord(ch) > 127 else 1.0 for ch in plain)
                        col_lens.append(eff_l)
                    col_len = max(col_lens) if col_lens else 4.5
                    col_scores.append(max(4.5, min(32.0, col_len ** 0.78)))
                score_sum = sum(col_scores) or float(num_cols)
                col_ws = [content_w * (sc / score_sum) for sc in col_scores]
                min_cw = min(54.0, (content_w / float(num_cols)) * 0.72)
                deficits = sum(max(0.0, min_cw - cw) for cw in col_ws)
                if deficits > 0.0:
                    surplus = sum(max(0.0, cw - min_cw) for cw in col_ws) or 1.0
                    col_ws = [
                        min_cw if cw < min_cw else cw - deficits * ((cw - min_cw) / surplus)
                        for cw in col_ws
                    ]
                col_xs: list[float] = [left_x]
                for cw in col_ws[:-1]:
                    col_xs.append(col_xs[-1] + cw)

                total_rows = len(blk.rows)
                for r_idx, row in enumerate(blk.rows):
                    is_header = r_idx == 0 and total_rows > 1
                    is_last = r_idx == total_rows - 1
                    is_even_body = (not is_header) and (r_idx % 2 == 0)
                    cell_lines: list[list[list[_InlineSpan]]] = []
                    cell_lhs: list[list[float]] = []
                    max_cell_h = 13.0
                    for c_idx in range(num_cols):
                        cell_txt = row[c_idx] if c_idx < len(row) else ""
                        cspans = _parse_inline_spans(cell_txt, default_bold=is_header)
                        cw_lines = _wrap_spans(
                            cspans,
                            font_name,
                            8.8,
                            max(22.0, col_ws[c_idx] - pad_x * 2),
                        )
                        cw_lhs = [_spans_line_height(wl, 13.0, extra=7.5) for wl in cw_lines]
                        cell_lines.append(cw_lines)
                        cell_lhs.append(cw_lhs)
                        max_cell_h = max(max_cell_h, sum(cw_lhs))
                    row_h = max_cell_h + 9.0
                    ensure_space(row_h + 28.0 if is_header else row_h)
                    ry = y

                    def _draw_table_row(
                        c: canvas.Canvas,
                        top_ry: float = ry,
                        rh: float = row_h,
                        clines_list: list = cell_lines,
                        clhs_list: list = cell_lhs,
                        hdr: bool = is_header,
                        last_r: bool = is_last,
                        first_r: bool = r_idx == 0,
                        even_r: bool = is_even_body,
                        cxs: list = col_xs,
                    ) -> None:
                        c.saveState()
                        if hdr:
                            c.setFillColorRGB(0.935, 0.968, 0.952)
                        elif even_r:
                            c.setFillColorRGB(0.978, 0.986, 0.983)
                        else:
                            c.setFillColorRGB(1.0, 1.0, 1.0)
                        c.rect(left_x, top_ry - rh, content_w, rh, fill=1, stroke=0)

                        # Subtle inner column dividers
                        c.setStrokeColorRGB(0.90, 0.93, 0.915)
                        c.setLineWidth(0.35)
                        for col_x_div in cxs[1:]:
                            c.line(col_x_div, top_ry, col_x_div, top_ry - rh)

                        # Booktabs horizontal rules (Academic Pine toprule, midrule, bottomrule)
                        if first_r:
                            c.setStrokeColorRGB(0.094, 0.361, 0.239)
                            c.setLineWidth(1.15)
                            c.line(left_x, top_ry, right_x, top_ry)
                        if hdr:
                            c.setStrokeColorRGB(0.094, 0.361, 0.239)
                            c.setLineWidth(0.85)
                            c.line(left_x, top_ry - rh, right_x, top_ry - rh)
                        elif last_r:
                            c.setStrokeColorRGB(0.094, 0.361, 0.239)
                            c.setLineWidth(1.05)
                            c.line(left_x, top_ry - rh, right_x, top_ry - rh)
                        else:
                            c.setStrokeColorRGB(0.85, 0.89, 0.87)
                            c.setLineWidth(0.4)
                            c.line(left_x, top_ry - rh, right_x, top_ry - rh)

                        for col_i, (clines, clhs) in enumerate(zip(clines_list, clhs_list)):
                            cx = cxs[col_i] + pad_x
                            cy_top = top_ry - 2.5
                            for wl, l_h in zip(clines, clhs):
                                ly = cy_top - (13.0 if _has_tall_span(wl) else 9.5)
                                _draw_spans_line(
                                    c,
                                    wl,
                                    cx,
                                    ly,
                                    font_name,
                                    8.8,
                                    (0.075, 0.290, 0.192) if hdr else (0.14, 0.18, 0.20),
                                    bold_rgb=(0.065, 0.255, 0.168) if hdr else (0.08, 0.14, 0.12),
                                    math_rgb=(0.075, 0.290, 0.192) if hdr else (0.08, 0.26, 0.20),
                                )
                                cy_top -= l_h
                        c.restoreState()

                    cur_ops.append(_draw_table_row)
                    y -= row_h
                y -= 6.5
            continue

        if blk.kind == "image_card" and blk.visual is not None:
            vis = blk.visual
            raw_bytes = _optimize_pdf_jpeg(vis.image_bytes)
            jinfo = _jpeg_info(raw_bytes)
            if jinfo is not None:
                iw, ih, _ = jinfo
                max_img_w = 290.0
                max_img_h = 152.0
                scale = min(max_img_w / float(iw), max_img_h / float(ih))
                draw_w = iw * scale
                draw_h = ih * scale

                cap_lines = _wrap_spans([_InlineSpan(vis.caption, bold=True)], font_name, 8.4, content_w - 20.0)
                note_lines: list[list[_InlineSpan]] = []
                if vis.board_note:
                    note_lines = _wrap_spans([_InlineSpan(vis.board_note)], font_name, 8.2, content_w - 20.0)

                card_h = draw_h + 14.0 + len(cap_lines) * 11.5 + len(note_lines) * 11.5
                ensure_space(card_h + 6.0)
                iy_top = y

                def _draw_image_card(
                    c: canvas.Canvas,
                    top_iy: float = iy_top,
                    ch: float = card_h,
                    dw: float = draw_w,
                    dh: float = draw_h,
                    img_b: bytes = raw_bytes,
                    clines: list = cap_lines,
                    nlines: list = note_lines,
                ) -> None:
                    c.saveState()
                    c.setFillColorRGB(0.988, 0.990, 0.993)
                    c.setStrokeColorRGB(0.84, 0.86, 0.89)
                    c.setLineWidth(0.55)
                    c.roundRect(left_x, top_iy - ch, content_w, ch, 4.0, fill=1, stroke=1)
                    img_x = left_x + (content_w - dw) / 2.0
                    img_y = top_iy - dh - 6.0
                    try:
                        img_reader = ImageReader(io.BytesIO(img_b))
                        c.drawImage(img_reader, img_x, img_y, width=dw, height=dh, preserveAspectRatio=True)
                    except Exception:
                        pass
                    ty = img_y - 11.0
                    for cl in clines:
                        _draw_spans_line(c, cl, left_x + 10.0, ty, font_name, 8.4, (0.22, 0.26, 0.32))
                        ty -= 11.5
                    for nl in nlines:
                        _draw_spans_line(c, nl, left_x + 10.0, ty, font_name, 8.2, (0.38, 0.42, 0.46))
                        ty -= 11.5
                    c.restoreState()

                cur_ops.append(_draw_image_card)
                y -= card_h + 6.0
            continue

        if blk.kind == "promo_card":
            card_h = 66.0
            ensure_space(card_h + 6.0)
            py_top = y

            def _draw_promo(c: canvas.Canvas, top_py: float = py_top, ch: float = card_h) -> None:
                c.saveState()
                c.setFillColorRGB(0.955, 0.976, 0.966)
                c.setStrokeColorRGB(0.62, 0.78, 0.70)
                c.setLineWidth(0.75)
                c.roundRect(left_x, top_py - ch, content_w, ch, 4.5, fill=1, stroke=1)
                _draw_spans_line(
                    c,
                    [_InlineSpan("本课堂笔记由 PKU All in One 自动整理 · 欢迎分享给同班同学", bold=True)],
                    left_x + 12.0,
                    top_py - 15.5,
                    font_name,
                    9.6,
                    (0.075, 0.290, 0.192),
                )
                _draw_spans_line(
                    c,
                    [_InlineSpan("1. 自动提取北大教学网回放音轨、课堂板书/PPT 章节配图与结构化复习提纲")],
                    left_x + 12.0,
                    top_py - 30.5,
                    font_name,
                    8.5,
                    (0.20, 0.24, 0.22),
                )
                _draw_spans_line(
                    c,
                    [_InlineSpan("2. 智能捕捉老师课堂口述通知、随堂测验口令与期中期末必考重点，支持 AI 原生 MCP 助教")],
                    left_x + 12.0,
                    top_py - 44.0,
                    font_name,
                    8.5,
                    (0.20, 0.24, 0.22),
                )
                _draw_spans_line(
                    c,
                    [_InlineSpan(f"本地开源版：{WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL})", bold=True)],
                    left_x + 12.0,
                    top_py - 57.5,
                    font_name,
                    8.8,
                    (0.094, 0.361, 0.239),
                )
                c.restoreState()

            cur_ops.append(_draw_promo)
            y -= card_h + 6.0
            continue

    if cur_ops or not pages_ops:
        pages_ops.append(cur_ops)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595.0, 842.0), pageCompression=0, pdfVersion=(1, 4))
    c.setTitle(document_title)
    c.setAuthor(WATERMARK_SITE_NAME)
    c.setCreator(f"{WATERMARK_SITE_NAME} - {WATERMARK_SITE_URL}")
    c.setSubject("PKU All in One Course Notes & Exam Highlights")

    total_pages = len(pages_ops)
    for page_idx, ops in enumerate(pages_ops, start=1):
        _draw_page_chrome(c, font_name, page_idx, total_pages, document_title)
        for fn in ops:
            fn(c)
        c.showPage()

    c.save()
    pdf_bytes = buf.getvalue()
    if len(pdf_bytes) <= _PDF_DOC_CACHE_SINGLE_MAX_BYTES:
        with _PDF_DOC_CACHE_LOCK:
            if doc_cache_key not in _PDF_DOC_CACHE:
                while (
                    len(_PDF_DOC_CACHE) >= _PDF_DOC_CACHE_MAX_ITEMS
                    or (_PDF_DOC_CACHE_BYTES + len(pdf_bytes) > _PDF_DOC_CACHE_MAX_BYTES and _PDF_DOC_CACHE)
                ):
                    _, evicted = _PDF_DOC_CACHE.popitem(last=False)
                    _PDF_DOC_CACHE_BYTES = max(0, _PDF_DOC_CACHE_BYTES - len(evicted))
                _PDF_DOC_CACHE[doc_cache_key] = pdf_bytes
                _PDF_DOC_CACHE_BYTES += len(pdf_bytes)
            else:
                _PDF_DOC_CACHE.move_to_end(doc_cache_key)
    return pdf_bytes


def collect_course_pdf_sections(
    root: Path,
    course_id: str = "",
    recording_id: str = "",
    *,
    include_summary: bool = True,
    include_lectures: bool = True,
    note_variant: str = "auto",
) -> list[NotePdfSection]:
    """Collect NotePdfSection items from `root` for single-note, single-course, or all-courses batch PDF export."""
    from .campus_catalog import _courses, campus_course, campus_courses
    from .course_summary import extract_note_oral_highlights, summary_markdown
    from .note_images import build_note_visual_sections
    from .recordings_api import find_job

    variant = str(note_variant or "auto").strip().lower()
    if variant not in {"auto", "refined", "raw"}:
        variant = "auto"

    root = Path(root)
    sections: list[NotePdfSection] = []
    payload = campus_courses(root)
    courses_list = payload.get("courses") if isinstance(payload, dict) else list(payload or [])
    target_courses = [
        c
        for c in courses_list
        if not course_id or c.get("id") == course_id or course_id in str(c.get("title") or "")
    ]
    course_folders = {key: folder for folder, key, _, _ in _courses(root)}

    for c_meta in target_courses:
        cid = str(c_meta.get("id") or "")
        if not cid:
            continue
        try:
            full_course = campus_course(root, cid)
        except Exception:
            continue
        ctitle = str(full_course.get("title") or cid)

        if include_summary and not recording_id:
            try:
                sum_md = summary_markdown(root, cid)
            except Exception:
                sum_md = ""
            if not (sum_md and sum_md.strip()):
                sum_obj = full_course.get("course_summary") or {}
                if isinstance(sum_obj, dict):
                    sum_md = str(sum_obj.get("markdown") or "")
            if sum_md and sum_md.strip():
                sections.append(
                    NotePdfSection(
                        course_title=ctitle,
                        doc_title="课程总结与复习提纲",
                        subtitle=f"由 {WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL}) 自动汇总生成",
                        markdown=sum_md,
                    )
                )

        if include_lectures:
            seen_note_files: set[Path] = set()
            for lesson in full_course.get("lessons") or []:
                lid = str(lesson.get("id") or "")
                if recording_id and lid != recording_id:
                    continue
                job = find_job(root, lid, cid)
                notes_file = (job.directory / "notes.md") if job is not None else None
                raw_text = ""
                if notes_file is not None and notes_file.is_file():
                    try:
                        seen_note_files.add(notes_file.resolve())
                        raw_text = notes_file.read_text(encoding="utf-8")
                    except Exception:
                        raw_text = ""
                md_text = ""
                if variant == "raw":
                    md_text = raw_text
                elif variant == "refined":
                    if job is not None:
                        try:
                            from .refined_notes import load_saved_refined_note

                            saved_ref = load_saved_refined_note(job.directory, raw_text)
                            if saved_ref and str(saved_ref.get("markdown") or "").strip():
                                md_text = str(saved_ref["markdown"])
                        except Exception:
                            md_text = ""
                else:
                    md_text = raw_text
                    if job is not None:
                        try:
                            from .refined_notes import load_saved_refined_note

                            saved_ref = load_saved_refined_note(job.directory, raw_text)
                            if saved_ref and str(saved_ref.get("markdown") or "").strip():
                                if not saved_ref.get("stale") or not raw_text.strip():
                                    md_text = str(saved_ref["markdown"])
                        except Exception:
                            pass
                if not md_text.strip():
                    continue
                ltitle = str(lesson.get("title") or lid)
                ldate = str(lesson.get("date") or "")
                imgs, vsecs = load_recording_visual_assets(root, cid, lid, md_text, job=job)
                try:
                    oral = extract_note_oral_highlights(md_text, limit=8)
                except Exception:
                    oral = []
                sections.append(
                    NotePdfSection(
                        course_title=ctitle,
                        doc_title=ltitle,
                        subtitle=f"讲次日期：{ldate}  |  由 {WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL}) 自动整理",
                        markdown=md_text,
                        images=imgs,
                        visual_sections=vsecs,
                        oral_highlights=oral,
                    )
                )

            course_folder = course_folders.get(cid)
            if not recording_id and variant != "refined" and course_folder is not None and course_folder.is_dir():
                extra_candidates = sorted(
                    set(course_folder.glob("recordings/*/notes.md"))
                    | set(course_folder.glob("*/notes.md"))
                    | set(course_folder.glob("notes/*.md"))
                )
                for extra_md in extra_candidates:
                    if not extra_md.is_file():
                        continue
                    try:
                        resolved_extra = extra_md.resolve()
                    except OSError:
                        continue
                    if resolved_extra in seen_note_files:
                        continue
                    seen_note_files.add(resolved_extra)
                    try:
                        md_text = extra_md.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if not md_text.strip():
                        continue
                    ltitle = extra_md.parent.name if extra_md.name == "notes.md" else extra_md.stem
                    try:
                        oral = extract_note_oral_highlights(md_text, limit=8)
                    except Exception:
                        oral = []
                    sections.append(
                        NotePdfSection(
                            course_title=ctitle,
                            doc_title=ltitle,
                            subtitle=f"由 {WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL}) 自动整理",
                            markdown=md_text,
                            oral_highlights=oral,
                        )
                    )

        if not recording_id and include_lectures and variant != "refined" and not any(s.course_title == ctitle for s in sections):
            overview_lines = [
                f"# {ctitle} · 课程目录与近期动态",
                "",
                f"> 导出自 **{WATERMARK_SITE_NAME}**（{WATERMARK_SITE_URL}）",
                "",
            ]
            anns = full_course.get("announcements") or []
            if anns:
                overview_lines.append("## 课程通知")
                for a in anns[:10]:
                    overview_lines.append(
                        f"- **{a.get('title') or '通知'}**（{a.get('date') or ''}）：{a.get('body') or a.get('summary') or ''}"
                    )
                overview_lines.append("")
            asgs = full_course.get("assignments") or []
            if asgs:
                overview_lines.append("## 课程作业与截止时间")
                for a in asgs[:10]:
                    overview_lines.append(f"- **{a.get('title') or '作业'}**（截止：{a.get('due_at') or '未标注'}）")
                overview_lines.append("")
            lessons = full_course.get("lessons") or []
            if lessons:
                overview_lines.append("## 课堂回放列表")
                for l in lessons:
                    status_tag = "已生成笔记" if l.get("notes_available") else "待生成笔记"
                    overview_lines.append(f"- {l.get('date') or ''} {l.get('title') or ''}（{status_tag}）")
                overview_lines.append("")
            sections.append(
                NotePdfSection(
                    course_title=ctitle,
                    doc_title="课程概览与教学网动态",
                    subtitle=f"由 {WATERMARK_SITE_NAME} ({WATERMARK_SITE_URL}) 自动整理",
                    markdown="\n".join(overview_lines),
                )
            )

    return sections


def _clean_zip_entry_stem(raw: str, fallback: str = "课堂笔记") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(raw or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned or fallback


def _clean_course_folder_name(raw_title: str, fallback: str = "课程") -> str:
    title = re.sub(r"\s*[（(][^）)]*学期[^）)]*[）)]\s*$", "", str(raw_title or "").strip()).strip()
    return _clean_zip_entry_stem(title or raw_title, fallback)


def build_notes_pdf_zip(
    root: Path,
    *,
    course_id: str = "",
    include_summary: bool = True,
    note_variant: str = "auto",
) -> tuple[bytes, str]:
    """Build a `.zip` archive where each lecture note (and course summary if available)
    is exported as an individual watermarked PDF file.

    - Single-course export (`course_id` specified): returns `(zip_bytes, "{课程名}-笔记.zip")`
      with each lecture's PDF at the top level of the archive.
    - All-courses export (`course_id` empty): returns `(zip_bytes, "笔记.zip")`
      organized by `{课程名}/{每节课笔记}.pdf`.
    """
    import zipfile

    cid = str(course_id or "").strip()
    sections = collect_course_pdf_sections(
        root,
        course_id=cid,
        include_summary=bool(include_summary),
        include_lectures=True,
        note_variant=note_variant,
    )
    if not sections:
        return b"", ""

    real_sections = [s for s in sections if s.doc_title != "课程概览与教学网动态"]
    active_sections = real_sections if real_sections else sections

    grouped: list[tuple[str, list[NotePdfSection]]] = []
    by_course: dict[str, list[NotePdfSection]] = {}
    for sec in active_sections:
        ctitle = str(sec.course_title or cid or "课程")
        if ctitle not in by_course:
            bucket: list[NotePdfSection] = []
            by_course[ctitle] = bucket
            grouped.append((ctitle, bucket))
        by_course[ctitle].append(sec)

    single_course_mode = bool(cid) and len(grouped) == 1
    buf = io.BytesIO()
    used_folders: set[str] = set()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for raw_ctitle, course_sections in grouped:
            base_folder = _clean_course_folder_name(raw_ctitle, "课程")
            folder_name = base_folder
            folder_dup = 2
            while folder_name in used_folders:
                folder_name = f"{base_folder}-{folder_dup}"
                folder_dup += 1
            used_folders.add(folder_name)

            used_stems: set[str] = set()
            lec_idx = 0
            for sec in course_sections:
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
                stem_dup = 2
                while stem in used_stems:
                    stem = f"{base_stem}-{stem_dup}"
                    stem_dup += 1
                used_stems.add(stem)

                pdf_bytes = build_watermarked_notes_pdf(
                    [sec],
                    document_title=f"{sec.course_title} · {sec.doc_title}",
                )
                arc_path = f"{stem}.pdf" if single_course_mode else f"{folder_name}/{stem}.pdf"
                zf.writestr(arc_path, pdf_bytes)

    if single_course_mode:
        first_folder = _clean_course_folder_name(grouped[0][0], "课程")
        zip_filename = f"{first_folder}-笔记.zip"
    else:
        zip_filename = "笔记.zip"

    return buf.getvalue(), zip_filename


def load_recording_visual_assets(
    root: Path,
    course_id: str,
    recording_id: str,
    markdown: str = "",
    *,
    job=None,
) -> tuple[list[tuple[str, bytes]], list[dict]]:
    """Load section-aligned visual metadata and their referenced JPEG bytes for a recording."""
    if not recording_id:
        return [], []
    from urllib.parse import parse_qs, unquote, urlsplit
    from .note_images import _local_material, _shared_seed_recording_dir, _slide_cache, build_note_visual_sections
    from .recordings_api import find_job

    if job is None:
        job = find_job(Path(root), recording_id)
    if job is None:
        return [], []
    kf_dir = job.directory / "keyframes"
    shared_rec_dir = _shared_seed_recording_dir(job)
    if (
        shared_rec_dir is not None
        and (not kf_dir.is_dir() or not any(p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} for p in kf_dir.iterdir() if p.is_file()))
        and (shared_rec_dir / "keyframes").is_dir()
    ):
        kf_dir = shared_rec_dir / "keyframes"
    search_dirs = [d for d in (kf_dir, job.directory / "note-frames", shared_rec_dir / "note-frames" if shared_rec_dir else None) if d is not None and d.is_dir()]

    vsecs: list[dict] = []
    md_text = markdown or ""
    has_ts = bool(re.search(r"\[\d{1,2}:\d{2}", md_text))
    has_embedded_imgs = bool(re.search(r"!\[[^\]]*\]\([^)\n]+\)", md_text))
    if md_text.strip() and not (has_embedded_imgs and not has_ts):
        try:
            vsecs = (
                build_note_visual_sections(
                    job,
                    course_id or "course",
                    recording_id,
                    md_text,
                    fast_only=(not has_ts),
                )
                or []
            )
        except Exception:
            vsecs = []

    needed_files: list[Path] = []
    seen_names: set[str] = set()
    imgs: list[tuple[str, bytes]] = []
    seen_slide_urls: set[str] = set()

    def _add_file_by_name(fname: str) -> None:
        if not fname or fname in seen_names:
            return
        for d in search_dirs:
            cand = d / fname
            if cand.is_file():
                seen_names.add(fname)
                needed_files.append(cand)
                return

    def _load_slide_page_jpeg(ref_url: str) -> None:
        if "/slide-page?" not in ref_url or ref_url in seen_slide_urls:
            return
        seen_slide_urls.add(ref_url)
        try:
            qs = parse_qs(urlsplit(ref_url).query)
            file_param = unquote((qs.get("file") or [""])[0]).strip()
            page_int = int((qs.get("page") or ["0"])[0])
        except (ValueError, TypeError):
            return
        if not file_param or page_int < 1:
            return
        cache = _slide_cache(job, file_param, page_int)
        for ext in (".jpg", ".png"):
            img_path = Path(str(cache) + ext)
            if img_path.is_file():
                try:
                    raw_b = img_path.read_bytes()
                    if ext == ".png" and _jpeg_info(raw_b) is None:
                        import cv2
                        import numpy as np

                        arr = np.frombuffer(raw_b, dtype=np.uint8)
                        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if decoded is not None:
                            ok, enc = cv2.imencode(".jpg", decoded)
                            if ok:
                                raw_b = enc.tobytes()
                    if _jpeg_info(raw_b) is not None:
                        imgs.append((ref_url, raw_b))
                        return
                except Exception:
                    pass
        pdf = _local_material(job, file_param)
        if pdf is None:
            return
        try:
            import cv2
            import pypdfium2 as pdfium

            with pdfium.PdfDocument(str(pdf)) as document:
                if page_int > len(document):
                    return
                pdf_page = document[page_int - 1]
                bitmap = None
                try:
                    bitmap = pdf_page.render(scale=min(2, 1600 / max(pdf_page.get_size())))
                    pixels = bitmap.to_numpy()
                    ok, encoded = cv2.imencode(".jpg", pixels)
                    if ok:
                        raw_b = encoded.tobytes()
                        if _jpeg_info(raw_b) is not None:
                            imgs.append((ref_url, raw_b))
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    pdf_page.close()
        except Exception:
            return

    for _alt, ref_url in re.findall(r"!\[([^\]]*)\]\(([^)\n]+)\)", markdown or ""):
        clean_ref = ref_url.strip()
        if "/slide-page?" in clean_ref:
            _load_slide_page_jpeg(clean_ref)
        else:
            _add_file_by_name(Path(clean_ref.split("?")[0]).name)

    for vs in vsecs:
        furl = str(vs.get("frame_url") or "")
        _add_file_by_name(Path(furl.split("?")[0]).name)
        surl = str(vs.get("slide_url") or "")
        if "/slide-page?" in surl:
            _load_slide_page_jpeg(surl)

    if not needed_files and kf_dir.is_dir():
        for jpg_file in sorted(kf_dir.glob("*.jpg"))[:24]:
            if jpg_file.name not in seen_names:
                seen_names.add(jpg_file.name)
                needed_files.append(jpg_file)

    for jpg_file in needed_files:
        try:
            raw_b = jpg_file.read_bytes()
            imgs.append((jpg_file.name, raw_b))
            imgs.append((f"keyframes/{jpg_file.name}", raw_b))
        except Exception:
            continue
    return imgs, vsecs


def load_recording_keyframe_images(root: Path, recording_id: str) -> list[tuple[str, bytes]]:
    """Backwards-compatible helper returning keyframe JPEG bytes for a recording_id."""
    imgs, _ = load_recording_visual_assets(Path(root), "", recording_id)
    return imgs
