"""Local BYOK course workspace, model settings, MCP downloads, and recording APIs."""

from __future__ import annotations

import copy as _copy
import mimetypes as _mimetypes
import os as _os
import re as _re
from pathlib import Path
from typing import Any
from urllib.parse import quote as _url_quote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel

from .. import ai_provider as _ai_provider
from ..envfile import write_env_values
from ..notion_login import env_file_path
from .ai_bridge import LocalAIBridge
from .jobs import ExerciseJobGate

_BLOCKED_MATERIAL_SUFFIXES = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".com",
        ".scr",
        ".msi",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".bash",
        ".zsh",
        ".vbs",
        ".js",
        ".mjs",
        ".cjs",
        ".html",
        ".htm",
        ".xhtml",
        ".svg",
        ".php",
        ".jsp",
        ".asp",
        ".aspx",
        ".jar",
        ".app",
        ".dmg",
        ".pkg",
        ".deb",
        ".rpm",
    }
)


class ModelSettingsUpdate(BaseModel):
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    clear_openai_api_key: bool = False
    notes_model: str | None = None
    refine_model: str | None = None
    notes_strategy: str | None = None
    vision_enabled: bool | None = None
    vision_base_url: str | None = None
    vision_api_key: str | None = None
    clear_vision_api_key: bool = False
    vision_model: str | None = None
    transcription_backend: str | None = None
    asr_base_url: str | None = None
    asr_api_key: str | None = None
    clear_asr_api_key: bool = False
    asr_model: str | None = None
    dashscope_api_key: str | None = None
    clear_dashscope_api_key: bool = False
    whisper_model: str | None = None
    whisper_device: str | None = None
    whisper_compute_type: str | None = None


class ModelTestRequest(BaseModel):
    target: str = "llm"
    overrides: ModelSettingsUpdate | None = None


def _clone_settings(settings: object) -> object:
    if hasattr(settings, "model_copy"):
        return settings.model_copy()
    return _copy.copy(settings)


def _apply_model_patch(target: object, patch: ModelSettingsUpdate) -> dict[str, str]:
    """Apply non-None model config fields onto ``target`` and return `.env` updates."""
    env_updates: dict[str, str] = {}

    str_map = (
        ("openai_base_url", "OPENAI_BASE_URL"),
        ("notes_model", "NOTES_MODEL"),
        ("refine_model", "REFINE_MODEL"),
        ("vision_base_url", "VISION_BASE_URL"),
        ("vision_model", "VISION_MODEL"),
        ("asr_base_url", "ASR_BASE_URL"),
        ("asr_model", "ASR_MODEL"),
        ("whisper_model", "WHISPER_MODEL"),
        ("whisper_device", "WHISPER_DEVICE"),
        ("whisper_compute_type", "WHISPER_COMPUTE_TYPE"),
    )
    for attr, env_key in str_map:
        val = getattr(patch, attr)
        if val is not None:
            clean = str(val).strip()
            setattr(target, attr, clean)
            env_updates[env_key] = clean

    if patch.notes_strategy is not None:
        strategy = str(patch.notes_strategy).strip().lower()
        if strategy not in {"topics", "transcript"}:
            raise HTTPException(status_code=422, detail="笔记模式仅支持 topics 或 transcript。")
        setattr(target, "notes_strategy", strategy)
        env_updates["NOTES_STRATEGY"] = strategy

    if patch.transcription_backend is not None:
        backend = str(patch.transcription_backend).strip().lower()
        if backend not in {"openai", "dashscope", "local"}:
            raise HTTPException(
                status_code=422,
                detail="语音转写后端仅支持 openai、dashscope 或 local。",
            )
        setattr(target, "transcription_backend", backend)
        env_updates["TRANSCRIPTION_BACKEND"] = backend

    if patch.vision_enabled is not None:
        enabled = bool(patch.vision_enabled)
        setattr(target, "vision_enabled", enabled)
        env_updates["VISION_ENABLED"] = "true" if enabled else "false"

    secret_map = (
        ("openai_api_key", "clear_openai_api_key", "OPENAI_API_KEY"),
        ("vision_api_key", "clear_vision_api_key", "VISION_API_KEY"),
        ("asr_api_key", "clear_asr_api_key", "ASR_API_KEY"),
        ("dashscope_api_key", "clear_dashscope_api_key", "DASHSCOPE_API_KEY"),
    )
    for attr, clear_attr, env_key in secret_map:
        if getattr(patch, clear_attr):
            setattr(target, attr, "")
            env_updates[env_key] = ""
        else:
            raw_secret = getattr(patch, attr)
            if raw_secret is not None and str(raw_secret).strip():
                clean_secret = str(raw_secret).strip()
                setattr(target, attr, clean_secret)
                env_updates[env_key] = clean_secret

    return env_updates


