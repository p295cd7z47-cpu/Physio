"""Telegram front end: chat with the assistant, approve outgoing messages, daily jobs."""

import asyncio
import base64
import logging
import mimetypes
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .actions import ActionQueue
from .agent import Agent
from .config import Settings, load_settings
from .db import Database
from .google_services import GoogleServices, load_credentials
from .sms import SmsSender
from .tools import Toolbox, appointment_reminder_text

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

HELP = """Hi! I'm your {business} assistant 🐴

Just message me normally, for example:
• "New client: Sarah Jones, 07700 900123, sarah@example.com – horse Bella, 12yo Irish Sport Horse, vet Dr Patel at Oakfield Vets"
• "Book Bella in for Tuesday at 10"
• "What have I got on this week?"
• "Invoice Sarah £65 for Bella's session today and email it"
• "Who owes me money?"
• "Send Bella the kissing spine programme, but only walk work in week 1"
• Send a photo of a report sheet and say which horse it's for
• "Any new emails?"

I never send anything to a client without showing you first – you'll get Send / Cancel buttons.

/new – start a fresh conversation
/pending – show messages waiting for your OK"""


class Bot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.google = self._connect_google()
        self.sms = (SmsSender(settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from)
                    if settings.twilio_configured else None)
        self.queue = ActionQueue(self.db, self.google, self.sms)
        self.toolbox = Toolbox(self.db, settings, self.queue, self.google)
        self.agent = Agent(settings, self.toolbox)
        self.locks: dict[int, asyncio.Lock] = {}

    def _connect_google(self) -> GoogleServices | None:
        try:
            creds = load_credentials(self.settings.google_token_json, self.settings.google_token_file)
        except Exception:
            log.exception("Couldn't load Google credentials")
            return None
        if not creds:
            log.warning("Google not connected – run scripts/google_login.py")
            return None
        return GoogleServices(creds, self.settings.business_email, self.settings.business_name)

    # ---- access control ----------------------------------------------
    def _is_owner(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and self.settings.owner_telegram_id and user.id == self.settings.owner_telegram_id)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.owner_telegram_id:
            await update.message.reply_text(
                f"Setup step: your Telegram id is {update.effective_user.id}. "
                "Put it in OWNER_TELEGRAM_ID and restart the bot.")
            return
        if not self._is_owner(update):
            await update.message.reply_text("Sorry, this is a private assistant.")
            return
        await update.message.reply_text(HELP.format(business=self.settings.business_name))
        problems = []
        if not self.google:
            problems.append("Google (email, calendar, Drive) isn't connected yet.")
        if not self.sms:
            problems.append("Twilio (text messages) isn't set up yet.")
        if problems:
            await update.message.reply_text("Still to set up:\n• " + "\n• ".join(problems))

    async def cmd_myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(f"Your Telegram id is {update.effective_user.id}")

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        self.agent.reset(update.effective_chat.id)
        await update.message.reply_text("Fresh start 👍 What can I do?")

    async def cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        pending = self.db.list_pending()
        if not pending:
            await update.message.reply_text("Nothing waiting – all clear ✅")
            return
        await self.announce(context, update.effective_chat.id, pending)

    # ---- messages ------------------------------------------------------
    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            if update.message:
                await update.message.reply_text("Sorry, this is a private assistant.")
            return
        msg = update.message
        chat_id = update.effective_chat.id
        lock = self.locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            text = msg.text or msg.caption or ""
            attachments, notes = await self._collect_attachments(msg)
            if notes:
                text = " ".join(notes) + ("\n" + text if text else "\nWhat would you like me to do with this?")
            typing = asyncio.create_task(self._keep_typing(context, chat_id))
            try:
                reply = await self.agent.handle(chat_id, text, attachments)
            except Exception:
                log.exception("Agent failed")
                reply = "Sorry, something went wrong there. Please try again."
            finally:
                typing.cancel()
            for chunk in _chunks(reply):
                await msg.reply_text(chunk)
            await self.announce(context, chat_id, self.db.list_pending(only_unannounced=True))

    async def _keep_typing(self, context, chat_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(4)
                await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except asyncio.CancelledError:
            pass

    async def _collect_attachments(self, msg) -> tuple[list[dict], list[str]]:
        """Save any photo/file to disk, record it as an upload, and build Claude content blocks."""
        blocks, notes = [], []
        tg_file, name, mime = None, None, None
        if msg.photo:
            tg_file = await msg.photo[-1].get_file()
            mime, name = "image/jpeg", f"photo_{msg.message_id}.jpg"
        elif msg.document:
            tg_file = await msg.document.get_file()
            name = msg.document.file_name or f"file_{msg.message_id}"
            mime = msg.document.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if not tg_file:
            return blocks, notes

        folder = self.settings.files_dir / "uploads" / datetime.now().strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{name}"
        await tg_file.download_to_drive(str(path))
        upload_id = self.db.add_upload(str(path), mime, name)
        data = base64.standard_b64encode(path.read_bytes()).decode()
        if mime in IMAGE_TYPES:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            notes.append(f"[Upload #{upload_id}: photo]")
        elif mime == "application/pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}})
            notes.append(f"[Upload #{upload_id}: PDF '{name}']")
        else:
            notes.append(f"[Upload #{upload_id}: file '{name}' ({mime}) – I can attach or file it but not read it]")
        return blocks, notes

    # ---- approvals -----------------------------------------------------
    async def announce(self, context, chat_id: int, actions: list[dict]) -> None:
        for action in actions:
            preview = action["preview"]
            if len(preview) > 3500:
                preview = preview[:3500] + "…"
            buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Send", callback_data=f"ok:{action['id']}"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"no:{action['id']}"),
            ]])
            await context.bot.send_message(chat_id, f"Ready to send (#{action['id']}):\n\n{preview}",
                                           reply_markup=buttons)
            self.db.mark_announced(action["id"])
        if len(actions) > 1:
            ids = ",".join(str(a["id"]) for a in actions)
            if len(ids) <= 60:
                await context.bot.send_message(
                    chat_id, f"{len(actions)} messages ready.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        f"✅ Send all {len(actions)}", callback_data=f"all:{ids}")]]))

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._is_owner(update):
            await query.answer("Not allowed")
            return
        await query.answer()
        verb, _, arg = query.data.partition(":")
        if verb == "all":
            results = [await self.queue.approve(int(i)) for i in arg.split(",") if i]
            for r in results:
                self.agent.add_note(update.effective_chat.id, r)
            await query.edit_message_text("\n".join(results))
            return
        action_id = int(arg)
        original = query.message.text or ""
        if verb == "ok":
            result = await self.queue.approve(action_id)
        else:
            result = self.queue.cancel(action_id)
        # Tell Claude on the next message, so it knows what has actually gone out.
        self.agent.add_note(update.effective_chat.id, f"#{action_id}: {result}")
        await query.edit_message_text(f"{original}\n\n— {result}"[:TELEGRAM_LIMIT])

    # ---- daily jobs ----------------------------------------------------
    async def job_overdue(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = datetime.now(self.settings.timezone).date()
        invoices = self.db.invoices_needing_reminder(today, self.settings.reminder_gap_days)
        if not invoices:
            return
        summary = self.toolbox.queue_reminders(invoices, today)
        chat = self.settings.owner_telegram_id
        await context.bot.send_message(chat, f"💷 Morning! {len(invoices)} overdue invoice(s). {summary}")
        await self.announce(context, chat, self.db.list_pending(only_unannounced=True))

    async def job_appointment_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        tomorrow = datetime.now(self.settings.timezone).date() + timedelta(days=1)
        appts = self.db.appointments_between(tomorrow.isoformat(), (tomorrow + timedelta(days=1)).isoformat())
        appts = [a for a in appts if a["status"] == "booked" and not a["reminder_sent_at"]]
        chat = self.settings.owner_telegram_id
        if not appts:
            return
        lines = [f"📅 Tomorrow you have {len(appts)} appointment(s):"]
        lines += [f"• {a['start'][11:16]} {a['horse_name']} ({a['owner_name']})"
                  + (f" – {a['location']}" if a["location"] else "") for a in appts]
        await context.bot.send_message(chat, "\n".join(lines))
        for a in appts:
            ref = f"appointment_reminder:{a['id']}"
            if a["owner_phone"] and not self.db.has_pending_ref(ref):
                self.queue.queue_sms(a["owner_phone"], appointment_reminder_text(a, self.settings),
                                     recipient_name=a["owner_name"], ref=ref,
                                     after=[{"type": "appointment_reminder", "appointment_id": a["id"]}])
        await self.announce(context, chat, self.db.list_pending(only_unannounced=True))

    async def job_backup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        backup_dir = self.settings.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"pht-{datetime.now().strftime('%Y-%m-%d')}.sqlite3"
        self.db.backup_to(target)
        for old in sorted(backup_dir.glob("pht-*.sqlite3"))[:-14]:
            old.unlink()
        if self.google:
            try:
                folder = await asyncio.to_thread(self.google.ensure_folder, "Backups")
                await asyncio.to_thread(self.google.upload_file, str(target), folder)
            except Exception:
                log.exception("Backup upload failed")

    # ---- wiring --------------------------------------------------------
    def build(self) -> Application:
        app = Application.builder().token(self.settings.telegram_token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("myid", self.cmd_myid))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("pending", self.cmd_pending))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_handler(MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL, self.on_message))

        if self.settings.owner_telegram_id:
            tz = self.settings.timezone
            jq = app.job_queue
            jq.run_daily(self.job_overdue, _at(self.settings.overdue_check_time, tz), name="overdue")
            if self.settings.appointment_reminders_enabled:
                jq.run_daily(self.job_appointment_reminders, _at(self.settings.appointment_reminder_time, tz),
                             name="appointment_reminders")
            jq.run_daily(self.job_backup, _at(self.settings.backup_time, tz), name="backup")
        return app


def _at(hhmm: str, tz) -> dtime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dtime(hour, minute, tzinfo=tz)


def _chunks(text: str, size: int = TELEGRAM_LIMIT) -> list[str]:
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return [c for c in chunks if c.strip()] or ["Done."]


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing – see the README.")
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set – relying on other Anthropic credentials if present.")
    bot = Bot(settings)
    bot.build().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
