"""BYOK AI provider configuration, connectivity tests, and 3-backend ASR tests."""

from __future__ import annotations

import builtins
import sys

import httpx
import pytest

from pku_sync import ai_provider, transcription
from pku_sync.config import Settings


def test_resolve_llm_config_and_refine_fallback():
    s = Settings(
        _env_file=None,
        openai_base_url="https://api.deepseek.com/v1/",
        openai_api_key="sk-deepseek-1234567890",
        notes_model="deepseek-chat",
        refine_model="",
    )
    assert ai_provider.resolve_llm_config(s, "notes") == (
        "https://api.deepseek.com/v1",
        "sk-deepseek-1234567890",
        "deepseek-chat",
    )
    assert ai_provider.resolve_llm_config(s, "refine") == (
        "https://api.deepseek.com/v1",
        "sk-deepseek-1234567890",
        "deepseek-chat",
    )
    s.refine_model = "deepseek-reasoner"
    assert ai_provider.resolve_llm_config(s, "refine")[2] == "deepseek-reasoner"
    assert ai_provider.resolve_llm_config(s, "refine", model_override="custom-m")[2] == "custom-m"


def test_resolve_vision_config_fallback_and_override():
    s = Settings(
        _env_file=None,
        openai_base_url="https://api.deepseek.com/v1",
        openai_api_key="sk-llm-key",
        notes_model="deepseek-chat",
        vision_enabled=True,
        vision_base_url="",
        vision_api_key="",
        vision_model="",
    )
    assert ai_provider.resolve_vision_config(s) == (
        True,
        "https://api.deepseek.com/v1",
        "sk-llm-key",
        "deepseek-chat",
    )
    assert ai_provider.is_vision_configured(s) is True

    s.vision_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/"
    s.vision_api_key = "sk-vlm-only"
    s.vision_model = "qwen-vl-max-latest"
    assert ai_provider.resolve_vision_config(s) == (
        True,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "sk-vlm-only",
        "qwen-vl-max-latest",
    )

    s.vision_enabled = False
    assert ai_provider.is_vision_configured(s) is False


def test_model_presets_and_readiness_flags():
    preset_ids = {p["id"] for p in ai_provider.MODEL_PRESETS}
    assert {"deepseek", "openai", "dashscope", "siliconflow", "zhipu", "openrouter", "ollama"} <= preset_ids

    unconfigured = Settings(_env_file=None, openai_api_key="", transcription_backend="openai")
    assert ai_provider.is_llm_configured(unconfigured) is False
    assert ai_provider.is_asr_configured(unconfigured) is False

    ollama = Settings(
        _env_file=None,
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_api_key="",
        notes_model="qwen2.5:14b",
        transcription_backend="dashscope",
        dashscope_api_key="sk-dash-1234",
    )
    assert ai_provider.is_llm_configured(ollama) is True
    assert ai_provider.is_asr_configured(ollama) is True
    asr_cfg = ai_provider.resolve_asr_config(ollama)
    assert asr_cfg["backend"] == "dashscope"
    assert asr_cfg["model"] == "qwen3-asr-flash"


