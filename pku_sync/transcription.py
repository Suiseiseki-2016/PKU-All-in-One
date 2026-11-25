"""BYOK speech-to-text backends: OpenAI-compatible ASR, DashScope ASR, and local faster-whisper.

Audio extraction decodes inside this process via PyAV (which bundles its own
FFmpeg libraries), so no external ``ffmpeg`` binary is required for audio
transcoding. Chunk uploads run purely from RAM using ``MemoryAudioPart`` when
streaming lectures without saving video files to disk.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from functools import partial
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

_LOGGER = logging.getLogger(__name__)

_AUDIO_CODEC = "aac"
_AUDIO_RATE = 16000
_AUDIO_LAYOUT = "mono"
_CHUNK_SECONDS = 600
_DASHSCOPE_CHUNK_SECONDS = 120
_AUDIO_PARTS_VERSION = 3
_RETRY_DELAY = 2.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class _NoAudioTrack(Exception):
    """The container opened and decoded, but carries no audio stream."""


def _decoder_note() -> str:
    return "（音轨由内置解码器 PyAV 在本地解码）"


def _detail(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= 200 else text[:197] + "..."


def _to_simplified(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    try:
        from zhconv import convert

        return convert(clean, "zh-cn")
    except Exception:
        return clean


def _chunk_seconds_for_backend(backend: str) -> int:
    return _DASHSCOPE_CHUNK_SECONDS if backend == "dashscope" else _CHUNK_SECONDS


def chunk_seconds_for_settings(settings: "Settings | None") -> int:
    """Return optimal audio chunk length in seconds for the active ASR backend."""
    if settings is None:
        return _CHUNK_SECONDS
    backend = str(getattr(settings, "transcription_backend", "openai") or "openai").strip().lower()
    return _chunk_seconds_for_backend(backend)


def transcribe(
    video: Path,
    target: Path,
    settings: "Settings",
    progress=None,
    direct_oss: bool = False,
) -> dict:
    """Dispatch one recording to the configured BYOK transcription backend."""
    if target.exists():
        return json.loads(target.read_text("utf-8"))
    backend = str(getattr(settings, "transcription_backend", "openai") or "openai").strip().lower()
    if backend == "local":
        from .media import transcribe as local_transcribe

        return local_transcribe(video, target, settings)
    if backend in {"openai", "dashscope"}:
        return transcribe_cloud(video, target, settings, progress=progress, direct_oss=direct_oss)
    raise ValueError(f"不支持的语音转写后端：{settings.transcription_backend!r}（可选 openai / dashscope / local）")


def _extract_audio(video: Path, out: Path) -> Path:
    """Extract a 16 kHz mono AAC track from ``video`` via PyAV."""
    try:
        import av
        from av.audio.resampler import AudioResampler
    except Exception as exc:
        raise RuntimeError(
            f"音频提取需要内置解码器 PyAV，当前无法加载：{_detail(exc)}。"
        ) from exc

    try:
        with av.open(str(video)) as container:
            source = next((s for s in container.streams if s.type == "audio"), None)
            if source is None:
                raise _NoAudioTrack(video.name)
            out.parent.mkdir(parents=True, exist_ok=True)
            with av.open(str(out), mode="w") as sink:
                track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                track.layout = _AUDIO_LAYOUT
                resampler = AudioResampler(
                    format=track.format.name, layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE
                )
                for frame in container.decode(source):
                    for resampled in resampler.resample(frame):
                        for packet in track.encode(resampled):
                            sink.mux(packet)
                for resampled in resampler.resample(None):
                    for packet in track.encode(resampled):
                        sink.mux(packet)
                for packet in track.encode(None):
                    sink.mux(packet)
    except _NoAudioTrack as exc:
        raise RuntimeError(f"在该录像里找不到音轨：{exc.args[0]}。") from None
    except Exception as exc:
        raise RuntimeError(f"无法读取该录像的音轨：{_detail(exc)}。") from exc
    return out


def _audio_duration(video: Path) -> float:
    """Return container audio duration in seconds, or 0 when unavailable."""
    try:
        import av

        with av.open(str(video)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is not None and stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
    except Exception:
        pass
    return 0.0


class MemoryAudioPart:
    """In-memory encoded 16 kHz mono AAC (.m4a) chunk — never touches disk."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self.stem = Path(name).stem
        self.suffix = Path(name).suffix
        self._data = data

    @property
    def data(self) -> bytes:
        return self._data

    def size_bytes(self) -> int:
        return len(self._data)

    def open(self, mode: str = "rb"):
        return io.BytesIO(self._data)


