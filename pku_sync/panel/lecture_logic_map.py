"""Interactive lecture logic chain, mindmap, and interspersed derivation cards built from notes.md."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .llm_json import extract_single_json_payload

LOGIC_MAP_FILENAME = "lecture_logic_map.json"
MAX_NOTE_CHARS_FOR_LOGIC_MAP = 15000


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _extract_markdown_headings(markdown: str) -> list[str]:
    headings: list[str] = []
    for line in (markdown or "").splitlines():
        m = re.match(r"^(#{2,3})\s+(.+?)\s*$", line.strip())
        if m:
            h = re.sub(r"\s*\[[0-9:–\-]+\]\s*$", "", m.group(2)).strip()
            if h and h not in headings:
                headings.append(h)
    return headings


def load_saved_lecture_logic_map(job_dir: Path, markdown: str = "") -> dict | None:
    """Read locally cached logic chain & mindmap for a lecture if valid."""
    from types import SimpleNamespace
    from .note_images import _shared_seed_recording_dir

    candidates = [job_dir / LOGIC_MAP_FILENAME, job_dir / "lecture-logic-map.json"]
    shared_dir = _shared_seed_recording_dir(SimpleNamespace(directory=job_dir, course_dir=job_dir.parent.parent))
    if shared_dir is not None:
        candidates.extend([shared_dir / LOGIC_MAP_FILENAME, shared_dir / "lecture-logic-map.json"])
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("logic_chain") or data.get("mindmap")):
            return data
    return None


def _fallback_from_markdown(title: str, markdown: str) -> dict:
    """Build a structured logic map from markdown outline for offline fallback."""
    headings = _extract_markdown_headings(markdown)
    if not headings:
        headings = ["核心概念与定义", "关键推导与机制", "应用与边界条件"]
    stages = ("问题与动机", "核心定义", "关键推导/机制", "定理结论", "应用与边界")
    logic_chain = []
    for idx, h in enumerate(headings[:5]):
        logic_chain.append({
            "stage": stages[min(idx, len(stages) - 1)],
            "title": h,
            "detail": f"围绕「{h}」展开课堂核心定义、推导脉络与关键结论。",
            "section": h,
        })
    mindmap = []
    for h in headings[:4]:
        mindmap.append({
            "topic": h,
            "section": h,
            "summary": f"{h}的核心概念与课堂推导要点",
            "children": [
                {"label": "核心定义与前提", "detail": f"明确 {h} 的基本概念与适用条件"},
                {"label": "推导机制与结论", "detail": f"梳理 {h} 的关键步骤与定量关系"},
            ],
        })
    first_sec = headings[0]
    second_sec = headings[1] if len(headings) > 1 else first_sec
    return {
        "title": f"{title} · 逻辑主线与思维导图",
        "core_thesis": f"本讲围绕「{' ➔ '.join(headings[:3])}」展开，从基本定义与动机出发推导核心机制与边界条件。",
        "logic_chain": logic_chain,
        "mindmap": mindmap,
        "proof_cards": [
            {
                "section": first_sec,
                "title": f"{first_sec} · 推导思路与形式化步骤",
                "intuition": "先抓住问题受限条件与核心不变量，再通过代数/结构变换将复杂目标拆解为可计算的递推或等价关系。",
                "formal_steps": [
                    "明确输入状态、约束条件与目标性质",
                    "引入关键中间变换或辅助量进行等价化简",
                    "验证结论在目标范围内的正确性与复杂度/可逆性",
                ],
            }
        ],
        "contrast_cards": [
            {
                "section": second_sec,
                "title": f"{second_sec} · 成立条件与反例边界对比",
                "positive_title": "前提满足（正例）",
                "positive": "满足课堂给定的结构前提（如互素/素域/同步状态一致）时，性质与逆运算严格成立。",
                "counter_title": "前提破坏（反例/边界）",
                "counterexample": "若忽略前提（如模合数存在零因子或状态失步），会导致逆元不存在或推导失效。",
            }
        ],
    }


_DEFAULT_STAGES = ("问题动机", "核心定义", "关键桥梁", "定理结论", "算法演练", "拓展应用")


def _clean_latex_str(val: Any) -> str:
    """Restore LaTeX commands whose single backslash was parsed as a JSON control escape."""
    s = str(val or "")
    if not s:
        return ""
    replacements = (
        ("\x09imes", r"\times"),
        ("\x09ext", r"\text"),
        ("\x09heta", r"\theta"),
        ("\x09au", r"\tau"),
        ("\x09riangle", r"\triangle"),
        ("\x09o", r"\to"),
        ("\x0dightleftharpoons", r"\rightleftharpoons"),
        ("\x0dightarrow", r"\rightarrow"),
        ("\x0dho", r"\rho"),
        ("\x08mod", r"\bmod"),
        ("\x08igoplus", r"\bigoplus"),
        ("\x08igotimes", r"\bigotimes"),
        ("\x08igcup", r"\bigcup"),
        ("\x08igcap", r"\bigcap"),
        ("\x08ig", r"\big"),
        ("\x08inom", r"\binom"),
        ("\x08eta", r"\beta"),
        ("\x08ar", r"\bar"),
        ("\x0crac", r"\frac"),
        ("\x0corall", r"\forall"),
        ("\not\to", r"\equiv"),
        (r"\not\to", r"\equiv"),
    )
    for bad, good in replacements:
        if bad in s:
            s = s.replace(bad, good)
    return s.strip()


def _clean_stage(raw_stage: Any, idx: int) -> str:
    s = _clean_latex_str(raw_stage).strip(" |\t\r\n")
    if not s or ("问题/动机" in s and "核心定义" in s):
        return _DEFAULT_STAGES[idx % len(_DEFAULT_STAGES)]
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        if parts:
            s = parts[-1] if len(parts) == 2 and parts[0] in ("问题/动机", "核心定义") else parts[0]
    return s[:16]


def _normalize_logic_map_payload(raw_payload: Any, fallback_title: str, markdown: str) -> dict:
    """Validate and normalize AI JSON into {title, core_thesis, logic_chain, mindmap, proof_cards, contrast_cards}."""
    if not isinstance(raw_payload, dict):
        raise ValueError("AI 返回的逻辑导图格式不完整，请重试。")

    headings = _extract_markdown_headings(markdown)
    default_section = headings[0] if headings else ""

    def match_section(sec: Any) -> str:
        s = _clean_latex_str(sec)
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"\s*\[[0-9:–\-]+\]\s*$", "", s).strip()
        if not s:
            return default_section
        s_compact = re.sub(r"\s+", "", s)
        for h in headings:
            h_compact = re.sub(r"\s+", "", h)
            if s == h or s in h or h in s or (s_compact and h_compact and (s_compact == h_compact or s_compact in h_compact or h_compact in s_compact)):
                return h
        return s

    logic_chain: list[dict] = []
    for idx, item in enumerate((raw_payload.get("logic_chain") if isinstance(raw_payload.get("logic_chain"), list) else [])[:6]):
        if not isinstance(item, dict):
            continue
        t = _clean_latex_str(item.get("title"))
        detail = _clean_latex_str(item.get("detail") or item.get("description"))
        if not t and not detail:
            continue
        logic_chain.append({
            "stage": _clean_stage(item.get("stage"), idx),
            "title": t or detail[:20],
            "detail": detail,
            "section": match_section(item.get("section") or t),
        })

    mindmap: list[dict] = []
    for node in (raw_payload.get("mindmap") if isinstance(raw_payload.get("mindmap"), list) else [])[:6]:
        if not isinstance(node, dict):
            continue
        topic = _clean_latex_str(node.get("topic") or node.get("title"))
        if not topic:
            continue
        children: list[dict] = []
        raw_children = node.get("children") if isinstance(node.get("children"), list) else []
        for ch in raw_children[:6]:
            if isinstance(ch, dict):
                lbl = _clean_latex_str(ch.get("label") or ch.get("title"))
                det = _clean_latex_str(ch.get("detail") or ch.get("summary"))
                if lbl or det:
                    children.append({"label": lbl or det[:18], "detail": det})
            elif isinstance(ch, str) and ch.strip():
                children.append({"label": _clean_latex_str(ch), "detail": ""})
        mindmap.append({
            "topic": topic,
            "section": match_section(node.get("section") or topic),
            "summary": _clean_latex_str(node.get("summary")),
            "children": children,
        })

    proof_cards: list[dict] = []
    for card in (raw_payload.get("proof_cards") if isinstance(raw_payload.get("proof_cards"), list) else [])[:4]:
        if not isinstance(card, dict):
            continue
        title = _clean_latex_str(card.get("title"))
        intuition = _clean_latex_str(card.get("intuition") or card.get("idea"))
        raw_steps = card.get("formal_steps") if isinstance(card.get("formal_steps"), list) else []
        steps = [_clean_latex_str(s) for s in raw_steps if _clean_latex_str(s)][:6]
        if title and (intuition or steps):
            proof_cards.append({
                "section": match_section(card.get("section") or title),
                "title": title,
                "intuition": intuition,
                "formal_steps": steps,
            })

    contrast_cards: list[dict] = []
    for card in (raw_payload.get("contrast_cards") if isinstance(raw_payload.get("contrast_cards"), list) else [])[:4]:
        if not isinstance(card, dict):
            continue
        title = _clean_latex_str(card.get("title"))
        pos = _clean_latex_str(card.get("positive"))
        neg = _clean_latex_str(card.get("counterexample") or card.get("negative"))
        if title and (pos or neg):
            contrast_cards.append({
                "section": match_section(card.get("section") or title),
                "title": title,
                "positive_title": _clean_latex_str(card.get("positive_title") or "成立条件 / 正例"),
                "positive": pos,
                "counter_title": _clean_latex_str(card.get("counter_title") or "边界破坏 / 反例"),
                "counterexample": neg,
            })

    if not logic_chain and not mindmap:
        # Fallback for offline/minimal test payloads
        if isinstance(raw_payload.get("questions"), list):
            return _fallback_from_markdown(fallback_title, markdown)
        raise ValueError("AI 未返回有效的逻辑主线与思维导图数据，请稍后重试。")

    return {
        "title": _clean_latex_str(raw_payload.get("title") or f"{fallback_title} · 逻辑主线与思维导图"),
        "core_thesis": _clean_latex_str(raw_payload.get("core_thesis")),
        "logic_chain": logic_chain,
        "mindmap": mindmap,
        "proof_cards": proof_cards,
        "contrast_cards": contrast_cards,
    }


def generate_lecture_logic_map(
    job_dir: Path,
    *,
    course_id: str,
    recording_id: str,
    title: str,
    markdown: str,
    platform_service: Any,
    force: bool = False,
) -> dict:
    """Generate and persist lecture logic chain, mindmap, and section derivation/contrast cards."""
    cleaned_md = (markdown or "").strip()
    if not cleaned_md:
        raise ValueError("本讲尚未生成课堂笔记，请先生成笔记后再提炼逻辑线与思维导图。")

    md_sha = _sha256_text(cleaned_md)
    if not force:
        existing = load_saved_lecture_logic_map(job_dir, cleaned_md)
        if existing and existing.get("notes_sha256") == md_sha:
            return {**existing, "reused": True, "points_charged": 0}

    if platform_service is None:
        raise ValueError("AI 服务尚未初始化，请稍后重试。")

    headings = _extract_markdown_headings(cleaned_md)
    headings_hint = "、".join(f'"{h}"' for h in headings[:15]) if headings else "按笔记小标题"
    excerpt = cleaned_md[:MAX_NOTE_CHARS_FOR_LOGIC_MAP]

    prompt = (
        "你是北京大学数学与专业课金牌助教。请严格依据下方提供的本讲真实课堂笔记 Markdown，"
        "提炼出帮助学生建立全局直觉与严密推导脉络的【本讲逻辑主线、思维导图与章节精析卡片】"
        "（严禁编造课堂笔记未提及的公式或事实；涉及数学符号请使用标准 LaTeX，并在 JSON 字符串中对反斜杠使用双反斜杠转义，如 `$x \\\\equiv 2 \\\\pmod 3$`）。\n"
        f"本讲现有章节标题包括：{headings_hint}。请确保返回 JSON 中的 `section` 字段优先精确填写上述某个章节标题，以便前端将该卡片穿插定位到对应章节。\n\n"
        "只返回一个合法的 JSON 对象，不要包含任何其他解释文字，格式如下：\n"
        "{\n"
        '  "title": "本讲逻辑主线与思维导图",\n'
        '  "core_thesis": "一句话凝练本讲的核心问题与解决主线（如：从模算术可逆性出发，利用有限域 GF(p)/GF(2^n) 构造现代分组密码的非线性与扩散变换）",\n'
        '  "logic_chain": [\n'
        "    {\n"
        '      "stage": "核心定义",\n'
        '      "title": "节点简短标题",\n'
        '      "detail": "1~2句讲清因果推进关系（为什么从上一步走到这一步，可含 $LaTeX$）",\n'
        '      "section": "对应笔记章节标题"\n'
        "    }\n"
        "  ],\n"
        '  "mindmap": [\n'
        "    {\n"
        '      "topic": "核心模块名称",\n'
        '      "section": "对应笔记章节标题",\n'
        '      "summary": "该模块一句话定位",\n'
        '      "children": [\n'
        '        {"label": "关键概念/定理/性质", "detail": "核心公式或机制要点（可含 $LaTeX$）"}\n'
        "      ]\n"
        "    }\n"
        "  ],\n"
        '  "proof_cards": [\n'
        "    {\n"
        '      "section": "对应笔记章节标题",\n'
        '      "title": "关键定理/公式/机制名称",\n'
        '      "intuition": "【直觉/Proof Idea】用1~2句大白话点破核心思想：为什么要这样构造或变换？关键桥梁是什么？",\n'
        '      "formal_steps": [\n'
        '        "步骤 1：形式化推导或构造起点（含 $LaTeX$）",\n'
        '        "步骤 2：关键代数变形或递推关系",\n'
        '        "步骤 3：得出最终定理结论或算法流程"\n'
        "      ]\n"
        "    }\n"
        "  ],\n"
        '  "contrast_cards": [\n'
        "    {\n"
        '      "section": "对应笔记章节标题",\n'
        '      "title": "易混概念 / 定理前提边界对比",\n'
        '      "positive_title": "成立条件 / 正例",\n'
        '      "positive": "满足何种前提时性质成立及具体正例（如 $\\mathbb{Z}_7$ 中非零元均有乘法逆元）",\n'
        '      "counter_title": "边界破坏 / 反例",\n'
        '      "counterexample": "前提不满足时的具体反例或失效后果（如 $\\mathbb{Z}_8$ 中偶数无乘法逆元，导致加密不可逆）"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "要求：\n"
        "1. `logic_chain` 含 3~5 个环环相扣的推演节点；\n"
        "2. `mindmap` 含 2~4 个主干分支，每个分支 2~4 个子节点；\n"
        "3. `proof_cards` 精选 1~3 个本讲最核心的推导/算法/机制；\n"
        "4. `contrast_cards` 精选 1~2 个本讲老师强调的对比、边界条件或反例。\n\n"
        f"【课程讲次】：{title}\n"
        f"【本讲课堂笔记】：\n{excerpt}"
    )

    def _safe_ctx_token(raw: str, fallback: str) -> str:
        val = str(raw or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_:.-]{1,80}", val):
            return val
        ascii_part = re.sub(r"[^A-Za-z0-9_:.-]+", "_", val).strip("_")[:40]
        digest = _sha256_text(val or fallback)[:10]
        return f"{ascii_part}_{digest}" if ascii_part else f"{fallback}_{digest}"

    safe_cid = _safe_ctx_token(course_id, "course")
    safe_rid = _safe_ctx_token(recording_id, "rec")
    op_id = f"lecture-logic-map:{safe_cid}:{safe_rid}:{md_sha[:12]}"[:160]
    context = {
        "purpose": "lecture-logic-map",
        "course_id": safe_cid,
        "source_id": safe_rid,
        "client_operation_id": op_id,
    }
    response = (
        platform_service.quiz(prompt, operation_context=context)
        if getattr(platform_service, "operation_context_supported", False) is True
        else platform_service.quiz(prompt)
    )
    payload = extract_single_json_payload(response.get("content"))
    normalized = _normalize_logic_map_payload(payload, title or recording_id, cleaned_md)
    charged = response.get("points_charged", 0)
    saved = {
        "schema": "lecture-logic-map-v1",
        "course_id": course_id,
        "recording_id": recording_id,
        "notes_sha256": md_sha,
        **normalized,
    }
    target = job_dir / LOGIC_MAP_FILENAME
    tmp = job_dir / f"{LOGIC_MAP_FILENAME}.tmp"
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(target)
    return {**saved, "reused": False, "points_charged": charged}
