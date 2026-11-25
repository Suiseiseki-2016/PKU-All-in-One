"""FastAPI TestClient tests for OSS BYOK model settings, UI shell, and open MCP status."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from pku_sync import ai_provider
from pku_sync.config import Settings
from pku_sync.panel.connection import build_fake_connection_service
from pku_sync.panel.webapi import create_app


def _make_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path, Settings]:
    env_path = tmp_path / ".env"
    monkeypatch.setenv("PKU_ENV_FILE", str(env_path))
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, data_dir=data_dir)
    app = create_app(
        settings=settings,
        connection_service=build_fake_connection_service(),
    )
    return TestClient(app), env_path, settings


def test_root_and_app_shell_have_zero_external_urls_or_commercial_copy(tmp_path, monkeypatch):
    client, _, _ = _make_client(tmp_path, monkeypatch)

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/app"

    app_page = client.get("/app")
    assert app_page.status_code == 200
    body = app_page.text
    assert "http://" not in body
    assert "https://" not in body
    assert "登录学习空间" not in body
    assert "兑换" not in body
    assert "模型配置" in body


def test_get_model_settings_masks_secrets_and_returns_presets(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("PKU_ENV_FILE", str(env_path))
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        openai_base_url="https://api.deepseek.com/v1",
        openai_api_key="sk-secret-deepseek-9999",
        notes_model="deepseek-chat",
        transcription_backend="dashscope",
        dashscope_api_key="sk-dash-secret-8888",
    )
    client = TestClient(create_app(settings=settings, connection_service=build_fake_connection_service()))

    res = client.get("/api/settings/models")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["openai_api_key_set"] is True
    assert payload["openai_api_key_masked"] == "sk-s...9999"
    assert "sk-secret-deepseek-9999" not in res.text
    assert payload["dashscope_api_key_set"] is True
    assert payload["dashscope_api_key_masked"] == "sk-d...8888"
    assert "sk-dash-secret-8888" not in res.text
    assert payload["llm_configured"] is True
    assert payload["asr_configured"] is True
    assert isinstance(payload["presets"], list) and len(payload["presets"]) >= 5


def test_post_model_settings_persists_env_preserves_and_clears_keys(tmp_path, monkeypatch):
    client, env_path, settings = _make_client(tmp_path, monkeypatch)

    # 1. Save initial BYOK keys & models
    res1 = client.post(
        "/api/settings/models",
        json={
            "openai_base_url": "https://api.deepseek.com/v1",
            "openai_api_key": "sk-initial-key-1234",
            "notes_model": "deepseek-chat",
            "refine_model": "deepseek-reasoner",
            "vision_enabled": True,
            "vision_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "vision_api_key": "sk-vision-5678",
            "vision_model": "qwen-vl-max-latest",
            "transcription_backend": "dashscope",
            "dashscope_api_key": "sk-dash-4321",
        },
    )
    assert res1.status_code == 200
    p1 = res1.json()
    assert p1["openai_api_key_set"] is True
    assert p1["vision_api_key_set"] is True
    assert p1["dashscope_api_key_set"] is True
    assert settings.openai_api_key == "sk-initial-key-1234"
    assert settings.refine_model == "deepseek-reasoner"

    env_text = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-initial-key-1234" in env_text
    assert "REFINE_MODEL=deepseek-reasoner" in env_text
    assert "DASHSCOPE_API_KEY=sk-dash-4321" in env_text

    # 2. Omitting/sending empty key strings preserves existing keys; clear_* clears explicitly
    res2 = client.post(
        "/api/settings/models",
        json={
            "notes_model": "deepseek-chat",
            "openai_api_key": "",
            "clear_vision_api_key": True,
        },
    )
    assert res2.status_code == 200
    p2 = res2.json()
    assert p2["openai_api_key_set"] is True
    assert settings.openai_api_key == "sk-initial-key-1234"
    assert p2["vision_api_key_set"] is False
    assert settings.vision_api_key == ""
    assert "VISION_API_KEY=" in env_path.read_text(encoding="utf-8")


def test_post_model_settings_test_uses_unsaved_overrides_without_mutating_env(tmp_path, monkeypatch):
    client, env_path, settings = _make_client(tmp_path, monkeypatch)
    seen = []

    def fake_test_connection(candidate, *, target="llm"):
        seen.append((target, candidate.openai_base_url, candidate.openai_api_key, candidate.notes_model))
        return {
            "ok": True,
            "target": target,
            "model": candidate.notes_model,
            "base_url": candidate.openai_base_url,
            "latency_ms": 12,
            "detail": "连接成功",
        }

    monkeypatch.setattr(ai_provider, "test_connection", fake_test_connection)

    res = client.post(
        "/api/settings/models/test",
        json={
            "target": "llm",
            "overrides": {
                "openai_base_url": "https://api.openai.com/v1",
                "openai_api_key": "sk-unsaved-probe",
                "notes_model": "gpt-4.1-mini",
            },
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["message"] == "连接成功"
    assert seen == [("llm", "https://api.openai.com/v1", "sk-unsaved-probe", "gpt-4.1-mini")]
    assert settings.openai_api_key == ""
    assert not env_path.exists()


def test_mcp_status_is_open_without_auth(tmp_path, monkeypatch):
    client, _, _ = _make_client(tmp_path, monkeypatch)
    res = client.get("/api/mcp/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["mcp_full_enabled"] is True
    assert data["effective_plan"] == "open"
    tools = data.get("tools_summary") or []
    assert len(tools) == 7
    assert all(tool.get("tier") == "open" and tool.get("locked") is False for tool in tools)
