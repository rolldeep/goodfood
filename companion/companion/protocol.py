"""The daily protocol, anchored to the user's wake and bed times.

Built around Andrew Huberman's "three things" (a16z Show): bright light soon
after waking, resistance training, and the best sleep you can get, plus the
founder advice from the same episode: find the hours your brain works best
and protect them. General education, not medical advice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# How long after its due time a step may still be sent (e.g. bot was down).
SEND_WINDOW = timedelta(minutes=90)


@dataclass(frozen=True)
class Step:
    key: str
    anchor: str            # "wake" or "bed"
    offset_min: int        # minutes relative to the anchor
    title: str
    text: str              # what the bot says (also spoken)
    ask: str               # check-in question; the reply is saved
    buttons: bool = True   # show Done / Skip / Later
    training_only: bool | None = None  # True: training days only, False: rest days only


STEPS: list[Step] = [
    Step(
        key="morning_light",
        anchor="wake", offset_min=10,
        title="Morning light",
        text=(
            "Good morning. First thing: get outside and get bright light in your eyes. "
            "About five to ten minutes if it's sunny, ten to twenty if it's cloudy, "
            "longer if it's grey and overcast. No sunglasses, and don't stare at the sun. "
            "Drink a big glass of water on the way out. "
            "This sets your body clock for the whole day and makes tonight's sleep easier."
        ),
        ask="How did you sleep last night, 1 to 10? Reply with a number or a few words.",
    ),
    Step(
        key="caffeine_focus",
        anchor="wake", offset_min=90,
        title="Caffeine and your protected focus block",
        text=(
            "You're about ninety minutes in. If you drink coffee, now is a good time. "
            "Then start your protected block: these early hours are when your brain does its best work. "
            "Pick the one hardest thing, put your phone in another room, and go for about ninety minutes. "
            "No messages, no social media until it's done."
        ),
        ask="What is the one thing you're protecting this block for?",
    ),
    Step(
        key="training",
        anchor="wake", offset_min=5 * 60,
        title="Resistance training",
        text=(
            "Training day. Resistance training is one of the three things that matter most. "
            "Forty-five to sixty minutes, compound lifts, leave one or two reps in reserve. "
            "Warm up first, and eat a proper protein meal afterwards."
        ),
        ask="What did you train? Tap Done when finished.",
        training_only=True,
    ),
    Step(
        key="movement",
        anchor="wake", offset_min=5 * 60,
        title="Easy movement",
        text=(
            "Rest day from lifting. Still move: a brisk walk or easy cardio where you can "
            "hold a conversation, thirty to forty-five minutes. Outside if you can, for more daylight."
        ),
        ask="Did you get your walk or easy cardio in?",
        training_only=False,
    ),
    Step(
        key="afternoon_reset",
        anchor="wake", offset_min=7 * 60,
        title="Afternoon reset",
        text=(
            "Afternoon dip time. If you're foggy, try ten to twenty minutes of non-sleep deep rest: "
            "lie down, eyes closed, slow breathing, or a guided yoga nidra. "
            "And this is your caffeine cut-off, so caffeine doesn't eat into tonight's sleep."
        ),
        ask="Energy level right now, 1 to 10?",
    ),
    Step(
        key="evening_light",
        anchor="bed", offset_min=-4 * 60,
        title="Evening light",
        text=(
            "If the sun is setting soon, step outside and look toward the sky for a few minutes. "
            "Evening light helps your clock know it's the end of the day. "
            "Keep dinner on the lighter side and not too late."
        ),
        ask="Did you get outside this evening?",
    ),
    Step(
        key="wind_down",
        anchor="bed", offset_min=-2 * 60,
        title="Wind down",
        text=(
            "Two hours to bed. Dim the lights and use lamps low down instead of bright overhead lights. "
            "Put the social media phone away, in a drawer or a box. "
            "Make the bedroom cool and dark. "
            "If you can't sleep perfectly tonight, that's fine: just make the sleep you can get as good as possible."
        ),
        ask="Phone away? Tap Done when the lights are down.",
    ),
    Step(
        key="reflection",
        anchor="bed", offset_min=-30,
        title="Evening reflection",
        text=(
            "Quick look back before sleep. Light this morning, training or movement, "
            "and your protected focus block: how did they go? "
            "Tell me in a sentence or a voice note. I'll remember it and use it tomorrow."
        ),
        ask="One win from today, and one thing to adjust tomorrow?",
        buttons=False,
    ),
]

STEPS_BY_KEY = {s.key: s for s in STEPS}


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    t = time(int(h), int(m))
    return t


def parse_training_days(value: str) -> set[str]:
    days = {d.strip().lower()[:3] for d in value.split(",") if d.strip()}
    unknown = days - set(WEEKDAYS)
    if unknown:
        raise ValueError(f"unknown day(s): {', '.join(sorted(unknown))}")
    return days


def is_training_day(d: date, training_days: str) -> bool:
    return WEEKDAYS[d.weekday()] in parse_training_days(training_days)


def schedule_for(
    local_day: date, tz: str, wake: str, bed: str, training_days: str
) -> list[tuple[Step, datetime]]:
    """Return (step, aware local datetime) pairs for one day, sorted by time.

    A bed time earlier than the wake time (e.g. 00:30) is treated as after midnight.
    """
    zone = ZoneInfo(tz)
    wake_dt = datetime.combine(local_day, parse_hhmm(wake), zone)
    bed_dt = datetime.combine(local_day, parse_hhmm(bed), zone)
    if bed_dt <= wake_dt:
        bed_dt += timedelta(days=1)
    training = is_training_day(local_day, training_days)

    out: list[tuple[Step, datetime]] = []
    for step in STEPS:
        if step.training_only is True and not training:
            continue
        if step.training_only is False and training:
            continue
        base = wake_dt if step.anchor == "wake" else bed_dt
        out.append((step, base + timedelta(minutes=step.offset_min)))
    out.sort(key=lambda pair: pair[1])
    return out


def due_steps(
    now: datetime, tz: str, wake: str, bed: str, training_days: str
) -> list[tuple[str, Step]]:
    """Steps that are due at `now` (within SEND_WINDOW), as (local_date_key, step).

    Looks at today and yesterday so a late bed time after midnight still fires
    under the previous day's key.
    """
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    due: list[tuple[str, Step]] = []
    for day in (local_now.date() - timedelta(days=1), local_now.date()):
        for step, at in schedule_for(day, tz, wake, bed, training_days):
            if at <= local_now < at + SEND_WINDOW:
                due.append((day.isoformat(), step))
    return due


def day_plan_text(local_day: date, tz: str, wake: str, bed: str, training_days: str) -> str:
    lines = [f"Plan for {local_day.strftime('%A %d %b')} (wake {wake}, bed {bed}):"]
    for step, at in schedule_for(local_day, tz, wake, bed, training_days):
        lines.append(f"• {at.strftime('%H:%M')}  {step.title}")
    return "\n".join(lines)
