# Daily companion (Telegram, voice)

A private Telegram bot that guides you through the day with short **voice notes**, built around Andrew Huberman's "three things" from the a16z Show:

1. Bright light soon after waking
2. Resistance training
3. The best sleep you can get

It also includes his advice for founders from the same episode: find the hours when your brain works best and protect them.

**Everything you say is saved**: text, voice notes (transcribed if speech-to-text is on), and button taps. The history is stored in SQLite and used as context for every reply. `/export` downloads all of it as JSON.

General education, not medical advice.

## Your day (defaults: wake 07:00, bed 23:00)

| Time | Step | It asks |
|---|---|---|
| wake +10 min | **Morning light**: 5–10 min outside if sunny, 10–20 if cloudy; water | Sleep last night, 1–10? |
| wake +90 min | **Caffeine OK + protected focus block** (~90 min, phone in another room) | What's the one thing? |
| wake +5 h | **Resistance training** (training days) or **easy movement** (rest days) | What did you train? |
| wake +7 h | **Afternoon reset**: 10–20 min NSDR / yoga nidra; caffeine cut-off | Energy, 1–10? |
| bed −4 h | **Evening light**, lighter dinner | Got outside? |
| bed −2 h | **Wind down**: dim, low lights; social phone in a box; cool, dark room | Phone away? |
| bed −30 min | **Reflection** | One win, one thing to adjust tomorrow? |

Each step comes as a voice note plus text, with ✅ Done / ⏭ Skip / ⏰ 30 min buttons. Any reply within 4 hours is saved as your answer to that step. Steps are only sent within 90 minutes of their time, so a restart never sends a flood of old messages, and each step is sent at most once a day.

## Voice: which TTS?

| Provider | Cost | Quality | Set |
|---|---|---|---|
| **Edge TTS** (default) | Free, no key | Good neural voices | `TTS_PROVIDER=edge` |
| **OpenAI** `gpt-4o-mini-tts` | Cheap, roughly cents per day at this volume | Very natural, accepts tone instructions | `TTS_PROVIDER=openai` + `OPENAI_API_KEY` |
| **ElevenLabs** Flash v2.5 | Most expensive (subscription credits) | Best, voice cloning | `TTS_PROVIDER=elevenlabs` + `ELEVENLABS_API_KEY` |

Start with Edge, which is free. Switch to ElevenLabs if you want a particular voice. The daily protocol messages are fixed text, so each one is synthesized once and served from `data/audio_cache/` after that. Only Claude's conversational replies cost TTS characters. If the paid provider fails, the bot falls back to Edge, then to text only.

The Edge option uses the unofficial `edge-tts` package. It works well but has no SLA, which is why there's an automatic fallback.

Prices change, so check current pricing before you pick a paid tier.

**Voice in (optional):** `STT_PROVIDER=groq` (Whisper on Groq, the cheapest) or `openai`. With `none`, voice notes are still saved (Telegram `file_id`), just not understood.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. `cp .env.example .env`, then fill in `TELEGRAM_BOT_TOKEN` and (for real conversation) `ANTHROPIC_API_KEY`.
3. Run it (pick one):

```bash
# Docker (recommended on a VPS)
docker compose up -d --build
docker compose logs -f

# Plain Python (needs ffmpeg installed)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m companion
```

4. Open your bot in Telegram and send `/start`. If `ALLOWED_USER_IDS` is empty, the first person to send `/start` becomes the owner and everyone else is refused.
5. Set your day: `/tz Europe/Berlin`, `/wake 06:45`, `/bed 22:45`, `/train mon,wed,fri`. Try `/preview morning_light` to hear a step now.

The bot uses long polling, so the VPS needs no open ports, domain or HTTPS. Any small VPS (1 vCPU / 512 MB) is enough.

### Without Docker on a VPS (systemd)

```bash
sudo useradd -r -m -d /opt/companion companion
sudo cp -r . /opt/companion && sudo chown -R companion /opt/companion
sudo -u companion python3 -m venv /opt/companion/.venv
sudo -u companion /opt/companion/.venv/bin/pip install -r /opt/companion/requirements.txt
sudo apt install -y ffmpeg
sudo cp deploy/companion.service /etc/systemd/system/
sudo systemctl enable --now companion
```

## Commands

`/today` plan · `/tz` `/wake` `/bed` `/train` settings · `/voice on|off` · `/pause` `/resume` · `/history` last replies · `/export` all data as JSON · `/preview <step>` · `/help`

## Data

`data/companion.sqlite3` contains these tables:
- `messages`: every message in both directions, with timestamp, kind (text / voice / button / nudge), the step it relates to, and voice transcripts
- `checkins`: structured answers per day and step (done / skipped / later / answered + value)
- `users`: your schedule settings
- `sent_events`, `reminders`: scheduler state, so restarts don't double-send

Back up `data/`, for example with a nightly `sqlite3 data/companion.sqlite3 ".backup backup.sqlite3"`.

## Brain

Replies come from Claude (`CLAUDE_MODEL`, default `claude-opus-5-5`, effort `low`). Each request includes your last 30 messages, today's plan and your last 7 days of check-ins. The system prompt keeps replies short and spoken-style, ready for TTS. Server-side refusal fallback is enabled. Without `ANTHROPIC_API_KEY` the bot still runs the full daily protocol and saves everything, but its replies are a short canned acknowledgement.

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```
