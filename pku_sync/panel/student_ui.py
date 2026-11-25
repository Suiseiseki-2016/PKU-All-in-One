"""The student-facing panel UI: copy source + same-origin asset routes."""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .connection import (
    CONNECTED_COPY,
    CONNECT_BUSY_MESSAGE,
    CONNECT_FAILED_COPY,
    CONNECTING_COPY,
    DISCONNECT_TOAST,
    DISCONNECTED_COPY,
    RECONNECT_LABEL,
    RECONNECT_TOAST,
    REVOKE_LABEL,
)

STUDENT_UI_PATH = "/app"
STATIC_DIR = Path(__file__).resolve().parent / "static"

CONNECTION_COPY = {
    "workspace_wait_hint": "首次创建会按课程建立 Notion 页面结构；页面保持打开即可，完成后会自动刷新。",
    "connected": CONNECTED_COPY,
    "disconnected": DISCONNECTED_COPY,
    "connecting": CONNECTING_COPY,
    "revoke": REVOKE_LABEL,
    "reconnect": RECONNECT_LABEL,
    "disconnect_toast": DISCONNECT_TOAST,
    "reconnect_toast": RECONNECT_TOAST,
    "busy": CONNECT_BUSY_MESSAGE,
    "failed": CONNECT_FAILED_COPY,
    "timeout": "仍在等待 Notion 授权；完成浏览器中的授权后，请重新打开面板查看状态。",
    "manage_label": "Notion 连接",
}

TIME_COPY = {
    "just_now": "刚刚",
    "minutes": "分钟前",
    "hours": "小时前",
    "days": "天前",
    "never": "尚未同步",
    "today": "今天",
    "yesterday": "昨天",
    "date": "{month}月{day}日",
    "full_date": "{year} 年 {month} 月 {day} 日",
}

ONBOARDING_COPY = {
    "notion": {
        "badge": "N",
        "title": "Notion 学习空间",
        "check_label": "已连接",
        "revoke": REVOKE_LABEL,
        "reconnect": RECONNECT_LABEL,
    }
}

COURSE_COPY = {
    "recordings": {
        "save": "验证并连接",
        "saving": "正在验证…",
        "verified": "教学网验证成功，已通过本地密钥加密连接。",
        "credentials_hint": "请填写 10 位北大学号（勿带邮箱后缀）与统一身份认证（IAAA）密码。",
        "username": "教学网账号（北大学号）",
        "password": "北大门户（IAAA）密码",
    },
}

LAUNCH_COPY = {
    "failed": "打开失败，请稍后重试。",
    "opened_external": "已在浏览器打开 Notion；可返回本窗口继续使用。",
}

MODELS_COPY = {
    "title": "模型配置（BYOK）",
    "subtitle": "自由配置您自己的大模型、视觉识别与语音转写服务。所有 API Key 仅保存于本机 .env 文件。",
    "security_note": "本地直连模式：请求直接由您的电脑发送至所填 API 服务商或本地模型，不经过任何第三方中转服务器。",
    "save_success": "模型配置已保存到本机 .env 并立即生效。",
    "save_failed": "保存模型配置失败，请检查输入参数。",
    "test_success": "连接测试通过",
    "test_failed": "连接测试失败",
}

PANEL_COPY = {
    "onboarding": ONBOARDING_COPY,
    "course": COURSE_COPY,
    "launch": LAUNCH_COPY,
    "models": MODELS_COPY,
    "brand": {
        "mark": "PKU",
        "name": "All in One",
        "note": "开源自托管工作台",
        "title": "PKU All in One",
    },
    "nav": {
        "label": "主导航",
        "kicker": "本地学习空间",
        "dashboard": "我的课程",
        "models": "模型配置",
        "mcp_hub": "AI 接入 (MCP)",
        "campus_account": "教学网连接",
    },
    "update": {
        "version": "版本 v{version}",
        "desktop_managed": "开源本地工作台（MIT 协议）",
        "up_to_date": "已是最新版本",
        "available": "发现新版本 {version}",
        "action": "下次启动时更新",
        "retry": "下次启动时重试",
        "requested": "将在下次启动时更新",
        "deferred": "更新已推迟，重试后请重新启动。",
        "request_failed": "更新请求失败，请重试。",
    },
    "connection": CONNECTION_COPY,
    "notion": {"workspace_wait_hint": CONNECTION_COPY["workspace_wait_hint"]},
    "time": TIME_COPY,
}

