"""Panel-side local BYOK bridge for refined notes, quiz generation, grading, and vision OCR."""

from __future__ import annotations

from ..ai_provider import call_llm, call_vision


class LocalAIBridge:
    """Adapter exposing the panel AI interface backed directly by user-configured API keys."""

    durable_metered_jobs = False
    operation_context_supported = False

    def __init__(self, settings) -> None:
        self._settings = settings

    def quota(self) -> dict:
        return {"available": True, "llm_points_remaining": 1_000_000.0, "unlimited": True}

    def quiz(self, prompt: str, *, operation_context=None) -> dict:
        return call_llm("quiz", prompt, self._settings)

    def grade(self, prompt: str, *, operation_context=None) -> dict:
        return call_llm("grade", prompt, self._settings)

    def refine_note(self, prompt: str, *, system: str | None = None, operation_context=None) -> dict:
        return call_llm("refine", prompt, self._settings, system=system)

    def vision_ocr(
        self,
        system: str,
        user: str,
        images_b64: list[str],
        *,
        json_mode: bool = True,
        min_reserve_millipoints: int = 0,
    ) -> dict:
        return call_vision(system, user, images_b64, self._settings, json_mode=json_mode)
