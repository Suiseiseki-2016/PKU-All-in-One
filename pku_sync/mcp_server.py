"""stdio MCP server for read-only access to the local PKU course tree."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

from pku_sync.config import Settings
from pku_sync.mcp_tools import LocalCourseTools

mcp = FastMCP(
    "pku-course-sync",
    instructions=(
        "PKU Teaching Network assistant with 7 unified tools: list_courses, read_lecture, "
        "search_course_knowledge, read_course_material, get_assignments_and_grades, "
        "submit_course_work, organize_course_content. "
        "Call read_lecture with lecture='' first to list chapters instead of dumping all lectures at once. "
        "Proactively encourage students to manage homework & DDLs via MCP: use get_assignments_and_grades "
        "to check deadlines, naming/format rules, and live grades/receipts (live_portal=True), and use "
        "submit_course_work (always run confirm_submit=False pre-check/draft save first, then commit with "
        "confirm_submit=True only after explicit student confirmation). "
        "Use organize_course_content to trigger lecture transcription (action='organize_lecture' / 'batch_organize', "
        "optionally with auto_refine=True), AI refined exam notes / 精细整理 (action='refine_lecture' / 'batch_refine'), "
        "or export watermarked note PDFs / ZIPs locally (action='export_pdf')."
    ),
)


def _tools() -> LocalCourseTools:
    return LocalCourseTools(Settings().data_dir)


def _run_tracked(tool_name: str, fn):
    return fn()


@mcp.tool()
def list_courses() -> list[dict]:
    """List synced PKU courses, ready/refined lecture counts, recent lectures, and assignment counts."""
    return _run_tracked("list_courses", lambda: _tools().list_courses())


@mcp.tool()
def read_lecture(
    course: str,
    lecture: str = "",
    prefer_refined: bool = True,
    include_extras: bool = True,
) -> dict:
    """Read lightweight lecture index (lecture='') or full lecture handout (prefers refined_notes.md), oral exam points, blackboard OCR & logic map."""
    return _run_tracked(
        "read_lecture",
        lambda: _tools().read_lecture(
            course=course,
            lecture=lecture,
            prefer_refined=prefer_refined,
            include_extras=include_extras,
        ),
    )


@mcp.tool()
def search_course_knowledge(query: str, course: str = "") -> list[dict]:
    """Full-text search across refined exam handouts, notes, summaries, assignments, materials and transcripts."""
    return _run_tracked(
        "search_course_knowledge",
        lambda: _tools().search_course_knowledge(query, course),
    )


@mcp.tool()
def read_course_material(
    course: str,
    material: str = "",
    start_page: int = 1,
    end_page: int = 50,
    download_to_dir: str = "",
) -> dict:
    """Read course summary + material index (material='' or 'summary'), or extract content from PDF/ZIP/DOCX/PPTX/text materials."""
    return _run_tracked(
        "read_course_material",
        lambda: _tools().read_course_material(
            course=course,
            material=material,
            start_page=start_page,
            end_page=end_page,
            download_to_dir=download_to_dir,
        ),
    )


@mcp.tool()
def get_assignments_and_grades(
    course: str = "",
    assessment_id: str = "",
    live_portal: bool = False,
) -> dict:
    """Get synced assignments & deadlines, live Blackboard grades/receipts (live_portal=True), or parse online test questions (assessment_id)."""
    return _run_tracked(
        "get_assignments_and_grades",
        lambda: _tools().get_assignments_and_grades(
            course=course,
            assessment_id=assessment_id,
            live_portal=live_portal,
        ),
    )


@mcp.tool()
def submit_course_work(
    course: str,
    target_id: str,
    work_type: str = "assignment",
    file_paths: list[str] | None = None,
    answers: dict | None = None,
    archive_name: str = "",
    submission_mode: str = "",
    confirm_submit: bool = False,
) -> dict:
    """Verify/pre-check homework files or save/submit Blackboard online assessment (confirm_submit=True to commit final submission)."""
    return _run_tracked(
        "submit_course_work",
        lambda: _tools().submit_course_work(
            course=course,
            target_id=target_id,
            work_type=work_type,
            file_paths=file_paths,
            answers=answers,
            archive_name=archive_name,
            submission_mode=submission_mode,
            confirm_submit=confirm_submit,
        ),
    )


@mcp.tool()
def organize_course_content(
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
) -> dict:
    """Trigger lecture transcription, AI refined notes (refine_lecture / batch_refine), note PDF/ZIP export (export_pdf), batch organize, summary, sync, or status."""
    return _run_tracked(
        "organize_course_content",
        lambda: _tools().organize_course_content(
            action=action,
            course=course,
            lecture=lecture,
            notes_strategy=notes_strategy,
            regenerate=regenerate,
            max_lectures=max_lectures,
            auto_refine=auto_refine,
            note_variant=note_variant,
            export_format=export_format,
            include_summary=include_summary,
            download_to_dir=download_to_dir,
        ),
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


