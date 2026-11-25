"""Interactive lecture self-test (随堂自测) built from a lecture's notes.md."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .llm_json import extract_single_json_payload

QUIZ_FILENAME = "lecture_quiz.json"
MAX_NOTE_CHARS_FOR_PROMPT = 14000


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def load_saved_lecture_quiz(job_dir: Path, markdown: str = "") -> dict | None:
    """Read locally cached interactive self-test for a lecture if valid."""
    from types import SimpleNamespace
    from .note_images import _shared_seed_recording_dir

    candidates = [job_dir / QUIZ_FILENAME, job_dir / "lecture-quiz.json"]
    shared_dir = _shared_seed_recording_dir(SimpleNamespace(directory=job_dir, course_dir=job_dir.parent.parent))
    if shared_dir is not None:
        candidates.extend([shared_dir / QUIZ_FILENAME, shared_dir / "lecture-quiz.json"])
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("questions"), list) and data["questions"]:
            return data
    return None


def _normalize_interactive_questions(raw_payload: Any, fallback_title: str) -> list[dict]:
    """Normalize LLM JSON into interactive [{question, options:[{label,text,correct,explanation}]}]."""
    if not isinstance(raw_payload, dict) or not isinstance(raw_payload.get("questions"), list):
        raise ValueError("AI 返回的随堂自测格式不完整，请重试。")

    normalized: list[dict] = []
    for item in raw_payload["questions"]:
        if not isinstance(item, dict):
            continue
        q_text = str(item.get("question") or "").strip()
        if not q_text:
            continue
        raw_opts = item.get("options")
        if isinstance(raw_opts, list) and len(raw_opts) >= 2:
            opts: list[dict] = []
            labels = ("A", "B", "C", "D")
            for idx, opt in enumerate(raw_opts[:4]):
                if not isinstance(opt, dict):
                    continue
                label = str(opt.get("label") or labels[idx]).strip().upper()[:1] or labels[idx]
                text = str(opt.get("text") or "").strip()
                text = re.sub(r"^[A-Da-d][.、:：\s]+", "", text).strip()
                if not text:
                    continue
                correct = bool(opt.get("correct"))
                explain = str(opt.get("explanation") or opt.get("explain") or "").strip()
                if not explain:
                    explain = "完全正确，符合本讲课堂笔记所述。" if correct else "与本讲课堂笔记内容不符，请再核对笔记要点。"
                opts.append({
                    "label": label,
                    "text": text,
                    "correct": correct,
                    "explanation": explain,
                })
            if len(opts) >= 2 and any(o["correct"] for o in opts):
                normalized.append({"question": q_text, "options": opts})
                continue

        # Fallback conversion for offline or legacy {type, question, answer} items
        ans = str(item.get("answer") or "").strip()
        choice_matches = list(re.finditer(r"([A-D])[.、:：]\s*([^A-D]+?)(?=\s+[A-D][.、:：]|$)", q_text))
        if choice_matches:
            stem = q_text[:choice_matches[0].start()].strip()
            opts = []
            for m in choice_matches[:4]:
                lbl = m.group(1).upper()
                txt = m.group(2).strip()
                is_ok = lbl == ans.upper()[:1]
                opts.append({
                    "label": lbl,
                    "text": txt,
                    "correct": is_ok,
                    "explanation": (
                        f"完全正确！标准答案为 {lbl}（{txt}）。"
                        if is_ok
                        else f"不对哦：本题正确选项为 {ans.upper()[:1]}，请结合《{fallback_title}》笔记核对。"
                    ),
                })
            if any(o["correct"] for o in opts):
                normalized.append({"question": stem or q_text, "options": opts})

    if not normalized:
        raise ValueError("AI 未返回有效的随堂自测选择题，请稍后重试。")
    return normalized[:3]


def generate_lecture_quiz(
    job_dir: Path,
    *,
    course_id: str,
    recording_id: str,
    title: str,
    markdown: str,
    platform_service: Any,
    force: bool = False,
) -> dict:
    """Generate and persist 3 interactive multiple-choice self-test questions from notes.md."""
    cleaned_md = (markdown or "").strip()
    if not cleaned_md:
        raise ValueError("本讲尚未生成课堂笔记，请先生成笔记后再开启随堂自测。")

    md_sha = _sha256_text(cleaned_md)
    if not force:
        existing = load_saved_lecture_quiz(job_dir, cleaned_md)
        if existing and existing.get("notes_sha256") == md_sha:
            return {**existing, "reused": True, "points_charged": 0}

    if platform_service is None:
        raise ValueError("AI 服务尚未初始化，请稍后重试。")

    excerpt = cleaned_md[:MAX_NOTE_CHARS_FOR_PROMPT]
    prompt = (
        "你是北京大学课程助教。请严格依据下方提供的本讲真实课堂笔记 Markdown，为学生出 3 道高质量的【随堂自测单选题】"
        "（优先考察老师强调的易错点、核心机制推导、关键对比或具体案例，严禁编造笔记未提及的事实）。\n"
        "只返回一个合法的 JSON 对象，不要包含任何其他解释文字，格式如下：\n"
        "{\n"
        '  "title": "本讲随堂自测",\n'
        '  "questions": [\n'
        "    {\n"
        '      "question": "题干内容（清晰具体，贴合本讲课堂细节）",\n'
        '      "options": [\n'
        '        {"label": "A", "text": "选项A内容（不含A.前缀）", "correct": true, "explanation": "为什么A正确（引用本讲笔记中的具体原理或老师强调点）"},\n'
        '        {"label": "B", "text": "选项B内容（不含B.前缀）", "correct": false, "explanation": "为什么B不对（指出混淆点或反例）"},\n'
        '        {"label": "C", "text": "选项C内容（不含C.前缀）", "correct": false, "explanation": "为什么C不对（指出混淆点或反例）"}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "要求：每道题固定 3 个选项（A、B、C），且有且仅有 1 个选项的 correct 为 true，其余为 false；"
        "3 道题的正确选项位置请尽量错开（例如分别在 A、B、C）。\n\n"
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
    op_id = f"lecture-quiz:{safe_cid}:{safe_rid}:{md_sha[:12]}"[:160]
    context = {
        "purpose": "lecture-self-test",
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
    questions = _normalize_interactive_questions(payload, title or recording_id)
    charged = response.get("points_charged", 0)
    saved = {
        "schema": "lecture-self-test-v1",
        "course_id": course_id,
        "recording_id": recording_id,
        "title": str(payload.get("title") or f"{title} · 随堂自测").strip(),
        "notes_sha256": md_sha,
        "questions": questions,
    }
    target = job_dir / QUIZ_FILENAME
    tmp = job_dir / f"{QUIZ_FILENAME}.tmp"
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(target)
    return {**saved, "reused": False, "points_charged": charged}
