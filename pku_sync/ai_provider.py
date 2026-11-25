"""BYOK (Bring Your Own Key) OpenAI-compatible LLM, VLM and ASR configuration & execution."""

from __future__ import annotations

import importlib.util
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .network import dead_loopback_proxy

_DEFAULT_LLM_SYSTEM = "你是大学课程学习助手。只按要求输出准确、结构化的中文内容。"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3

# Allow long-context BYOK requests directly to user-configured endpoints.
LLM_REQUEST_MAX_BYTES = 1024 * 1024

# Valid 16x16 solid white JPEG used strictly for VLM connectivity probes.
_PING_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYG"
    "CAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoK"
    "CgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI"
    "CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRol"
    "JicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
    "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAA"
    "AAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLR"
    "ChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaX"
    "mJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEA"
    "PwD9/KKKKAP/2Q=="
)

MODEL_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "openai_base_url": "https://api.deepseek.com/v1",
        "notes_model": "deepseek-chat",
        "refine_model": "",
        "vision_model": "",
        "asr_backend": "",
        "asr_base_url": "",
        "asr_model": "",
        "hint": "性价比高（纯文本模型；如需板书 OCR / 语音转写可搭配百炼或本地 Whisper）",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "openai_base_url": "https://api.openai.com/v1",
        "notes_model": "gpt-4.1-mini",
        "refine_model": "",
        "vision_model": "gpt-4.1-mini",
        "asr_backend": "openai",
        "asr_base_url": "https://api.openai.com/v1",
        "asr_model": "whisper-1",
        "hint": "一套密钥通配文本笔记、视觉板书 OCR 与 Whisper 语音转写",
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼",
        "openai_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "notes_model": "qwen-plus",
        "refine_model": "",
        "vision_model": "qwen-vl-max-latest",
        "asr_backend": "dashscope",
        "asr_base_url": "",
        "asr_model": "qwen3-asr-flash",
        "hint": "支持 Qwen 文本、Qwen-VL 板书识图及 Qwen3-ASR 中文课堂转写",
    },
    {
        "id": "siliconflow",
        "label": "硅基流动",
        "openai_base_url": "https://api.siliconflow.cn/v1",
        "notes_model": "deepseek-ai/DeepSeek-V3",
        "refine_model": "",
        "vision_model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "asr_backend": "openai",
        "asr_base_url": "https://api.siliconflow.cn/v1",
        "asr_model": "FunAudioLLM/SenseVoiceSmall",
        "hint": "国内高速直连，一套密钥支持 DeepSeek 文本 + Qwen-VL + SenseVoice ASR",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "openai_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "notes_model": "glm-4-plus",
        "refine_model": "",
        "vision_model": "glm-4v-plus",
        "asr_backend": "",
        "asr_base_url": "",
        "asr_model": "",
        "hint": "国内直连稳定，支持 GLM 文本精编与 GLM-4V 视觉识图",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "openai_base_url": "https://openrouter.ai/api/v1",
        "notes_model": "deepseek/deepseek-chat",
        "refine_model": "",
        "vision_model": "google/gemini-2.5-flash",
        "asr_backend": "",
        "asr_base_url": "",
        "asr_model": "",
        "hint": "单密钥路由 DeepSeek、Gemini、Claude、GPT 等多家族模型",
    },
    {
        "id": "ollama",
        "label": "Ollama 本地",
        "openai_base_url": "http://127.0.0.1:11434/v1",
        "notes_model": "qwen2.5:14b",
        "refine_model": "",
        "vision_model": "",
        "asr_backend": "local",
        "asr_base_url": "",
        "asr_model": "",
        "hint": "零密钥本机推理，搭配本地 Faster-Whisper 可实现全离线整理",
    },
]


