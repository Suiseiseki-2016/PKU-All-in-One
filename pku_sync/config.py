"""Local open-source BYOK configuration loaded from `.env` and environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the standalone BYOK PKU All in One client."""

    # Per-task ephemeral state (never persisted to disk or .env).
    _incremental_sync: bool = PrivateAttr(default=False)
    _shared_campus_session: dict | None = PrivateAttr(default=None)
    _course_deltas: dict | None = PrivateAttr(default=None)

    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parent.parent / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PKU Blackboard / IAAA credentials (stored locally in .env) ---
    pku_username: str = ""
    pku_password: str = ""

    data_dir: Path = Path("data")

    course_allowlist: str = ""
    course_denylist: str = ""

    # Blackboard course codes start with a 5-digit term, e.g. 26271 is
    # "2026-27 academic year, term 1". Empty means auto-detect the newest term
    # present in the account. Not named `term`: pydantic-settings matches env
    # vars case-insensitively, so that would inherit the shell's TERM.
    course_term: str = ""
    include_past_terms: bool = False

    # --- BYOK Text LLM (notes, refined handouts, summary, quiz, grading, logic map) ---
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_api_key: str = ""
    notes_model: str = "deepseek-chat"
    refine_model: str = ""  # Empty = reuse notes_model
    notes_strategy: Literal["transcript", "topics"] = "topics"
    notes_merge_model: str = ""

    # Legacy settings retained so existing .env files continue loading cleanly.
    notes_fact_check: bool = True
    notes_claim_audit: bool = True
    notes_structured_composition: bool = False
    notes_source_matching: bool = True

    # Backend for CLI one-shot calls ("auto" uses OpenAI-compatible API when
    # configured, else logged-in claude / codex / droid CLI).
    llm_provider: str = "auto"
    llm_model: str = ""  # Empty = falls back to notes_model
    llm_timeout: int = 600

    # --- BYOK Vision VLM (optional blackboard & slide OCR; empty fields inherit LLM config) ---
    vision_enabled: bool = True
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""

    # --- BYOK Speech-to-Text ASR ("openai" | "dashscope" | "local") ---
    transcription_backend: str = "openai"
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = "whisper-1"
    dashscope_api_key: str = ""

    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"

    # --- Optional Notion Sync ---
    agent_host: str = "claude"
    notion_token: str = ""
    notion_oauth_client_id: str = ""
    notion_oauth_client_secret: str = ""

    # --- Local Recording Storage ---
    delete_video_after_processing: bool = True
    save_recordings_locally: bool = False
    recordings_download_dir: str = ""

    @property
    def allowlist(self) -> set[str]:
        return _split_ids(self.course_allowlist)

    @property
    def denylist(self) -> set[str]:
        return _split_ids(self.course_denylist)

    def course_selected(self, course_id: str) -> bool:
        if self.denylist and course_id in self.denylist:
            return False
        if self.allowlist:
            return course_id in self.allowlist
        return True


def _split_ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


settings = Settings()
