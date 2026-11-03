from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from companion.brain import build_messages
from companion.config import Config
from companion.db import DB
from companion.protocol import due_steps, schedule_for
from companion.tts import Speaker, to_ogg_opus

TZ = "Europe/Berlin"


def keys(pairs):
    return [s.key for s, _ in pairs]


def test_training_day_schedule_order():
    # 2026-11-02 is a Monday
    plan = schedule_for(date(2026, 11, 2), TZ, "07:00", "23:00", "mon,wed,fri")
    assert keys(plan) == [
        "morning_light", "caffeine_focus", "training", "afternoon_reset",
        "evening_light", "wind_down", "reflection",
    ]
    times = {s.key: at.strftime("%H:%M") for s, at in plan}
    assert times["morning_light"] == "07:10"
    assert times["caffeine_focus"] == "08:30"
    assert times["wind_down"] == "21:00"
    assert times["reflection"] == "22:30"


def test_rest_day_uses_movement_instead_of_training():
    plan = schedule_for(date(2026, 11, 3), TZ, "07:00", "23:00", "mon,wed,fri")  # Tuesday
    assert "training" not in keys(plan)
    assert "movement" in keys(plan)


def test_bed_after_midnight_rolls_over():
    plan = dict((s.key, at) for s, at in schedule_for(date(2026, 11, 2), TZ, "09:00", "00:30", "mon"))
    assert plan["reflection"].date() == date(2026, 11, 3)
    assert plan["reflection"].strftime("%H:%M") == "00:00"


def test_due_steps_window_and_previous_day_key():
    # 07:15 Berlin (CET, UTC+1) on Tuesday -> morning_light due
    now = datetime(2026, 11, 3, 6, 15, tzinfo=timezone.utc)
    due = due_steps(now, TZ, "07:00", "23:00", "mon")
    assert [(d, s.key) for d, s in due] == [("2026-11-03", "morning_light")]
    # 3 hours later the window has passed
    later = datetime(2026, 11, 3, 9, 30, tzinfo=timezone.utc)
    assert "morning_light" not in [s.key for _, s in due_steps(later, TZ, "07:00", "23:00", "mon")]
    # Reflection at 00:00 after a 00:30 bedtime belongs to the previous day
    midnight = datetime(2026, 11, 3, 23, 5, tzinfo=timezone.utc)  # 00:05 Berlin on Nov 4
    due = due_steps(midnight, TZ, "09:00", "00:30", "mon")
    assert ("2026-11-03", "reflection") in [(d, s.key) for d, s in due]


def test_persistence_roundtrip(tmp_path: Path):
    db_path = tmp_path / "c.sqlite3"
    db = DB(db_path)
    db.upsert_user(1, 1, "Al", tz=TZ, wake="07:00", bed="23:00", training_days="mon")
    db.log_message(1, "out", "nudge", "How did you sleep?", event_key="morning_light",
                   meta={"local_date": "2026-11-03"})
    db.log_message(1, "in", "text", "7, woke up once")
    db.add_checkin(1, "2026-11-03", "morning_light", "answered", "7, woke up once")
    assert db.mark_sent(1, "2026-11-03", "morning_light") is True
    assert db.mark_sent(1, "2026-11-03", "morning_light") is False  # never double-send
    db.conn.close()

    db2 = DB(db_path)  # survives a restart
    export = db2.export_user(1)
    assert [m["text"] for m in export["messages"]] == ["How did you sleep?", "7, woke up once"]
    assert export["checkins"][0]["value"] == "7, woke up once"
    assert db2.has_answer(1, "2026-11-03", "morning_light")
    assert db2.mark_sent(1, "2026-11-03", "morning_light") is False


def test_build_messages_alternates_and_adds_context():
    db = DB(":memory:")
    user = db.upsert_user(1, 1, "Al", tz=TZ, wake="07:00", bed="23:00", training_days="mon")
    db.log_message(1, "out", "nudge", "Morning light time")
    db.log_message(1, "in", "text", "done")
    db.log_message(1, "in", "text", "also slept 8")
    db.log_message(1, "out", "text", "Great")
    db.log_message(1, "in", "command", "/today")
    db.log_message(1, "in", "text", "what now?")  # current message
    msgs = build_messages(db, user, "what now?")
    roles = [m["role"] for m in msgs]
    assert roles[0] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:]))
    assert msgs[0]["content"] == "done\n\nalso slept 8"
    assert "<context>" in msgs[-1]["content"] and msgs[-1]["content"].endswith("what now?")


@pytest.mark.asyncio
async def test_ffmpeg_conversion(tmp_path: Path):
    import subprocess

    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", str(wav)], check=True)
    ogg = tmp_path / "tone.ogg"
    await to_ogg_opus(wav, ogg)
    assert ogg.read_bytes()[:4] == b"OggS"


@pytest.mark.asyncio
async def test_speaker_falls_back_and_caches(tmp_path: Path, monkeypatch):
    cfg = Config(telegram_token="x", tts_provider="elevenlabs", data_dir=tmp_path)  # no key -> fails
    speaker = Speaker(cfg)
    calls = []

    async def fake_synth(provider, text, out_mp3):
        calls.append(provider)
        if provider == "elevenlabs":
            raise RuntimeError("no key")
        import subprocess
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.3", str(out_mp3)], check=True)

    monkeypatch.setattr(speaker, "_synth", fake_synth)
    first = await speaker.speak("hello")
    second = await speaker.speak("hello")
    assert first == second and first.exists()
    assert calls == ["elevenlabs", "edge", "elevenlabs"]  # edge result cached the 2nd time