def _serialize_model_settings(settings: object) -> dict[str, Any]:
    llm_base, _, llm_model = _ai_provider.resolve_llm_config(settings, "notes")
    _, _, refine_model = _ai_provider.resolve_llm_config(settings, "refine")
    v_enabled, v_base, _, v_model = _ai_provider.resolve_vision_config(settings)
    asr_cfg = _ai_provider.resolve_asr_config(settings)

    openai_key = str(getattr(settings, "openai_api_key", "") or "").strip()
    vision_key = str(getattr(settings, "vision_api_key", "") or "").strip()
    asr_key = str(getattr(settings, "asr_api_key", "") or "").strip()
    dashscope_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()

    return {
        "ok": True,
        "openai_base_url": str(getattr(settings, "openai_base_url", "") or ""),
        "openai_api_key_masked": _ai_provider.mask_secret(openai_key),
        "openai_api_key_set": bool(openai_key),
        "notes_model": str(getattr(settings, "notes_model", "") or ""),
        "refine_model": str(getattr(settings, "refine_model", "") or ""),
        "notes_strategy": str(getattr(settings, "notes_strategy", "topics") or "topics"),
        "vision_enabled": bool(getattr(settings, "vision_enabled", True)),
        "vision_base_url": str(getattr(settings, "vision_base_url", "") or ""),
        "vision_api_key_masked": _ai_provider.mask_secret(vision_key),
        "vision_api_key_set": bool(vision_key),
        "vision_model": str(getattr(settings, "vision_model", "") or ""),
        "transcription_backend": asr_cfg["backend"],
        "asr_base_url": str(getattr(settings, "asr_base_url", "") or ""),
        "asr_api_key_masked": _ai_provider.mask_secret(asr_key),
        "asr_api_key_set": bool(asr_key),
        "asr_model": str(getattr(settings, "asr_model", "whisper-1") or "whisper-1"),
        "dashscope_api_key_masked": _ai_provider.mask_secret(dashscope_key),
        "dashscope_api_key_set": bool(dashscope_key),
        "whisper_model": str(getattr(settings, "whisper_model", "small") or "small"),
        "whisper_device": str(getattr(settings, "whisper_device", "auto") or "auto"),
        "whisper_compute_type": str(getattr(settings, "whisper_compute_type", "auto") or "auto"),
        "llm_configured": _ai_provider.is_llm_configured(settings, "notes"),
        "vision_configured": _ai_provider.is_vision_configured(settings),
        "asr_configured": _ai_provider.is_asr_configured(settings),
        "local_asr_available": _ai_provider.local_asr_available(),
        "resolved": {
            "llm_base_url": llm_base,
            "llm_model": llm_model,
            "refine_model": refine_model,
            "vision_enabled": v_enabled,
            "vision_base_url": v_base,
            "vision_model": v_model,
            "asr_backend": asr_cfg["backend"],
            "asr_base_url": asr_cfg.get("base_url", ""),
            "asr_model": asr_cfg.get("model", ""),
        },
        "presets": _ai_provider.MODEL_PRESETS,
    }