class AIProviderError(RuntimeError):
    """Raised when BYOK AI configuration is missing or the upstream provider fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def mask_secret(value: str) -> str:
    """Return a non-sensitive masked display string for an API key."""
    secret = str(value or "").strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def is_local_base_url(url: str) -> bool:
    """Return True for localhost/loopback endpoints (e.g. Ollama / vLLM without API key)."""
    host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def llm_request_body_size(
    operation: str,
    system: str,
    prompt: str,
    *,
    model: str | None = None,
    operation_context: dict | None = None,
) -> int:
    """Estimate UTF-8 byte length of the serialized chat-completions envelope."""
    body: dict[str, Any] = {
        "model": model or "",
        "messages": [
            {"role": "system", "content": system or ""},
            {"role": "user", "content": prompt or ""},
        ],
    }
    if operation_context:
        body["metadata"] = operation_context
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def resolve_llm_config(
    settings: object,
    operation: str = "notes",
    *,
    model_override: str = "",
) -> tuple[str, str, str]:
    """Return ``(base_url, api_key, model)`` for the given text LLM operation."""
    base_url = str(getattr(settings, "openai_base_url", "") or "").strip().rstrip("/")
    api_key = str(getattr(settings, "openai_api_key", "") or "").strip()
    notes_model = str(getattr(settings, "notes_model", "") or "").strip()
    refine_model = str(getattr(settings, "refine_model", "") or "").strip()
    general_model = str(getattr(settings, "llm_model", "") or "").strip()

    if model_override and str(model_override).strip():
        model = str(model_override).strip()
    elif operation == "refine":
        model = refine_model or notes_model or general_model
    elif operation == "notes":
        model = notes_model or general_model
    else:
        model = general_model or notes_model or refine_model

    return base_url, api_key, model


def resolve_vision_config(settings: object) -> tuple[bool, str, str, str]:
    """Return ``(enabled, base_url, api_key, model)`` for optional VLM board/slide OCR."""
    enabled = getattr(settings, "vision_enabled", True) is not False
    llm_base, llm_key, llm_model = resolve_llm_config(settings, "vision")
    base_url = str(getattr(settings, "vision_base_url", "") or "").strip().rstrip("/") or llm_base
    api_key = str(getattr(settings, "vision_api_key", "") or "").strip() or llm_key
    model = str(getattr(settings, "vision_model", "") or "").strip() or llm_model
    return enabled, base_url, api_key, model


def local_asr_available() -> bool:
    """Return True when ``faster_whisper`` is importable in the current environment."""
    return importlib.util.find_spec("faster_whisper") is not None


def resolve_asr_config(settings: object) -> dict[str, Any]:
    """Resolve normalized BYOK ASR settings across ``openai``, ``dashscope`` and ``local``."""
    raw_backend = str(getattr(settings, "transcription_backend", "openai") or "openai").strip().lower()
    backend = raw_backend if raw_backend in {"openai", "dashscope", "local"} else "openai"
    has_local = local_asr_available()

    if backend == "local":
        return {
            "backend": "local",
            "configured": has_local,
            "local_available": has_local,
            "whisper_model": str(getattr(settings, "whisper_model", "small") or "small").strip(),
            "whisper_device": str(getattr(settings, "whisper_device", "auto") or "auto").strip(),
            "whisper_compute_type": str(getattr(settings, "whisper_compute_type", "auto") or "auto").strip(),
            "base_url": "",
            "api_key": "",
            "model": str(getattr(settings, "whisper_model", "small") or "small").strip(),
        }

    raw_asr_url = str(getattr(settings, "asr_base_url", "") or "").strip().rstrip("/")
    raw_asr_key = str(getattr(settings, "asr_api_key", "") or "").strip()
    raw_asr_model = str(getattr(settings, "asr_model", "") or "").strip()
    llm_url = str(getattr(settings, "openai_base_url", "") or "").strip().rstrip("/")
    llm_key = str(getattr(settings, "openai_api_key", "") or "").strip()

    if backend == "dashscope":
        base_url = raw_asr_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        dashscope_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
        api_key = dashscope_key or raw_asr_key or (llm_key if "dashscope" in llm_url.lower() else "")
        model = "qwen3-asr-flash" if raw_asr_model in {"", "whisper-1"} else raw_asr_model
        configured = bool(base_url and model and (api_key or is_local_base_url(base_url)))
    else:
        base_url = raw_asr_url or llm_url or "https://api.openai.com/v1"
        api_key = raw_asr_key or llm_key
        model = raw_asr_model or "whisper-1"
        configured = bool(base_url and model and (api_key or is_local_base_url(base_url)))

    return {
        "backend": backend,
        "configured": configured,
        "local_available": has_local,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
    }


def is_llm_configured(settings: object, operation: str = "notes") -> bool:
    """Return True when the text LLM endpoint, model, and required credentials are configured."""
    base_url, api_key, model = resolve_llm_config(settings, operation)
    if not base_url or not model:
        return False
    return bool(api_key or is_local_base_url(base_url))


def is_vision_configured(settings: object) -> bool:
    """Return True when VLM OCR is enabled and usable."""
    enabled, base_url, api_key, model = resolve_vision_config(settings)
    if not enabled or not base_url or not model:
        return False
    return bool(api_key or is_local_base_url(base_url))


def is_asr_configured(settings: object) -> bool:
    """Return True when the active ASR backend is ready to transcribe audio."""
    return bool(resolve_asr_config(settings).get("configured"))


def _extract_message_content(message: dict[str, Any]) -> str:
    raw = message.get("content")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts).strip()
    return ""


def _post_chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    label: str = "模型服务",
) -> str:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    detail = ""
    current_payload = dict(payload)
    trust_env = not dead_loopback_proxy()

    with httpx.Client(trust_env=trust_env, timeout=timeout_seconds) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = client.post(endpoint, headers=headers, json=current_payload)
            except httpx.TransportError as exc:
                detail = f"网络请求失败（{type(exc).__name__}）"
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(1.5 * attempt)
                    continue
                raise AIProviderError(
                    f"{label}（{model} @ {base_url}）连接失败，请检查 Base URL 与网络设置后重试：{detail}"
                ) from exc

            if response.status_code == 400 and "response_format" in current_payload:
                current_payload.pop("response_format", None)
                continue

            if response.status_code in (401, 403):
                raise AIProviderError(
                    f"{label}（{model}）认证失败（HTTP {response.status_code}），请在「模型配置」中检查 API Key 与权限。",
                    status_code=response.status_code,
                )

            if response.status_code != 200:
                snippet = (response.text or "").strip().replace("\n", " ")[:200]
                detail = f"HTTP {response.status_code}: {snippet}"
                if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                    time.sleep(2.0 * attempt)
                    continue
                raise AIProviderError(
                    f"{label}（{model}）调用失败：{detail}",
                    status_code=response.status_code,
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise AIProviderError(f"{label}（{model}）返回了非 JSON 响应。") from exc

            if isinstance(body, dict) and body.get("error"):
                detail = json.dumps(body["error"], ensure_ascii=False)[:200]
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(2.0 * attempt)
                    continue
                raise AIProviderError(f"{label}（{model}）返回错误：{detail}")

            choices = body.get("choices") if isinstance(body, dict) else None
            if not choices:
                detail = "响应中 choices 为空"
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(1.5 * attempt)
                    continue
                raise AIProviderError(f"{label}（{model}）未返回任何候选输出。")

            message = (choices[0] or {}).get("message") or {}
            content = _extract_message_content(message)
            if content:
                return content

            detail = f"content 为空（finish={choices[0].get('finish_reason')}）"
            if attempt < _MAX_ATTEMPTS:
                time.sleep(1.5 * attempt)
                continue
            raise AIProviderError(f"{label}（{model}）返回内容为空（{detail}）。")

    raise AIProviderError(f"{label}（{model}）调用失败：{detail}")


def call_llm(
    operation: str,
    prompt: str,
    settings: object,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
    json_mode: bool = False,
    operation_context: dict | None = None,
) -> dict[str, Any]:
    """Run one BYOK OpenAI-compatible text completion and return ``{content, points_charged, model}``."""
    base_url, api_key, resolved_model = resolve_llm_config(
        settings, operation, model_override=model or ""
    )
    if not base_url:
        raise AIProviderError("尚未配置文本模型 Base URL，请先前往「模型配置」保存设置。")
    if not resolved_model:
        raise AIProviderError("尚未配置文本模型名称（NOTES_MODEL），请先前往「模型配置」保存设置。")
    if not api_key and not is_local_base_url(base_url):
        raise AIProviderError("尚未配置模型 API Key，请先前往「模型配置」填写密钥后重试。")

    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system or _DEFAULT_LLM_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    elif operation in {"notes", "refine"}:
        payload["temperature"] = 0.2
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else getattr(settings, "llm_timeout", 600) or 600
    )
    content = _post_chat_completions(
        base_url=base_url,
        api_key=api_key,
        model=resolved_model,
        payload=payload,
        timeout_seconds=timeout,
        label="文本大模型",
    )
    return {
        "content": content,
        "points_charged": 0.0,
        "model": resolved_model,
    }


def call_vision(
    system: str,
    user: str,
    images_b64: list[str],
    settings: object,
    *,
    json_mode: bool = True,
    max_tokens: int = 2048,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Run one BYOK OpenAI-compatible multimodal vision request."""
    enabled, base_url, api_key, model = resolve_vision_config(settings)
    if not enabled:
        return {"content": "", "points_charged": 0.0, "skipped": True, "model": ""}
    if not base_url or not model or (not api_key and not is_local_base_url(base_url)):
        raise AIProviderError("视觉识别（板书/PPT OCR）尚未配置完整 Base URL、模型与密钥。")

    blocks: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for encoded in images_b64:
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": blocks},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    content = _post_chat_completions(
        base_url=base_url,
        api_key=api_key,
        model=model,
        payload=payload,
        timeout_seconds=timeout_seconds,
        label="视觉识别模型",
    )
    return {
        "content": content,
        "points_charged": 0.0,
        "model": model,
    }


