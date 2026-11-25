"""Unit tests for the BYOK transcription backend dispatch (no real whisper)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pku_sync import transcription
from pku_sync.config import Settings


def make_settings(backend: str, **extra) -> SimpleNamespace:
    defaults = {
        "transcription_backend": backend,
        "openai_base_url": "",
        "openai_api_key": "",
        "asr_base_url": "",
        "asr_api_key": "",
        "asr_model": "",
        "dashscope_api_key": "",
    }
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_default_backend_uses_openai_byok(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_BACKEND", raising=False)
    assert Settings(_env_file=None).transcription_backend == "openai"


def test_local_backend_dispatches_to_media_transcribe(monkeypatch, tmp_path):
    cfg = make_settings("local")
    calls = {}

    def fake_media_transcribe(video, target, config):
        calls["args"] = (video, target, config)
        return {"segments": []}

    monkeypatch.setattr("pku_sync.media.transcribe", fake_media_transcribe)
    result = transcription.transcribe(tmp_path / "video.mp4", tmp_path / "t.json", cfg)
    assert result == {"segments": []}
    assert calls["args"] == (tmp_path / "video.mp4", tmp_path / "t.json", cfg)


def test_local_backend_works_even_with_desktop_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PKU_DESKTOP_APP", "1")
    seen = []
    monkeypatch.setattr(
        "pku_sync.media.transcribe",
        lambda video, target, cfg: seen.append((video, target)) or {"segments": [{"start": 0.0, "text": "本地"}]},
    )
    result = transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", make_settings("local"))
    assert seen == [(tmp_path / "v.mp4", tmp_path / "t.json")]
    assert result["segments"][0]["text"] == "本地"


def test_backend_name_is_normalized(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(
        "pku_sync.media.transcribe", lambda video, target, cfg: seen.append(1) or {}
    )
    transcription.transcribe(
        tmp_path / "v.mp4", tmp_path / "t.json", make_settings("  Local  ")
    )
    assert seen == [1]


def test_empty_backend_falls_back_to_openai(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(transcription, "transcribe_cloud", lambda *args, **kwargs: seen.append(1) or {})
    transcription.transcribe(
        tmp_path / "v.mp4", tmp_path / "t.json", make_settings("")
    )
    assert seen == [1]


def test_openai_requires_key_and_base_url(tmp_path):
    with pytest.raises(RuntimeError, match="尚未配置 OPENAI 语音转写服务"):
        transcription.transcribe(
            tmp_path / "v.mp4", tmp_path / "t.json", make_settings("openai")
        )


def test_dashscope_requires_api_key(tmp_path):
    with pytest.raises(RuntimeError, match="尚未配置 DASHSCOPE 语音转写服务"):
        transcription.transcribe(
            tmp_path / "v.mp4", tmp_path / "t.json", make_settings("dashscope")
        )


def test_openai_uploads_audio_only_and_writes_target(tmp_path, monkeypatch):
    cfg = make_settings(
        "openai",
        asr_base_url="https://asr.example/v1",
        asr_api_key="o-key",
        asr_model="whisper-1",
    )
    calls = {}

    def fake_extract(video, out):
        calls["extract"] = (video, Path(out))
        Path(out).write_bytes(b"fake-audio")
        return Path(out)

    def fake_upload(base_url, api_key, model, audio):
        calls["upload"] = (base_url, api_key, model, Path(audio))
        assert Path(audio).read_bytes() == b"fake-audio"
        return {"language": "zh", "segments": [{"start": 0.0, "text": "第一讲"}]}

    monkeypatch.setattr(transcription, "_extract_audio", fake_extract)
    monkeypatch.setattr(transcription, "_split_audio", lambda audio, directory, seconds: [(audio, 0.0)])
    monkeypatch.setattr(transcription, "_upload_openai", fake_upload)
    result = transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)

    assert result["segments"][0]["text"] == "第一讲"
    assert calls["extract"][0] == tmp_path / "v.mp4"
    assert calls["upload"][3].suffix == ".m4a"
    assert calls["upload"][3] != tmp_path / "v.mp4"
    assert calls["upload"][:3] == ("https://asr.example/v1", "o-key", "whisper-1")
    written = json.loads((tmp_path / "t.json").read_text("utf-8"))
    assert written == result


def test_dashscope_uploads_audio_only_and_writes_target(tmp_path, monkeypatch):
    cfg = make_settings(
        "dashscope",
        dashscope_api_key="ds-key",
        asr_model="qwen3-asr-flash",
    )
    calls = {}

    def fake_extract(video, out):
        Path(out).write_bytes(b"ds-audio")
        return Path(out)

    def fake_upload(base_url, api_key, model, audio):
        calls["upload"] = (base_url, api_key, model, Path(audio))
        return {"language": "zh", "segments": [{"start": 0.0, "end": 1.2, "text": "百炼转写"}]}

    monkeypatch.setattr(transcription, "_extract_audio", fake_extract)
    monkeypatch.setattr(transcription, "_split_audio", lambda audio, directory, seconds: [(audio, 0.0)])
    monkeypatch.setattr(transcription, "_upload_dashscope", fake_upload)
    result = transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)

    assert result["segments"][0]["text"] == "百炼转写"
    assert calls["upload"][1] == "ds-key"
    assert calls["upload"][2] == "qwen3-asr-flash"
    assert "dashscope.aliyuncs.com" in calls["upload"][0]


def test_openai_records_wall_clock_elapsed(tmp_path, monkeypatch):
    cfg = make_settings(
        "openai",
        asr_base_url="https://asr.example/v1",
        asr_api_key="o-key",
        asr_model="whisper-1",
    )
    clock = iter((10.0, 17.25))
    monkeypatch.setattr(transcription.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(transcription, "_audio_duration", lambda video: 0.0)

    def fake_extract(video, out):
        out.write_bytes(b"audio")
        return out

    monkeypatch.setattr(transcription, "_extract_audio", fake_extract)
    monkeypatch.setattr(
        transcription,
        "_split_audio",
        lambda audio, directory, seconds: [(audio, 0.0)],
    )
    monkeypatch.setattr(
        transcription,
        "_upload_openai",
        lambda base_url, api_key, model, audio: {"segments": [{"start": 0, "text": "课堂"}]},
    )

    result = transcription.transcribe(tmp_path / "v.mp4", tmp_path / "t.json", cfg)

    assert result["elapsed"] == 7.2
    assert json.loads((tmp_path / "t.json").read_text("utf-8"))["elapsed"] == 7.2


def test_unknown_backend_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="carrier-pigeon"):
        transcription.transcribe(
            tmp_path / "v.mp4", tmp_path / "t.json", make_settings("carrier-pigeon")
        )


def test_upload_openai_retries_once_on_transport_error(tmp_path, monkeypatch):
    """A transient transport failure retries with a fresh audio stream."""
    import httpx

    calls = {"n": 0}

    class FlakyClient:
        def __init__(self, *args, **kwargs): ...

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            calls["n"] += 1
            assert kwargs["files"]["file"][1].read() == b"fake-audio"
            if calls["n"] == 1:
                raise httpx.TransportError("connection killed mid-upload")
            return httpx.Response(
                200, json={"segments": [{"start": 0.0, "end": 1.0, "text": "重试"}]}
            )

    monkeypatch.setattr(httpx, "Client", FlakyClient)
    monkeypatch.setattr(transcription, "_RETRY_DELAY", 0.0)
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake-audio")
    payload = transcription._upload_openai(
        "https://asr.example/v1", "o-key", "whisper-1", audio
    )
    assert calls["n"] == 2
    assert payload["segments"][0]["text"] == "重试"


def test_upload_openai_never_retries_non_retryable_http_errors(tmp_path, monkeypatch):
    """Non-retryable client HTTP rejections (402 / 401) stop after a single attempt."""
    import httpx

    calls = {"n": 0}

    class Always402:
        def __init__(self, *args, **kwargs): ...

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            calls["n"] += 1
            return httpx.Response(402, json={"detail": "quota exhausted"})

    monkeypatch.setattr(httpx, "Client", Always402)
    monkeypatch.setattr(transcription, "_RETRY_DELAY", 0.0)
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"fake-audio")
    with pytest.raises(RuntimeError, match="HTTP 402"):
        transcription._upload_openai(
            "https://asr.example/v1", "o-key", "whisper-1", audio
        )
    assert calls["n"] == 1
