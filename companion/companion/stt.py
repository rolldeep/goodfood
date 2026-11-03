"""Speech-to-text for the user's voice replies (optional).

groq   whisper-large-v3-turbo on Groq: very cheap and fast
openai gpt-4o-mini-transcribe
none   voice notes are still saved (Telegram file_id), just not transcribed
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from companion.config import Config

log = logging.getLogger(__name__)

_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
    "openai": "https://api.openai.com/v1/audio/transcriptions",
}


def stt_enabled(cfg: Config) -> bool:
    return (cfg.stt_provider == "groq" and bool(cfg.groq_api_key)) or (
        cfg.stt_provider == "openai" and bool(cfg.openai_api_key)
    )


async def transcribe(cfg: Config, audio_path: Path) -> str | None:
    if not stt_enabled(cfg):
        return None
    if cfg.stt_provider == "groq":
        key, model = cfg.groq_api_key, cfg.groq_stt_model
    else:
        key, model = cfg.openai_api_key, cfg.openai_stt_model
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with audio_path.open("rb") as fh:
                r = await client.post(
                    _ENDPOINTS[cfg.stt_provider],
                    headers={"Authorization": f"Bearer {key}"},
                    data={"model": model},
                    files={"file": (audio_path.name, fh, "audio/ogg")},
                )
        r.raise_for_status()
        return (r.json().get("text") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        log.warning("transcription failed: %s", exc)
        return None
