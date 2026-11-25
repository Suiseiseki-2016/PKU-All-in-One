"""Blackboard online assessments (tests/quizzes), live grade/submission inspection, and course material extraction."""

from __future__ import annotations

import io
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader

from .catalog_schema import ASSESSMENT_KINDS
from .grades import course_grades
from .materials import ANNOUNCEMENT_PAGE, ASSIGNMENT_KINDS, LIST_CONTENT, _top_level_areas
from .submit import inspect_submission

LAUNCH_ASSESSMENT = "/webapps/blackboard/content/launchAssessment.jsp"
SAVE_ATTEMPT = "/webapps/assessment/do/take/saveAttempt"

_CONTENT_ID_RE = re.compile(r"(_\d+_\d+)")
_TAKE_PAGE_ID_RE = re.compile(r"assessment\.takePageId\s*=\s*(\d+)")
_LAUNCH_JSP_RE = re.compile(r"(/webapps/assessment/take/launch\.jsp\?[^'\"\s<>]+)")

_TRUE_ALIASES = {"true", "t", "对", "正确", "是", "yes", "y", "√", "✓", "1"}
_FALSE_ALIASES = {"false", "f", "错", "错误", "否", "no", "n", "×", "✗", "0"}


def _collapse(value: str) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _discover_menu_areas(client: httpx.Client, course_id: str) -> list[dict[str, str]]:
    """Fallback to scraping left-nav content folders when REST /contents is unavailable."""
    try:
        resp = client.get(
            ANNOUNCEMENT_PAGE,
            params={
                "method": "search",
                "context": "course_entry",
                "course_id": course_id,
                "handle": "announcements_entry",
                "mode": "view",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    areas: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("#courseMenuPalette_contents a[href]"):
        href = str(anchor.get("href") or "")
        if "listContent.jsp" not in href:
            continue
        match = re.search(r"content_id=(_\d+_\d+)", href)
        if not match:
            continue
        cid = match.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        areas.append({"id": cid, "title": _collapse(anchor.get_text(" ", strip=True)) or cid})
    return areas


def walk_course_content_items(
    client: httpx.Client,
    course_id: str,
    *,
    max_depth: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    """Walk a course's Blackboard content folders to discover assignments, online assessments, and files."""
    try:
        areas = _top_level_areas(client, course_id)
    except Exception:
        areas = []
    if not areas:
        areas = _discover_menu_areas(client, course_id)

    written_assignments: list[dict[str, Any]] = []
    online_assessments: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    visited_folders: set[str] = set()
    seen_assignments: set[str] = set()
    seen_assessments: set[str] = set()
    seen_materials: set[str] = set()

    def _walk(folder_id: str, parent_path: str, depth: int) -> None:
        if depth > max_depth or not folder_id or folder_id in visited_folders:
            return
        visited_folders.add(folder_id)
        try:
            resp = client.get(
                LIST_CONTENT,
                params={"course_id": course_id, "content_id": folder_id},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        container = soup.find("ul", id="content_listContainer")
        if not isinstance(container, Tag):
            return

        for li in container.find_all("li", recursive=False):
            if not isinstance(li, Tag) or "liItem" not in (li.get("class") or []):
                continue
            heading = li.find(["h3", "h4"])
            title = _collapse(heading.get_text(" ", strip=True)) if heading else ""
            if not title:
                continue

            icon = li.find("img", alt=True)
            kind = _collapse(icon["alt"]) if icon else ""
            li_id_match = _CONTENT_ID_RE.search(str(li.get("id") or ""))
            own_id = li_id_match.group(1) if li_id_match else ""

            details = li.find("div", class_=lambda v: bool(v) and ("details" in v or "vtbegenerated" in v))
            description = _collapse(details.get_text(" ", strip=True)) if details else ""

            child_folder_id = ""
            upload_content_id = ""
            assessment_content_id = ""
            assessment_href = ""
            item_attachments: list[dict[str, str]] = []

            for anchor in li.find_all("a", href=True):
                href = str(anchor.get("href") or "").strip()
                label = _collapse(anchor.get_text(" ", strip=True))
                if "listContent.jsp" in href:
                    m = re.search(r"content_id=(_\d+_\d+)", href)
                    if m:
                        child_folder_id = m.group(1)
                elif "uploadAssignment" in href:
                    m = re.search(r"content_id=(_\d+_\d+)", href)
                    if m:
                        upload_content_id = m.group(1)
                elif "launchAssessment.jsp" in href:
                    m = re.search(r"content_id=(_\d+_\d+)", href)
                    if m:
                        assessment_content_id = m.group(1)
                    assessment_href = href
                elif "/bbcswebdav/" in href:
                    fname = label
                    if not fname or not Path(fname).suffix:
                        url_path = unquote(urlsplit(href).path)
                        tail = Path(url_path).name
                        if tail and Path(tail).suffix:
                            fname = tail
                    fname = fname or title or "attachment.pdf"
                    item_attachments.append(
                        {
                            "title": title,
                            "filename": fname,
                            "url": href,
                            "section": parent_path,
                            "description": description,
                        }
                    )
                    if href not in seen_materials:
                        seen_materials.add(href)
                        materials.append(item_attachments[-1])

            if upload_content_id or kind in ASSIGNMENT_KINDS:
                cid = upload_content_id or own_id
                if cid and cid not in seen_assignments:
                    seen_assignments.add(cid)
                    written_assignments.append(
                        {
                            "content_id": cid,
                            "title": title,
                            "section": parent_path,
                            "instructions": description,
                            "attachments": item_attachments,
                        }
                    )

            if assessment_content_id or kind in ASSESSMENT_KINDS:
                cid = assessment_content_id or own_id
                if cid and cid not in seen_assessments:
                    seen_assessments.add(cid)
                    launch_url = assessment_href or (
                        f"{LAUNCH_ASSESSMENT}?course_id={course_id}&content_id={cid}&mode=view"
                    )
                    online_assessments.append(
                        {
                            "content_id": cid,
                            "title": title,
                            "section": parent_path,
                            "description": description,
                            "launch_url": launch_url,
                        }
                    )

            if child_folder_id:
                next_path = f"{parent_path}/{title}" if parent_path else title
                _walk(child_folder_id, next_path, depth + 1)

    for area in areas:
        _walk(str(area.get("id") or ""), str(area.get("title") or ""), 0)

    return {
        "written_assignments": written_assignments,
        "online_assessments": online_assessments,
        "materials": materials,
    }


def _match_grade_entry(title: str, grades_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    norm_title = _collapse(title).lower()
    if not norm_title:
        return None
    for g in grades_list:
        gt = _collapse(str(g.get("title") or "")).lower()
        if gt == norm_title:
            return g
    for g in grades_list:
        gt = _collapse(str(g.get("title") or "")).lower()
        if gt and (gt in norm_title or norm_title in gt):
            return g
    return None


def _serialize_grade_records(raw_grades: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in raw_grades:
        col_name = getattr(g, "column_name", None) or getattr(g, "title", "")
        out.append(
            {
                "title": col_name,
                "column_name": col_name,
                "course_name": getattr(g, "course_name", ""),
                "score": getattr(g, "score", None),
                "possible": getattr(g, "possible", None),
            }
        )
    return out


def inspect_course_grades_and_submissions(
    client: httpx.Client,
    course_id: str,
    course_name: str = "",
    *,
    synced_assignments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch live Gradebook scores, written assignment submission receipts, and online assessments."""
    raw_grades = course_grades(client, course_id, course_name or course_id)
    grades_serialized = _serialize_grade_records(raw_grades)

    discovered = walk_course_content_items(client, course_id)
    written_list = list(discovered["written_assignments"])
    seen_cids = {str(a.get("content_id") or "") for a in written_list if a.get("content_id")}

    for sa in synced_assignments or []:
        cid = str(sa.get("content_id") or "").strip()
        if cid and cid not in seen_cids:
            seen_cids.add(cid)
            written_list.append(
                {
                    "content_id": cid,
                    "title": str(sa.get("title") or ""),
                    "section": "课程作业",
                    "instructions": str(sa.get("instructions") or ""),
                    "due_at": str(sa.get("due_at") or sa.get("due_at_label") or ""),
                    "attachments": list(sa.get("attachments") or []),
                }
            )
        elif cid:
            for existing in written_list:
                if existing.get("content_id") == cid and not existing.get("due_at"):
                    existing["due_at"] = str(sa.get("due_at") or sa.get("due_at_label") or "")

    for item in written_list:
        cid = str(item.get("content_id") or "").strip()
        if cid:
            try:
                sub_state = inspect_submission(client, course_id, cid)
                item["state"] = sub_state.get("state", "unknown")
                item["submitted"] = sub_state.get("state") == "review_history"
                item["review_url"] = sub_state.get("review_url") or sub_state.get("form_url") or ""
            except Exception as exc:
                item["state"] = "error"
                item["submitted"] = False
                item["error"] = str(exc)
        matched_grade = _match_grade_entry(str(item.get("title") or ""), grades_serialized)
        if matched_grade is not None:
            item["grade"] = matched_grade
            if matched_grade.get("score") not in (None, "", "-"):
                item["submitted"] = True

    online_list = list(discovered["online_assessments"])
    for assess in online_list:
        matched_grade = _match_grade_entry(str(assess.get("title") or ""), grades_serialized)
        if matched_grade is not None:
            assess["grade"] = matched_grade
            assess["submitted"] = matched_grade.get("score") not in (None, "", "-")
        else:
            assess["grade"] = None
            assess["submitted"] = False

    return {
        "ok": True,
        "course": course_name or course_id,
        "course_id": course_id,
        "grades": grades_serialized,
        "written_assignments": written_list,
        "online_assessments": online_list,
        "materials": discovered["materials"],
    }


def _resolve_assessment_target(
    client: httpx.Client,
    course_id: str,
    assessment_id_or_title: str,
) -> tuple[str, str]:
    target = (assessment_id_or_title or "").strip()
    if not target:
        raise ValueError("assessment_id is required (e.g. '_1726646_1' or '自测练习二')")
    if _CONTENT_ID_RE.fullmatch(target):
        return target, target

    discovered = walk_course_content_items(client, course_id)
    q = target.lower()
    for item in discovered["online_assessments"]:
        cid = str(item.get("content_id") or "")
        title = str(item.get("title") or "")
        if q in {cid.lower(), title.lower()} or q in title.lower():
            return cid, title

    m = _CONTENT_ID_RE.search(target)
    if m:
        return m.group(1), target
    raise ValueError(f"Online assessment not found in course {course_id}: {assessment_id_or_title}")


def _extract_launch_jsp_url(html: str, current_url: str, course_id: str, content_id: str) -> str:
    if "/webapps/assessment/take/launch.jsp" in current_url:
        return current_url
    m = _LAUNCH_JSP_RE.search(html)
    if m:
        return m.group(1).replace("&amp;", "&")
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if "/webapps/assessment/take/launch.jsp" in href:
            return href
    ca_match = re.search(r"course_assessment_id=(_\d+_\d+)", html)
    if ca_match:
        ca_id = ca_match.group(1)
        return (
            f"/webapps/assessment/take/launch.jsp?course_assessment_id={ca_id}"
            f"&course_id={course_id}&content_id={content_id}&step=null"
        )
    raise ValueError(
        f"Could not extract assessment launch.jsp URL from launchAssessment.jsp for {content_id}."
    )


def _parse_assessment_launch_html(
    html: str,
    *,
    course_id: str,
    content_id: str,
    fallback_title: str = "",
    launch_url: str = "",
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    page_title_node = soup.find("span", id="pageTitleText") or soup.find("h1")
    title = _collapse(page_title_node.get_text(" ", strip=True)) if page_title_node else ""
    if not title or title.startswith("进行测试"):
        title = re.sub(r"^进行测试\s*[:：]\s*", "", title).strip() or fallback_title or content_id

    take_page_match = _TAKE_PAGE_ID_RE.search(html)
    take_page_id = take_page_match.group(1) if take_page_match else "1"

    form = soup.find("form", attrs={"name": "saveAttemptForm"}) or soup.find("form", id="bb-take-assessment")
    hidden_fields: dict[str, str] = {}
    search_root: Tag = form if isinstance(form, Tag) else soup
    for h_inp in search_root.find_all("input", attrs={"type": "hidden"}):
        name = str(h_inp.get("name") or "").strip()
        if name:
            hidden_fields[name] = str(h_inp.get("value") or "")

    course_assessment_id = hidden_fields.get("course_assessment_id") or ""
    if not course_assessment_id and launch_url:
        qs = parse_qs(urlsplit(launch_url).query)
        if qs.get("course_assessment_id"):
            course_assessment_id = qs["course_assessment_id"][0]

    questions: list[dict[str, Any]] = []
    q_divs = soup.select("div.takeQuestionDiv")
    for idx, q_div in enumerate(q_divs, start=1):
        h3 = q_div.find(["h3", "h4"])
        q_title = _collapse(h3.get_text(" ", strip=True)) if h3 else f"问题 {idx}"
        pts_node = q_div.find(class_="points")
        pts_text = _collapse(pts_node.get_text(" ", strip=True)) if pts_node else ""

        vtb_nodes = q_div.select(".vtbegenerated")
        prompt_text = ""
        if vtb_nodes:
            prompt_text = _collapse(vtb_nodes[0].get_text(" ", strip=True))

        inputs = [
            inp
            for inp in q_div.find_all(["input", "textarea"])
            if str(inp.get("type") or "").lower() not in {"hidden", "button", "submit"}
        ]

        options: list[dict[str, Any]] = []
        input_name = ""
        question_type = "text"

        choice_inputs = [
            inp
            for inp in inputs
            if str(inp.get("type") or "").lower() in {"radio", "checkbox"}
            and not str(inp.get("name") or "").endswith("empty")
        ]

        if choice_inputs:
            first_type = str(choice_inputs[0].get("type") or "").lower()
            input_name = str(choice_inputs[0].get("name") or "")
            raw_vals = [str(inp.get("value") or "").lower() for inp in choice_inputs]
            if first_type == "checkbox":
                question_type = "multiple_choice"
            elif set(raw_vals) <= {"true", "false"} or input_name.startswith("tf-ans-"):
                question_type = "true_false"
            else:
                question_type = "single_choice"

            for opt_idx, inp in enumerate(choice_inputs):
                inp_id = str(inp.get("id") or "")
                val = str(inp.get("value") or "")
                checked = inp.has_attr("checked")
                label_text = ""
                if inp_id:
                    lbl = q_div.find("label", attrs={"for": inp_id})
                    if lbl:
                        label_text = _collapse(lbl.get_text(" ", strip=True))
                if not label_text:
                    parent_tr = inp.find_parent(["tr", "p", "li", "td"])
                    if parent_tr:
                        label_text = _collapse(parent_tr.get_text(" ", strip=True))

                if question_type == "true_false":
                    letter = "对" if val.lower() == "true" else "错"
                    if not label_text:
                        label_text = letter
                else:
                    letter = chr(ord("A") + opt_idx) if opt_idx < 26 else str(opt_idx)
                    label_text = re.sub(rf"^{letter}\s*[.．、:：]\s*", "", label_text).strip() or label_text

                options.append(
                    {
                        "index": opt_idx,
                        "letter": letter,
                        "input_name": str(inp.get("name") or input_name),
                        "value": val,
                        "label": label_text,
                        "checked": checked,
                    }
                )
        elif inputs:
            inp = inputs[0]
            input_name = str(inp.get("name") or "")
            val = inp.get_text() if inp.name == "textarea" else str(inp.get("value") or "")
            options.append(
                {
                    "index": 0,
                    "letter": "TEXT",
                    "input_name": input_name,
                    "value": val,
                    "label": val,
                    "checked": bool(val),
                }
            )

        if not prompt_text:
            full_q_text = _collapse(q_div.get_text(" ", strip=True))
            prompt_text = full_q_text[:400]

        checked_opts = [opt for opt in options if opt["checked"]]
        if question_type == "multiple_choice":
            saved_answer = {
                "letters": [opt["letter"] for opt in checked_opts],
                "values": [opt["value"] for opt in checked_opts],
                "labels": [opt["label"] for opt in checked_opts],
            }
        elif checked_opts:
            saved_answer = {
                "letter": checked_opts[0]["letter"],
                "value": checked_opts[0]["value"],
                "label": checked_opts[0]["label"],
            }
        else:
            saved_answer = None

        questions.append(
            {
                "number": idx,
                "question_id": str(q_div.get("id") or ""),
                "title": q_title,
                "points": pts_text,
                "question_type": question_type,
                "input_name": input_name,
                "prompt": prompt_text,
                "options": options,
                "saved_answer": saved_answer,
            }
        )

    return {
        "ok": True,
        "course_id": course_id,
        "content_id": content_id,
        "course_assessment_id": course_assessment_id,
        "title": title,
        "take_page_id": take_page_id,
        "launch_url": launch_url,
        "question_count": len(questions),
        "questions": questions,
        "hidden_fields": hidden_fields,
    }


def _open_online_assessment_session(
    client: httpx.Client,
    course_id: str,
    assessment_id_or_title: str,
) -> tuple[dict[str, Any], str]:
    content_id, matched_title = _resolve_assessment_target(client, course_id, assessment_id_or_title)
    r1 = client.get(
        LAUNCH_ASSESSMENT,
        params={"course_id": course_id, "content_id": content_id, "mode": "view"},
        follow_redirects=True,
    )
    r1.raise_for_status()
    launch_url = _extract_launch_jsp_url(r1.text, str(r1.url), course_id, content_id)

    if str(r1.url) == launch_url and "takeQuestionDiv" in r1.text:
        r2 = r1
    else:
        r2 = client.get(launch_url, headers={"Referer": str(r1.url)}, follow_redirects=True)
        r2.raise_for_status()

    parsed = _parse_assessment_launch_html(
        r2.text,
        course_id=course_id,
        content_id=content_id,
        fallback_title=matched_title,
        launch_url=str(r2.url),
    )
    return parsed, str(r2.url)


def fetch_online_assessment(
    client: httpx.Client,
    course_id: str,
    assessment_id_or_title: str,
) -> dict[str, Any]:
    """Open a Blackboard online test/assessment and return all parsed questions, options, and saved answers."""
    parsed, _ = _open_online_assessment_session(client, course_id, assessment_id_or_title)
    result = dict(parsed)
    result.pop("hidden_fields", None)
    return result


def _normalize_question_key(raw_key: str) -> int | str:
    key = str(raw_key or "").strip()
    if key.isdigit():
        return int(key)
    m = re.fullmatch(r"(?:q|question|问题|第)?\s*(\d+)\s*(?:题)?", key, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    return key


def _resolve_single_option(question: dict[str, Any], raw_val: Any) -> dict[str, Any]:
    options = question.get("options") or []
    q_type = question.get("question_type")
    if q_type == "text":
        val_str = str(raw_val or "")
        return {
            "letter": "TEXT",
            "input_name": question.get("input_name") or "",
            "value": val_str,
            "label": val_str,
        }

    if isinstance(raw_val, bool):
        s_val = "true" if raw_val else "false"
    else:
        s_val = str(raw_val or "").strip()
    s_lower = s_val.lower()

    if q_type == "true_false":
        if s_lower in _TRUE_ALIASES:
            target_val = "true"
        elif s_lower in _FALSE_ALIASES:
            target_val = "false"
        else:
            target_val = s_lower
        for opt in options:
            if str(opt.get("value") or "").lower() == target_val:
                return opt
        raise ValueError(
            f"Invalid true/false answer {raw_val!r} for question {question.get('number')} "
            f"(use '对'/'错' or 'true'/'false')."
        )

    cleaned_letter = re.sub(r"^(?:选|option\s*)", "", s_val, flags=re.IGNORECASE).strip(" .．、:：()")
    for opt in options:
        if cleaned_letter.upper() == str(opt.get("letter") or "").upper():
            return opt
    for opt in options:
        if s_val == str(opt.get("value") or ""):
            return opt
    for opt in options:
        lbl = str(opt.get("label") or "").strip()
        if lbl and (s_val == lbl or s_lower == lbl.lower()):
            return opt

    valid_letters = [str(o.get("letter")) for o in options]
    valid_vals = [str(o.get("value")) for o in options]
    raise ValueError(
        f"Invalid answer {raw_val!r} for question {question.get('number')}: "
        f"expected one of {valid_letters} (or raw values {valid_vals})."
    )


def _map_answers_to_form_fields(
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[int]]:
    by_number = {int(q["number"]): q for q in questions}
    by_input_name = {str(q["input_name"]): q for q in questions if q.get("input_name")}

    matched_for_question: dict[int, Any] = {}
    for raw_k, raw_v in (answers or {}).items():
        norm_k = _normalize_question_key(str(raw_k))
        if isinstance(norm_k, int) and norm_k in by_number:
            matched_for_question[norm_k] = raw_v
        elif str(raw_k) in by_input_name:
            q_num = int(by_input_name[str(raw_k)]["number"])
            matched_for_question[q_num] = raw_v
        else:
            raise ValueError(f"Unknown question key in answers: {raw_k!r}")

    form_fields: dict[str, Any] = {}
    preview: list[dict[str, Any]] = []
    unanswered: list[int] = []

    for q in questions:
        q_num = int(q["number"])
        inp_name = str(q.get("input_name") or "")
        if q_num not in matched_for_question:
            if q.get("saved_answer"):
                saved = q["saved_answer"]
                if q.get("question_type") == "multiple_choice":
                    form_fields[inp_name] = list(saved.get("values") or [])
                else:
                    form_fields[inp_name] = str(saved.get("value") or "")
                preview.append(
                    {
                        "number": q_num,
                        "input_name": inp_name,
                        "prompt": str(q.get("prompt") or "")[:120],
                        "selected": saved,
                        "source": "previously_saved",
                    }
                )
            else:
                unanswered.append(q_num)
            continue

        raw_ans = matched_for_question[q_num]
        if q.get("question_type") == "multiple_choice":
            if isinstance(raw_ans, str):
                parts = [p.strip() for p in re.split(r"[,，;；\s]+", raw_ans) if p.strip()]
                if len(parts) == 1 and re.fullmatch(r"[A-Za-z]{2,}", parts[0]):
                    parts = list(parts[0])
            elif isinstance(raw_ans, (list, tuple, set)):
                parts = list(raw_ans)
            else:
                parts = [raw_ans]
            resolved_opts = [_resolve_single_option(q, p) for p in parts]
            form_fields[inp_name] = [str(o["value"]) for o in resolved_opts]
            preview.append(
                {
                    "number": q_num,
                    "input_name": inp_name,
                    "prompt": str(q.get("prompt") or "")[:120],
                    "selected": {
                        "letters": [o["letter"] for o in resolved_opts],
                        "values": [o["value"] for o in resolved_opts],
                        "labels": [o["label"] for o in resolved_opts],
                    },
                    "source": "provided",
                }
            )
        else:
            opt = _resolve_single_option(q, raw_ans)
            form_fields[inp_name] = str(opt["value"])
            preview.append(
                {
                    "number": q_num,
                    "input_name": inp_name,
                    "prompt": str(q.get("prompt") or "")[:120],
                    "selected": {
                        "letter": opt["letter"],
                        "value": opt["value"],
                        "label": opt["label"],
                    },
                    "source": "provided",
                }
            )

    return form_fields, preview, unanswered


def save_or_submit_online_assessment(
    client: httpx.Client,
    course_id: str,
    assessment_id_or_title: str,
    answers: dict[str, Any],
    *,
    confirm_submit: bool = False,
    course_name: str = "",
) -> dict[str, Any]:
    """Save draft answers (confirm_submit=False) or finally submit (confirm_submit=True) a Blackboard online assessment."""
    if not answers or not isinstance(answers, dict):
        raise ValueError("answers must be a non-empty dict mapping question numbers or input names to choices.")

    parsed, launch_resp_url = _open_online_assessment_session(client, course_id, assessment_id_or_title)
    content_id = str(parsed["content_id"])
    course_assessment_id = str(parsed["course_assessment_id"])
    take_page_id = str(parsed["take_page_id"])
    hidden_fields = dict(parsed.get("hidden_fields") or {})
    questions = list(parsed.get("questions") or [])

    if not questions:
        raise ValueError(f"No questions found on assessment launch page for {assessment_id_or_title}.")

    form_answers, answer_preview, unanswered = _map_answers_to_form_fields(questions, answers)

    save_params = {
        "saveSequence": "0",
        "takePageId": take_page_id,
        "course_assessment_id": course_assessment_id,
        "course_id": course_id,
        "content_id": content_id,
    }
    draft_data = dict(hidden_fields)
    draft_data["method"] = "saveAllAnswers"
    draft_data["save_and_submit"] = ""
    draft_data.update(form_answers)

    r_save = client.post(
        SAVE_ATTEMPT,
        params=save_params,
        data=draft_data,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": launch_resp_url,
        },
    )
    r_save.raise_for_status()
    save_text = r_save.text.strip()

    if not confirm_submit:
        verified_saved_count = len(answer_preview)
        try:
            r_verify = client.get(launch_resp_url, headers={"Referer": launch_resp_url}, follow_redirects=True)
            if r_verify.status_code == 200 and "takeQuestionDiv" in r_verify.text:
                re_parsed = _parse_assessment_launch_html(
                    r_verify.text,
                    course_id=course_id,
                    content_id=content_id,
                    fallback_title=str(parsed.get("title") or ""),
                    launch_url=launch_resp_url,
                )
                verified_saved_count = sum(
                    1 for q in re_parsed.get("questions") or [] if q.get("saved_answer") is not None
                )
        except Exception:
            pass

        return {
            "ok": True,
            "status": "draft_saved_ready_to_confirm",
            "message": (
                "All answers have been saved as a draft on Blackboard (NOT finally submitted yet). "
                "Please review answer_preview and unanswered_questions with the student, then call "
                "submit_online_assessment with confirm_submit=True to finalize submission."
            ),
            "course": course_name or course_id,
            "course_id": course_id,
            "content_id": content_id,
            "course_assessment_id": course_assessment_id,
            "title": parsed.get("title") or content_id,
            "save_response": save_text,
            "total_questions": len(questions),
            "answered_count": len(answer_preview),
            "verified_saved_count": verified_saved_count,
            "unanswered_questions": unanswered,
            "answer_preview": answer_preview,
        }

    submit_data = dict(hidden_fields)
    submit_data["method"] = "notajax"
    submit_data["save_and_submit"] = "true"
    submit_data["bottom_保存并提交"] = "保存并提交"
    submit_data.update(form_answers)

    r_sub = client.post(
        SAVE_ATTEMPT,
        params=save_params,
        data=submit_data,
        files={"_force_multipart": (None, "")},
        headers={"Referer": launch_resp_url},
        follow_redirects=True,
    )
    r_sub.raise_for_status()
    sub_url = str(r_sub.url)

    grades_serialized: list[dict[str, Any]] = []
    matched_grade: dict[str, Any] | None = None
    try:
        raw_grades = course_grades(client, course_id, course_name or course_id)
        grades_serialized = _serialize_grade_records(raw_grades)
        matched_grade = _match_grade_entry(str(parsed.get("title") or ""), grades_serialized)
    except Exception:
        pass

    return {
        "ok": True,
        "status": "submitted",
        "course": course_name or course_id,
        "course_id": course_id,
        "content_id": content_id,
        "course_assessment_id": course_assessment_id,
        "title": parsed.get("title") or content_id,
        "submitted_url": sub_url,
        "save_response": save_text,
        "total_questions": len(questions),
        "answered_count": len(answer_preview),
        "unanswered_questions": unanswered,
        "answer_preview": answer_preview,
        "grade": matched_grade,
        "grades": grades_serialized,
    }


_LEGACY_DOUBLE_EXT_SUFFIXES = {
    ".zip", ".docx", ".pptx", ".xlsx", ".rar", ".7z", ".txt", ".md", ".doc", ".ppt", ".xls",
}

_TEXT_MATERIAL_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".tex", ".py", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".java",
    ".js", ".ts", ".sh", ".html", ".css", ".sql", ".ini", ".cfg", ".conf", ".log",
}

_SPECIAL_TEXT_NAMES = {"readme", "makefile", "dockerfile", "license"}


def _is_legacy_non_pdf_shadow(fp: Path) -> bool:
    """Return True if a file named *.pdf actually has a double-extension stem (e.g. .zip.pdf) and non-PDF bytes."""
    if fp.suffix.lower() != ".pdf":
        return False
    inner_suffix = Path(fp.stem).suffix.lower()
    if inner_suffix not in _LEGACY_DOUBLE_EXT_SUFFIXES:
        return False
    try:
        with fp.open("rb") as fh:
            return fh.read(5) != b"%PDF-"
    except OSError:
        return False


def _list_local_materials(course_dir: Path) -> list[dict[str, Any]]:
    """List local course materials (materials/* plus non-hidden PDFs outside keyframes/)."""
    items: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    if not course_dir.is_dir():
        return items

    def _add_file(fp: Path) -> None:
        if not fp.is_file():
            return
        try:
            rel = fp.relative_to(course_dir)
        except ValueError:
            return
        if any(part.startswith(".") for part in rel.parts):
            return
        if fp.name.lower() == "index.json" and "materials" in rel.parts:
            return
        if "keyframes" in rel.parts and fp.parent.name != "materials":
            return
        if _is_legacy_non_pdf_shadow(fp):
            return
        resolved = fp.resolve()
        if resolved in seen_paths:
            return
        seen_paths.add(resolved)
        items.append(
            {
                "title": fp.stem,
                "filename": fp.name,
                "relative_path": rel.as_posix(),
                "local_path": str(fp),
                "bytes": fp.stat().st_size,
                "source": "local",
            }
        )

    materials_dir = course_dir / "materials"
    if materials_dir.is_dir():
        for fp in sorted(materials_dir.rglob("*")):
            _add_file(fp)
    for fp in sorted(course_dir.rglob("*.pdf")):
        _add_file(fp)
    items.sort(key=lambda row: row["relative_path"].lower())
    return items


def _resolve_download_filename(resp: httpx.Response, matched_remote: dict[str, Any]) -> str:
    """Resolve a real, sanitized filename from HTTP headers, URL, remote metadata, or magic bytes."""
    candidate = ""
    cd = str(resp.headers.get("content-disposition") or "")
    if cd:
        m_star = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\r\n]+)", cd)
        if m_star:
            candidate = unquote(m_star.group(1).strip().strip('"').strip("'"))
        else:
            m_plain = re.search(r'filename\s*=\s*"([^"\r\n]+)"', cd) or re.search(
                r"filename\s*=\s*([^;\r\n]+)", cd
            )
            if m_plain:
                candidate = unquote(m_plain.group(1).strip().strip('"').strip("'"))
        if candidate:
            candidate = Path(candidate.replace("\\", "/")).name

    if not candidate or not Path(candidate).suffix:
        url_tail = Path(unquote(urlsplit(str(resp.url)).path)).name
        if url_tail and Path(url_tail).suffix:
            candidate = url_tail

    if not candidate or not Path(candidate).suffix:
        remote_name = str(matched_remote.get("filename") or "").strip()
        if remote_name and (Path(remote_name).suffix or not candidate):
            candidate = Path(remote_name.replace("\\", "/")).name

    if not candidate:
        candidate = str(matched_remote.get("title") or "material").strip() or "material"

    if not Path(candidate).suffix:
        raw = resp.content or b""
        ctype = (resp.headers.get("content-type") or "").lower()
        inferred_ext = ""
        if raw.startswith(b"%PDF") or "application/pdf" in ctype:
            inferred_ext = ".pdf"
        elif raw.startswith(b"PK\x03\x04"):
            if "wordprocessingml" in ctype:
                inferred_ext = ".docx"
            elif "presentationml" in ctype:
                inferred_ext = ".pptx"
            elif "spreadsheetml" in ctype:
                inferred_ext = ".xlsx"
            else:
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                        names = set(zf.namelist())
                    if "word/document.xml" in names:
                        inferred_ext = ".docx"
                    elif "ppt/presentation.xml" in names:
                        inferred_ext = ".pptx"
                    elif "xl/workbook.xml" in names:
                        inferred_ext = ".xlsx"
                    else:
                        inferred_ext = ".zip"
                except Exception:
                    inferred_ext = ".zip"
        elif raw.startswith(b"Rar!"):
            inferred_ext = ".rar"
        elif "text/markdown" in ctype:
            inferred_ext = ".md"
        elif "text/plain" in ctype:
            inferred_ext = ".txt"
        if inferred_ext:
            candidate += inferred_ext

    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", candidate).strip(" .")
    return safe_name or "material"


def _match_material_entry(
    entries: list[dict[str, Any]],
    q_lower: str,
    exact_fields: tuple[str, ...],
    substr_fields: tuple[str, ...],
) -> dict[str, Any] | None:
    for item in entries:
        for field in exact_fields:
            if str(item.get(field) or "").strip().lower() == q_lower:
                return item
    for item in entries:
        for field in substr_fields:
            if q_lower in str(item.get(field) or "").lower():
                return item
    return None


def fetch_course_material_file(
    course_dir: Path,
    course_item: dict[str, Any],
    material_query: str,
    *,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> Path:
    """Resolve and return a local course material file Path, downloading from Blackboard if needed."""
    q = (material_query or "").strip()
    if not q:
        raise ValueError("material query must not be empty.")
    q_lower = q.lower()
    course_id = str(course_item.get("course_id") or "")
    course_name = str(course_item.get("name") or course_dir.name)
    materials_dir = course_dir / "materials"

    local_materials = _list_local_materials(course_dir)
    local_hit = _match_material_entry(
        local_materials,
        q_lower,
        ("filename", "title", "relative_path"),
        ("filename", "title"),
    )
    if local_hit is not None:
        return Path(local_hit["local_path"])

    if client_factory is not None and course_id:
        client = client_factory()
        try:
            matched_remote: dict[str, Any] | None = None
            if q.startswith("/bbcswebdav/") or q.startswith("https://course.pku.edu.cn/bbcswebdav/"):
                url_path = unquote(urlsplit(q).path)
                fname = Path(url_path).name or "material"
                matched_remote = {
                    "title": Path(fname).stem,
                    "filename": fname,
                    "url": q,
                    "section": "direct_url",
                }
            else:
                discovered = walk_course_content_items(client, course_id)
                remote_materials = discovered["materials"]
                matched_remote = _match_material_entry(
                    remote_materials,
                    q_lower,
                    ("filename", "title"),
                    ("filename", "title", "url"),
                )

            if matched_remote is not None:
                resp = client.get(str(matched_remote["url"]), follow_redirects=True)
                resp.raise_for_status()
                safe_name = _resolve_download_filename(resp, matched_remote)
                target_file = materials_dir / safe_name
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(resp.content)
                return target_file
        finally:
            client.close()

    raise FileNotFoundError(
        f"Course material {material_query!r} not found in {course_name} (checked local materials and live Blackboard)."
    )


def _decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw_bytes = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gb18030"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_docx_text(raw: bytes) -> str:
    try:
        import docx  # type: ignore

        doc = docx.Document(io.BytesIO(raw))
        parts: list[str] = []
        for para in doc.paragraphs:
            txt = para.text.strip()
            if txt:
                parts.append(txt)
        for tbl in doc.tables:
            for row in tbl.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        if parts:
            return "\n".join(parts)
    except Exception:
        pass

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        root = ET.fromstring(zf.read("word/document.xml"))
    paras: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("}p"):
            runs = [
                sub.text
                for sub in elem.iter()
                if sub.tag.endswith("}t") and sub.text
            ]
            line = "".join(runs).strip()
            if line:
                paras.append(line)
    return "\n".join(paras)


def _extract_pptx_slides(raw: bytes) -> list[str]:
    try:
        import pptx  # type: ignore

        prs = pptx.Presentation(io.BytesIO(raw))
        slides: list[str] = []
        for slide in prs.slides:
            chunks: list[str] = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    txt = shape.text_frame.text.strip()
                    if txt:
                        chunks.append(txt)
            slides.append("\n".join(chunks))
        return slides
    except Exception:
        pass

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        slide_names = [
            name
            for name in zf.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ]
        slide_names.sort(key=lambda n: int(re.search(r"slide(\d+)\.xml$", n).group(1)))  # type: ignore[union-attr]
        slides_out: list[str] = []
        for name in slide_names:
            root = ET.fromstring(zf.read(name))
            texts = [
                elem.text.strip()
                for elem in root.iter()
                if elem.tag.endswith("}t") and elem.text and elem.text.strip()
            ]
            slides_out.append("\n".join(texts))
        return slides_out


def _detect_material_kind(filename: str, raw: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if raw.startswith(b"%PDF"):
        return "pdf"
    if raw.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = set(zf.namelist())
            if "word/document.xml" in names or ext == ".docx":
                return "docx"
            if "ppt/presentation.xml" in names or ext == ".pptx":
                return "pptx"
            return "zip"
        except Exception:
            if ext == ".docx":
                return "docx"
            if ext == ".pptx":
                return "pptx"
            if ext == ".zip":
                return "zip"
    if ext in _TEXT_MATERIAL_EXTENSIONS or Path(filename).name.lower() in _SPECIAL_TEXT_NAMES:
        return "text"
    return ext.lstrip(".") or "binary"


def _extract_zip_preview(
    raw: bytes,
    *,
    max_entries: int = 200,
    max_extracted_files: int = 12,
    max_total_chars: int = 32000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    extracted_files: list[dict[str, Any]] = []
    remaining_chars = max_total_chars

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            member_name = _decode_zip_member_name(info).replace("\\", "/")
            clean_parts = [p for p in member_name.split("/") if p and p != "."]
            if not clean_parts or ".." in clean_parts:
                continue
            if clean_parts[0] == "__MACOSX" or clean_parts[-1] == ".DS_Store" or clean_parts[-1].startswith("._"):
                continue
            norm_path = "/".join(clean_parts)
            is_dir = info.is_dir()
            if len(entries) < max_entries:
                entries.append(
                    {
                        "path": norm_path,
                        "bytes": info.file_size,
                        "is_dir": is_dir,
                    }
                )
            if is_dir or len(extracted_files) >= max_extracted_files or remaining_chars <= 0:
                continue

            suffix = Path(norm_path).suffix.lower()
            base_lower = Path(norm_path).name.lower()
            is_text_like = suffix in _TEXT_MATERIAL_EXTENSIONS or base_lower in _SPECIAL_TEXT_NAMES
            is_office_or_pdf = suffix in {".pdf", ".docx", ".pptx"}
            if not (is_text_like or is_office_or_pdf):
                continue
            if is_text_like and info.file_size > 256 * 1024:
                continue
            if is_office_or_pdf and info.file_size > 8 * 1024 * 1024:
                continue

            try:
                member_bytes = zf.read(info)
            except Exception:
                continue
            kind = _detect_material_kind(norm_path, member_bytes)
            text = ""
            try:
                if kind == "pdf":
                    reader = PdfReader(io.BytesIO(member_bytes))
                    page_chunks: list[str] = []
                    budget = min(remaining_chars, 12000)
                    acc = 0
                    for idx, page in enumerate(reader.pages, start=1):
                        ptxt = (page.extract_text() or "").strip()
                        if ptxt:
                            chunk = f"[第 {idx} 页]\n{ptxt}"
                            page_chunks.append(chunk)
                            acc += len(chunk)
                            if acc >= budget:
                                break
                    text = "\n\n".join(page_chunks)
                elif kind == "docx":
                    text = _extract_docx_text(member_bytes)
                elif kind == "pptx":
                    slides = _extract_pptx_slides(member_bytes)
                    text = "\n\n".join(
                        f"[第 {i} 页]\n{s}" for i, s in enumerate(slides, start=1) if s.strip()
                    )
                elif kind == "text":
                    text = _decode_text_bytes(member_bytes).strip()
            except Exception:
                continue

            if not text.strip():
                continue
            per_file_cap = min(remaining_chars, 8000)
            truncated = len(text) > per_file_cap
            snippet = text[:per_file_cap]
            remaining_chars -= len(snippet)
            extracted_files.append(
                {
                    "path": norm_path,
                    "file_type": kind,
                    "bytes": info.file_size,
                    "text": snippet,
                    "truncated": truncated,
                }
            )
    return entries, extracted_files


def read_course_material_pdf(
    course_dir: Path,
    course_item: dict[str, Any],
    material_query: str = "",
    *,
    start_page: int = 1,
    end_page: int = 50,
    download_to_dir: str = "",
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """List course materials or extract text/contents from local or live Blackboard PDF/ZIP/DOCX/PPTX/text files."""
    course_id = str(course_item.get("course_id") or "")
    course_name = str(course_item.get("name") or course_dir.name)

    q = (material_query or "").strip()

    if not q:
        local_materials = _list_local_materials(course_dir)
        local_pdfs = [item for item in local_materials if item["filename"].lower().endswith(".pdf")]
        remote_materials: list[dict[str, Any]] = []
        if client_factory is not None and course_id:
            client = None
            try:
                client = client_factory()
            except Exception:
                client = None
            if client is not None:
                try:
                    discovered = walk_course_content_items(client, course_id)
                    remote_materials = discovered["materials"]
                finally:
                    client.close()

        combined_by_name: dict[str, dict[str, Any]] = {}
        combined_order: list[str] = []
        for item in local_materials:
            key = item["filename"].lower()
            entry = {
                "title": item["title"],
                "filename": item["filename"],
                "relative_path": item["relative_path"],
                "local_path": item["local_path"],
                "bytes": item["bytes"],
                "cached_locally": True,
                "source": "local",
            }
            combined_by_name[key] = entry
            combined_order.append(key)
        for rm in remote_materials:
            fname = str(rm.get("filename") or rm.get("title") or "").strip()
            key = fname.lower()
            if key and key in combined_by_name:
                combined_by_name[key]["url"] = rm.get("url") or ""
                if rm.get("section"):
                    combined_by_name[key]["section"] = rm["section"]
                if rm.get("description"):
                    combined_by_name[key]["description"] = rm["description"]
            else:
                entry = {
                    "title": rm.get("title") or fname,
                    "filename": fname,
                    "url": rm.get("url") or "",
                    "section": rm.get("section") or "",
                    "description": rm.get("description") or "",
                    "cached_locally": False,
                    "source": "remote",
                }
                unique_key = key or f"remote:{len(combined_order)}"
                combined_by_name[unique_key] = entry
                combined_order.append(unique_key)
        combined_materials = [combined_by_name[k] for k in combined_order]

        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "materials": combined_materials,
            "local_materials": local_materials,
            "local_pdfs": local_pdfs,
            "remote_materials": remote_materials,
            "message": "Pass material='<filename or title>' to read PDF/ZIP/DOCX/PPTX content or obtain a download_url.",
        }

    matched_local = fetch_course_material_file(
        course_dir,
        course_item,
        q,
        client_factory=client_factory,
    )

    copied_to: str | None = None
    if download_to_dir.strip():
        dest_dir = Path(download_to_dir).expanduser().resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / matched_local.name
        if dest_path.resolve() != matched_local.resolve():
            shutil.copy2(matched_local, dest_path)
        copied_to = str(dest_path)

    raw = matched_local.read_bytes()
    kind = _detect_material_kind(matched_local.name, raw)

    if kind == "pdf":
        reader = PdfReader(io.BytesIO(raw))
        total_pages = len(reader.pages)
        s_page = max(1, int(start_page or 1))
        e_page = min(total_pages, max(s_page, int(end_page or total_pages)))
        pages_out = [
            {"page": p_idx + 1, "text": (reader.pages[p_idx].extract_text() or "").strip()}
            for p_idx in range(s_page - 1, e_page)
        ]
        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "file_type": "pdf",
            "filename": matched_local.name,
            "bytes": len(raw),
            "saved_path": str(matched_local),
            "downloaded_to": copied_to,
            "total_pages": total_pages,
            "start_page": s_page,
            "end_page": e_page,
            "pages": pages_out,
        }

    if kind == "zip":
        entries, extracted_files = _extract_zip_preview(raw)
        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "file_type": "zip",
            "filename": matched_local.name,
            "bytes": len(raw),
            "saved_path": str(matched_local),
            "downloaded_to": copied_to,
            "entry_count": len(entries),
            "entries": entries,
            "extracted_files": extracted_files,
        }

    if kind == "docx":
        text = _extract_docx_text(raw)
        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "file_type": "docx",
            "filename": matched_local.name,
            "bytes": len(raw),
            "saved_path": str(matched_local),
            "downloaded_to": copied_to,
            "total_pages": 1,
            "start_page": 1,
            "end_page": 1,
            "pages": [{"page": 1, "text": text}],
            "text": text,
        }

    if kind == "pptx":
        slides = _extract_pptx_slides(raw)
        total_pages = len(slides)
        s_page = max(1, int(start_page or 1)) if total_pages else 0
        e_page = min(total_pages, max(s_page, int(end_page or total_pages))) if total_pages else 0
        pages_out = [
            {"page": idx + 1, "text": slides[idx]}
            for idx in range(s_page - 1, e_page)
        ] if total_pages else []
        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "file_type": "pptx",
            "filename": matched_local.name,
            "bytes": len(raw),
            "saved_path": str(matched_local),
            "downloaded_to": copied_to,
            "total_pages": total_pages,
            "start_page": s_page,
            "end_page": e_page,
            "pages": pages_out,
        }

    if kind == "text":
        text = _decode_text_bytes(raw)
        return {
            "ok": True,
            "course": course_name,
            "course_id": course_id,
            "file_type": "text",
            "filename": matched_local.name,
            "bytes": len(raw),
            "saved_path": str(matched_local),
            "downloaded_to": copied_to,
            "total_pages": 1,
            "start_page": 1,
            "end_page": 1,
            "pages": [{"page": 1, "text": text}],
            "text": text,
        }

    return {
        "ok": True,
        "course": course_name,
        "course_id": course_id,
        "file_type": kind,
        "filename": matched_local.name,
        "bytes": len(raw),
        "saved_path": str(matched_local),
        "downloaded_to": copied_to,
        "message": (
            "Binary or unsupported archive format cached on server; "
            "download the original file using download_url or /api/mcp/download-material."
        ),
    }
