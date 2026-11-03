"""Clone a voice on Fish Audio, or list your voices, and print the id for FISH_VOICE_ID.

Only clone a voice you have the right to use: your own, or one whose owner agreed.

Usage:
  python -m companion.clone_voice --title "My coach" sample1.mp3 [sample2.wav ...]
  python -m companion.clone_voice --title "My coach" --text "exact words spoken" sample.mp3
  python -m companion.clone_voice --list

Tips: 15-60 seconds of clean speech, one speaker, no music, a quiet room.
A Telegram voice note you forward to yourself and download (.ogg) works too.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from companion.config import Config, load_dotenv


async def _run(args: argparse.Namespace, cfg: Config) -> None:
    from fishaudio import AsyncFishAudio

    client = AsyncFishAudio(api_key=cfg.fish_api_key)
    if args.list:
        page = await client.voices.list(self_only=True, page_size=50)
        if not page.items:
            print("No voices of your own yet.")
        for v in page.items:
            print(f"{v.id}  {v.title}  ({v.state})")
        return

    voices = [Path(p).read_bytes() for p in args.samples]
    voice = await client.voices.create(
        title=args.title,
        voices=voices,
        texts=[args.text] * len(voices) if args.text else None,
        visibility="private",
        enhance_audio_quality=True,
    )
    print(f"Created voice {voice.title!r} ({voice.state})")
    print(f"\nAdd this to .env and restart the bot:\nFISH_VOICE_ID={voice.id}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("samples", nargs="*", help="audio files of the voice to clone")
    parser.add_argument("--title", default="Daily companion")
    parser.add_argument("--text", help="transcript of the samples (optional, improves quality)")
    parser.add_argument("--list", action="store_true", help="list your own voices")
    args = parser.parse_args()

    cfg = Config.from_env()
    if not cfg.fish_api_key:
        raise SystemExit("FISH_API_KEY is not set (.env)")
    if not args.list and not args.samples:
        parser.error("give at least one audio file, or --list")
    asyncio.run(_run(args, cfg))


if __name__ == "__main__":
    main()
