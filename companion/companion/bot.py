"""Telegram bot: scheduled voice nudges, check-in buttons, and conversation."""
from __future__ import annotations

import io
import json
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from companion.brain import Brain
from companion.config import Config, load_dotenv
from companion.db import DB, User, utcnow
from companion.protocol import (
    STEPS_BY_KEY,
    Step,
    day_plan_text,
    due_steps,
    parse_hhmm,
    parse_training_days,
)
from companion.stt import stt_enabled, transcribe
from companion.tts import Speaker

log = logging.getLogger("companion")

# A reply within this long after a protocol prompt is saved as the answer to it.
ANSWER_WINDOW = timedelta(hours=4)
SNOOZE = timedelta(minutes=30)

HELP = """Commands:
/today – today's plan
/tz Europe/Berlin – your timezone
/wake 07:00 – usual wake time
/bed 23:00 – usual bed time
/train mon,wed,fri – resistance training days
/voice on|off – voice notes on or off
/pause, /resume – stop or restart daily messages
/history – your last replies
/export – download everything you've told me (JSON)
/preview morning_light – hear a step now

Just talk to me any time, by text or voice. Everything you say is saved."""


class Companion:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.speaker = Speaker(cfg)
        self.brain = Brain(cfg)

    # ---- access control ----------------------------------------------------
    def allowed(self, user_id: int, claiming: bool = False) -> bool:
        if self.cfg.allowed_user_ids:
            return user_id in self.cfg.allowed_user_ids
        owner = self.db.kv_get("owner_user_id")
        if owner is None and claiming:
            self.db.kv_set("owner_user_id", str(user_id))
            log.info("user %s claimed the bot as owner", user_id)
            return True
        return owner is not None and int(owner) == user_id

    async def _guard(self, update: Update, claiming: bool = False) -> User | None:
        tg_user = update.effective_user
        if tg_user is None or not self.allowed(tg_user.id, claiming=claiming):
            if update.effective_message:
                await update.effective_message.reply_text("This is a private bot.")
            return None
        return self.db.get_user(tg_user.id)

    # ---- sending -----------------------------------------------------------
    async def send(
        self, context: ContextTypes.DEFAULT_TYPE, user: User, text: str, *,
        speak: str | None = None, event_key: str | None = None, local_date: str | None = None,
        markup: InlineKeyboardMarkup | None = None, kind: str = "text",
    ) -> None:
        """Send a voice note (if enabled) followed by the text, and log it."""
        bot = context.bot
        if user.voice_enabled:
            await bot.send_chat_action(user.chat_id, ChatAction.RECORD_VOICE)
            ogg = await self.speaker.speak(speak or text)
            if ogg:
                with ogg.open("rb") as fh:
                    await bot.send_voice(user.chat_id, voice=fh)
        await bot.send_message(user.chat_id, text, reply_markup=markup)
        meta = {"local_date": local_date} if local_date else None
        self.db.log_message(user.user_id, "out", kind, text, event_key=event_key, meta=meta)

    async def send_step(
        self, context: ContextTypes.DEFAULT_TYPE, user: User, step: Step, local_date: str
    ) -> None:
        markup = None
        if step.buttons:
            def btn(label: str, status: str) -> InlineKeyboardButton:
                return InlineKeyboardButton(label, callback_data=f"ci|{local_date}|{step.key}|{status}")

            markup = InlineKeyboardMarkup([[btn("✅ Done", "done"), btn("⏭ Skip", "skipped"),
                                            btn("⏰ 30 min", "later")]])
        text = f"{step.title}\n\n{step.text}\n\n👉 {step.ask}"
        await self.send(context, user, text, speak=step.text + " " + step.ask,
                        event_key=step.key, local_date=local_date, markup=markup, kind="nudge")

    # ---- scheduler ---------------------------------------------------------
    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = utcnow()
        for user in self.db.active_users():
            try:
                for local_date, step in due_steps(now, user.tz, user.wake, user.bed, user.training_days):
                    if self.db.mark_sent(user.user_id, local_date, step.key):
                        await self.send_step(context, user, step, local_date)
            except Exception:  # noqa: BLE001 - one user's failure must not stop the loop
                log.exception("tick failed for user %s", user.user_id)
        for rem in self.db.pop_due_reminders(now):
            user = self.db.get_user(rem["user_id"])
            step = STEPS_BY_KEY.get(rem["event_key"])
            if user and user.active and step:
                local_date = now.astimezone(ZoneInfo(user.tz)).date().isoformat()
                await self.send_step(context, user, step, local_date)

    # ---- commands ----------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg = update.effective_user
        if tg is None or not self.allowed(tg.id, claiming=True):
            await update.effective_message.reply_text(
                f"This is a private bot. (Your Telegram ID is {tg.id if tg else '?'}; "
                "the owner can add it to ALLOWED_USER_IDS.)"
            )
            return
        user = self.db.upsert_user(
            tg.id, update.effective_chat.id, tg.first_name,
            tz=self.cfg.default_tz, wake=self.cfg.default_wake, bed=self.cfg.default_bed,
            training_days=self.cfg.default_training_days,
        )
        self.db.log_message(user.user_id, "in", "command", "/start")
        name = f", {user.name}" if user.name else ""
        spoken = (
            f"Hi{name}. I'm your daily companion. Each day I'll nudge you through three things that matter most: "
            "bright light soon after waking, resistance training, and the best sleep you can get. "
            "I'll also help you protect your best focus hours. Reply any time by text or voice, and I'll remember it."
        )
        plan = day_plan_text(datetime.now(ZoneInfo(user.tz)).date(), user.tz, user.wake, user.bed, user.training_days)
        await self.send(context, user, f"{spoken}\n\n{plan}\n\nAdjust with /wake, /bed, /tz, /train.\n\n{HELP}",
                        speak=spoken)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.message.reply_text(HELP)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        today = datetime.now(ZoneInfo(user.tz)).date()
        await update.message.reply_text(day_plan_text(today, user.tz, user.wake, user.bed, user.training_days))

    async def _set(self, update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> None:
        user = await self._guard(update)
        if not user:
            return
        arg = " ".join(context.args).strip()
        self.db.log_message(user.user_id, "in", "command", f"/{field} {arg}".strip())
        try:
            if field == "tz":
                ZoneInfo(arg)
                value = arg
            elif field in ("wake", "bed"):
                value = parse_hhmm(arg).strftime("%H:%M")
            elif field == "training_days":
                value = ",".join(sorted(parse_training_days(arg), key="mon tue wed thu fri sat sun".split().index))
            else:
                raise ValueError(field)
        except (ValueError, ZoneInfoNotFoundError, IndexError):
            await update.message.reply_text(f"Couldn't read {arg!r}. See /help for examples.")
            return
        self.db.update_user(user.user_id, **{field: value})
        user = self.db.get_user(user.user_id)
        today = datetime.now(ZoneInfo(user.tz)).date()
        await update.message.reply_text(
            f"Saved.\n\n{day_plan_text(today, user.tz, user.wake, user.bed, user.training_days)}"
        )

    async def cmd_tz(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set(u, c, "tz")

    async def cmd_wake(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set(u, c, "wake")

    async def cmd_bed(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set(u, c, "bed")

    async def cmd_train(self, u: Update, c: ContextTypes.DEFAULT_TYPE) -> None:
        await self._set(u, c, "training_days")

    async def cmd_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        on = (context.args[0].lower() if context.args else "on") != "off"
        self.db.update_user(user.user_id, voice_enabled=int(on))
        await update.message.reply_text(f"Voice notes {'on' if on else 'off'}.")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if user:
            self.db.update_user(user.user_id, active=0)
            await update.message.reply_text("Paused. Your history is kept. /resume to start again.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if user:
            self.db.update_user(user.user_id, active=1)
            await update.message.reply_text("Back on. See you at the next step.")

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        rows = [r for r in self.db.recent_messages(user.user_id, limit=200)
                if r["direction"] == "in" and r["kind"] != "command" and r["text"]][-15:]
        if not rows:
            await update.message.reply_text("Nothing saved yet. Talk to me!")
            return
        tz = ZoneInfo(user.tz)
        lines = []
        for r in rows:
            ts = datetime.fromisoformat(r["ts_utc"]).astimezone(tz).strftime("%d %b %H:%M")
            tag = f" [{r['event_key']}]" if r["event_key"] else ""
            lines.append(f"{ts}{tag}: {r['text']}")
        await update.message.reply_text("\n".join(lines)[-4000:])

    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        data = json.dumps(self.db.export_user(user.user_id), indent=2, ensure_ascii=False)
        await update.message.reply_document(
            document=io.BytesIO(data.encode()), filename=f"companion-export-{datetime.now():%Y%m%d}.json"
        )

    async def cmd_preview(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        key = context.args[0] if context.args else "morning_light"
        step = STEPS_BY_KEY.get(key)
        if not step:
            await update.message.reply_text("Steps: " + ", ".join(STEPS_BY_KEY))
            return
        local_date = datetime.now(ZoneInfo(user.tz)).date().isoformat()
        await self.send_step(context, user, step, local_date)

    # ---- buttons -------------------------------------------------------------
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = await self._guard(update)
        if not user or not query.data or not query.data.startswith("ci|"):
            await query.answer()
            return
        _, local_date, key, status = query.data.split("|", 3)
        step = STEPS_BY_KEY.get(key)
        self.db.add_checkin(user.user_id, local_date, key, status)
        label = {"done": "Done", "skipped": "Skipped", "later": "Remind me in 30 min"}.get(status, status)
        self.db.log_message(user.user_id, "in", "button",
                            f"{step.title if step else key}: {label}", event_key=key)
        if status == "later":
            self.db.add_reminder(user.user_id, utcnow() + SNOOZE, key)
        await query.answer(label)
        await query.edit_message_reply_markup(reply_markup=None)
        reply = {"done": "Nice. Logged.", "skipped": "Logged as skipped. No stress, next one.",
                 "later": "OK, I'll ping you again in 30 minutes."}.get(status, "Logged.")
        await context.bot.send_message(user.chat_id, reply)
        self.db.log_message(user.user_id, "out", "text", reply, event_key=key)

    # ---- free text and voice --------------------------------------------------
    def _attach_to_nudge(self, user: User, text: str) -> str | None:
        """If this reply answers a recent protocol prompt, save it as that check-in."""
        nudge = self.db.last_nudge(user.user_id)
        if not nudge:
            return None
        sent = datetime.fromisoformat(nudge["ts_utc"])
        meta = json.loads(nudge["meta"]) if nudge["meta"] else {}
        local_date = meta.get("local_date")
        if utcnow() - sent > ANSWER_WINDOW or not local_date:
            return None
        if self.db.has_answer(user.user_id, local_date, nudge["event_key"]):
            return None
        self.db.add_checkin(user.user_id, local_date, nudge["event_key"], "answered", text)
        return nudge["event_key"]

    async def _converse(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user: User,
                        text: str, msg_id: int) -> None:
        event_key = self._attach_to_nudge(user, text)
        if event_key:
            self.db.conn.execute("UPDATE messages SET event_key=? WHERE id=?", (event_key, msg_id))
            self.db.conn.commit()
        await context.bot.send_chat_action(user.chat_id, ChatAction.TYPING)
        reply = await self.brain.reply(self.db, user, text)
        await self.send(context, user, reply, event_key=event_key)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        text = update.message.text
        msg_id = self.db.log_message(user.user_id, "in", "text", text,
                                     meta={"telegram_message_id": update.message.message_id})
        await self._converse(update, context, user, text, msg_id)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._guard(update)
        if not user:
            return
        voice = update.message.voice or update.message.audio
        msg_id = self.db.log_message(
            user.user_id, "in", "voice", None, file_id=voice.file_id,
            meta={"duration": voice.duration, "telegram_message_id": update.message.message_id},
        )
        if not stt_enabled(self.cfg):
            reply = "Voice note saved. To let me understand voice, set STT_PROVIDER on the server. For now, text works."
            await update.message.reply_text(reply)
            self.db.log_message(user.user_id, "out", "text", reply)
            return
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.ogg"
            tg_file = await voice.get_file()
            await tg_file.download_to_drive(path)
            transcript = await transcribe(self.cfg, path)
        if not transcript:
            await update.message.reply_text("Saved your voice note, but I couldn't transcribe it. Try text?")
            return
        self.db.set_message_text(msg_id, transcript)
        await self._converse(update, context, user, transcript, msg_id)


def build_app(cfg: Config) -> Application:
    db = DB(cfg.db_path)
    companion = Companion(cfg, db)
    app = Application.builder().token(cfg.telegram_token).build()

    commands = {
        "start": companion.cmd_start, "help": companion.cmd_help, "today": companion.cmd_today,
        "tz": companion.cmd_tz, "wake": companion.cmd_wake, "bed": companion.cmd_bed,
        "train": companion.cmd_train, "voice": companion.cmd_voice, "pause": companion.cmd_pause,
        "resume": companion.cmd_resume, "history": companion.cmd_history,
        "export": companion.cmd_export, "preview": companion.cmd_preview,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(companion.on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, companion.on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, companion.on_text))
    app.job_queue.run_repeating(companion.tick, interval=60, first=5)
    app.bot_data["companion"] = companion
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    log.info("starting: model=%s llm=%s tts=%s stt=%s",
             cfg.claude_model, cfg.llm_enabled, cfg.tts_provider, cfg.stt_provider)
    build_app(cfg).run_polling(allowed_updates=Update.ALL_TYPES)
