"""Text-to-speech with pluggable providers, producing Telegram voice notes (OGG/Opus).

Providers:
  edge        free Microsoft neural voices via the `edge-tts` package (no key)
  elevenlabs  best quality, paid (Flash v2.5 model by default: lowest cost tier)
  openai      gpt-4o-mini-tts, cheap and natural

Audio is cached on disk by (provider, voice, text) hash, so the fixed daily
protocol messages are generated once and then cost nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
from pathlib import Path

import httpx

from companion.config import Config

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


async def _edge(text: str, voice: str, out_mp3: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice).save(str(out_mp3))


async def _elevenlabs(text: str, cfg: Config, out_mp3: Path) -> None:
    if not cfg.elevenlabs_api_key:
        raise TTSError("ELEVENLABS_API_KEY is not set")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.elevenlabs_voice_id}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": cfg.elevenlabs_api_key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": cfg.elevenlabs_model},
        )
    if r.status_code != 200:
        raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    out_mp3.write_bytes(r.content)


async def _openai(text: str, cfg: Config, out_mp3: Path) -> None:
    if not cfg.openai_api_key:
        raise TTSError("OPENAI_API_KEY is not set")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            json={
                "model": cfg.openai_tts_model,
                "voice": cfg.openai_tts_voice,
                "input": text,
                "instructions": "Warm, calm, encouraging coach. Unhurried pace.",
                "response_format": "mp3",
            },
        )
    if r.status_code != 200:
        raise TTSError(f"OpenAI TTS {r.status_code}: {r.text[:200]}")
    out_mp3.write_bytes(r.content)


async def to_ogg_opus(src: Path, dst: Path) -> None:
    """Convert any audio file to mono OGG/Opus, the format Telegram voice notes need."""
    if not shutil.which("ffmpeg"):
        raise TTSError("ffmpeg is not installed")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise TTSError(f"ffmpeg failed: {err.decode()[:200]}")


class Speaker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache_dir = cfg.audio_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _voice_id(self, provider: str) -> str:
        return {
            "edge": self.cfg.edge_voice,
            "elevenlabs": f"{self.cfg.elevenlabs_voice_id}:{self.cfg.elevenlabs_model}",
            "openai": f"{self.cfg.openai_tts_voice}:{self.cfg.openai_tts_model}",
        }.get(provider, provider)

    def _providers(self) -> list[str]:
        # Configured provider first, then the free one as a fallback.
        order = [self.cfg.tts_provider]
        if "edge" not in order:
            order.append("edge")
        return order

    async def _synth(self, provider: str, text: str, out_mp3: Path) -> None:
        if provider == "edge":
            await _edge(text, self.cfg.edge_voice, out_mp3)
        elif provider == "elevenlabs":
            await _elevenlabs(text, self.cfg, out_mp3)
        elif provider == "openai":
            await _openai(text, self.cfg, out_mp3)
        else:
            raise TTSError(f"unknown TTS provider {provider!r}")

    async def speak(self, text: str) -> Path | None:
        """Return a path to an .ogg voice note, or None if every provider failed."""
        for provider in self._providers():
            key = hashlib.sha256(f"{provider}|{self._voice_id(provider)}|{text}".encode()).hexdigest()[:32]
            ogg = self.cache_dir / f"{key}.ogg"
            if ogg.exists() and ogg.stat().st_size > 0:
                return ogg
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    mp3 = Path(tmp) / "speech.mp3"
                    await self._synth(provider, text, mp3)
                    await to_ogg_opus(mp3, ogg)
                return ogg
            except Exception as exc:  # noqa: BLE001 - try the next provider
                log.warning("TTS via %s failed: %s", provider, exc)
                ogg.unlink(missing_ok=True)
        return None
