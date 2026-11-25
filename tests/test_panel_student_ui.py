"""Open-source BYOK student panel UI surface & zero external assets checks."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from grading_workspace import PANEL_FORBIDDEN_STRINGS
from pku_sync.panel.connection import (
    CONNECTED_COPY,
    DISCONNECTED_COPY,
)
from pku_sync.panel.student_ui import (
    MODELS_COPY,
    PANEL_COPY,
    STUDENT_UI_PATH,
    student_page,
)
from pku_sync.panel.webapi import create_app


def make_client() -> TestClient:
    from pku_sync.panel.connection import build_fake_connection_service

    return TestClient(
        create_app(
            settings=SimpleNamespace(data_dir="data"),
            connection_service=build_fake_connection_service(directory_service=None),
        )
    )


def served_assets(client) -> dict[str, str]:
    page = client.get(STUDENT_UI_PATH)
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    for response in (page, css, script):
        assert response.status_code == 200
    return {"page": page.text, "css": css.text, "js": script.text}


def test_student_ui_page_and_assets_are_served():
    client = make_client()
    page = client.get(STUDENT_UI_PATH)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert '<html lang="zh-CN">' in page.text
    assert 'name="viewport"' in page.text
    css = client.get(f"{STUDENT_UI_PATH}/app.css")
    assert css.headers["content-type"].startswith("text/css")
    script = client.get(f"{STUDENT_UI_PATH}/app.js")
    assert "javascript" in script.headers["content-type"]


def test_student_ui_loads_zero_external_assets():
    assets = served_assets(make_client())
    for name, text in assets.items():
        assert "http://" not in text, name
        assert "https://" not in text, name
        assert "@import" not in text, name
        assert "//fonts." not in text, name
    assert f'href="{STUDENT_UI_PATH}/app.css"' in assets["page"]
    assert f'src="{STUDENT_UI_PATH}/app.js"' in assets["page"]


def test_student_ui_has_no_auth_gate_or_answer_editors():
    assets = served_assets(make_client())
    for name, text in assets.items():
        assert "contenteditable" not in text.lower(), name
    assert "data-campus-quiz-paper" not in assets["js"]
    assert "generateCampusQuiz" not in assets["js"]
    assert 'id="auth-form"' not in assets["js"]
    assert 'id="models-config-form"' in assets["js"]
    assert 'type="password"' in assets["js"]


def test_models_copy_is_shipped_and_commercial_copy_removed():
    assert MODELS_COPY["title"] == "模型配置（BYOK）"
    assert PANEL_COPY["nav"]["models"] == "模型配置"
    page = student_page()
    assert "模型配置（BYOK）" in page
    assert "登录学习空间" not in page
    assert "兑换" not in page


def test_student_ui_never_carries_content_paths_or_tokens():
    assets = served_assets(make_client())
    for name, text in assets.items():
        for needle in PANEL_FORBIDDEN_STRINGS:
            if name == "css" and needle == "keyframes":
                assert text.count("keyframes") == text.count("@keyframes")
                continue
            if name == "js" and needle == "keyframes":
                public_names_removed = text.replace("keyframes_available", "").replace(
                    "campus-match-slides-keyframes", ""
                )
                assert needle not in public_names_removed
                continue
            assert needle not in text, (name, needle)
        assert "NOTION_TOKEN" not in text


def test_page_embeds_the_whole_copy_blob_for_the_client_renderer():
    page = student_page()
    assert "window.PANEL_COPY" in page
    blob = page.split("window.PANEL_COPY = ", 1)[1].split(";</script>", 1)[0]
    parsed = json.loads(blob)
    assert parsed == PANEL_COPY
    assert CONNECTED_COPY in page and DISCONNECTED_COPY in page


def test_root_redirects_to_student_ui_directly(tmp_path):
    from pku_sync.panel import webapi

    settings = SimpleNamespace(
        data_dir=tmp_path,
        transcription_backend="local",
    )
    client = TestClient(webapi.create_app(settings))
    page = client.get("/")
    assert page.status_code == 200
    assert "window.PANEL_COPY" in page.text
    assert str(page.url).endswith(STUDENT_UI_PATH)
    assert "跑 daily" not in page.text


def test_responsive_navigation_and_accessibility_contract():
    assets = served_assets(make_client())
    page, css, script = assets["page"], assets["css"], assets["js"]
    for expected in (
        ".sidebar{ display: none; }",
        "html { min-width: 320px; }",
        "overflow-wrap: anywhere",
        ".course-card",
        "flex-direction: column",
        ".recording-row",
        "flex-wrap: wrap",
        "min-height: 44px",
        "button:focus-visible",
        "select:focus-visible",
    ):
        assert "".join(expected.split()) in "".join(css.split()), expected
    for expected in (
        'class="mobile-overview"',
        'data-action="open-dashboard"',
        'data-action="open-models"',
        'aria-label="',
        'aria-current="page"',
        'data-persistent-disclosure=',
        'aria-busy=',
        'role="status" aria-live="polite"',
    ):
        assert expected in script, expected
    assert '<div id="app" aria-live="polite"></div>' in page