def test_connection(settings: object, *, target: str = "llm") -> dict[str, Any]:
    """Verify live connectivity or local readiness for ``llm``, ``vision`` or ``asr``."""
    kind = str(target or "llm").strip().lower()
    t0 = time.monotonic()

    if kind == "llm":
        base_url, _, model = resolve_llm_config(settings, "notes")
        try:
            call_llm(
                "test",
                "Reply with OK only.",
                settings,
                system="You are a connectivity check. Reply with OK.",
                temperature=0.0,
                max_tokens=8,
                timeout_seconds=15.0,
            )
            return {
                "ok": True,
                "target": "llm",
                "model": model,
                "base_url": base_url,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"连接成功（模型：{model}）",
            }
        except Exception as exc:
            return {
                "ok": False,
                "target": "llm",
                "model": model,
                "base_url": base_url,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": str(exc),
            }

    if kind == "vision":
        enabled, base_url, api_key, model = resolve_vision_config(settings)
        if not enabled:
            return {
                "ok": False,
                "target": "vision",
                "model": model,
                "base_url": base_url,
                "latency_ms": 0,
                "detail": "当前已关闭视觉识别（板书与投影 OCR）。勾选启用后即可测试。",
            }
        try:
            call_vision(
                "Reply OK only.",
                "Connectivity check image.",
                [_PING_JPEG_B64],
                settings,
                json_mode=False,
                max_tokens=16,
                timeout_seconds=20.0,
            )
            return {
                "ok": True,
                "target": "vision",
                "model": model,
                "base_url": base_url,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"视觉模型连接成功（{model}）",
            }
        except Exception as exc:
            return {
                "ok": False,
                "target": "vision",
                "model": model,
                "base_url": base_url,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": str(exc),
            }

    if kind == "asr":
        cfg = resolve_asr_config(settings)
        backend = cfg["backend"]
        if backend == "local":
            if cfg["local_available"]:
                return {
                    "ok": True,
                    "target": "asr",
                    "backend": "local",
                    "model": cfg["whisper_model"],
                    "base_url": "local://faster-whisper",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "detail": (
                        f"本地 Faster-Whisper 已就绪（模型：{cfg['whisper_model']} · "
                        f"设备：{cfg['whisper_device']}）"
                    ),
                }
            return {
                "ok": False,
                "target": "asr",
                "backend": "local",
                "model": cfg["whisper_model"],
                "base_url": "local://faster-whisper",
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": "未安装本地语音识别依赖 faster-whisper，请执行 `uv sync --extra local-asr` 安装后重试。",
            }

        base_url = str(cfg.get("base_url") or "")
        api_key = str(cfg.get("api_key") or "")
        model = str(cfg.get("model") or "")
        if not base_url or not model or (not api_key and not is_local_base_url(base_url)):
            return {
                "ok": False,
                "target": "asr",
                "backend": backend,
                "model": model,
                "base_url": base_url,
                "latency_ms": 0,
                "detail": f"请填写完整的 {backend.upper()} 语音识别接口地址、密钥与模型名称。",
            }

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            with httpx.Client(trust_env=not dead_loopback_proxy(), timeout=15.0) as client:
                resp = client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            latency = int((time.monotonic() - t0) * 1000)
            if resp.status_code in (401, 403):
                return {
                    "ok": False,
                    "target": "asr",
                    "backend": backend,
                    "model": model,
                    "base_url": base_url,
                    "latency_ms": latency,
                    "detail": f"ASR 密钥认证失败（HTTP {resp.status_code}），请核对 API Key。",
                }
            if resp.status_code == 200:
                return {
                    "ok": True,
                    "target": "asr",
                    "backend": backend,
                    "model": model,
                    "base_url": base_url,
                    "latency_ms": latency,
                    "detail": f"ASR 接口连通正常（{backend} · {model}）",
                }
            return {
                "ok": True,
                "target": "asr",
                "backend": backend,
                "model": model,
                "base_url": base_url,
                "latency_ms": latency,
                "detail": f"配置已保存且网络可达（{backend} · {model}；该接口未开放 /models 探针）",
            }
        except Exception as exc:
            return {
                "ok": False,
                "target": "asr",
                "backend": backend,
                "model": model,
                "base_url": base_url,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": f"无法连接到 ASR 接口（{type(exc).__name__}），请检查网络或代理设置。",
            }

    raise AIProviderError(f"未知测试目标：{target}")


__all__ = [
    "AIProviderError",
    "LLM_REQUEST_MAX_BYTES",
    "MODEL_PRESETS",
    "call_llm",
    "call_vision",
    "is_asr_configured",
    "is_llm_configured",
    "is_local_base_url",
    "is_vision_configured",
    "llm_request_body_size",
    "local_asr_available",
    "mask_secret",
    "resolve_asr_config",
    "resolve_llm_config",
    "resolve_vision_config",
    "test_connection",
]
