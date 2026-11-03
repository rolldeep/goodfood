"""SQLite persistence: profiles, every message in and out, check-ins, schedule state.

Everything the user says (text, voice transcript, button taps) is stored in
`messages`; structured answers to protocol prompts also land in `checkins`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL,
    name           TEXT,
    tz             TEXT NOT NULL,
    wake           TEXT NOT NULL,          -- HH:MM local
    bed            TEXT NOT NULL,          -- HH:MM local
    training_days  TEXT NOT NULL,          -- e.g. "mon,wed,fri"
    voice_enabled  INTEGER NOT NULL DEFAULT 1,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    ts_utc      TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    kind        TEXT NOT NULL,             -- text | voice | button | command | nudge
    event_key   TEXT,                      -- protocol step this relates to, if any
    text        TEXT,                      -- text, or transcript of a voice note
    file_id     TEXT,                      -- Telegram file_id for voice notes
    meta        TEXT                       -- JSON
);
CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages (user_id, ts_utc);

CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    local_date  TEXT NOT NULL,             -- YYYY-MM-DD in the user's timezone
    event_key   TEXT NOT NULL,
    status      TEXT NOT NULL,             -- done | skipped | later | answered
    value       TEXT,                      -- free text / number (e.g. sleep score)
    ts_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkins_user_date ON checkins (user_id, local_date);

CREATE TABLE IF NOT EXISTS sent_events (
    user_id     INTEGER NOT NULL,
    local_date  TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    ts_utc      TEXT NOT NULL,
    PRIMARY KEY (user_id, local_date, event_key)
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    due_utc     TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass
class User:
    user_id: int
    chat_id: int
    name: str | None
    tz: str
    wake: str
    bed: str
    training_days: str
    voice_enabled: bool
    active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            name=row["name"],
            tz=row["tz"],
            wake=row["wake"],
            bed=row["bed"],
            training_days=row["training_days"],
            voice_enabled=bool(row["voice_enabled"]),
            active=bool(row["active"]),
        )


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- key/value -------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---- users -----------------------------------------------------------
    def upsert_user(
        self, user_id: int, chat_id: int, name: str | None, *, tz: str, wake: str,
        bed: str, training_days: str,
    ) -> User:
        self.conn.execute(
            """INSERT INTO users (user_id, chat_id, name, tz, wake, bed, training_days, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
                                                  name=excluded.name, active=1""",
            (user_id, chat_id, name, tz, wake, bed, training_days, iso(utcnow())),
        )
        self.conn.commit()
        user = self.get_user(user_id)
        assert user is not None
        return user

    def get_user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def active_users(self) -> list[User]:
        rows = self.conn.execute("SELECT * FROM users WHERE active=1").fetchall()
        return [User.from_row(r) for r in rows]

    _UPDATABLE = {"tz", "wake", "bed", "training_days", "voice_enabled", "active", "name"}

    def update_user(self, user_id: int, **fields: Any) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*fields.values(), user_id))
        self.conn.commit()

    # ---- messages --------------------------------------------------------
    def log_message(
        self, user_id: int, direction: str, kind: str, text: str | None = None, *,
        event_key: str | None = None, file_id: str | None = None,
        meta: dict | None = None, ts: datetime | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO messages (user_id, ts_utc, direction, kind, event_key, text, file_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, iso(ts or utcnow()), direction, kind, event_key, text, file_id,
             json.dumps(meta) if meta else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_message_text(self, message_id: int, text: str) -> None:
        self.conn.execute("UPDATE messages SET text=? WHERE id=?", (text, message_id))
        self.conn.commit()

    def recent_messages(self, user_id: int, limit: int = 30) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return list(reversed(rows))

    def last_nudge(self, user_id: int) -> sqlite3.Row | None:
        """Most recent protocol prompt sent to the user (to attach replies to it)."""
        return self.conn.execute(
            "SELECT * FROM messages WHERE user_id=? AND direction='out' AND kind='nudge' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def has_answer(self, user_id: int, local_date: str, event_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM checkins WHERE user_id=? AND local_date=? AND event_key=? "
            "AND status='answered' LIMIT 1",
            (user_id, local_date, event_key),
        ).fetchone()
        return row is not None

    # ---- check-ins -------------------------------------------------------
    def add_checkin(
        self, user_id: int, local_date: str, event_key: str, status: str, value: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO checkins (user_id, local_date, event_key, status, value, ts_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, local_date, event_key, status, value, iso(utcnow())),
        )
        self.conn.commit()

    def checkins_for(self, user_id: int, local_dates: Iterable[str]) -> list[sqlite3.Row]:
        dates = list(local_dates)
        if not dates:
            return []
        q = ",".join("?" * len(dates))
        return self.conn.execute(
            f"SELECT * FROM checkins WHERE user_id=? AND local_date IN ({q}) ORDER BY id",
            (user_id, *dates),
        ).fetchall()

    # ---- schedule state --------------------------------------------------
    def mark_sent(self, user_id: int, local_date: str, event_key: str) -> bool:
        """Returns True if this is the first time (i.e. we should send)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO sent_events (user_id, local_date, event_key, ts_utc) "
            "VALUES (?, ?, ?, ?)",
            (user_id, local_date, event_key, iso(utcnow())),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def add_reminder(self, user_id: int, due: datetime, event_key: str) -> None:
        self.conn.execute(
            "INSERT INTO reminders (user_id, due_utc, event_key) VALUES (?, ?, ?)",
            (user_id, iso(due), event_key),
        )
        self.conn.commit()

    def pop_due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE done=0 AND due_utc<=?", (iso(now),)
        ).fetchall()
        if rows:
            self.conn.executemany(
                "UPDATE reminders SET done=1 WHERE id=?", [(r["id"],) for r in rows]
            )
            self.conn.commit()
        return rows

    # ---- export ----------------------------------------------------------
    def export_user(self, user_id: int) -> dict:
        def rows(sql: str) -> list[dict]:
            return [dict(r) for r in self.conn.execute(sql, (user_id,)).fetchall()]

        user = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return {
            "user": dict(user) if user else None,
            "messages": rows("SELECT * FROM messages WHERE user_id=? ORDER BY id"),
            "checkins": rows("SELECT * FROM checkins WHERE user_id=? ORDER BY id"),
        }