class _MemoryAudioChunkEncoder:
    """Stateful 16 kHz mono AAC (.m4a) chunk encoder that muxes entirely into RAM."""

    _FIFO_BATCH_SAMPLES = 16384

    def __init__(
        self, chunk_seconds: int = _CHUNK_SECONDS, progress=None, total_samples: int = 0
    ) -> None:
        import av
        from av.audio.fifo import AudioFifo
        from av.audio.resampler import AudioResampler

        if type(chunk_seconds) is not int or chunk_seconds <= 0:
            raise ValueError("音频分段长度必须是正整数秒。")
        self._av = av
        self._limit = chunk_seconds * _AUDIO_RATE
        self._time_base = Fraction(1, _AUDIO_RATE)
        self._progress = progress
        self._total_samples = total_samples
        self._fifo = AudioFifo()
        self._resampler = AudioResampler(format="fltp", layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE)
        self._buf = None
        self._sink = None
        self._track = None
        self._part_offset = 0.0
        self._samples = 0
        self._start = 0
        self._last_report = 0
        self.parts: list[tuple[MemoryAudioPart, float]] = []
        if self._progress:
            self._progress(0, self._total_samples, "audio_extract", 0)

    def _open_sink(self) -> None:
        self._buf = io.BytesIO()
        self._part_offset = self._start / _AUDIO_RATE
        self._sink = self._av.open(
            self._buf,
            mode="w",
            format="mp4",
            options={"movie_timescale": str(_AUDIO_RATE)},
        )
        try:
            self._track = self._sink.add_stream(
                _AUDIO_CODEC, rate=_AUDIO_RATE, options={"aac_coder": "fast"}
            )
        except Exception:
            self._track = self._sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
        self._track.layout = _AUDIO_LAYOUT

    def _close_sink(self) -> None:
        if self._sink is None:
            return
        for packet in self._track.encode(None):
            self._sink.mux(packet)
        self._sink.close()
        name = f"part-{len(self.parts):04d}.m4a"
        self.parts.append((MemoryAudioPart(name, self._buf.getvalue()), self._part_offset))
        self._sink = None
        self._track = None
        self._buf = None

    def _drain_fifo(self, *, flush: bool = False) -> None:
        while self._fifo.samples:
            remaining = self._limit - (self._samples - self._start)
            threshold = min(self._FIFO_BATCH_SAMPLES, remaining)
            if not flush and self._fifo.samples < threshold:
                break
            if self._sink is None:
                self._open_sink()
            take = min(self._fifo.samples, remaining, self._FIFO_BATCH_SAMPLES)
            piece = self._fifo.read(take)
            piece.pts = self._samples - self._start
            piece.time_base = self._time_base
            for packet in self._track.encode(piece):
                self._sink.mux(packet)
            self._samples += piece.samples
            if self._progress and self._samples - self._last_report >= 30 * _AUDIO_RATE:
                self._progress(
                    min(self._samples, self._total_samples) if self._total_samples else self._samples,
                    self._total_samples,
                    "audio_extract",
                    0,
                )
                self._last_report = self._samples
            if self._samples - self._start == self._limit:
                self._close_sink()
                self._start = self._samples

    def _write_resampled(self, frame, *, flush: bool = False) -> None:
        frame.pts = None
        self._fifo.write(frame)
        self._drain_fifo(flush=flush)

    def feed(self, frame) -> None:
        for resampled in self._resampler.resample(frame):
            self._write_resampled(resampled, flush=False)

    def finish(self) -> list[tuple[MemoryAudioPart, float]]:
        try:
            for resampled in self._resampler.resample(None):
                self._write_resampled(resampled, flush=False)
            self._drain_fifo(flush=True)
            if self._sink is not None:
                self._close_sink()
            if self._progress:
                total = self._total_samples or self._samples
                self._progress(total, total, "audio_extract", 0)
        finally:
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass
                self._sink = None
                self._track = None
                self._buf = None
        return self.parts

    def close(self) -> None:
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
            self._sink = None
            self._track = None
            self._buf = None