def _attachment_headers(filename: str) -> dict[str, str]:
    clean_name = _re.sub(r'[\\/:*?"<>|\r\n]+', "-", str(filename or "download")).strip(" .-") or "download"
    p = Path(clean_name)
    stem_ascii = _re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem).strip("._-") or "download"
    suffix_ascii = _re.sub(r"[^A-Za-z0-9.]+", "", p.suffix)
    ascii_name = f"{stem_ascii}{suffix_ascii}"
    return {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{_url_quote(clean_name)}",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _parse_bool_param(raw: str | None, *, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    settings=None,
    connection_service=None,
    platform_service=None,
    exercise_job_gate: ExerciseJobGate | None = None,
    update_service=None,
    notion_opener=None,
    submission_fence_factory=None,
) -> FastAPI:
    """Build the local open-source BYOK workspace FastAPI app."""
    if settings is None:
        from ..config import settings as default_settings

        settings = default_settings
    if connection_service is None:
        from .connection import make_connection_service

        connection_service = make_connection_service(settings)
    if platform_service is None:
        platform_service = LocalAIBridge(settings)

    app = FastAPI(title="pku-sync panel", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.connection_service = connection_service
    app.state.platform_service = platform_service

    if exercise_job_gate is None:
        exercise_job_gate = ExerciseJobGate()
    app.state.exercise_job_gate = exercise_job_gate

    if update_service is None:
        from ..autoupdate import UpdateService

        update_service = UpdateService.from_settings(settings)
    app.state.update_service = update_service

    from .connection_api import add_connection_routes
    from .student_ui import add_student_ui_routes
    from .external_navigation import add_external_navigation_routes
    from .recordings_api import RecordingWorkManager, add_recording_routes
    from .course_tracker import AutoTrackScheduler
    from .assignment_workflow import add_assignment_routes
    from .campus_quiz import CampusQuizService
    from .campus_quiz_api import add_campus_quiz_routes
    from .campus_quiz_grading import CampusQuizGrading
    from .connection import FakeConnectionProvider

    add_connection_routes(app, connection_service)
    add_student_ui_routes(app)
    add_external_navigation_routes(app, opener=notion_opener)

    recording_manager = RecordingWorkManager(settings)
    recording_manager.platform_service = platform_service
    app.state.recording_manager = recording_manager

    demo_connection = (
        isinstance(connection_service.provider, FakeConnectionProvider)
        and not getattr(settings, "notion_token", "")
    )
    add_recording_routes(app, recording_manager, demo_connection=demo_connection)

    auto_track_scheduler = AutoTrackScheduler(app, poll_seconds=60.0)
    app.state.auto_track_scheduler = auto_track_scheduler
    if _os.environ.get("PKU_AUTO_TRACK_DAEMON", "1") != "0" and not _os.environ.get("PYTEST_CURRENT_TEST"):
        auto_track_scheduler.start()

    add_assignment_routes(app, recording_manager, fence_factory=submission_fence_factory)
    app.state.campus_quiz_service = CampusQuizService(settings, platform_service)
    app.state.campus_quiz_grading = CampusQuizGrading(settings, platform_service, app.state.campus_quiz_service)
    add_campus_quiz_routes(app, app.state.campus_quiz_service, exercise_job_gate, app.state.campus_quiz_grading)

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/app", status_code=307)

    @app.get("/healthz")
    def healthz() -> dict:
        instance_id = _os.environ.get("PKU_PANEL_INSTANCE_ID")
        return {"ok": True, **({"instance_id": instance_id} if instance_id else {})}

    @app.get("/api/update")
    def update_status() -> dict:
        return update_service.panel_state()

    @app.post("/api/update/request")
    def request_update(version: str = Body(..., embed=True)) -> dict:
        try:
            update_service.request_apply(version)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid update request") from exc
        return update_service.panel_state()

    @app.get("/api/settings/models")
    def get_model_settings() -> dict:
        with recording_manager._lock:
            return _serialize_model_settings(settings)

    @app.post("/api/settings/models")
    def update_model_settings(body: ModelSettingsUpdate) -> dict:
        with recording_manager._lock:
            staged = _clone_settings(settings)
            env_updates = _apply_model_patch(staged, body)
            if env_updates:
                try:
                    write_env_values(env_file_path(settings), env_updates)
                except OSError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail="无法写入本地 .env 配置文件，请检查目录权限或磁盘空间。",
                    ) from exc
            _apply_model_patch(settings, body)
            return _serialize_model_settings(settings)

    @app.post("/api/settings/models/test")
    def test_model_settings(body: ModelTestRequest) -> dict:
        target = str(body.target or "llm").strip().lower()
        if target not in {"llm", "vision", "asr"}:
            raise HTTPException(status_code=422, detail="测试目标仅支持 llm、vision 或 asr。")
        with recording_manager._lock:
            candidate = _clone_settings(settings)
            if body.overrides is not None:
                _apply_model_patch(candidate, body.overrides)
        res = _ai_provider.test_connection(candidate, target=target)
        detail = str(res.get("detail") or "")
        return {**res, "message": detail}

    @app.get("/api/mcp/download-material")
    def mcp_download_material_endpoint(request: Request):
        from ..mcp_tools import LocalCourseTools

        root = Path(getattr(settings, "data_dir", "data")).expanduser().resolve()
        course = (request.query_params.get("course") or "").strip()
        material = (request.query_params.get("material") or "").strip()
        if not course or not material:
            raise HTTPException(status_code=400, detail="请提供 course 与 material 参数。")
        try:
            fetched = LocalCourseTools(root).fetch_course_material_file(course=course, material=material)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        resolved_file = fetched.resolve()
        if not resolved_file.is_relative_to(root):
            raise HTTPException(status_code=403, detail="无权访问目录外的课程文件。")
        if resolved_file.suffix.lower() in _BLOCKED_MATERIAL_SUFFIXES:
            raise HTTPException(status_code=403, detail="该文件类型不允许直接下载。")
        return FileResponse(
            resolved_file,
            media_type=_mimetypes.guess_type(resolved_file.name)[0] or "application/octet-stream",
            headers=_attachment_headers(resolved_file.name),
        )

    @app.get("/api/mcp/download-note-pdf")
    def mcp_download_note_pdf_endpoint(request: Request):
        from ..mcp_tools import LocalCourseTools

        root = Path(getattr(settings, "data_dir", "data")).expanduser().resolve()
        course_id_param = (request.query_params.get("course_id") or "").strip()
        course_param = (request.query_params.get("course") or "").strip()
        recording_id_param = (request.query_params.get("recording_id") or "").strip()
        lecture_param = (request.query_params.get("lecture") or "").strip()
        variant = (
            request.query_params.get("variant")
            or request.query_params.get("note_variant")
            or "auto"
        ).strip()
        fmt = (
            request.query_params.get("format")
            or request.query_params.get("export_format")
            or "pdf"
        ).strip()
        include_summary = _parse_bool_param(request.query_params.get("include_summary"), default=True)
        summary_only = _parse_bool_param(request.query_params.get("summary_only"), default=False)
        course_q = course_param or course_id_param
        lecture_q = lecture_param or recording_id_param
        if summary_only and not lecture_q:
            lecture_q = "summary"

        tools = LocalCourseTools(root)
        try:
            meta = tools.export_note_pdf(
                course=course_q,
                lecture=lecture_q,
                note_variant=variant,
                export_format=fmt,
                include_summary=include_summary,
                write_file=False,
            )
            if not meta.get("ok"):
                raise HTTPException(
                    status_code=404,
                    detail=str(meta.get("message") or "未找到可导出的笔记 PDF。"),
                )
            raw_bytes, filename, media_type = tools.render_note_pdf_bytes(
                course_id=str(meta.get("course_id") or ""),
                recording_id=str(meta.get("recording_id") or ""),
                note_variant=str(meta.get("note_variant") or variant),
                export_format=str(meta.get("export_format") or fmt),
                include_summary=bool(meta.get("include_summary", include_summary)),
                summary_only=bool(meta.get("summary_only", summary_only)),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=raw_bytes,
            media_type=media_type,
            headers=_attachment_headers(filename),
        )

    return app
