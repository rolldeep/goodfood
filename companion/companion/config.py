"""Settings read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader so the bot runs without extra dependencies.

    Existing environment variables win over the file.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    telegram_token: str
    allowed_user_ids: set[int] = field(default_factory=set)

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "low"

    tts_provider: str = "edge"
    edge_voice: str = "en-US-AndrewNeural"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_model: str = "eleven_flash_v2_5"
    openai_api_key: str = ""
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"

    stt_provider: str = "none"
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    openai_stt_model: str = "gpt-4o-mini-transcribe"

    default_tz: str = "Europe/Berlin"
    default_wake: str = "07:00"
    default_bed: str = "23:00"
    default_training_days: str = "mon,wed,fri"

    data_dir: Path = Path("./data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "companion.sqlite3"

    @property
    def audio_cache_dir(self) -> Path:
        return self.data_dir / "audio_cache"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @classmethod
    def from_env(cls) -> "Config":
        ids = {int(x) for x in _env("ALLOWED_USER_IDS").replace(" ", "").split(",") if x}
        return cls(
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            allowed_user_ids=ids,
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model=_env("CLAUDE_MODEL", "claude-opus-5-5"),
            claude_effort=_env("CLAUDE_EFFORT", "low"),
            tts_provider=_env("TTS_PROVIDER", "edge").lower(),
            edge_voice=_env("EDGE_VOICE", "en-US-AndrewNeural"),
            elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),
            elevenlabs_model=_env("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_tts_model=_env("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            openai_tts_voice=_env("OPENAI_TTS_VOICE", "alloy"),
            stt_provider=_env("STT_PROVIDER", "none").lower(),
            groq_api_key=_env("GROQ_API_KEY"),
            groq_stt_model=_env("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
            openai_stt_model=_env("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe"),
            default_tz=_env("DEFAULT_TZ", "Europe/Berlin"),
            default_wake=_env("DEFAULT_WAKE", "07:00"),
            default_bed=_env("DEFAULT_BED", "23:00"),
            default_training_days=_env("DEFAULT_TRAINING_DAYS", "mon,wed,fri"),
            data_dir=Path(_env("DATA_DIR", "./data")),
        )