def _encode_audio_parts_in_memory(
    frames,
    chunk_seconds: int = _CHUNK_SECONDS,
    progress=None,
    total_samples: int = 0,
) -> list[tuple[MemoryAudioPart, float]]:
    encoder = _MemoryAudioChunkEncoder(
        chunk_seconds=chunk_seconds, progress=progress, total_samples=total_samples
    )
    try:
        for frame in frames:
            encoder.feed(frame)
        return encoder.finish()
    finally:
        encoder.close()


def _extract_audio_parts_in_memory(
    video: Path,
    chunk_seconds: int = _CHUNK_SECONDS,
    progress=None,
) -> list[tuple[MemoryAudioPart, float]]:
    try:
        import av
    except Exception as exc:
        raise RuntimeError(
            f"音频提取需要内置解码器 PyAV，当前无法加载：{_detail(exc)}。"
        ) from exc

    try:
        with av.open(str(video)) as container:
            source = next((s for s in container.streams if s.type == "audio"), None)
            if source is None:
                raise _NoAudioTrack(video.name)
            total_samples = (
                int(float(source.duration * source.time_base) * _AUDIO_RATE)
                if source.duration and source.time_base
                else 0
            )
            parts = _encode_audio_parts_in_memory(
                container.decode(source),
                chunk_seconds,
                progress=progress,
                total_samples=total_samples,
            )
            if not parts:
                raise _NoAudioTrack(video.name)
            return parts
    except _NoAudioTrack as exc:
        raise RuntimeError(f"在该录像里找不到音轨：{exc.args[0]}。") from None
    except Exception as exc:
        raise RuntimeError(f"无法读取该录像的音轨：{_detail(exc)}。") from exc


def _encode_audio_parts(
    frames,
    directory: Path,
    chunk_seconds: int,
    progress=None,
    total_samples: int = 0,
) -> list[tuple[Path, float]]:
    import av
    from av.audio.fifo import AudioFifo
    from av.audio.resampler import AudioResampler

    if type(chunk_seconds) is not int or chunk_seconds <= 0:
        raise ValueError("音频分段长度必须是正整数秒。")
    limit = chunk_seconds * _AUDIO_RATE
    time_base = Fraction(1, _AUDIO_RATE)
    batch_samples = 16384
    directory.mkdir(parents=True, exist_ok=True)
    parts = []
    fifo = AudioFifo()
    resampler = AudioResampler(format="fltp", layout=_AUDIO_LAYOUT, rate=_AUDIO_RATE)
    sink, track = None, None
    samples, start, last_report = 0, 0, 0
    if progress:
        progress(0, total_samples, "audio_extract", 0)

    def drain_fifo(*, flush: bool = False):
        nonlocal sink, track, samples, start, last_report
        while fifo.samples:
            remaining = limit - (samples - start)
            threshold = min(batch_samples, remaining)
            if not flush and fifo.samples < threshold:
                break
            if sink is None:
                path = directory / f"part-{len(parts):04d}.m4a"
                sink = av.open(str(path), mode="w", options={"movie_timescale": str(_AUDIO_RATE)})
                try:
                    track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE, options={"aac_coder": "fast"})
                except Exception:
                    track = sink.add_stream(_AUDIO_CODEC, rate=_AUDIO_RATE)
                track.layout = _AUDIO_LAYOUT
                parts.append((path, start / _AUDIO_RATE))
            take = min(fifo.samples, remaining, batch_samples)
            piece = fifo.read(take)
            piece.pts = samples - start
            piece.time_base = time_base
            for packet in track.encode(piece):
                sink.mux(packet)
            samples += piece.samples
            if progress and samples - last_report >= 30 * _AUDIO_RATE:
                progress(
                    min(samples, total_samples) if total_samples else samples,
                    total_samples,
                    "audio_extract",
                    0,
                )
                last_report = samples
            if samples - start == limit:
                for packet in track.encode(None):
                    sink.mux(packet)
                sink.close()
                sink = None
                start = samples

    for frame in frames:
        for resampled in resampler.resample(frame):
            resampled.pts = None
            fifo.write(resampled)
            drain_fifo()
    for resampled in resampler.resample(None):
        resampled.pts = None
        fifo.write(resampled)
        drain_fifo()
    drain_fifo(flush=True)
    if sink is not None:
        for packet in track.encode(None):
            sink.mux(packet)
        sink.close()
    if progress:
        total = total_samples or samples
        progress(total, total, "audio_extract", 0)
    return parts


