"""Conversational replies from Claude, grounded in the user's saved history."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from companion.config import Config
from companion.db import DB, User
from companion.protocol import STEPS_BY_KEY, day_plan_text

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a daily health companion that talks to one person through Telegram, mostly by voice.

Your approach follows Andrew Huberman's core advice: if you can only do three things, get bright light shortly after waking, do resistance training, and make the sleep you can get as high-quality as possible. For work, help them find the hours when their brain does its best work and protect those hours. Supporting habits: delay caffeine 60 to 90 minutes after waking and stop it in the afternoon, short non-sleep deep rest for afternoon dips, evening light, dim lights and phone away before bed, a cool dark bedroom. Don't optimise so hard that it gets in the way of the rest of their life: progress over perfection.

How to reply:
- Your reply is turned into a voice note, so write plain spoken sentences. No markdown, lists, emoji, or headings.
- Keep it short: usually 2 to 4 sentences, under 80 words. Longer only if they ask for an explanation.
- Be warm and direct, like a good coach. Acknowledge what they said, then give one concrete next action.
- Use their history: refer back to what they told you earlier (sleep scores, wins, struggles) when it helps.
- If they report a problem, adjust the plan instead of repeating it.
- You are not a doctor. For symptoms, medication, or anything that sounds medical, suggest they talk to a professional."""


def _local_now(user: User) -> datetime:
    return datetime.now(ZoneInfo(user.tz))


def build_context(db: DB, user: User) -> str:
    now = _local_now(user)
    today = now.date()
    dates = [(today.fromordinal(today.toordinal() - i)).isoformat() for i in range(6, -1, -1)]
    checkins = db.checkins_for(user.user_id, dates)
    lines = [
        f"Now: {now.strftime('%A %Y-%m-%d %H:%M')} ({user.tz}).",
        day_plan_text(today, user.tz, user.wake, user.bed, user.training_days),
        "",
        "Check-ins from the last 7 days:" if checkins else "No check-ins yet.",
    ]
    for c in checkins:
        title = STEPS_BY_KEY[c["event_key"]].title if c["event_key"] in STEPS_BY_KEY else c["event_key"]
        val = f": {c['value']}" if c["value"] else ""
        lines.append(f"- {c['local_date']} {title} -> {c['status']}{val}")
    return "\n".join(lines)


def build_messages(db: DB, user: User, user_text: str, limit: int = 30) -> list[dict]:
    """Turn the stored transcript into alternating user/assistant turns."""
    turns: list[dict] = []
    for row in db.recent_messages(user.user_id, limit=limit):
        if not row["text"] or row["kind"] == "command":
            continue
        role = "user" if row["direction"] == "in" else "assistant"
        text = row["text"]
        if role == "user" and row["kind"] == "button":
            text = f"[tapped button] {text}"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})
    # The current message is already logged; drop it so we can append it with context.
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    while turns and turns[0]["role"] != "user":
        turns.pop(0)

    context = build_context(db, user)
    turns.append({
        "role": "user",
        "content": f"<context>\n{context}\n</context>\n\n{user_text}",
    })
    return turns


FALLBACK_REPLY = "Got it, saved. I'll keep that in mind for your plan."


class Brain:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_api_key) if cfg.llm_enabled else None

    async def reply(self, db: DB, user: User, user_text: str) -> str:
        if self.client is None:
            return FALLBACK_REPLY
        messages = build_messages(db, user, user_text)
        try:
            response = await self.client.beta.messages.create(
                model=self.cfg.claude_model,
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                output_config={"effort": self.cfg.claude_effort},
                # If a safety classifier declines, the API retries on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError:
            log.warning("Claude rate limited")
            return "I'm a bit overloaded right now. Your message is saved, I'll catch up shortly."
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            return FALLBACK_REPLY
        except anthropic.APIConnectionError as exc:
            log.error("Claude connection error: %s", exc)
            return FALLBACK_REPLY

        if response.stop_reason == "refusal":
            return "I can't help with that one. For anything medical, please check with a professional."
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or FALLBACK_REPLY