_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<link rel="icon" href="data:,">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f6f8f5">
<title>__TITLE__ · 开源北大教学网学习工作台</title>
<meta name="description" content="PKU All in One 开源本地版：连接北大教学网同步课件、作业 DDL 与通知，使用您自己的大模型、VLM 与语音转写（DeepSeek / OpenAI / 通义听悟百炼 / 本地 Whisper）生成课堂板书图文笔记，并通过 MCP 接入个人 AI 助手。">
<link rel="stylesheet" href="__BASE__/app.css">
</head>
<body>
<div id="app" aria-live="polite"></div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<noscript>
  <main style="max-width:760px;margin:32px auto;padding:24px;font-family:system-ui,sans-serif;line-height:1.7">
    <h1>PKU All in One · 开源本地工作台</h1>
    <p>本工具为开源（MIT 协议）、纯本地运行的北京大学教学网学习工作台，请启用 JavaScript 后使用。</p>
    <ul>
      <li>本地直连北大教学网，同步课程、课件 PDF、作业要求与通知公告，不经任何第三方平台账号。</li>
      <li>支持自带 API Key（BYOK）接入 DeepSeek / OpenAI / 百炼 Qwen / Ollama 及云端或本地 Faster-Whisper 语音转写。</li>
      <li>支持将课堂回放转为「黑板板书 + 课件同屏对齐」Markdown 与 PDF 讲义，并通过 MCP 接入 AI 客户端。</li>
    </ul>
  </main>
</noscript>
<script>window.PANEL_COPY = __COPY__;</script>
<script src="__BASE__/app.js" defer></script>
</body>
</html>
"""


def _ensure_js_bundle() -> None:
    modules_dir = STATIC_DIR / "modules"
    app_js = STATIC_DIR / "app.js"
    if not modules_dir.is_dir():
        return
    try:
        mod_files = sorted(modules_dir.glob("*.js"))
        if not mod_files:
            return
        app_mtime = app_js.stat().st_mtime if app_js.is_file() else 0.0
        if any(m.stat().st_mtime > app_mtime for m in mod_files):
            from .bundle_frontend import build_bundle_from_modules

            build_bundle_from_modules()
    except OSError:
        pass


def student_page(*, cloud_web_mode: bool = False, has_session: bool = False) -> str:
    """The local open-source student SPA shell with embedded copy."""
    _ = (cloud_web_mode, has_session)
    _ensure_js_bundle()
    page = (
        _PAGE_TEMPLATE.replace("__TITLE__", PANEL_COPY["brand"]["title"])
        .replace("__COPY__", json.dumps(PANEL_COPY, ensure_ascii=False))
        .replace("__BASE__", STUDENT_UI_PATH)
    )
    return page


def _asset(name: str, media_type: str) -> Response:
    if name == "app.js":
        _ensure_js_bundle()
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    return Response(
        content=text,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "CDN-Cache-Control": "no-store",
        },
    )


@lru_cache(maxsize=1)
def _mermaid_gz_bytes() -> bytes:
    return (STATIC_DIR / "mermaid.min.js.gz").read_bytes()


def add_student_ui_routes(app) -> None:
    """Mount the student UI shell and its same-origin assets."""

    @app.get(STUDENT_UI_PATH, response_class=HTMLResponse)
    def student_ui(request: Request):
        _ = request
        return HTMLResponse(
            student_page(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(f"{STUDENT_UI_PATH}/", include_in_schema=False)
    def student_ui_trailing_slash():
        return RedirectResponse(STUDENT_UI_PATH, status_code=307)

    @app.get(f"{STUDENT_UI_PATH}/app.css")
    def student_ui_css() -> Response:
        return _asset("app.css", "text/css")

    @app.get(f"{STUDENT_UI_PATH}/app.js")
    def student_ui_js() -> Response:
        return _asset("app.js", "application/javascript")

    @app.get(f"{STUDENT_UI_PATH}/mermaid.min.js")
    def student_ui_mermaid_js(request: Request) -> Response:
        gz_data = _mermaid_gz_bytes()
        headers = {
            "Vary": "Accept-Encoding",
            "Cache-Control": "public, max-age=2592000, immutable",
        }
        accept_enc = (request.headers.get("accept-encoding") or "").lower()
        if "gzip" in accept_enc:
            return Response(
                content=gz_data,
                media_type="application/javascript; charset=utf-8",
                headers={**headers, "Content-Encoding": "gzip"},
            )
        return Response(
            content=gzip.decompress(gz_data),
            media_type="application/javascript; charset=utf-8",
            headers=headers,
        )


__all__ = [
    "STUDENT_UI_PATH",
    "STATIC_DIR",
    "ONBOARDING_COPY",
    "CONNECTION_COPY",
    "TIME_COPY",
    "COURSE_COPY",
    "LAUNCH_COPY",
    "MODELS_COPY",
    "PANEL_COPY",
    "student_page",
    "add_student_ui_routes",
]
