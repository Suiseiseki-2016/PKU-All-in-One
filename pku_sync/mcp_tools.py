"""Read-only local tools exposed by the pku-sync MCP server.

The server deliberately does not expose arbitrary filesystem access or invoke
the expensive sync/transcription pipeline. The system scheduler runs those
deterministic stages first; MCP-capable agents then read the resulting local
course tree here.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Literal

from .pipeline import collect_jobs, recording_stage

TextSection = Literal["all", "assignments", "announcements", "materials", "recordings"]
_TEXT_SUFFIXES = {".json", ".log", ".md", ".txt"}
_MAX_READ_CHARS = 200_000
_LOCAL_WORK_MANAGERS_LOCK = threading.RLock()
_LOCAL_WORK_MANAGERS: dict[str, Any] = {}


def _get_or_create_local_work_manager(data_dir: Path):
    key = str(Path(data_dir).expanduser().resolve())
    with _LOCAL_WORK_MANAGERS_LOCK:
        mgr = _LOCAL_WORK_MANAGERS.get(key)
        if mgr is None:
            from .config import Settings
            from .panel.ai_bridge import LocalAIBridge
            from .panel.recordings_api import RecordingWorkManager

            settings = Settings(data_dir=Path(key))
            mgr = RecordingWorkManager(settings)
            mgr.platform_service = LocalAIBridge(settings)
            _LOCAL_WORK_MANAGERS[key] = mgr
        return mgr


class LocalCourseTools:
    """Safe, deterministic view over one configured ``DATA_DIR``."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()

    def health(self) -> dict:
        return {
            "ok": self.data_dir.is_dir(),
            "data_dir": str(self.data_dir),
            "courses": len(self.list_courses()),
        }

    def list_courses(self) -> list[dict]:
        courses: list[dict] = []
        if not self.data_dir.is_dir():
            return courses
        for meta in sorted(self.data_dir.glob("*/course.json")):
            try:
                item = json.loads(meta.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                item = {}
            if not isinstance(item, dict):
                item = {}
            course_dir = meta.parent
            recent_lectures: list[dict] = []
            ready_count = 0
            refined_count = 0
            rec_root = course_dir / "recordings"
            if rec_root.is_dir():
                for rec_dir in sorted(p for p in rec_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
                    has_notes = (rec_dir / "notes.md").is_file()
                    has_refined = (rec_dir / "refined_notes.md").is_file()
                    if not (has_notes or has_refined):
                        continue
                    ready_count += 1
                    if has_refined:
                        refined_count += 1
                    title, day = rec_dir.name, ""
                    rec_meta = rec_dir / "recording.json"
                    if rec_meta.is_file():
                        try:
                            parsed_rec = json.loads(rec_meta.read_text("utf-8"))
                            if isinstance(parsed_rec, dict):
                                title = str(parsed_rec.get("title") or title)
                                day = str(parsed_rec.get("date") or parsed_rec.get("recorded_at") or "")[:10]
                        except (OSError, ValueError):
                            pass
                    recent_lectures.append({"title": title, "date": day, "has_refined": has_refined})
            assigns_count = 0
            assign_idx = course_dir / "assignments" / "index.json"
            if assign_idx.is_file():
                try:
                    raw_assigns = json.loads(assign_idx.read_text("utf-8"))
                    if isinstance(raw_assigns, list):
                        assigns_count = len(raw_assigns)
                except (OSError, ValueError):
                    pass
            courses.append(
                {
                    "folder": course_dir.name,
                    "course_id": item.get("course_id") or item.get("id") or "",
                    "name": item.get("name") or course_dir.name,
                    "code": item.get("code", ""),
                    "synced_at": item.get("synced_at", ""),
                    "lectures_ready_count": ready_count,
                    "refined_lectures_count": refined_count,
                    "assignments_count": assigns_count,
                    "recent_lectures": recent_lectures[-12:],
                }
            )
        return courses

    def list_files(
        self,
        course_folder: str = "",
        section: TextSection = "all",
    ) -> list[dict]:
        """List relevant local artifacts, returning paths relative to DATA_DIR."""
        base = self._course_dir(course_folder) if course_folder else self.data_dir
        patterns = _section_patterns(section)
        files: dict[str, dict] = {}
        for pattern in patterns:
            effective = (
                pattern
                if course_folder
                else f"*/{pattern}"
            )
            for path in base.glob(effective):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.data_dir).as_posix()
                stat = path.stat()
                files[relative] = {
                    "path": relative,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                }
        return [files[key] for key in sorted(files)]

    def read_text(self, relative_path: str, max_chars: int = 50_000) -> dict:
        """Read one text artifact below DATA_DIR; traversal and binary files fail."""
        if max_chars < 1 or max_chars > _MAX_READ_CHARS:
            raise ValueError(f"max_chars must be between 1 and {_MAX_READ_CHARS}")
        path = self._resolve(relative_path)
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            raise ValueError(f"unsupported text file type: {path.suffix or '(none)'}")
        text = path.read_text("utf-8", errors="replace")
        return {
            "path": path.relative_to(self.data_dir).as_posix(),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
            "total_chars": len(text),
        }

    def recording_status(self, course_id: str = "") -> list[dict]:
        rows: list[dict] = []
        for job in collect_jobs(self.data_dir, course_id):
            status, unavailable = recording_stage(job)
            notes = job.directory / "notes.md"
            rows.append(
                {
                    "course": job.course_name,
                    "course_id": job.recording.course_id,
                    "title": job.recording.title,
                    "recorded_at": job.recording.recorded_at,
                    "status": status,
                    "unavailable_reason": unavailable,
                    "directory": job.directory.relative_to(self.data_dir).as_posix(),
                    "notes_chars": notes.stat().st_size if notes.exists() else 0,
                    "keyframes": len(list((job.directory / "keyframes").glob("*"))),
                }
            )
        return rows

    def _matching_courses(self, course: str) -> list[dict]:
        rows = self.list_courses()
        if not course.strip():
            return rows
        query = course.strip().casefold()
        exact = [row for row in rows if any(query == str(row[key]).casefold()
                 for key in ('course_id', 'folder', 'code', 'name'))]
        return exact or [row for row in rows if query in row['name'].casefold()]

    def get_lecture_notes(self, course: str, lecture: str = "", limit: int = 0) -> list[dict]:
        """Read generated lecture notes, optionally filtered by title or date."""
        rows = []
        for item in self._matching_courses(course):
            folder = self._course_dir(item['folder'])
            jobs = {job.directory.resolve(): job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.resolve() == folder}
            for path in sorted(folder.glob('recordings/*/notes.md')):
                path = self._resolve(path.relative_to(self.data_dir).as_posix())
                job = jobs.get(path.parent)
                title, day = path.parent.name, ''
                if job:
                    title, day = job.recording.title, job.recording.date
                else:
                    try:
                        meta = json.loads((path.parent / 'recording.json').read_text('utf-8'))
                        if isinstance(meta, dict):
                            title = str(meta.get('title') or title)
                            day = str(meta.get('recorded_at') or '')[:10]
                    except (OSError, ValueError):
                        pass
                if lecture.casefold() not in (title + ' ' + day).casefold():
                    continue
                try:
                    markdown = path.read_text('utf-8', errors='replace')
                except OSError:
                    continue
                if markdown.strip():
                    rows.append({'course': item['name'], 'course_id': item['course_id'],
                                 'title': title, 'date': day, 'notes_markdown': markdown})
        return rows[:int(limit)] if limit and int(limit) > 0 else rows

    def get_course_summaries(self, course: str = "") -> dict:
        return self.get_course_summary(course)

    def get_course_assignments(self, course_folder: str) -> list[dict]:
        """Read the selected course's local assignment index."""
        folder = self._course_dir(course_folder)
        if not (folder / 'assignments/index.json').exists():
            return []
        path = self._resolve((folder / 'assignments/index.json').relative_to(self.data_dir).as_posix())
        assignments = json.loads(path.read_text('utf-8'))
        if not isinstance(assignments, list):
            raise ValueError('assignment index must be a list')
        return [item for item in assignments if isinstance(item, dict)]

    def get_course_summary(self, course: str = "") -> dict:
        """Read course digests, lecture coverage and local assignment deadlines."""
        from .panel.course_summary import summary_markdown
        rows = []
        for item in self._matching_courses(course):
            notes = self.get_lecture_notes(item['folder'])
            jobs = [job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.name == item['folder']]
            try:
                markdown = summary_markdown(self.data_dir, item['course_id']) if item['course_id'] else ''
            except (ValueError, OSError):
                markdown = ''
            if not markdown:
                markdown = '# ' + item['name'] + '\n\n' + '\n\n'.join(
                    '## ' + row['title'] + '\n' + row['notes_markdown'] for row in notes)
            try:
                assignments = self.get_course_assignments(item['folder'])
            except (OSError, ValueError):
                assignments = []
            rows.append({**item, 'markdown': markdown, 'total_lessons': max(len(jobs), len(notes)),
                         'summarized_lessons': len(notes), 'assignments_count': len(assignments),
                         'assignments': [{'title': a.get('title', ''),
                                          'due_at': a.get('due_at') or a.get('due_at_label') or ''}
                                         for a in assignments if isinstance(a, dict)]})
        return rows[0] if course and len(rows) == 1 else {'courses': rows}

    def search_course_knowledge(self, query: str, course: str = "") -> list[dict]:
        """Rank local summary, note, assignment, announcement, material and transcript excerpts."""
        tokens = [token for token in re.split(r"[\s,，、。；;：:！!？?/|]+", query.casefold()) if token]
        if not tokens:
            return []
        hits = []
        patterns = [
            ('summary', 'course-summary.md'),
            ('refined_notes', 'recordings/*/refined_notes.md'),
            ('notes', 'recordings/*/notes.md'),
            ('assignments', 'assignments/index.json'),
            ('announcements', 'announcements/index.json'),
            ('materials', 'materials/index.json'),
            ('transcript', 'recordings/*/transcript.json'),
        ]
        for item in self._matching_courses(course):
            folder = self._course_dir(item['folder'])
            jobs = {job.directory.resolve(): job for job in collect_jobs(self.data_dir, item['course_id'])
                    if job.course_dir.resolve() == folder}
            for kind, pattern in patterns:
                for path in sorted(folder.glob(pattern)):
                    try:
                        content = self.read_text(path.relative_to(self.data_dir).as_posix(), _MAX_READ_CHARS)['content']
                        if path.suffix == '.json':
                            value = json.loads(content)
                            # Search student-facing text, never private media URLs or credentials.
                            def prose(value):
                                if isinstance(value, list):
                                    return '\n'.join(prose(v) for v in value)
                                if isinstance(value, dict):
                                    return '\n'.join(prose(v) for k, v in value.items() if k in
                                        {'title', 'text', 'body', 'body_text', 'instructions', 'content', 'segments', 'due_at', 'files', 'category'})
                                return str(value or '')
                            content = prose(value)
                    except (OSError, ValueError):
                        continue
                    lowered = content.casefold()
                    score = sum(lowered.count(token) for token in tokens)
                    if not score:
                        continue
                    offset = min(lowered.find(token) for token in tokens if token in lowered)
                    title, day = item['name'], ''
                    if kind == 'summary':
                        title = f"{item['name']} 课程总结"
                    if kind in {'refined_notes', 'notes', 'transcript'}:
                        title = path.parent.name
                        job = jobs.get(path.parent.resolve())
                        if job:
                            title, day = job.recording.title, job.recording.date
                        else:
                            try:
                                meta = json.loads((path.parent / 'recording.json').read_text('utf-8'))
                                if isinstance(meta, dict):
                                    title = str(meta.get('title') or title)
                                    day = str(meta.get('recorded_at') or '')[:10]
                            except (OSError, ValueError):
                                pass
                    hit = {'course': item['name'], 'source_type': kind, 'title': title,
                           'snippet': content[max(0, offset - 100):offset + 400], '_score': score}
                    if day:
                        hit['date'] = day
                    hits.append(hit)
        priority = {'summary': 0, 'refined_notes': 0, 'notes': 1, 'assignments': 2, 'announcements': 2, 'materials': 2, 'transcript': 3}
        hits.sort(key=lambda row: (-row['_score'], priority[row['source_type']]))
        return [{k: v for k, v in row.items() if k != '_score'} for row in hits[:20]]

    def _iter_lecture_dirs(self, course: str = "", lecture: str = "") -> list[tuple[dict, Path, str, str]]:
        """Yield `(course_item, recording_dir, lecture_title, lecture_date)` matching filters."""
        from .panel.recordings_api import recording_id as _recording_id

        needle = (lecture or "").strip().casefold()
        matched: list[tuple[dict, Path, str, str]] = []
        for item in self._matching_courses(course):
            folder = self._course_dir(item["folder"])
            rec_root = folder / "recordings"
            if not rec_root.is_dir():
                continue
            jobs = {
                job.directory.resolve(): job
                for job in collect_jobs(self.data_dir, item["course_id"])
                if job.course_dir.resolve() == folder
            }
            for rec_dir in sorted(p for p in rec_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
                title = rec_dir.name
                day = ""
                rid = ""
                src_id = ""
                job = jobs.get(rec_dir.resolve())
                if job:
                    title = job.recording.title or title
                    day = job.recording.date or ""
                    rid = _recording_id(job)
                    src_id = str(getattr(job.recording, "source_id", "") or "")
                else:
                    rec_meta = rec_dir / "recording.json"
                    if rec_meta.is_file():
                        try:
                            parsed = json.loads(rec_meta.read_text(encoding="utf-8"))
                            if isinstance(parsed, dict):
                                title = str(parsed.get("title") or title)
                                day = str(parsed.get("date") or parsed.get("recorded_at") or "")[:10]
                                src_id = str(parsed.get("source_id") or "")
                        except (OSError, ValueError):
                            pass
                if needle:
                    haystack = f"{rec_dir.name} {rid} {src_id} {title} {day}".casefold()
                    if needle not in haystack:
                        continue
                matched.append((item, rec_dir, title, day))
        return matched

    def get_lecture_logic_map(self, course: str = "", lecture: str = "") -> dict:
        """Read structured lecture knowledge mindmaps (Mermaid) and formula derivation chains."""
        items = []
        for course_item, rec_dir, title, day in self._iter_lecture_dirs(course, lecture):
            path = rec_dir / "lecture-logic-map.json"
            if not path.is_file():
                path = rec_dir / "lecture_logic_map.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            mindmap_obj = data.get("mindmap") if isinstance(data.get("mindmap"), dict) else {}
            mermaid_str = str(data.get("mermaid") or mindmap_obj.get("mermaid") or "")
            derivs = (
                data.get("derivations")
                if isinstance(data.get("derivations"), list)
                else (data.get("proof_cards") if isinstance(data.get("proof_cards"), list) else [])
            )
            items.append(
                {
                    "course": course_item["name"],
                    "course_id": course_item["course_id"],
                    "lecture": title,
                    "date": day,
                    "core_takeaway": str(data.get("core_takeaway") or data.get("core_thesis") or ""),
                    "mermaid": mermaid_str,
                    "logic_chain": data.get("logic_chain") if isinstance(data.get("logic_chain"), list) else [],
                    "derivations": derivs,
                    "contrast_cards": data.get("contrast_cards") if isinstance(data.get("contrast_cards"), list) else [],
                }
            )
        return {"count": len(items), "items": items}

    def get_blackboard_notes(self, course: str = "", lecture: str = "") -> dict:
        """Read recognized handwritten blackboard derivations (`board-notes.json`) across lectures."""
        items = []
        for course_item, rec_dir, title, day in self._iter_lecture_dirs(course, lecture):
            path = rec_dir / "board-notes.json"
            if not path.is_file():
                path = rec_dir / "keyframes" / "board-notes.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            boards_raw = None
            if isinstance(data, dict):
                boards_raw = data.get("boards") if isinstance(data.get("boards"), dict) else data.get("notes")
            if not isinstance(boards_raw, dict) or not boards_raw:
                continue
            boards = []
            for fname, entry in sorted(
                boards_raw.items(),
                key=lambda kv: float(kv[1].get("timestamp") or 0.0) if isinstance(kv[1], dict) else 0.0,
            ):
                if not isinstance(entry, dict):
                    continue
                if "is_board" in entry and not entry.get("is_board"):
                    continue
                md = str(entry.get("markdown") or entry.get("text") or "").strip()
                if not md:
                    continue
                boards.append(
                    {
                        "file": fname,
                        "label": str(entry.get("label") or ""),
                        "timestamp": float(entry.get("timestamp") or 0.0),
                        "markdown": md,
                    }
                )
            if boards:
                items.append(
                    {
                        "course": course_item["name"],
                        "course_id": course_item["course_id"],
                        "lecture": title,
                        "date": day,
                        "boards_count": len(boards),
                        "boards": boards,
                    }
                )
        return {"count": len(items), "items": items}

    def get_lecture_quiz(self, course: str = "", lecture: str = "") -> dict:
        """Read lecture self-test quizzes, options, answers, and explanations."""
        items = []
        for course_item, rec_dir, title, day in self._iter_lecture_dirs(course, lecture):
            path = rec_dir / "lecture-quiz.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            questions = data.get("questions") if isinstance(data.get("questions"), list) else []
            if not questions:
                continue
            items.append(
                {
                    "course": course_item["name"],
                    "course_id": course_item["course_id"],
                    "lecture": title,
                    "date": day,
                    "questions_count": len(questions),
                    "questions": questions,
                }
            )
        return {"count": len(items), "items": items}

    def get_oral_notices_and_exam_points(self, course: str = "") -> dict:
        """Extract teacher oral announcements, quiz codes, exam tips, and homework rules from lectures and notices."""
        cue_re = re.compile(
            r"(随堂|口述|测验|密钥|口令|考点|必考|易错|期中|期末|作业|截止|DDL|提交|打包|命名|注意)",
            re.IGNORECASE,
        )
        items: list[dict] = []
        for course_item, rec_dir, title, day in self._iter_lecture_dirs(course, ""):
            notes_path = rec_dir / "notes.md"
            if not notes_path.is_file():
                continue
            try:
                text = notes_path.read_text(encoding="utf-8")
            except OSError:
                continue
            matched_lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("![") and cue_re.search(line)
            ]
            if matched_lines:
                items.append(
                    {
                        "course": course_item["name"],
                        "course_id": course_item["course_id"],
                        "source_type": "lecture_oral_notice_or_exam_point",
                        "lecture": title,
                        "date": day,
                        "highlights": matched_lines[:15],
                    }
                )
        q = course.strip().lower()
        for course_item in self.list_courses():
            if q and not any(
                q in str(course_item.get(k, "")).lower() for k in ("course_id", "name", "folder")
            ):
                continue
            try:
                assignments = self.get_course_assignments(course_item["folder"])
            except Exception:
                assignments = []
            for a in assignments:
                items.append(
                    {
                        "course": course_item["name"],
                        "course_id": course_item["course_id"],
                        "source_type": "official_assignment",
                        "content_id": str(a.get("content_id") or ""),
                        "title": str(a.get("title") or ""),
                        "due_at": str(a.get("due_at") or a.get("due_at_label") or ""),
                        "instructions": str(a.get("instructions") or ""),
                    }
                )
        return {"count": len(items), "items": items}

    def _staged_dirs(self) -> list[Path]:
        return [self.data_dir / ".mcp-staged"]

    def _resolve_staged_or_local_file(
        self, fp: str, all_paths: list[str] | None = None
    ) -> tuple[str, bytes, str]:
        raw_fp = str(fp or "").strip()
        if not raw_fp.startswith("staged:"):
            p = Path(raw_fp).expanduser()
            try:
                resolved = p.resolve()
            except OSError:
                resolved = p
            if resolved.is_file():
                return resolved.name, resolved.read_bytes(), str(resolved)

        staged_dirs = self._staged_dirs()
        if raw_fp.startswith("staged:"):
            rel = raw_fp[len("staged:") :].strip().lstrip("/")
            rel_path = Path(rel)
            if rel and ".." not in rel_path.parts:
                for sroot in staged_dirs:
                    for sub in ("by_hash", "by_name"):
                        cand = sroot / sub / (rel_path if sub == "by_hash" else rel_path.name)
                        if cand.is_file():
                            return cand.name, cand.read_bytes(), f"staged://{cand.name}"

        fname = Path(raw_fp.replace("\\", "/")).name
        if fname and fname not in (".", ".."):
            for sroot in staged_dirs:
                cand = sroot / "by_name" / fname
                if cand.is_file():
                    return fname, cand.read_bytes(), f"staged://{fname}"

        raise FileNotFoundError(f"Homework file not found locally: {fp}")

    def submit_course_assignment(
        self,
        course: str,
        assignment_id: str,
        file_paths: list[str] | None = None,
        archive_name: str = "",
        confirm_submit: bool = False,
        files_payload: list[dict] | None = None,
        submission_mode: str = "",
    ) -> dict:
        """Prepare, verify, and (when confirm_submit=True) submit homework files to PKU Blackboard via Remote or Local MCP."""
        import base64
        import hashlib
        import io
        import zipfile

        q = course.strip().lower()
        matched_course = self._match_course(q)

        assignments = self.get_course_assignments(matched_course["folder"])
        target_assign = None
        aid = assignment_id.strip().lower()
        for a in assignments:
            if aid in {
                str(a.get("content_id", "")).lower(),
                str(a.get("title", "")).lower(),
            } or aid in str(a.get("title", "")).lower():
                target_assign = a
                break
        if target_assign is None:
            raise ValueError(f"Assignment not found in {matched_course['name']}: {assignment_id}")

        resolved_entries: list[tuple[str, bytes]] = []
        file_summaries: list[dict] = []

        for item in files_payload or []:
            if not isinstance(item, dict):
                continue
            fname = Path(str(item.get("filename") or item.get("name") or "answer.txt")).name or "answer.txt"
            raw_content = str(item.get("content") or "")
            enc = str(item.get("encoding") or "utf-8").lower()
            data = base64.b64decode(raw_content.encode("ascii")) if enc == "base64" else raw_content.encode("utf-8")
            resolved_entries.append((fname, data))
            file_summaries.append(
                {
                    "name": fname,
                    "path": f"mcp://{fname}",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

        for fp in file_paths or []:
            fname, data, loc = self._resolve_staged_or_local_file(fp, file_paths)
            resolved_entries.append((fname, data))
            file_summaries.append(
                {
                    "name": fname,
                    "path": loc,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

        if not resolved_entries:
            raise ValueError("Please provide files_payload or file_paths (after /api/mcp/stage-upload if remote) to submit.")

        req_mode = (submission_mode or "").strip().lower()
        wants_zip = req_mode == "zip" or archive_name.strip().lower().endswith(".zip")
        if wants_zip:
            upload_name = archive_name.strip() or f"{target_assign.get('title', 'homework')}.zip"
            if not upload_name.lower().endswith(".zip"):
                upload_name += ".zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fname, fbytes in resolved_entries:
                    zf.writestr(fname, fbytes)
            upload_bytes = buf.getvalue()
            mode = "zip"
        elif len(resolved_entries) == 1:
            upload_name = Path(archive_name.strip()).name if archive_name.strip() else resolved_entries[0][0]
            upload_bytes = resolved_entries[0][1]
            mode = "single_file"
        else:
            mode = "multiple_files"
            upload_name = ", ".join(fname for fname, _ in resolved_entries)
            upload_bytes = b"".join(fbytes for _, fbytes in resolved_entries)

        payload_sha256 = hashlib.sha256(upload_bytes).hexdigest()
        preview_info = {
            "course": matched_course["name"],
            "course_id": matched_course["course_id"],
            "assignment_id": str(target_assign.get("content_id") or ""),
            "assignment_title": str(target_assign.get("title") or ""),
            "due_at": str(target_assign.get("due_at") or target_assign.get("due_at_label") or ""),
            "instructions": str(target_assign.get("instructions") or ""),
            "submission_mode": mode,
            "upload_filename": upload_name,
            "upload_bytes": len(upload_bytes),
            "upload_sha256": payload_sha256,
            "files": file_summaries,
        }
        if not confirm_submit:
            return {
                "ok": True,
                "status": "ready_to_confirm",
                "message": "Pre-flight check passed. Ask the student to confirm filename and target assignment, then call submit_course_assignment again with confirm_submit=True.",
                **preview_info,
            }

        from .submit import fetch_form, submit_file, submit_files

        batch_dir = self.data_dir / ".mcp-submissions" / payload_sha256[:12]
        batch_dir.mkdir(parents=True, exist_ok=True)
        client = self._get_campus_client()
        try:
            course_id_str = str(matched_course["course_id"])
            content_id_str = str(target_assign.get("content_id") or "")
            form = fetch_form(client, course_id_str, content_id_str, action="newAttempt")
            if mode == "multiple_files":
                upload_paths: list[Path] = []
                expected_hashes: list[str] = []
                for fname, fbytes in resolved_entries:
                    safe_f = Path(fname).name
                    p = batch_dir / safe_f
                    p.write_bytes(fbytes)
                    upload_paths.append(p)
                    expected_hashes.append(hashlib.sha256(fbytes).hexdigest())
                receipt = submit_files(client, form, upload_paths, expected_sha256s=expected_hashes)
            else:
                safe_u = Path(upload_name).name
                tmp_upload = batch_dir / safe_u
                tmp_upload.write_bytes(upload_bytes)
                receipt = submit_file(client, form, tmp_upload, expected_sha256=payload_sha256)
        finally:
            client.close()
        return {
            "ok": True,
            "status": "submitted",
            "receipt": receipt if isinstance(receipt, dict) else str(receipt),
            **preview_info,
        }

    def _match_course(self, course: str) -> dict:
        q = (course or "").strip().lower()
        if not q:
            raise ValueError("course is required")

        courses = self.list_courses()
        for item in courses:
            if q in {
                str(item.get("course_id", "")).lower(),
                str(item.get("name", "")).lower(),
                str(item.get("folder", "")).lower(),
            }:
                return item

        def _norm(s: str) -> str:
            return re.sub(r"[\s_()（）\-:：·]+", "", s.lower())

        aliases = (
            ("中枢神经系统", "cns"),
            ("中枢神经", "cns"),
            ("神经系统解剖", "cns解剖"),
            ("计概", "计算概论"),
            ("计网", "计算机网络"),
            ("信安", "信息安全"),
            ("发心", "发展心理学"),
            ("心测", "心理测量"),
            ("认心", "认知心理学"),
            ("组管", "组织管理心理学"),
        )
        q_norm = _norm(q)
        q_variants = [q_norm]
        for src, dst in aliases:
            if src in q_norm:
                q_variants.append(q_norm.replace(src, dst))

        for item in courses:
            raw_fields = (
                str(item.get("name", "")).lower(),
                str(item.get("folder", "")).lower(),
            )
            if any(q in rf for rf in raw_fields):
                return item
            norm_fields = tuple(_norm(rf) for rf in raw_fields)
            for qv in q_variants:
                if qv and any(qv in nf or nf in qv for nf in norm_fields if nf):
                    return item
        raise ValueError(f"Course not found: {course}")

    def _resolve_campus_credentials(self) -> tuple[str, str]:
        from .config import Settings

        settings = Settings()
        username = settings.pku_username
        password = settings.pku_password
        if not username or not password:
            raise ValueError(
                "PKU credentials not configured; please configure PKU_USERNAME / PKU_PASSWORD in .env or the local panel."
            )
        return username, password

    def _get_campus_client(self):
        from .auth import get_session

        username, password = self._resolve_campus_credentials()
        return get_session(username=username, password=password)

    def get_course_grades_and_submissions(self, course: str = "") -> dict:
        """Fetch live PKU Blackboard gradebook scores, written assignment submission receipts, and online assessment entries."""
        from .assessment import inspect_course_grades_and_submissions

        q = (course or "").strip()
        if q:
            matched = self._match_course(q)
            try:
                synced_assigns = self.get_course_assignments(matched["folder"])
            except Exception:
                synced_assigns = []
            client = self._get_campus_client()
            try:
                return inspect_course_grades_and_submissions(
                    client,
                    str(matched["course_id"]),
                    str(matched["name"]),
                    synced_assignments=synced_assigns,
                )
            finally:
                client.close()

        courses_out: list[dict] = []
        client = self._get_campus_client()
        try:
            for item in self.list_courses():
                cid = str(item.get("course_id") or "")
                if not cid:
                    continue
                try:
                    synced_assigns = self.get_course_assignments(item["folder"])
                except Exception:
                    synced_assigns = []
                courses_out.append(
                    inspect_course_grades_and_submissions(
                        client,
                        cid,
                        str(item.get("name") or item["folder"]),
                        synced_assignments=synced_assigns,
                    )
                )
        finally:
            client.close()
        return {"ok": True, "count": len(courses_out), "courses": courses_out}

    def get_online_assessment(self, course: str, assessment_id: str) -> dict:
        """Open a PKU Blackboard online test/self-assessment and parse all questions, options, points, and saved answers."""
        from .assessment import fetch_online_assessment

        matched = self._match_course(course)
        client = self._get_campus_client()
        try:
            res = fetch_online_assessment(client, str(matched["course_id"]), assessment_id)
            res["course"] = matched["name"]
            return res
        finally:
            client.close()

    def submit_online_assessment(
        self,
        course: str,
        assessment_id: str,
        answers: dict,
        confirm_submit: bool = False,
    ) -> dict:
        """Save draft answers (confirm_submit=False) or finally submit (confirm_submit=True) a PKU Blackboard online test/assessment."""
        from .assessment import save_or_submit_online_assessment

        matched = self._match_course(course)
        client = self._get_campus_client()
        try:
            return save_or_submit_online_assessment(
                client,
                str(matched["course_id"]),
                assessment_id,
                answers,
                confirm_submit=bool(confirm_submit),
                course_name=str(matched["name"]),
            )
        finally:
            client.close()

    def fetch_course_material_file(self, course: str, material: str) -> Path:
        """Resolve and return a local course material file Path, downloading from Blackboard if needed."""
        from .assessment import fetch_course_material_file as _fetch_material

        matched = self._match_course(course)
        course_dir = self._course_dir(matched["folder"])
        return _fetch_material(
            course_dir,
            matched,
            material,
            client_factory=self._get_campus_client,
        )

    def read_course_material_pdf(
        self,
        course: str,
        material: str = "",
        start_page: int = 1,
        end_page: int = 50,
        download_to_dir: str = "",
    ) -> dict:
        """List course materials or extract content from local/live Blackboard PDF/ZIP/DOCX/PPTX/text files."""
        from .assessment import read_course_material_pdf as _read_pdf

        matched = self._match_course(course)
        course_dir = self._course_dir(matched["folder"])
        return _read_pdf(
            course_dir,
            matched,
            material,
            start_page=int(start_page or 1),
            end_page=int(end_page or 50),
            download_to_dir=str(download_to_dir or ""),
            client_factory=self._get_campus_client,
        )

    def read_lecture(
        self,
        course: str,
        lecture: str = "",
        prefer_refined: bool = True,
        include_extras: bool = True,
    ) -> dict:
        """Read lecture index (when `lecture` is omitted) or full single-lecture handout + board OCR + oral tips."""
        from .panel.recordings_api import recording_id as _recording_id

        matched_course = self._match_course(course)
        folder = matched_course["folder"]
        rid_by_dir = {
            job.directory.resolve(): _recording_id(job)
            for job in collect_jobs(self.data_dir, matched_course["course_id"])
        }
        lec_query = (lecture or "").strip()
        if not lec_query:
            index_rows: list[dict] = []
            for _citem, rec_dir, title, day in self._iter_lecture_dirs(folder, ""):
                refined_p = rec_dir / "refined_notes.md"
                raw_p = rec_dir / "notes.md"
                board_p = (
                    rec_dir / "board-notes.json"
                    if (rec_dir / "board-notes.json").is_file()
                    else rec_dir / "keyframes" / "board-notes.json"
                )
                logic_p = (
                    rec_dir / "lecture-logic-map.json"
                    if (rec_dir / "lecture-logic-map.json").is_file()
                    else rec_dir / "lecture_logic_map.json"
                )
                has_notes = raw_p.is_file()
                has_refined = refined_p.is_file()
                if not (has_notes or has_refined or board_p.is_file()):
                    continue
                topics: list[str] = []
                preview_src = refined_p if has_refined else raw_p
                if preview_src.is_file():
                    try:
                        for ln in preview_src.read_text("utf-8", errors="replace").splitlines():
                            m = re.match(r"^#{2,3}\s+(.+?)\s*$", ln.strip())
                            if m:
                                topics.append(m.group(1).strip())
                            if len(topics) >= 6:
                                break
                    except OSError:
                        pass
                index_rows.append(
                    {
                        "recording_id": rid_by_dir.get(rec_dir.resolve(), ""),
                        "title": title,
                        "date": day,
                        "has_notes": has_notes,
                        "has_refined": has_refined,
                        "has_board_notes": board_p.is_file(),
                        "has_logic_map": logic_p.is_file(),
                        "topics": topics,
                    }
                )
            oral = self.get_oral_notices_and_exam_points(folder)
            return {
                "course": matched_course["name"],
                "course_id": matched_course["course_id"],
                "mode": "lecture_index",
                "count": len(index_rows),
                "lectures": index_rows[:40],
                "oral_and_assignment_highlights": oral.get("items", [])[:10],
                "hint": "请传入具体的 lecture 标题或日期关键词以读取精编考点讲义、板书推导与逻辑导图。",
            }

        oral_all = self.get_oral_notices_and_exam_points(folder).get("items", []) if include_extras else []
        lectures_out: list[dict] = []
        for _citem, rec_dir, title, day in self._iter_lecture_dirs(folder, lec_query)[:3]:
            refined_p = rec_dir / "refined_notes.md"
            raw_p = rec_dir / "notes.md"
            refined_text = refined_p.read_text("utf-8", errors="replace") if refined_p.is_file() else ""
            raw_text = raw_p.read_text("utf-8", errors="replace") if raw_p.is_file() else ""
            if prefer_refined and refined_text.strip():
                chosen_md = refined_text
                variant = "refined"
            elif raw_text.strip():
                chosen_md = raw_text
                variant = "raw"
            elif refined_text.strip():
                chosen_md = refined_text
                variant = "refined"
            else:
                chosen_md = ""
                variant = "none"

            refined_meta_p = rec_dir / "refined_notes.json"
            matched_cw: list = []
            if refined_meta_p.is_file():
                try:
                    rmeta = json.loads(refined_meta_p.read_text("utf-8"))
                    if isinstance(rmeta, dict) and isinstance(rmeta.get("courseware"), list):
                        matched_cw = rmeta["courseware"]
                except (OSError, ValueError):
                    matched_cw = []

            entry: dict = {
                "recording_id": rid_by_dir.get(rec_dir.resolve(), ""),
                "title": title,
                "date": day,
                "note_variant": variant,
                "has_refined": bool(refined_text.strip()),
                "has_raw": bool(raw_text.strip()),
                "matched_courseware": matched_cw,
                "notes_markdown": chosen_md,
            }
            if include_extras:
                boards_res = self.get_blackboard_notes(folder, title)
                logic_res = self.get_lecture_logic_map(folder, title)
                quiz_res = self.get_lecture_quiz(folder, title)
                matching_oral = [
                    row.get("highlights", [])
                    for row in oral_all
                    if row.get("source_type") == "lecture_oral_notice_or_exam_point"
                    and (row.get("lecture") == title or (day and row.get("date") == day))
                ]
                entry["oral_highlights"] = matching_oral[0] if matching_oral else []
                entry["blackboard_notes"] = boards_res.get("items", [])
                entry["logic_map"] = (logic_res.get("items") or [None])[0]
                if quiz_res.get("items"):
                    entry["quiz"] = quiz_res["items"][0]
            lectures_out.append(entry)

        return {
            "course": matched_course["name"],
            "course_id": matched_course["course_id"],
            "mode": "lecture_detail",
            "count": len(lectures_out),
            "lectures": lectures_out,
        }

    @staticmethod
    def _load_materials_index(course_dir: Path) -> list[dict[str, Any]]:
        """Load synced materials/index.json safely into normalized dicts."""
        idx_path = course_dir / "materials" / "index.json"
        if not idx_path.is_file():
            return []
        try:
            raw = json.loads(idx_path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            kind = str(item.get("kind") or "").strip()
            path = str(item.get("path") or "").strip()
            body_text = str(item.get("body_text") or "").strip()
            atts_raw = item.get("attachments") or []
            atts: list[str] = []
            if isinstance(atts_raw, list):
                for a in atts_raw:
                    if isinstance(a, dict):
                        lbl = str(a.get("filename") or a.get("title") or a.get("path") or "").strip()
                    else:
                        lbl = str(a or "").strip()
                    if lbl and lbl not in atts:
                        atts.append(lbl)
            files_raw = item.get("files") or []
            files: list[str] = []
            if isinstance(files_raw, list):
                for f_item in files_raw:
                    if isinstance(f_item, dict):
                        lbl = str(f_item.get("filename") or f_item.get("name") or f_item.get("path") or "").strip()
                    else:
                        lbl = str(f_item or "").strip()
                    if lbl and lbl not in files:
                        files.append(lbl)
            links_raw = item.get("source_links") or []
            links: list[str] = []
            if isinstance(links_raw, list):
                for lk in links_raw:
                    if isinstance(lk, dict):
                        lbl = str(lk.get("url") or lk.get("href") or lk.get("title") or "").strip()
                    else:
                        lbl = str(lk or "").strip()
                    if lbl and lbl not in links:
                        links.append(lbl)
            entries.append(
                {
                    "title": title or (atts[0] if atts else path) or "未命名条目",
                    "kind": kind,
                    "path": path,
                    "body_text": body_text,
                    "attachments": atts,
                    "files": files,
                    "source_links": links,
                }
            )
        return entries

    @staticmethod
    def _format_synced_index_entry(entry: dict[str, Any], *, max_body: int = 3000) -> dict[str, Any]:
        body = str(entry.get("body_text") or "")
        if len(body) > max_body:
            body = body[:max_body] + "\n…(正文已截断)"
        return {
            "title": str(entry.get("title") or ""),
            "kind": str(entry.get("kind") or ""),
            "path": str(entry.get("path") or ""),
            "body_text": body,
            "attachments": list(entry.get("attachments") or []),
            "source_links": list(entry.get("source_links") or []),
        }

    def read_course_material(
        self,
        course: str,
        material: str = "",
        start_page: int = 1,
        end_page: int = 50,
        download_to_dir: str = "",
    ) -> dict:
        """Read course exam summary (`material=''` or `'summary'`), synced index (`material='index'`), or extract content from PDF/ZIP/DOCX/PPTX/text materials."""
        matched = self._match_course(course)
        course_dir = self._course_dir(matched["folder"])
        needle = (material or "").strip()
        synced_entries = self._load_materials_index(course_dir)
        vault_locked = self._cookie_vault_locked()

        if not needle or needle.lower() in ("summary", "总纲", "复习总纲"):
            summary = self.get_course_summary(course)
            materials_list: list[dict] = []
            live_remote: list[dict] = []
            try:
                pdf_index = self.read_course_material_pdf(course, "")
                materials_list = list(pdf_index.get("materials") or [])
                if not materials_list:
                    materials_list = list(
                        pdf_index.get("local_materials") or pdf_index.get("local_pdfs") or []
                    ) + list(pdf_index.get("remote_materials") or [])
                live_remote = list(pdf_index.get("remote_materials") or [])
            except Exception:
                materials_list = []

            seen_names: set[str] = set()
            for item in materials_list:
                if isinstance(item, dict):
                    for key in ("filename", "title"):
                        val = str(item.get(key) or "").strip().lower()
                        if val:
                            seen_names.add(val)

            added_synced = 0
            for entry in synced_entries:
                if added_synced >= 100:
                    break
                title = entry["title"]
                atts = entry["attachments"]
                if title.lower() in seen_names:
                    continue
                if (not entry["body_text"]) and atts and all(a.lower() in seen_names for a in atts):
                    continue
                seen_names.add(title.lower())
                materials_list.append(
                    {
                        "title": title,
                        "filename": atts[0] if atts else title,
                        "kind": entry["kind"],
                        "path": entry["path"],
                        "source": "synced_index",
                        "cached_locally": False,
                        "has_body_text": bool(entry["body_text"]),
                        "attachments": atts,
                    }
                )
                added_synced += 1

            out: dict[str, Any] = {
                "mode": "summary_and_materials_index",
                **summary,
                "materials": materials_list,
                "synced_index_count": len(synced_entries),
            }
            return out

        if needle.lower() in ("index", "index.json", "materials/index.json", "目录", "课件目录"):
            formatted_entries: list[dict[str, Any]] = []
            remaining_chars = 24_000
            for entry in synced_entries[:80]:
                fmt = self._format_synced_index_entry(entry, max_body=min(3000, max(200, remaining_chars)))
                body_len = len(fmt.get("body_text") or "")
                remaining_chars = max(0, remaining_chars - body_len)
                formatted_entries.append(fmt)
                if remaining_chars <= 0 and len(formatted_entries) >= 20:
                    break
            return {
                "ok": True,
                "mode": "materials_index_detail",
                "course": matched["name"],
                "course_id": matched["course_id"],
                "total_entries": len(synced_entries),
                "entries": formatted_entries,
                "credential_mode": "cookie_vault" if vault_locked else "standard",
            }

        try:
            return self.read_course_material_pdf(
                course=course,
                material=needle,
                start_page=int(start_page or 1),
                end_page=int(end_page or 50),
                download_to_dir=str(download_to_dir or ""),
            )
        except Exception as exc:
            q_lower = needle.lower()
            cred_locked = ("pku credentials not configured" in str(exc).lower()) or vault_locked
            matched_entries: list[dict[str, Any]] = []
            for entry in synced_entries:
                haystack = " ".join(
                    [
                        entry["title"],
                        entry["path"],
                        *entry["attachments"],
                        *entry["files"],
                    ]
                ).lower()
                if q_lower in haystack:
                    matched_entries.append(entry)

            binary_exts = (".pdf", ".pptx", ".ppt", ".docx", ".doc", ".zip", ".xlsx", ".xls", ".rar", ".7z")
            asks_binary = q_lower.endswith(binary_exts)
            if matched_entries and not asks_binary and any(
                e["body_text"] or e["source_links"] for e in matched_entries
            ):
                return {
                    "ok": True,
                    "mode": "synced_index_entry",
                    "course": matched["name"],
                    "course_id": matched["course_id"],
                    "material": needle,
                    "matches": [self._format_synced_index_entry(e) for e in matched_entries[:10]],
                    "note": (
                        "已返回本地同步的课程栏目正文与附件清单。"
                        + ("未缓存的二进制附件因浏览器密钥托管模式无法离线直连教学网下载。" if cred_locked else "")
                    ),
                }
            if cred_locked:
                return self._campus_credentials_locked_payload(
                    "实时从教学网下载尚未缓存的 PDF/PPTX 等原文件",
                    course=matched["name"],
                    course_id=matched["course_id"],
                    material=needle,
                    matched_index_entries=[
                        self._format_synced_index_entry(e, max_body=1200) for e in matched_entries[:5]
                    ],
                )
            raise

    def _collect_local_assignments(self, course: str = "") -> tuple[list[dict], bool]:
        courses_out: list[dict] = []
        any_index_file = False
        for item in self._matching_courses(course):
            cdir = self._course_dir(item["folder"])
            if (cdir / "assignments" / "index.json").is_file():
                any_index_file = True
            assigns = self.get_course_assignments(item["folder"])
            courses_out.append(
                {
                    "course": item["name"],
                    "course_id": item["course_id"],
                    "assignments_count": len(assigns),
                    "assignments": assigns,
                }
            )
        return courses_out, any_index_file

    def get_assignments_and_grades(
        self,
        course: str = "",
        assessment_id: str = "",
        live_portal: bool = False,
    ) -> dict:
        """Read synced assignments & deadlines, live Blackboard grades/receipts, or online assessment questions."""
        vault_locked = self._cookie_vault_locked()
        if (assessment_id or "").strip():
            try:
                return self.get_online_assessment(course, assessment_id)
            except Exception as exc:
                if "pku credentials not configured" in str(exc).lower():
                    return self._campus_credentials_locked_payload(
                        "实时拉取教学网在线自测/测验题目",
                        course=course,
                        assessment_id=assessment_id,
                    )
                raise
        if live_portal:
            try:
                return self.get_course_grades_and_submissions(course)
            except Exception as exc:
                if "pku credentials not configured" in str(exc).lower():
                    try:
                        local_courses, _ = self._collect_local_assignments(course)
                    except Exception:
                        local_courses = []
                    return self._campus_credentials_locked_payload(
                        "实时查询教学网成绩册与作业提交回执（已附带本地已同步的作业列表）",
                        course=course,
                        live_portal=True,
                        local_courses=local_courses,
                    )
                raise

        courses_out, any_index_file = self._collect_local_assignments(course)
        if not any_index_file and len(courses_out) == 1:
            try:
                return self.get_course_grades_and_submissions(course)
            except Exception:
                pass
        out: dict[str, Any] = {
            "mode": "local_assignments",
            "count": len(courses_out),
            "courses": courses_out,
        }
        if vault_locked:
            out["credential_mode"] = "cookie_vault"
            out["live_portal_available"] = False
            out["tip"] = "请先在本地面板「教学网连接」或 .env 配置教学网账号。"
        else:
            out["tip"] = "如需实时拉取教学网成绩册、作业提交状态与在线自测列表，请传 live_portal=true；读取某道在线自测请传 assessment_id。"
        return out

    def submit_course_work(
        self,
        course: str,
        target_id: str,
        work_type: str = "assignment",
        file_paths: list[str] | None = None,
        files_payload: list[dict] | None = None,
        answers: dict | None = None,
        archive_name: str = "",
        submission_mode: str = "",
        confirm_submit: bool = False,
    ) -> dict:
        """Unified entry point to verify/submit homework files or save/submit an online Blackboard assessment."""
        wtype = (work_type or "").strip().lower()
        try:
            if wtype in ("online_assessment", "assessment", "quiz") or (
                isinstance(answers, dict) and answers and not file_paths and not files_payload
            ):
                return self.submit_online_assessment(
                    course=course,
                    assessment_id=target_id,
                    answers=dict(answers or {}),
                    confirm_submit=bool(confirm_submit),
                )
            return self.submit_course_assignment(
                course=course,
                assignment_id=target_id,
                file_paths=list(file_paths or []),
                archive_name=str(archive_name or ""),
                confirm_submit=bool(confirm_submit),
                files_payload=list(files_payload or []),
                submission_mode=str(submission_mode or ""),
            )
        except Exception as exc:
            if "pku credentials not configured" in str(exc).lower():
                return self._campus_credentials_locked_payload(
                    "连接北大教学网执行作业提交或在线自测草稿保存",
                    course=course,
                    target_id=target_id,
                )
            raise

    @staticmethod
    def _cookie_vault_locked_message(action_desc: str) -> str:
        return f"请先在本地工作台「教学网连接」保存教学网账号与密码（或 .env 配置 PKU_USERNAME / PKU_PASSWORD），以便{action_desc}。"

    def _campus_credentials_locked_payload(self, action_desc: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "campus_credentials_locked",
            "message": self._cookie_vault_locked_message(action_desc),
            **extra,
        }

    def _cookie_vault_locked(self) -> bool:
        return False

    def _job_has_shared_cache(self, course_id: str, job) -> bool:
        return False

    @staticmethod
    def _clean_pdf_stem(raw: str, fallback: str = "PKU-All-in-One-笔记") -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(raw or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
        return cleaned or fallback

    @staticmethod
    def _normalize_note_variant(raw: str) -> str:
        val = str(raw or "auto").strip().lower()
        if val in {"refined", "精编", "精细", "精细整理", "精细转写", "精编讲义", "考点讲义"}:
            return "refined"
        if val in {"raw", "流水账", "原始", "notes", "raw_notes", "基础笔记"}:
            return "raw"
        return "auto"

    @staticmethod
    def _normalize_export_format(raw: str, act_raw: str = "") -> str:
        val = str(raw or "pdf").strip().lower()
        if act_raw in {"export_zip", "download_zip"} and val == "pdf":
            return "zip"
        return "zip" if val in {"zip", "archive"} else "pdf"

    @staticmethod
    def _match_recording_job(course_name: str, jobs: list[Any], query: str):
        from .panel.recordings_api import recording_id

        q_low = (query or "").strip().casefold()
        for j in jobs:
            if q_low in (
                recording_id(j).casefold(),
                str(getattr(j.recording, "source_id", "") or "").casefold(),
            ):
                return j
        for j in reversed(jobs):
            hay = f"{j.recording.title} {j.recording.date} {j.recording.recorded_at} {j.directory.name}".casefold()
            if q_low in hay:
                return j
        titles = ", ".join(j.recording.title for j in jobs[-8:])
        raise ValueError(f"在《{course_name}》中未找到匹配 '{query}' 的回放；近期讲次：{titles}")

    def _plan_pdf_export_meta(
        self,
        *,
        course_id: str,
        recording_id_val: str,
        variant: str,
        fmt: str,
        summary_only: bool,
        sections: list[Any],
    ) -> tuple[str, str, str]:
        """Compute `(filename, document_title_banner, effective_variant)` consistently for PDF/ZIP export."""
        from .panel.pdf_export import _clean_course_folder_name
        from .panel.recordings_api import find_job
        from .panel.refined_notes import load_saved_refined_note

        first_raw_ctitle = str(sections[0].course_title or course_id or "北大课程") if sections else (course_id or "北大课程")
        ctitle = self._clean_pdf_stem(first_raw_ctitle, "北大课程")

        if summary_only:
            return (
                f"{ctitle}-课程总结.pdf",
                f"{first_raw_ctitle} · 课程总结与复习提纲",
                "summary",
            )
        if fmt == "zip" and not recording_id_val:
            if course_id:
                zip_stem = _clean_course_folder_name(first_raw_ctitle, "课程")
                zip_name = f"{zip_stem}-笔记.zip"
            else:
                zip_name = "笔记.zip"
            eff = variant if variant in {"refined", "raw"} else "mixed"
            return zip_name, "", eff
        if recording_id_val:
            raw_doc = str(sections[0].doc_title or recording_id_val) if sections else recording_id_val
            clean_doc = self._clean_pdf_stem(raw_doc, "课堂笔记")
            used_refined = variant == "refined"
            if variant == "auto":
                job = find_job(self.data_dir, recording_id_val, course_id)
                if job is not None:
                    raw_p = job.directory / "notes.md"
                    raw_t = raw_p.read_text("utf-8", errors="replace") if raw_p.is_file() else ""
                    sref = load_saved_refined_note(job.directory, raw_t)
                    if sref and str(sref.get("markdown") or "").strip() and (not sref.get("stale") or not raw_t.strip()):
                        used_refined = True
            suffix = "-精编考点讲义" if used_refined else ""
            return (
                f"{ctitle}-{clean_doc}{suffix}.pdf",
                f"{first_raw_ctitle} · {raw_doc}",
                "refined" if used_refined else "raw",
            )
        eff = variant if variant in {"refined", "raw"} else "mixed"
        if course_id:
            return (
                f"{ctitle}-全课笔记合集.pdf",
                f"{first_raw_ctitle} · 全课笔记合集",
                eff,
            )
        return (
            "PKU-All-in-One-全部课程笔记合集.pdf",
            "PKU All in One · 全部课程笔记合集",
            eff,
        )

    def render_note_pdf_bytes(
        self,
        *,
        course_id: str = "",
        recording_id: str = "",
        note_variant: str = "auto",
        export_format: str = "pdf",
        include_summary: bool = True,
        summary_only: bool = False,
    ) -> tuple[bytes, str, str]:
        """Render watermarked PDF bytes (or PDF ZIP archive bytes) for download endpoints."""
        from .panel.pdf_export import (
            build_notes_pdf_zip,
            build_watermarked_notes_pdf,
            collect_course_pdf_sections,
        )

        variant = self._normalize_note_variant(note_variant)
        fmt = self._normalize_export_format(export_format)
        cid = str(course_id or "").strip()
        rid = "" if summary_only else str(recording_id or "").strip()
        inc_sum = True if summary_only else bool(include_summary)
        inc_lec = not bool(summary_only)

        if fmt == "zip" and not rid and not summary_only:
            zip_bytes, zip_filename = build_notes_pdf_zip(
                self.data_dir,
                course_id=cid,
                include_summary=inc_sum,
                note_variant=variant,
            )
            if not zip_bytes:
                raise FileNotFoundError(
                    "没有找到符合条件的精细整理讲义可打包。"
                    if variant == "refined"
                    else "当前还没有可导出的课程或课堂笔记。"
                )
            return zip_bytes, zip_filename, "application/zip"

        sections = collect_course_pdf_sections(
            self.data_dir,
            course_id=cid,
            recording_id=rid,
            include_summary=inc_sum,
            include_lectures=inc_lec,
            note_variant=variant,
        )
        if not sections:
            if summary_only:
                raise FileNotFoundError("该课程尚未生成课程总结，请先调用 organize_course_content(action='course_summary')。")
            if variant == "refined":
                raise FileNotFoundError("该讲次或课程尚未生成精细整理讲义，请先调用 organize_course_content(action='refine_lecture')。")
            raise FileNotFoundError("当前还没有可导出的课堂笔记，请先触发转写整理。")

        filename, doc_title_banner, _ = self._plan_pdf_export_meta(
            course_id=cid,
            recording_id_val=rid,
            variant=variant,
            fmt="pdf",
            summary_only=bool(summary_only),
            sections=sections,
        )
        pdf_bytes = build_watermarked_notes_pdf(sections, document_title=doc_title_banner)
        return pdf_bytes, filename, "application/pdf"

    def export_note_pdf(
        self,
        course: str = "",
        lecture: str = "",
        note_variant: str = "auto",
        export_format: str = "pdf",
        include_summary: bool = True,
        download_to_dir: str = "",
        write_file: bool = True,
    ) -> dict:
        """Resolve note PDF/ZIP export target, validate availability, and optionally write locally."""
        from .panel.pdf_export import collect_course_pdf_sections
        from .panel.recordings_api import recording_id as _rec_id
        from .panel.refined_notes import load_saved_refined_note

        variant = self._normalize_note_variant(note_variant)
        fmt = self._normalize_export_format(export_format)
        course_name = ""
        cid = ""
        course_folder = ""
        if (course or "").strip():
            matched = self._match_course(course)
            cid = str(matched["course_id"])
            course_name = str(matched["name"])
            course_folder = str(matched["folder"])

        lec_q = (lecture or "").strip()
        summary_only = lec_q.lower() in {"summary", "总纲", "课程总结"}
        recording_id_val = ""
        lecture_title = ""

        if summary_only:
            if not cid:
                raise ValueError("导出单门课程总结 PDF 请先指定 course。")
            fmt = "pdf"
        elif lec_q and lec_q.lower() not in {"all", "course", "全部", "*"}:
            if not cid:
                raise ValueError("按讲次导出笔记 PDF 时，请同时指定 course。")
            jobs = sorted(
                collect_jobs(self.data_dir, cid),
                key=lambda j: (j.recording.recorded_at, j.recording.title),
            )
            if not jobs:
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_organize",
                    "course": course_name,
                    "course_id": cid,
                    "message": f"课程《{course_name}》当前没有课堂回放记录，无法导出单讲 PDF。",
                }
            if lec_q.lower() in {"latest", "newest", "最新"}:
                target_job = None
                for j in reversed(jobs):
                    if (j.directory / "notes.md").is_file() or (j.directory / "refined_notes.md").is_file():
                        target_job = j
                        break
                if target_job is None:
                    target_job = jobs[-1]
            else:
                target_job = self._match_recording_job(course_name, jobs, lec_q)

            recording_id_val = _rec_id(target_job)
            lecture_title = str(target_job.recording.title or recording_id_val)
            raw_p = target_job.directory / "notes.md"
            raw_t = raw_p.read_text("utf-8", errors="replace") if raw_p.is_file() else ""
            has_raw = bool(raw_t.strip())
            sref = load_saved_refined_note(target_job.directory, raw_t)
            has_refined = bool(sref and str(sref.get("markdown") or "").strip())

            if variant == "refined" and not has_refined:
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_refine",
                    "course": course_name,
                    "course_id": cid,
                    "recording_id": recording_id_val,
                    "lecture_title": lecture_title,
                    "has_raw_notes": has_raw,
                    "message": (
                        f"《{lecture_title}》尚未生成精细整理讲义；"
                        + (
                            "可先调用 organize_course_content(action='refine_lecture') 进行精细整理，或改用 note_variant='raw' 导出基础笔记。"
                            if has_raw
                            else "本讲也尚未生成基础课堂笔记，请先调用 organize_course_content(action='refine_lecture') 触发转写并精细整理。"
                        )
                    ),
                }
            if variant == "raw" and not has_raw:
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_organize",
                    "course": course_name,
                    "course_id": cid,
                    "recording_id": recording_id_val,
                    "lecture_title": lecture_title,
                    "has_refined_notes": has_refined,
                    "message": f"《{lecture_title}》尚未生成基础课堂笔记，请先调用 organize_course_content(action='organize_lecture')。",
                }
            if variant == "auto" and not has_raw and not has_refined:
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_organize",
                    "course": course_name,
                    "course_id": cid,
                    "recording_id": recording_id_val,
                    "lecture_title": lecture_title,
                    "message": f"《{lecture_title}》尚未生成课堂笔记或精细整理讲义，请先触发转写整理。",
                }
            fmt = "pdf"

        inc_sum = True if summary_only else bool(include_summary)
        inc_lec = not bool(summary_only)
        sections = collect_course_pdf_sections(
            self.data_dir,
            course_id=cid,
            recording_id="" if summary_only else recording_id_val,
            include_summary=inc_sum,
            include_lectures=inc_lec,
            note_variant=variant,
        )
        if fmt == "zip" and not recording_id_val and not summary_only:
            real_sections = [s for s in sections if s.doc_title != "课程概览与教学网动态"]
            sections = real_sections if real_sections else sections

        if not sections:
            if summary_only:
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_summary",
                    "course": course_name,
                    "course_id": cid,
                    "message": f"课程《{course_name}》尚未生成课程总结，请先调用 organize_course_content(action='course_summary')。",
                }
            if variant == "refined":
                return {
                    "ok": False,
                    "action": "export_pdf",
                    "status": "needs_refine",
                    "course": course_name,
                    "course_id": cid,
                    "message": "所选范围尚未生成精细整理讲义，请先调用 organize_course_content(action='refine_lecture' / 'batch_refine')。",
                }
            return {
                "ok": False,
                "action": "export_pdf",
                "status": "nothing_to_export",
                "course": course_name,
                "course_id": cid,
                "message": "当前范围还没有可导出的课程笔记或总结。",
            }

        filename, _, effective_variant = self._plan_pdf_export_meta(
            course_id=cid,
            recording_id_val=recording_id_val,
            variant=variant,
            fmt=fmt,
            summary_only=bool(summary_only),
            sections=sections,
        )
        mode = (
            "course_summary_pdf"
            if summary_only
            else (
                "lecture_pdf"
                if recording_id_val
                else (
                    "course_zip"
                    if fmt == "zip" and cid
                    else ("all_zip" if fmt == "zip" else ("course_pdf" if cid else "all_pdf"))
                )
            )
        )
        result: dict[str, Any] = {
            "ok": True,
            "action": "export_pdf",
            "mode": mode,
            "course": course_name or (str(sections[0].course_title or "") if cid and sections else ""),
            "course_id": cid,
            "recording_id": recording_id_val,
            "lecture_title": lecture_title or (str(sections[0].doc_title or "") if recording_id_val and sections else ""),
            "note_variant": variant,
            "effective_variant": effective_variant,
            "export_format": fmt,
            "include_summary": inc_sum,
            "summary_only": bool(summary_only),
            "filename": filename,
            "sections_count": len(sections),
        }

        if write_file:
            raw_bytes, filename, _ = self.render_note_pdf_bytes(
                course_id=cid,
                recording_id=recording_id_val,
                note_variant=variant,
                export_format=fmt,
                include_summary=inc_sum,
                summary_only=bool(summary_only),
            )
            if (download_to_dir or "").strip():
                dest_dir = Path(download_to_dir).expanduser().resolve()
            elif course_folder:
                dest_dir = self._course_dir(course_folder) / "exports"
            else:
                dest_dir = self.data_dir / "_exports"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / filename
            tmp_path = dest_dir / f".{filename}.tmp"
            tmp_path.write_bytes(raw_bytes)
            tmp_path.replace(dest_path)
            result["filename"] = filename
            result["saved_path"] = str(dest_path)
            result["size_bytes"] = len(raw_bytes)
        return result

    def organize_course_content(
        self,
        action: str = "organize_lecture",
        course: str = "",
        lecture: str = "",
        notes_strategy: str = "topics",
        regenerate: bool = False,
        max_lectures: int = 5,
        auto_refine: bool = False,
        note_variant: str = "auto",
        export_format: str = "pdf",
        include_summary: bool = True,
        download_to_dir: str = "",
        write_local_pdf: bool = True,
        work_manager: Any = None,
    ) -> dict:
        """Trigger lecture/course organization (transcribe, refine notes, export PDF, batch, summary, sync, status)."""
        from .panel.campus_catalog import campus_course, campus_courses
        from .panel.course_tracker import build_auto_track_status
        from .panel.recordings_api import (
            BatchOrganizeItem,
            BatchOrganizeRequest,
            batch_organize_preview,
            recording_id,
        )
        from .panel.refined_notes import estimate_refine_cost, load_saved_refined_note

        act_raw = (action or "organize_lecture").strip().lower()
        aliases = {
            "organize": "organize_lecture",
            "organize_lecture": "organize_lecture",
            "transcribe": "organize_lecture",
            "transcribe_lecture": "organize_lecture",
            "process": "organize_lecture",
            "process_lecture": "organize_lecture",
            "batch": "batch_organize",
            "batch_organize": "batch_organize",
            "batch_transcribe": "batch_organize",
            "refine": "refine_lecture",
            "refine_lecture": "refine_lecture",
            "refine_note": "refine_lecture",
            "fine_transcribe": "refine_lecture",
            "refined_notes": "refine_lecture",
            "精细整理": "refine_lecture",
            "精细转写": "refine_lecture",
            "精编": "refine_lecture",
            "batch_refine": "batch_refine",
            "refine_all": "batch_refine",
            "batch_refine_notes": "batch_refine",
            "export_pdf": "export_pdf",
            "download_pdf": "export_pdf",
            "pdf": "export_pdf",
            "export_note_pdf": "export_pdf",
            "export_notes_pdf": "export_pdf",
            "export_zip": "export_pdf",
            "download_zip": "export_pdf",
            "summary": "course_summary",
            "course_summary": "course_summary",
            "sync": "sync_course",
            "sync_course": "sync_course",
            "auto_track": "auto_track_check",
            "auto_track_check": "auto_track_check",
            "status": "status",
        }
        act = aliases.get(act_raw)
        if not act:
            raise ValueError(
                f"未知 action='{action}'；可选值：organize_lecture / batch_organize / refine_lecture / "
                "batch_refine / export_pdf / course_summary / sync_course / auto_track_check / status。"
            )
        resolved_fmt = self._normalize_export_format(export_format, act_raw=act_raw)

        if act == "export_pdf":
            return self.export_note_pdf(
                course=course,
                lecture=lecture,
                note_variant=note_variant,
                export_format=resolved_fmt,
                include_summary=bool(include_summary),
                download_to_dir=download_to_dir,
                write_file=bool(write_local_pdf),
            )

        mgr = work_manager if work_manager is not None else _get_or_create_local_work_manager(self.data_dir)
        has_creds = bool(
            getattr(mgr.settings, "pku_username", "")
            and getattr(mgr.settings, "pku_password", "")
            and getattr(mgr.settings, "pku_password", "") != "demo-readonly"
        )
        cookie_locked = (not has_creds) and self._cookie_vault_locked()
        cred_msg = (
            self._cookie_vault_locked_message("访问北大教学网同步目录或拉取未缓存回放")
            if cookie_locked
            else "请先在网页工作台「连接设置」保存教学网账号与密码（或本地配置 PKU_USERNAME / PKU_PASSWORD）。"
        )
        strategy = notes_strategy if notes_strategy in ("topics", "transcript") else "topics"

        def _needs_model_config(act_name: str, *, need_asr: bool = False) -> dict | None:
            from .ai_provider import is_asr_configured, is_llm_configured

            if not is_llm_configured(mgr.settings, "notes"):
                return {
                    "ok": False,
                    "action": act_name,
                    "status": "needs_model_config",
                    "message": "尚未配置文本大模型：请在本地工作台「模型配置」或 .env 中设置 OPENAI_BASE_URL / OPENAI_API_KEY / NOTES_MODEL。",
                }
            if need_asr and not is_asr_configured(mgr.settings):
                return {
                    "ok": False,
                    "action": act_name,
                    "status": "needs_model_config",
                    "message": "尚未配置语音转写（ASR）：请在本地工作台「模型配置」或 .env 中选择并配置转写后端。",
                }
            return None

        if act == "status":
            task_state = mgr.state()
            bg_running = (task_state.get("background_sync") or {}).get("state") == "running"
            courses_list = campus_courses(self.data_dir).get("courses") or []
            auto_track = build_auto_track_status(
                self.data_dir,
                courses_list,
                campus_configured=has_creds,
                bg_running=bg_running,
            )
            out: dict[str, Any] = {
                "ok": True,
                "action": "status",
                "campus_configured": has_creds,
                "cookie_vault_locked": cookie_locked,
                "task": task_state,
                "auto_track": auto_track,
            }
            if course.strip():
                matched = self._match_course(course)
                cid = str(matched["course_id"])
                detail = campus_course(self.data_dir, cid)
                out["course"] = matched["name"]
                out["course_id"] = cid
                out["recordings"] = [
                    {
                        "recording_id": lesson.get("id"),
                        "title": lesson.get("title"),
                        "date": lesson.get("date", ""),
                        "status": lesson.get("status", ""),
                        "notes_available": bool(lesson.get("notes_available")),
                        "refined_available": bool(lesson.get("refined_available")),
                        "transcript_available": bool(lesson.get("transcript_available")),
                    }
                    for lesson in (detail.get("lessons") or [])
                ]
            return out

        if act == "course_summary":
            cfg_err = _needs_model_config("course_summary")
            if cfg_err is not None:
                return cfg_err
            cid = str(self._match_course(course)["course_id"]) if course.strip() else ""
            task_state = mgr.start_course_summaries(course_id=cid)
            return {
                "ok": True,
                "action": "course_summary",
                "status": "started",
                "course_id": cid,
                "task": task_state,
                "tip": "课程总结生成已启动，可调用 organize_course_content(action='status') 查看进度。",
            }

        if act == "sync_course":
            if not has_creds:
                return {
                    "ok": False,
                    "action": "sync_course",
                    "status": "needs_campus_credentials",
                    "cookie_vault_locked": cookie_locked,
                    "message": cred_msg,
                }
            cid = str(self._match_course(course)["course_id"]) if course.strip() else ""
            task_state = mgr.start_sync(course_id=cid)
            return {
                "ok": True,
                "action": "sync_course",
                "status": "started",
                "course_id": cid,
                "task": task_state,
            }

        if act == "auto_track_check":
            if not has_creds:
                return {
                    "ok": False,
                    "action": "auto_track_check",
                    "status": "needs_campus_credentials",
                    "cookie_vault_locked": cookie_locked,
                    "message": cred_msg,
                }
            res = mgr.trigger_auto_track_if_due(trigger="mcp", force=True)
            return {
                "ok": bool(res.get("triggered")),
                "action": "auto_track_check",
                **res,
            }

        if act == "batch_refine":
            target_cid = str(self._match_course(course)["course_id"]) if course.strip() else ""
            limit = max(1, min(20, int(max_lectures or 5)))
            selected_pairs: list[tuple[str, str]] = []
            selected_meta: list[dict[str, str]] = []
            for c_item in self._matching_courses(course):
                cid = str(c_item["course_id"])
                if target_cid and cid != target_cid:
                    continue
                c_jobs = sorted(
                    collect_jobs(self.data_dir, cid),
                    key=lambda j: (j.recording.recorded_at, j.recording.title),
                )
                for j in reversed(c_jobs):
                    notes_p = j.directory / "notes.md"
                    raw_md = notes_p.read_text("utf-8", errors="replace") if notes_p.is_file() else ""
                    if not raw_md.strip():
                        continue
                    saved_ref = load_saved_refined_note(j.directory, raw_md)
                    if saved_ref is not None:
                        continue
                    rid = recording_id(j)
                    selected_pairs.append((cid, rid))
                    selected_meta.append(
                        {
                            "course_id": cid,
                            "course_title": str(c_item["name"]),
                            "recording_id": rid,
                            "title": str(j.recording.title or ""),
                            "date": str(j.recording.date or ""),
                        }
                    )
                    if len(selected_pairs) >= limit:
                        break
                if len(selected_pairs) >= limit:
                    break
            if not selected_pairs:
                return {
                    "ok": True,
                    "action": "batch_refine",
                    "status": "nothing_to_refine",
                    "message": "所有已有笔记的讲次均已完成精细整理（不支持重复生成）。",
                }
            cfg_err = _needs_model_config("batch_refine")
            if cfg_err is not None:
                return cfg_err
            task_state = mgr.start_batch_refine(selected_pairs, force=False)
            return {
                "ok": True,
                "action": "batch_refine",
                "status": "started",
                "queued_count": len(selected_pairs),
                "items": selected_meta,
                "task": task_state,
                "tip": "批量精细整理已在后台启动，可调用 organize_course_content(action='status') 查看实时进度。",
            }

        if act == "refine_lecture":
            if not course.strip():
                raise ValueError("请指定要精细整理的课程名称或 course_id（或使用 action='batch_refine' 批量精细整理）。")
            matched = self._match_course(course)
            cid = str(matched["course_id"])
            jobs = sorted(
                collect_jobs(self.data_dir, cid),
                key=lambda j: (j.recording.recorded_at, j.recording.title),
            )
            if not jobs:
                return {
                    "ok": False,
                    "action": "refine_lecture",
                    "status": "no_recordings",
                    "course": matched["name"],
                    "course_id": cid,
                    "message": f"课程《{matched['name']}》当前没有课堂回放记录；可先运行 action='sync_course' 刷新目录。",
                }
            lec_q = (lecture or "").strip()
            target_job = None
            if not lec_q or lec_q.lower() in ("latest", "newest", "最新"):
                unrefined_with_notes = []
                unorganized = []
                for j in jobs:
                    notes_p = j.directory / "notes.md"
                    raw_t = notes_p.read_text("utf-8", errors="replace") if notes_p.is_file() else ""
                    if raw_t.strip():
                        sref = load_saved_refined_note(j.directory, raw_t)
                        if sref is None:
                            unrefined_with_notes.append(j)
                    else:
                        unorganized.append(j)
                if unrefined_with_notes:
                    target_job = unrefined_with_notes[-1]
                elif unorganized:
                    target_job = unorganized[-1]
                else:
                    target_job = jobs[-1]
            else:
                target_job = self._match_recording_job(matched["name"], jobs, lec_q)

            cfg_err = _needs_model_config("refine_lecture")
            if cfg_err is not None:
                return cfg_err

            rid = recording_id(target_job)
            notes_path = target_job.directory / "notes.md"
            raw_md = notes_path.read_text("utf-8", errors="replace") if notes_path.is_file() else ""
            has_notes = bool(raw_md.strip())

            if has_notes:
                saved_ref = load_saved_refined_note(target_job.directory, raw_md)
                if saved_ref is not None:
                    return {
                        "ok": True,
                        "action": "refine_lecture",
                        "status": "already_refined",
                        "reused": True,
                        "points_charged": 0.0,
                        "course": matched["name"],
                        "course_id": cid,
                        "recording_id": rid,
                        "lecture_title": target_job.recording.title,
                        "lecture_date": target_job.recording.date,
                        "tip": "该讲已完成精细整理，不支持重复生成，可直接调用 read_lecture 或 organize_course_content(action='export_pdf', note_variant='refined') 查看或导出 PDF。",
                    }

            has_tx = (target_job.directory / "transcript.json").is_file()
            in_shared = self._job_has_shared_cache(cid, target_job)
            if not has_notes:
                needs_portal = (not has_tx) and (not target_job.video.is_file()) and (not in_shared)
                if needs_portal and not has_creds:
                    return {
                        "ok": False,
                        "action": "refine_lecture",
                        "status": "needs_campus_credentials",
                        "cookie_vault_locked": cookie_locked,
                        "course": matched["name"],
                        "course_id": cid,
                        "recording_id": rid,
                        "lecture_title": target_job.recording.title,
                        "message": cred_msg,
                    }

            task_state = mgr.start_refine_note(
                rid,
                cid,
                force=False,
                notes_strategy=strategy,
                auto_organize=True,
            )
            return {
                "ok": True,
                "action": "refine_lecture",
                "status": "started",
                "will_transcribe_first": not has_notes,
                "course": matched["name"],
                "course_id": cid,
                "recording_id": rid,
                "lecture_title": target_job.recording.title,
                "lecture_date": target_job.recording.date,
                "refine_estimate": estimate_refine_cost(raw_md) if has_notes else None,
                "task": task_state,
                "tip": (
                    "已启动「转写 -> 课堂笔记 -> AI 精细整理」全流程，可调用 organize_course_content(action='status') 查看进度。"
                    if not has_notes
                    else "AI 精细整理已启动，可调用 organize_course_content(action='status') 查看进度；完成后可用 read_lecture 或 export_pdf 获取。"
                ),
            }

        if act == "organize_lecture" and (lecture or "").strip().lower() in ("all", "all_unorganized", "*"):
            act = "batch_organize"

        if act == "batch_organize":
            target_cid = str(self._match_course(course)["course_id"]) if course.strip() else ""
            limit = max(1, min(20, int(max_lectures or 5)))
            preview = batch_organize_preview(mgr.settings)
            jobs_by_pair = {
                (job.recording.course_id, recording_id(job)): job
                for job in collect_jobs(self.data_dir, target_cid)
            }
            selected_items: list[BatchOrganizeItem] = []
            selected_meta: list[dict[str, str]] = []
            any_needs_portal = False
            for c_row in preview.get("courses") or []:
                cid = str(c_row.get("course_id") or "")
                if target_cid and cid != target_cid:
                    continue
                lessons = list(c_row.get("lessons") or [])
                for lesson in reversed(lessons):
                    if not lesson.get("eligible") or not lesson.get("needs_organize"):
                        continue
                    rid = str(lesson.get("id") or "")
                    job = jobs_by_pair.get((cid, rid))
                    has_local_tx = bool(lesson.get("transcript_available")) or (
                        job is not None and ((job.directory / "transcript.json").is_file() or job.video.is_file())
                    )
                    in_shared = (job is not None) and self._job_has_shared_cache(cid, job)
                    if not (has_local_tx or in_shared):
                        any_needs_portal = True
                        if not has_creds:
                            continue
                    selected_items.append(BatchOrganizeItem(course_id=cid, recording_id=rid))
                    selected_meta.append({
                        "course_id": cid,
                        "course_title": str(c_row.get("title") or ""),
                        "recording_id": rid,
                        "title": str(lesson.get("title") or ""),
                        "date": str(lesson.get("date") or ""),
                    })
                    if len(selected_items) >= limit:
                        break
                if len(selected_items) >= limit:
                    break
            if not selected_items:
                if any_needs_portal and not has_creds:
                    return {
                        "ok": False,
                        "action": "batch_organize",
                        "status": "needs_campus_credentials",
                        "cookie_vault_locked": cookie_locked,
                        "message": cred_msg,
                    }
                return {
                    "ok": True,
                    "action": "batch_organize",
                    "status": "nothing_to_organize",
                    "message": "没有待整理的未生成笔记回放。",
                }
            cfg_err = _needs_model_config("batch_organize")
            if cfg_err is not None:
                return cfg_err
            req = BatchOrganizeRequest(
                items=selected_items,
                notes_strategy=strategy,
            )
            task_state = mgr.start_batch_organize(req)
            return {
                "ok": True,
                "action": "batch_organize",
                "status": "started",
                "queued_count": len(selected_items),
                "notes_strategy": strategy,
                "items": selected_meta,
                "task": task_state,
                "tip": "批量整理已在后台并行启动，可调用 organize_course_content(action='status') 查看实时进度。",
            }

        # act == "organize_lecture"
        if not course.strip():
            raise ValueError("请指定要整理的课程名称或 course_id（或使用 action='batch_organize' 批量整理）。")
        matched = self._match_course(course)
        cid = str(matched["course_id"])
        jobs = sorted(
            collect_jobs(self.data_dir, cid),
            key=lambda j: (j.recording.recorded_at, j.recording.title),
        )
        if not jobs:
            return {
                "ok": False,
                "action": "organize_lecture",
                "status": "no_recordings",
                "course": matched["name"],
                "course_id": cid,
                "message": f"课程《{matched['name']}》当前没有课堂回放记录；可先运行 action='sync_course' 刷新目录。",
            }
        lec_q = (lecture or "").strip()
        target_job = None
        if not lec_q or lec_q.lower() in ("latest", "newest", "最新"):
            if regenerate:
                candidates = [
                    j for j in jobs
                    if (j.directory / "notes.md").is_file() and (j.directory / "transcript.json").is_file()
                ]
                if candidates:
                    target_job = candidates[-1]
            else:
                unorganized = [j for j in jobs if not (j.directory / "notes.md").is_file()]
                if not unorganized:
                    return {
                        "ok": True,
                        "action": "organize_lecture",
                        "status": "already_organized",
                        "course": matched["name"],
                        "course_id": cid,
                        "message": f"课程《{matched['name']}》的全部 {len(jobs)} 讲回放均已生成笔记；如需重写请传 regenerate=true 并指定 lecture，或调用 action='refine_lecture' 进行精细整理。",
                    }
                target_job = unorganized[-1]
        else:
            target_job = self._match_recording_job(matched["name"], jobs, lec_q)
        if target_job is None:
            titles = ", ".join(j.recording.title for j in jobs[-8:])
            raise ValueError(f"在《{matched['name']}》中未找到匹配 '{lecture}' 的回放；近期讲次：{titles}")

        rid = recording_id(target_job)
        has_tx = (target_job.directory / "transcript.json").is_file()
        in_shared = self._job_has_shared_cache(cid, target_job)
        needs_portal = (not regenerate) and (not has_tx) and (not target_job.video.is_file()) and (not in_shared)
        if needs_portal and not has_creds:
            return {
                "ok": False,
                "action": "organize_lecture",
                "status": "needs_campus_credentials",
                "cookie_vault_locked": cookie_locked,
                "course": matched["name"],
                "course_id": cid,
                "recording_id": rid,
                "lecture_title": target_job.recording.title,
                "message": cred_msg,
            }

        cfg_err = _needs_model_config("organize_lecture", need_asr=(not has_tx))
        if cfg_err is not None:
            return cfg_err

        task_state = mgr.start_campus_process(
            rid,
            cid,
            regenerate=bool(regenerate),
            notes_strategy=strategy,
            auto_refine=bool(auto_refine),
        )
        return {
            "ok": True,
            "action": "organize_lecture",
            "status": "started",
            "course": matched["name"],
            "course_id": cid,
            "recording_id": rid,
            "lecture_title": target_job.recording.title,
            "lecture_date": target_job.recording.date,
            "notes_strategy": strategy,
            "auto_refine": bool(auto_refine),
            "shared_cache_hit": bool(in_shared and not has_tx),
            "task": task_state,
            "tip": (
                "后台转写与精细整理串联任务已启动，可调用 organize_course_content(action='status') 查看进度。"
                if auto_refine
                else "后台整理已启动，可调用 organize_course_content(action='status') 查看进度，完成后用 read_lecture 读取笔记或 action='export_pdf' 下载 PDF。"
            ),
        }


    def _course_dir(self, course_folder: str) -> Path:
        if not course_folder or "/" in course_folder or "\\" in course_folder:
            raise ValueError("course_folder must be one direct child directory name")
        path = self._resolve(course_folder)
        if not path.is_dir():
            raise FileNotFoundError(f"course folder not found: {course_folder}")
        return path

    def _resolve(self, relative_path: str) -> Path:
        raw = Path(relative_path)
        if not relative_path or raw.is_absolute():
            raise ValueError("path must be relative to DATA_DIR")
        path = (self.data_dir / raw).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError("path escapes DATA_DIR")
        if not path.exists():
            raise FileNotFoundError(f"path not found: {relative_path}")
        return path


def _section_patterns(section: TextSection) -> tuple[str, ...]:
    patterns = {
        "assignments": ("assignments.md", "deadlines.json"),
        "announcements": ("announcements/*.md",),
        "materials": ("materials/**/*",),
        "recordings": (
            "recordings/index.json",
            "recordings/*/recording.json",
            "recordings/*/transcript.json",
            "recordings/*/notes.md",
        ),
    }
    if section == "all":
        return tuple(pattern for values in patterns.values() for pattern in values)
    try:
        return patterns[section]
    except KeyError as exc:
        raise ValueError(f"unknown section: {section}") from exc