def test_test_connection_mocked_for_llm_vision_and_asr(monkeypatch):
    seen_requests: list[tuple[str, str, dict]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, headers=None, json=None):
            seen_requests.append(("POST", url, json or {}))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "OK"}}]},
                request=httpx.Request("POST", url),
            )

        def get(self, url, *, headers=None):
            seen_requests.append(("GET", url, {}))
            return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(ai_provider.httpx, "Client", FakeClient)

    s = Settings(
        _env_file=None,
        openai_base_url="https://api.openai.com/v1",
        openai_api_key="sk-test-123456789",
        notes_model="gpt-4.1-mini",
        vision_enabled=True,
        vision_model="gpt-4.1-mini",
        transcription_backend="openai",
        asr_model="whisper-1",
    )

    llm_res = ai_provider.test_connection(s, target="llm")
    assert llm_res["ok"] is True
    assert llm_res["model"] == "gpt-4.1-mini"

    vision_res = ai_provider.test_connection(s, target="vision")
    assert vision_res["ok"] is True
    assert any(
        isinstance(m.get("content"), list)
        and any(part.get("type") == "image_url" for part in m["content"])
        for _, _, payload in seen_requests
        for m in payload.get("messages", [])
    )

    asr_res = ai_provider.test_connection(s, target="asr")
    assert asr_res["ok"] is True
    assert asr_res["backend"] == "openai"
    assert any(method == "GET" and url.endswith("/models") for method, url, _ in seen_requests)

    s_local = Settings(_env_file=None, transcription_backend="local")
    monkeypatch.setattr(ai_provider, "local_asr_available", lambda: False)
    local_res = ai_provider.test_connection(s_local, target="asr")
    assert local_res["ok"] is False
    assert "faster-whisper" in local_res["detail"]


def test_transcription_openai_dashscope_and_local_backends(tmp_path, monkeypatch):
    part = transcription.MemoryAudioPart("000.m4a", b"fake-aac-bytes")
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video")

    # 1. OpenAI-compatible backend
    openai_calls = []

    def fake_upload_openai(base_url, api_key, model, audio):
        openai_calls.append((base_url, api_key, model, audio.name))
        return {"language": "zh", "segments": [{"start": 0.0, "end": 5.0, "text": "OpenAI 转写"}]}

    monkeypatch.setattr(transcription, "_upload_openai", fake_upload_openai)
    s_openai = Settings(
        _env_file=None,
        transcription_backend="openai",
        asr_base_url="https://api.siliconflow.cn/v1",
        asr_api_key="sk-sf-1234",
        asr_model="FunAudioLLM/SenseVoiceSmall",
    )
    out_openai = tmp_path / "t_openai.json"
    res_openai = transcription.transcribe_memory_parts([(part, 0.0)], out_openai, s_openai)
    assert res_openai["segments"][0]["text"] == "OpenAI 转写"
    assert openai_calls == [
        ("https://api.siliconflow.cn/v1", "sk-sf-1234", "FunAudioLLM/SenseVoiceSmall", "000.m4a")
    ]

    # 2. DashScope backend
    ds_calls = []

    def fake_upload_dashscope(base_url, api_key, model, audio):
        ds_calls.append((base_url, api_key, model, audio.name))
        return {"language": "zh", "segments": [{"start": 0.0, "end": 4.0, "text": "百炼转写"}]}

    monkeypatch.setattr(transcription, "_upload_dashscope", fake_upload_dashscope)
    monkeypatch.setattr(transcription, "_rechunk_part_if_needed", lambda parts, sec: parts)
    s_ds = Settings(
        _env_file=None,
        transcription_backend="dashscope",
        dashscope_api_key="sk-ds-9999",
    )
    out_ds = tmp_path / "t_ds.json"
    res_ds = transcription.transcribe_memory_parts([(part, 0.0)], out_ds, s_ds)
    assert res_ds["segments"][0]["text"] == "百炼转写"
    assert ds_calls == [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "sk-ds-9999", "qwen3-asr-flash", "000.m4a")
    ]

    # 3. Local faster-whisper backend: missing package error vs mocked success
    s_local = Settings(_env_file=None, transcription_backend="local", whisper_model="base")
    real_import = builtins.__import__

    def deny_faster_whisper(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    monkeypatch.setattr(builtins, "__import__", deny_faster_whisper)
    with pytest.raises(RuntimeError, match="faster-whisper"):
        transcription.transcribe(video, tmp_path / "t_missing_local.json", s_local)

    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.setattr(
        "pku_sync.media.transcribe",
        lambda v, t, cfg: {"language": "zh", "segments": [{"start": 0.0, "end": 3.0, "text": "本地转写"}]},
    )
    res_local = transcription.transcribe(video, tmp_path / "t_local.json", s_local)
    assert res_local["segments"][0]["text"] == "本地转写"
