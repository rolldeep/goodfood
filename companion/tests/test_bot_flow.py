from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import companion.bot as botmod
from companion.bot import Companion
from companion.config import Config
from companion.db import DB


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_chat_action(self, *a, **k):
        pass

    async def send_voice(self, chat_id, voice):
        self.sent.append(("voice", chat_id))

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("text", text, reply_markup))


class FakeMessage:
    def __init__(self, text):
        self.text, self.message_id, self.replies = text, 1, []

    async def reply_text(self, t):
        self.replies.append(t)


def make(tmp_path, monkeypatch):
    cfg = Config(telegram_token="x", data_dir=tmp_path, allowed_user_ids={42})
    comp = Companion(cfg, DB(":memory:"))
    async def no_voice(text):
        return None
    monkeypatch.setattr(comp.speaker, "speak", no_voice)
    comp.db.upsert_user(42, 42, "Al", tz="Europe/Berlin", wake="07:00", bed="23:00", training_days="mon")
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    return comp, ctx


@pytest.mark.asyncio
async def test_tick_reply_and_button(tmp_path, monkeypatch):
    comp, ctx = make(tmp_path, monkeypatch)
    now = datetime(2026, 11, 3, 6, 15, tzinfo=timezone.utc)  # 07:15 Berlin
    monkeypatch.setattr(botmod, "utcnow", lambda: now)
    await comp.tick(ctx)
    await comp.tick(ctx)  # second tick must not resend
    nudges = [s for s in ctx.bot.sent if s[0] == "text"]
    assert len(nudges) == 1 and nudges[0][1].startswith("Morning light")
    assert nudges[0][2] is not None  # has buttons

    upd = SimpleNamespace(effective_user=SimpleNamespace(id=42), message=FakeMessage("slept 6, woke at 3"),
                          effective_message=None)
    upd.effective_message = upd.message
    await comp.on_text(upd, ctx)
    ci = comp.db.checkins_for(42, ["2026-11-03"])
    assert [(c["event_key"], c["status"], c["value"]) for c in ci] == [
        ("morning_light", "answered", "slept 6, woke at 3")]
    # Reply without API key -> canned ack, saved as outgoing message
    out = [m for m in comp.db.recent_messages(42) if m["direction"] == "out"]
    assert out[-1]["text"] == "Got it, saved. I'll keep that in mind for your plan."

    # Button tap: "later" schedules a reminder that fires 30 min later
    answered = []
    async def answer(t=None): answered.append(t)
    async def edit(reply_markup=None): pass
    q = SimpleNamespace(data="ci|2026-11-03|morning_light|later", answer=answer, edit_message_reply_markup=edit)
    upd2 = SimpleNamespace(effective_user=SimpleNamespace(id=42), callback_query=q, effective_message=None)
    await comp.on_button(upd2, ctx)
    monkeypatch.setattr(botmod, "utcnow", lambda: datetime(2026, 11, 3, 6, 50, tzinfo=timezone.utc))
    before = len(ctx.bot.sent)
    await comp.tick(ctx)
    assert any(s[0] == "text" and s[1].startswith("Morning light") for s in ctx.bot.sent[before:])


@pytest.mark.asyncio
async def test_stranger_is_refused(tmp_path, monkeypatch):
    comp, ctx = make(tmp_path, monkeypatch)
    msg = FakeMessage("hi")
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=7), message=msg, effective_message=msg)
    await comp.on_text(upd, ctx)
    assert msg.replies == ["This is a private bot."]
    assert comp.db.recent_messages(7) == []