def _extract_audio_parts(
    video: Path, directory: Path, chunk_seconds: int, progress=None
) -> list[tuple[Path, float]]:
    import av

    with av.open(str(video)) as container:
        source = next((s for s in container.streams if s.type == "audio"), None)
        if source is None:
            raise _NoAudioTrack(video.name)
        total_samples = (
            int(float(source.duration * source.time_base) * _AUDIO_RATE)
            if source.duration and source.time_base
            else 0
        )
        parts = _encode_audio_parts(
            container.decode(source),
            directory,
            chunk_seconds,
            progress=progress,
            total_samples=total_samples,
        )
        if not parts:
            raise _NoAudioTrack(video.name)
        return parts


def _read_audio_bytes(audio: Path | MemoryAudioPart) -> bytes:
    if isinstance(audio, MemoryAudioPart):
        return audio.data
    return Path(audio).read_bytes()


def _part_duration(audio: Path | MemoryAudioPart) -> float:
    try:
        import av

        if isinstance(audio, MemoryAudioPart):
            with av.open(io.BytesIO(audio.data)) as container:
                stream = next((s for s in container.streams if s.type == "audio"), None)
                if stream is not None and stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
        else:
            return _audio_duration(Path(audio))
    except Exception:
        pass
    return 0.0


def _segments_from_plain_text(text: str, duration: float) -> list[dict]:
    clean = _to_simplified(text)
    if not clean:
        return []
    sentences = [
        chunk.strip()
        for chunk in re.split(r"(?<=[。！？!?；;\n])", clean)
        if chunk and chunk.strip()
    ]
    if not sentences:
        sentences = [clean]
    if len(sentences) == 1 or duration <= 0.0:
        return [{"start": 0.0, "end": round(max(0.0, duration), 2), "text": clean}]
    total_chars = sum(len(s) for s in sentences) or 1
    cursor = 0.0
    rows: list[dict] = []
    for sent in sentences:
        span = duration * (len(sent) / total_chars)
        end = min(duration, cursor + span)
        rows.append({"start": round(cursor, 2), "end": round(end, 2), "text": sent})
        cursor = end
    return rows


def _upload_openai(
    base_url: str,
    api_key: str,
    model: str,
    audio: Path | MemoryAudioPart,
) -> dict:
    """POST one audio chunk to an OpenAI-compatible ``/audio/transcriptions`` endpoint."""
    import httpx
    from .network import dead_loopback_proxy

    endpoint = f"{base_url.rstrip('/')}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response_format = "verbose_json"
    trust_env = not dead_loopback_proxy()

    response = None
    for attempt in (1, 2, 3):
        try:
            with audio.open("rb") as source, httpx.Client(
                timeout=600.0, trust_env=trust_env
            ) as client:
                response = client.post(
                    endpoint,
                    headers=headers,
                    data={
                        "model": model,
                        "language": "zh",
                        "response_format": response_format,
                    },
                    files={"file": (audio.name, source, "audio/mp4")},
                )
            if response.status_code in (400, 422) and response_format == "verbose_json":
                response_format = "json"
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < 3:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            break
        except httpx.TransportError as exc:
            if attempt == 3:
                raise RuntimeError(f"ASR 音频上传失败（{type(exc).__name__}）：{_detail(exc)}") from exc
            time.sleep(_RETRY_DELAY * attempt)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"ASR 音频上传失败：{_detail(exc)}") from exc

    if response is None:
        raise RuntimeError("ASR 转写未收到响应。")
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"ASR 密钥认证失败（HTTP {response.status_code}），请在「模型配置」中核对 ASR API Key。"
        )
    if response.status_code != 200:
        snippet = (response.text or "").strip().replace("\n", " ")[:200]
        raise RuntimeError(f"ASR 转写失败（HTTP {response.status_code}）：{snippet}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("ASR 返回了非 JSON 响应。") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("ASR 返回格式无效。")

    raw_segments = payload.get("segments")
    segments: list[dict] = []
    if isinstance(raw_segments, list) and raw_segments:
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            text = _to_simplified(str(seg.get("text") or ""))
            if not text:
                continue
            segments.append(
                {
                    "start": round(float(seg.get("start") or 0.0), 2),
                    "end": round(float(seg.get("end") or seg.get("start") or 0.0), 2),
                    "text": text,
                }
            )
    else:
        full_text = str(payload.get("text") or "").strip()
        dur = float(payload.get("duration") or _part_duration(audio) or 0.0)
        segments = _segments_from_plain_text(full_text, dur)

    return {
        "language": str(payload.get("language") or "zh"),
        "segments": segments,
    }


def _upload_dashscope(
    base_url: str,
    api_key: str,
    model: str,
    audio: Path | MemoryAudioPart,
) -> dict:
    """POST one audio chunk to DashScope compatible-mode ``/chat/completions`` with ``input_audio``."""
    import httpx
    from .ai_provider import _extract_message_content
    from .network import dead_loopback_proxy

    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    audio_bytes = _read_audio_bytes(audio)
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    mime = "audio/wav" if Path(audio.name).suffix.lower() == ".wav" else "audio/mp4"
    body: dict = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                    }
                ],
            }
        ],
        "stream": False,
        "asr_options": {"language": "zh", "enable_itn": True},
    }

    trust_env = not dead_loopback_proxy()
    response = None
    for attempt in (1, 2, 3):
        try:
            with httpx.Client(timeout=300.0, trust_env=trust_env) as client:
                response = client.post(endpoint, headers=headers, json=body)
            if response.status_code == 400 and "asr_options" in body:
                body.pop("asr_options", None)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < 3:
                time.sleep(_RETRY_DELAY * attempt)
                continue
            break
        except httpx.TransportError as exc:
            if attempt == 3:
                raise RuntimeError(f"百炼 ASR 连接失败（{type(exc).__name__}）：{_detail(exc)}") from exc
            time.sleep(_RETRY_DELAY * attempt)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"百炼 ASR 请求失败：{_detail(exc)}") from exc

    if response is None:
        raise RuntimeError("百炼 ASR 未返回响应。")
    if response.status_code in (401, 403):
        raise RuntimeError(
            f"百炼 DashScope API Key 无效或无权限（HTTP {response.status_code}），请在「模型配置」中检查。"
        )
    if response.status_code != 200:
        snippet = (response.text or "").strip().replace("\n", " ")[:200]
        raise RuntimeError(f"百炼 ASR 转写失败（HTTP {response.status_code}）：{snippet}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("百炼 ASR 返回非 JSON 响应。") from exc

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        raise RuntimeError("百炼 ASR 返回缺少 choices。")
    text = _extract_message_content((choices[0] or {}).get("message") or {})
    dur = _part_duration(audio)
    return {
        "language": "zh",
        "segments": _segments_from_plain_text(text, dur),
    }


def _rechunk_part_if_needed(
    parts: list[tuple[MemoryAudioPart | Path, float]], max_chunk_seconds: int
) -> list[tuple[MemoryAudioPart | Path, float]]:
    """Split oversized chunks in memory when e.g. a 600s chunk is handed to DashScope (120s limit)."""
    try:
        import av
    except Exception:
        return parts

    result: list[tuple[MemoryAudioPart | Path, float]] = []
    for item, offset in parts:
        dur = _part_duration(item)
        if dur <= max_chunk_seconds + 5.0:
            result.append((item, offset))
            continue
        raw = _read_audio_bytes(item)
        with av.open(io.BytesIO(raw)) as container:
            source = next((s for s in container.streams if s.type == "audio"), None)
            if source is None:
                result.append((item, offset))
                continue
            sub_parts = _encode_audio_parts_in_memory(
                container.decode(source),
                chunk_seconds=max_chunk_seconds,
            )
            for idx, (sub_part, sub_offset) in enumerate(sub_parts):
                sub_part.name = f"rechunk-{len(result):04d}.m4a"
                result.append((sub_part, round(offset + sub_offset, 3)))
    return result or parts


def _local_transcribe_parts(
    parts: list[tuple[MemoryAudioPart | Path, float]],
    target: Path,
    settings: "Settings",
    *,
    video: Path | None = None,
    progress=None,
) -> dict:
    from .media import transcribe as local_transcribe

    if video is not None and video.exists():
        return local_transcribe(video, target, settings)

    if not parts:
        raise RuntimeError("没有可转写的音频片段。")

    if progress:
        progress(0, len(parts), "audio_parts", 0)
    merged: list[dict] = []
    language = "zh"
    with tempfile.TemporaryDirectory(prefix="pku-sync-local-asr-") as tmp:
        tmp_dir = Path(tmp)
        for index, (item, offset) in enumerate(parts):
            if isinstance(item, MemoryAudioPart):
                chunk_file = tmp_dir / f"part-{index:04d}.m4a"
                chunk_file.write_bytes(item.data)
            else:
                chunk_file = Path(item)
            part_json = tmp_dir / f"part-{index:04d}.json"
            part_res = local_transcribe(chunk_file, part_json, settings)
            language = str(part_res.get("language") or language)
            for seg in part_res.get("segments") or []:
                shifted = dict(seg)
                for key in ("start", "end"):
                    if isinstance(shifted.get(key), (int, float)):
                        shifted[key] = round(float(shifted[key]) + offset, 3)
                merged.append(shifted)
            if progress:
                progress(index + 1, len(parts), "audio_parts", 0)
    return {
        "language": language,
        "duration_seconds": round(
            max((float(s.get("end", s.get("start", 0))) for s in merged), default=0.0), 3
        ),
        "segments": merged,
    }


def transcribe_memory_parts(
    parts: list[tuple[MemoryAudioPart | Path, float]],
    target: Path,
    settings: "Settings",
    *,
    video: Path | None = None,
    stream_signature: str = "",
    progress=None,
    direct_oss: bool = False,
    elapsed_start: float | None = None,
) -> dict:
    """Transcribe in-memory audio parts with the user's configured ASR backend."""
    from .ai_provider import resolve_asr_config

    started = elapsed_start if elapsed_start is not None else time.perf_counter()
    if target.exists():
        return json.loads(target.read_text("utf-8"))

    cfg = resolve_asr_config(settings)
    backend = cfg["backend"]

    if backend == "local":
        payload = _local_transcribe_parts(parts, target, settings, video=video, progress=progress)
        payload = dict(payload)
        payload["elapsed"] = round(time.perf_counter() - started, 1)
        _atomic_json(target, payload)
        return payload

    if not cfg.get("configured"):
        raise RuntimeError(
            f"尚未配置 {backend.upper()} 语音转写服务（缺少密钥或接口地址），请先前往「模型配置」完成设置。"
        )
    if not parts:
        raise RuntimeError(f"在该录像里找不到音轨，无法提取语音{_decoder_note()}。")

    base_url = str(cfg["base_url"])
    api_key = str(cfg["api_key"])
    model = str(cfg["model"])

    if backend == "dashscope":
        parts = _rechunk_part_if_needed(parts, _DASHSCOPE_CHUNK_SECONDS)
        uploader = partial(_upload_dashscope, base_url, api_key, model)
    else:
        uploader = partial(_upload_openai, base_url, api_key, model)

    if progress:
        progress(0, len(parts), "audio_parts", 0)

    if len(parts) == 1:
        single = uploader(parts[0][0])
        if progress:
            progress(1, 1, "audio_parts", 0)
        segs = []
        offset = float(parts[0][1])
        for seg in single.get("segments") or []:
            row = dict(seg)
            for key in ("start", "end"):
                if isinstance(row.get(key), (int, float)):
                    row[key] = round(float(row[key]) + offset, 3)
            segs.append(row)
        payload = {
            "language": single.get("language", "zh"),
            "duration_seconds": round(
                max((float(s.get("end", s.get("start", 0))) for s in segs), default=0.0), 3
            ),
            "segments": segs,
        }
    else:
        video_ref = video if video is not None else (target.parent / "video.mp4")
        payload = _transcribe_parts(
            parts,
            base_url,
            api_key,
            video_ref,
            target,
            progress=progress,
            uploader=lambda _url, _token, part: uploader(part),
            stream_signature=stream_signature,
            settings=settings,
        )
        _clear_part_cache(video_ref, target, len(parts), stream_signature=stream_signature)

    payload = dict(payload)
    payload["elapsed"] = round(time.perf_counter() - started, 1)
    _atomic_json(target, payload)
    return payload


def transcribe_cloud(
    video: Path,
    target: Path,
    settings: "Settings",
    progress=None,
    direct_oss: bool = False,
) -> dict:
    """Extract audio in memory and run configured BYOK API ASR (``openai`` or ``dashscope``)."""
    from .ai_provider import resolve_asr_config

    started = time.perf_counter()
    if target.exists():
        return json.loads(target.read_text("utf-8"))

    cfg = resolve_asr_config(settings)
    if not cfg.get("configured"):
        raise RuntimeError(
            f"尚未配置 {cfg['backend'].upper()} 语音转写服务（缺少密钥或接口地址），请先前往「模型配置」完成设置。"
        )
    chunk_seconds = _chunk_seconds_for_backend(cfg["backend"])

    if _extract_audio is _DEFAULT_EXTRACT_AUDIO and _extract_audio_parts is _DEFAULT_EXTRACT_AUDIO_PARTS and _split_audio is _DEFAULT_SPLIT_AUDIO:
        parts = _extract_audio_parts_in_memory(video, chunk_seconds, progress=progress)
        return transcribe_memory_parts(
            parts,
            target,
            settings,
            video=video,
            progress=progress,
            direct_oss=direct_oss,
            elapsed_start=started,
        )

    with tempfile.TemporaryDirectory(prefix="pku-sync-audio-") as tmp:
        if _audio_duration(video) > chunk_seconds:
            parts = _extract_audio_parts(video, Path(tmp), chunk_seconds, progress=progress)
        else:
            audio = _extract_audio(video, Path(tmp) / "audio.m4a")
            parts = _split_audio(audio, Path(tmp), chunk_seconds)
        return transcribe_memory_parts(
            parts,
            target,
            settings,
            video=video,
            progress=progress,
            direct_oss=direct_oss,
            elapsed_start=started,
        )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    temporary.replace(path)


def _split_audio(audio: Path, directory: Path, chunk_seconds: int) -> list[tuple[Path, float]]:
    import av

    with av.open(str(audio)) as source:
        stream = next(s for s in source.streams if s.type == "audio")
        duration = float(stream.duration * stream.time_base) if stream.duration else 0
        if duration <= chunk_seconds:
            return [(audio, 0.0)]
        return _encode_audio_parts(source.decode(stream), directory, chunk_seconds) or [(audio, 0.0)]


def _part_cache_signature(
    parts: list[tuple[Path | MemoryAudioPart, float]],
    video: Path | None,
    stream_signature: str = "",
) -> str:
    chunk_marker = _CHUNK_SECONDS
    if stream_signature:
        return hashlib.sha256(
            f"{stream_signature}:{chunk_marker}:{_AUDIO_PARTS_VERSION}".encode()
        ).hexdigest()[:16]
    if video is not None and video.exists():
        stat = video.stat()
        return hashlib.sha256(
            f"{stat.st_size}:{stat.st_mtime_ns}:{chunk_marker}:{_AUDIO_PARTS_VERSION}".encode()
        ).hexdigest()[:16]
    digest = hashlib.sha256(f"{chunk_marker}:{_AUDIO_PARTS_VERSION}".encode())
    for item, offset in parts:
        digest.update(f":{offset}:".encode())
        if isinstance(item, MemoryAudioPart):
            digest.update(hashlib.sha256(item.data).digest())
        elif isinstance(item, Path) and item.exists():
            stat = item.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


def _transcribe_parts(
    parts: list[tuple[Path | MemoryAudioPart, float]],
    url: str,
    token: str,
    video: Path,
    target: Path,
    progress=None,
    uploader=None,
    *,
    stream_signature: str = "",
    settings: "Settings | None" = None,
) -> dict:
    signature = _part_cache_signature(parts, video, stream_signature=stream_signature)
    cache = target.parent / ".transcription-parts"
    cache.mkdir(parents=True, exist_ok=True)
    replies: list[dict | None] = [None] * len(parts)
    pending = []
    for index, (path, _) in enumerate(parts):
        saved = cache / f"{signature}-{index:04d}.json"
        try:
            reply = json.loads(saved.read_text("utf-8"))
            if not isinstance(reply.get("segments"), list):
                raise ValueError("invalid cached part")
            replies[index] = reply
        except (OSError, ValueError, TypeError, AttributeError):
            pending.append((index, path, saved))

    failures = []
    completed = len(parts) - len(pending)
    if progress:
        progress(completed, len(parts), "audio_parts", 0)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(uploader, url, token, path): (index, saved)
            for index, path, saved in pending
        }
        for future in as_completed(futures):
            index, saved = futures[future]
            try:
                reply = future.result()
                _atomic_json(saved, reply)
                replies[index] = reply
                completed += 1
                if progress:
                    progress(completed, len(parts), "audio_parts", 0)
            except Exception as exc:
                failures.append((index, exc))
    if failures:
        index, error = min(failures, key=lambda item: item[0])
        raise RuntimeError(
            f"第 {index + 1}/{len(parts)} 段转写失败（已完成片段会保留缓存以便重试）：{_detail(error)}"
        ) from error

    segments = []
    for reply, (_, offset) in zip(replies, parts):
        for segment in (reply or {}).get("segments") or []:
            seg = dict(segment)
            for name in ("start", "end"):
                if isinstance(seg.get(name), (int, float)):
                    seg[name] = round(float(seg[name]) + offset, 3)
            segments.append(seg)
    return {
        "language": (replies[0] or {}).get("language", "zh"),
        "duration_seconds": round(
            max((float(seg.get("end", seg.get("start", 0))) for seg in segments), default=0.0),
            3,
        ),
        "segments": segments,
    }


def _clear_part_cache(
    video: Path, target: Path, count: int, *, stream_signature: str = ""
) -> None:
    cache = target.parent / ".transcription-parts"
    if stream_signature or (video is not None and video.exists()):
        signature = _part_cache_signature([], video, stream_signature=stream_signature)
        for index in range(count):
            (cache / f"{signature}-{index:04d}.json").unlink(missing_ok=True)
    elif cache.is_dir():
        for item in cache.glob("*.json"):
            item.unlink(missing_ok=True)


_DEFAULT_EXTRACT_AUDIO = _extract_audio
_DEFAULT_EXTRACT_AUDIO_PARTS = _extract_audio_parts
_DEFAULT_SPLIT_AUDIO = _split_audio
