"""The actions Claude can take on the owner's behalf.

Anything that reaches a client (email or text) is only *prepared* here and queued
for the owner's approval in Telegram – see actions.py.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .actions import ActionQueue
from .config import Settings
from .db import Database, pence_to_str, pounds_to_pence
from .pdfs import render_invoice, render_rehab

log = logging.getLogger(__name__)


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


OWNER_PROPS = {
    "name": _str("Owner's full name"),
    "phone": _str("Mobile number, e.g. 07700 900123"),
    "email": _str("Email address"),
    "address": _str("Postal address for invoices (use new lines between lines)"),
    "notes": _str("Anything else worth remembering about this client"),
}
HORSE_PROPS = {
    "name": _str("Horse's name (stable name)"),
    "breed": _str("Breed"),
    "age": _str("Age or year of birth"),
    "discipline": _str("Work/discipline, e.g. eventing, dressage, hacking"),
    "yard_address": _str("Where the horse is kept (default visit location)"),
    "vet_name": _str("Referring/attending vet's name"),
    "vet_practice": _str("Vet practice"),
    "vet_consent_date": _str("Date vet consent/referral was given, YYYY-MM-DD"),
    "notes": _str("Medical history, temperament, anything relevant"),
}

TOOLS: list[dict] = [
    {"name": "find_clients",
     "description": "Search clients by owner name, horse name, phone or email. Returns owners with their "
                    "horses and ids. Use an empty query to list everyone. Always look a client up before "
                    "adding them, to avoid duplicates.",
     "input_schema": _obj({"query": _str("Text to search for; empty string lists all")}, ["query"])},
    {"name": "add_owner", "description": "Add a new horse owner (the client who pays).",
     "input_schema": _obj(OWNER_PROPS, ["name"])},
    {"name": "update_owner", "description": "Change an owner's details. Only include fields that change.",
     "input_schema": _obj({"owner_id": _int("Owner id"), **OWNER_PROPS}, ["owner_id"])},
    {"name": "add_horse", "description": "Add a horse belonging to an existing owner.",
     "input_schema": _obj({"owner_id": _int("Owner id"), **HORSE_PROPS}, ["owner_id", "name"])},
    {"name": "update_horse",
     "description": "Change a horse's details (including moving it to another owner). Only include fields that change.",
     "input_schema": _obj({"horse_id": _int("Horse id"), "owner_id": _int("New owner id, if changing"),
                           **HORSE_PROPS}, ["horse_id"])},
    {"name": "get_horse_history",
     "description": "Everything on file for one horse: details, vet, recent appointments, reports, "
                    "rehab programmes sent and invoices.",
     "input_schema": _obj({"horse_id": _int("Horse id")}, ["horse_id"])},

    {"name": "list_diary",
     "description": "Show the diary between two dates: client appointments plus everything else in the "
                    "Google Calendar. Dates are YYYY-MM-DD; end date is inclusive.",
     "input_schema": _obj({"start_date": _str("First day, YYYY-MM-DD"),
                           "end_date": _str("Last day, YYYY-MM-DD")}, ["start_date", "end_date"])},
    {"name": "book_appointment",
     "description": "Put an appointment in the diary (and Google Calendar). Clients can't book themselves; "
                    "only the owner books via you. Check the diary for clashes first. Does not message the client – "
                    "use send_text or send_email afterwards if the owner wants a confirmation sent.",
     "input_schema": _obj({
         "horse_id": _int("Horse id"),
         "start": _str("Local start time, YYYY-MM-DDTHH:MM"),
         "duration_minutes": _int("Length in minutes (default: the usual session length)"),
         "location": _str("Where the visit is (defaults to the horse's yard address)"),
         "notes": _str("Reason for visit or notes"),
     }, ["horse_id", "start"])},
    {"name": "reschedule_appointment", "description": "Move an existing appointment to a new time.",
     "input_schema": _obj({"appointment_id": _int("Appointment id"),
                           "new_start": _str("New local start time, YYYY-MM-DDTHH:MM"),
                           "duration_minutes": _int("New length in minutes, if changing"),
                           "location": _str("New location, if changing")},
                          ["appointment_id", "new_start"])},
    {"name": "cancel_appointment", "description": "Cancel an appointment and remove it from Google Calendar.",
     "input_schema": _obj({"appointment_id": _int("Appointment id")}, ["appointment_id"])},
    {"name": "mark_appointment_done", "description": "Mark an appointment as completed.",
     "input_schema": _obj({"appointment_id": _int("Appointment id")}, ["appointment_id"])},

    {"name": "create_invoice",
     "description": "Create an invoice (PDF, saved to Google Drive) for one owner. It can cover several "
                    "horses/sessions. Amounts are in pounds. This does NOT send it – call send_invoice next "
                    "if the owner wants it sent.",
     "input_schema": _obj({
         "owner_id": _int("Owner id"),
         "items": {"type": "array", "description": "Lines on the invoice",
                   "items": _obj({"description": _str("e.g. 'Physiotherapy assessment and treatment – 3 Nov'"),
                                  "amount_pounds": {"type": "number", "description": "Amount in £"},
                                  "horse_id": _int("Horse this line is for"),
                                  "appointment_id": _int("Appointment this line is for, if known")},
                                 ["description", "amount_pounds"])},
         "issue_date": _str("Invoice date YYYY-MM-DD (default today)"),
         "due_in_days": _int("Days until payment is due (default: usual payment terms)"),
         "notes": _str("Optional note printed at the bottom"),
     }, ["owner_id", "items"])},
    {"name": "send_invoice",
     "description": "Prepare an invoice to go to the client by email (PDF attached) and/or text (summary with "
                    "bank details). The owner approves before anything is sent.",
     "input_schema": _obj({
         "invoice_number": _str("e.g. PHT-0007"),
         "via": {"type": "array", "items": {"type": "string", "enum": ["email", "text"]},
                 "description": "How to send it"},
         "message": _str("Optional personal note to include"),
     }, ["invoice_number", "via"])},
    {"name": "list_invoices", "description": "List invoices by status.",
     "input_schema": _obj({"status": {"type": "string", "enum": ["unpaid", "overdue", "paid", "void", "all"]},
                           "owner_id": _int("Only this owner's invoices")}, ["status"])},
    {"name": "get_invoice", "description": "Full details of one invoice.",
     "input_schema": _obj({"invoice_number": _str("e.g. PHT-0007")}, ["invoice_number"])},
    {"name": "mark_invoice_paid", "description": "Record that an invoice has been paid.",
     "input_schema": _obj({"invoice_number": _str("e.g. PHT-0007"),
                           "paid_date": _str("YYYY-MM-DD, default today"),
                           "method": _str("e.g. bank transfer, cash, card")}, ["invoice_number"])},
    {"name": "void_invoice", "description": "Cancel an invoice that was raised by mistake.",
     "input_schema": _obj({"invoice_number": _str("e.g. PHT-0007")}, ["invoice_number"])},
    {"name": "prepare_payment_reminders",
     "description": "Prepare polite reminder texts for overdue invoices. With no invoice numbers, does every "
                    "overdue invoice that hasn't been reminded recently. The owner approves each text.",
     "input_schema": _obj({"invoice_numbers": {"type": "array", "items": {"type": "string"},
                                               "description": "Specific invoices; leave empty for all overdue"}})},

    {"name": "save_rehab_template",
     "description": "Save (or overwrite) a reusable rehab programme template. Write content as plain text: "
                    "'# Heading' lines for weeks/phases, '- ' for bullet points.",
     "input_schema": _obj({"name": _str("Short name, e.g. 'Kissing spine – 6 week'"),
                           "summary": _str("One line on when to use it"),
                           "content": _str("The full programme")}, ["name", "content"])},
    {"name": "list_rehab_templates", "description": "List saved rehab programme templates.",
     "input_schema": _obj({})},
    {"name": "get_rehab_template", "description": "Read a saved rehab template in full.",
     "input_schema": _obj({"name_or_id": _str("Template name (partial is fine) or id")}, ["name_or_id"])},
    {"name": "delete_rehab_template", "description": "Delete a saved rehab template.",
     "input_schema": _obj({"template_id": _int("Template id")}, ["template_id"])},
    {"name": "send_rehab_programme",
     "description": "Make a personalised rehab programme PDF for a horse and prepare an email to the owner "
                    "with it attached. Usually start from a template (get_rehab_template) and adapt it. "
                    "The owner approves before it is sent.",
     "input_schema": _obj({
         "horse_id": _int("Horse id"),
         "title": _str("Title on the PDF, e.g. 'Bella – 6 week back rehab programme'"),
         "content": _str("The full personalised programme text"),
         "template_id": _int("Template it was based on, if any"),
         "email_message": _str("Short covering email body"),
     }, ["horse_id", "title", "content"])},

    {"name": "save_report",
     "description": "File a treatment/assessment report for a horse. Use after the owner sends a photo of a "
                    "paper report sheet: transcribe it, then save it with the upload ids so the original "
                    "photo is stored in the horse's Google Drive folder.",
     "input_schema": _obj({
         "horse_id": _int("Horse id"),
         "visit_date": _str("Date of the visit, YYYY-MM-DD"),
         "summary": _str("2-3 sentence summary: findings, treatment, plan"),
         "transcription": _str("Full typed-up text of the report sheet"),
         "upload_ids": {"type": "array", "items": {"type": "integer"},
                        "description": "Upload ids of the photos/files of this report"},
         "appointment_id": _int("Matching appointment id, if known"),
     }, ["horse_id", "visit_date", "summary", "transcription"])},

    {"name": "send_email",
     "description": "Prepare an email from the business address. The owner sees it and approves before it is "
                    "sent. To reply to an email, pass its id from check_inbox/read_email.",
     "input_schema": _obj({
         "to": _str("Recipient email address"),
         "subject": _str("Subject line"),
         "body": _str("Email text, signed off as the business"),
         "upload_ids": {"type": "array", "items": {"type": "integer"}, "description": "Files to attach"},
         "reply_to_email_id": _str("Gmail id of the email being replied to"),
     }, ["to", "subject", "body"])},
    {"name": "send_text",
     "description": "Prepare a text message (SMS) to a client. Keep it short and friendly, signed with the "
                    "business name. The owner approves before it is sent.",
     "input_schema": _obj({"to_phone": _str("Mobile number"), "body": _str("Message text"),
                           "recipient_name": _str("Who it's to")}, ["to_phone", "body"])},
    {"name": "check_inbox",
     "description": "Look at recent emails in the business inbox. Uses Gmail search syntax, e.g. "
                    "'is:unread', 'from:sarah', 'newer_than:2d'.",
     "input_schema": _obj({"query": _str("Gmail search, default 'in:inbox'"),
                           "max_results": _int("How many, default 10")})},
    {"name": "read_email", "description": "Read one email in full.",
     "input_schema": _obj({"email_id": _str("Gmail message id from check_inbox")}, ["email_id"])},
]


def invoice_reminder_text(inv: dict, settings: Settings, today: date) -> str:
    first = (inv["owner_name"] or "").split(" ")[0] or "there"
    days = (today - date.fromisoformat(inv["due_date"])).days
    lines = [f"Hi {first}, a friendly reminder that invoice {inv['number']} for "
             f"{pence_to_str(inv['total_pence'])} was due on "
             f"{date.fromisoformat(inv['due_date']).strftime('%-d %b')}"
             + (f" ({days} days ago)." if days > 1 else ".")]
    if settings.bank_account_number:
        lines.append(f"Bank: {settings.bank_sort_code} / {settings.bank_account_number}, ref {inv['number']}.")
    lines.append("If you've already paid, thank you and please ignore this.")
    lines.append(f"{settings.owner_name.split(' ')[0]}, {settings.business_name}"
                 if settings.owner_name else settings.business_name)
    return " ".join(lines)


def appointment_reminder_text(appt: dict, settings: Settings) -> str:
    first = (appt["owner_name"] or "").split(" ")[0] or "there"
    start = datetime.fromisoformat(appt["start"])
    where = f" at {appt['location']}" if appt.get("location") else ""
    if settings.owner_name:
        who, sign = "me", f"{settings.owner_name.split(' ')[0]}, {settings.business_name}"
    else:
        who, sign = "us", settings.business_name
    return (f"Hi {first}, just a reminder that {appt['horse_name']}'s physio appointment is tomorrow "
            f"({start.strftime('%a %-d %b')}) at {start.strftime('%-I:%M%p').lower()}{where}. "
            f"Please let {who} know if you need to change it. {sign}")


class ToolError(Exception):
    pass


class Toolbox:
    def __init__(self, db: Database, settings: Settings, queue: ActionQueue, google=None):
        self.db = db
        self.settings = settings
        self.queue = queue
        self.google = google

    # ---- dispatch ------------------------------------------------------
    async def run(self, name: str, args: dict) -> str:
        handler = getattr(self, f"t_{name}", None)
        if not handler:
            raise ToolError(f"Unknown tool {name}")
        result = handler(**args)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, str) else json.dumps(result, default=str, indent=1)

    # ---- helpers -------------------------------------------------------
    def _today(self) -> date:
        return datetime.now(self.settings.timezone).date()

    def _parse_local(self, value: str) -> datetime:
        try:
            dt = datetime.fromisoformat(value.strip().replace(" ", "T"))
        except ValueError as exc:
            raise ToolError(f"Couldn't read the time '{value}'. Use YYYY-MM-DDTHH:MM.") from exc
        return dt.replace(tzinfo=None, second=0, microsecond=0)

    def _aware(self, naive: datetime) -> datetime:
        return naive.replace(tzinfo=self.settings.timezone)

    def _horse(self, horse_id: int) -> dict:
        horse = self.db.get_horse(horse_id)
        if not horse:
            raise ToolError(f"No horse with id {horse_id}. Use find_clients to look it up.")
        return horse

    def _owner(self, owner_id: int) -> dict:
        owner = self.db.get_owner(owner_id)
        if not owner:
            raise ToolError(f"No owner with id {owner_id}. Use find_clients to look them up.")
        return owner

    def _invoice(self, number: str) -> dict:
        inv = self.db.get_invoice(number)
        if not inv:
            raise ToolError(f"No invoice {number}.")
        return inv

    def _sign_off(self) -> str:
        name = self.settings.owner_name
        return f"{name}\n{self.settings.business_name}" if name else self.settings.business_name

    async def _drive_upload(self, local_path: str, *folders: str) -> str | None:
        if not self.google:
            return None
        try:
            folder = await asyncio.to_thread(self.google.ensure_folder, *folders)
            return await asyncio.to_thread(self.google.upload_file, local_path, folder)
        except Exception:
            log.exception("Drive upload failed for %s", local_path)
            return None

    def _horse_folder(self, horse: dict) -> tuple[str, str, str]:
        return ("Clients", f"{horse['owner_name']} (#{horse['owner_id']})", f"{horse['name']} (#{horse['id']})")

    # ---- clients -------------------------------------------------------
    def t_find_clients(self, query: str = "") -> Any:
        found = self.db.search_clients(query)
        return found or f"No clients match '{query}'."

    def t_add_owner(self, **fields) -> str:
        owner_id = self.db.add_owner(**fields)
        return f"Added owner {fields['name']} (owner id {owner_id})."

    def t_update_owner(self, owner_id: int, **fields) -> str:
        self._owner(owner_id)
        self.db.update_owner(owner_id, **fields)
        return f"Updated owner {owner_id}: {', '.join(fields) or 'nothing changed'}."

    def t_add_horse(self, owner_id: int, **fields) -> str:
        owner = self._owner(owner_id)
        horse_id = self.db.add_horse(owner_id, **fields)
        msg = f"Added {fields['name']} (horse id {horse_id}) for {owner['name']}."
        if not fields.get("vet_consent_date"):
            msg += " No vet consent recorded yet."
        return msg

    def t_update_horse(self, horse_id: int, **fields) -> str:
        self._horse(horse_id)
        if "owner_id" in fields:
            self._owner(fields["owner_id"])
        self.db.update_horse(horse_id, **fields)
        return f"Updated horse {horse_id}: {', '.join(fields) or 'nothing changed'}."

    def t_get_horse_history(self, horse_id: int) -> Any:
        self._horse(horse_id)
        history = self.db.horse_history(horse_id)
        for inv in history["invoices"]:
            inv["total"] = pence_to_str(inv.pop("total_pence"))
        return history

    # ---- diary ---------------------------------------------------------
    async def t_list_diary(self, start_date: str, end_date: str) -> Any:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date) + timedelta(days=1)
        appts = self.db.appointments_between(start.isoformat(), end.isoformat())
        known_events = {a["calendar_event_id"] for a in appts if a["calendar_event_id"]}
        out = {"client_appointments": [
            {"appointment_id": a["id"], "start": a["start"], "end": a["end"], "horse": a["horse_name"],
             "horse_id": a["horse_id"], "owner": a["owner_name"], "location": a["location"],
             "notes": a["notes"], "status": a["status"]} for a in appts]}
        if self.google:
            try:
                events = await asyncio.to_thread(
                    self.google.list_events,
                    self._aware(datetime.combine(start, datetime.min.time())),
                    self._aware(datetime.combine(end, datetime.min.time())))
                out["other_calendar_events"] = [e for e in events if e["id"] not in known_events]
            except Exception as exc:
                log.exception("Calendar list failed")
                out["calendar_error"] = str(exc)
        else:
            out["note"] = "Google Calendar not connected – showing bot bookings only."
        return out

    async def t_book_appointment(self, horse_id: int, start: str, duration_minutes: int | None = None,
                                 location: str | None = None, notes: str | None = None) -> str:
        horse = self._horse(horse_id)
        begin = self._parse_local(start)
        end = begin + timedelta(minutes=duration_minutes or self.settings.default_session_minutes)
        location = location or horse.get("yard_address")

        clashes = [a for a in self.db.appointments_between(
            (begin - timedelta(hours=12)).isoformat(), end.isoformat())
            if datetime.fromisoformat(a["end"]) > begin]
        event_id = None
        warning = ""
        if self.google:
            try:
                event_id = await asyncio.to_thread(
                    self.google.create_event,
                    f"🐴 {horse['name']} ({horse['owner_name']})", self._aware(begin), self._aware(end),
                    location, self._event_description(horse, notes), str(self.settings.timezone))
            except Exception as exc:
                log.exception("Calendar insert failed")
                warning = f" (Couldn't add it to Google Calendar: {exc})"
        appt_id = self.db.add_appointment(horse_id, begin.isoformat(), end.isoformat(), location, notes, event_id)
        msg = (f"Booked {horse['name']} for {begin.strftime('%a %-d %b %Y %H:%M')}–{end.strftime('%H:%M')}"
               f"{' at ' + location if location else ''} (appointment id {appt_id}).{warning}")
        if clashes:
            msg += " ⚠️ Overlaps with: " + "; ".join(
                f"{c['horse_name']} {c['start'][11:16]}–{c['end'][11:16]}" for c in clashes)
        if not horse.get("vet_consent_date"):
            msg += " Reminder: no vet consent is recorded for this horse."
        return msg

    def _event_description(self, horse: dict, notes: str | None) -> str:
        parts = [f"Owner: {horse['owner_name']}"]
        if horse.get("owner_phone"):
            parts.append(f"Phone: {horse['owner_phone']}")
        if notes:
            parts.append(f"Notes: {notes}")
        return "\n".join(parts)

    async def t_reschedule_appointment(self, appointment_id: int, new_start: str,
                                       duration_minutes: int | None = None, location: str | None = None) -> str:
        appt = self.db.get_appointment(appointment_id)
        if not appt:
            raise ToolError(f"No appointment {appointment_id}.")
        old_len = datetime.fromisoformat(appt["end"]) - datetime.fromisoformat(appt["start"])
        begin = self._parse_local(new_start)
        end = begin + (timedelta(minutes=duration_minutes) if duration_minutes else old_len)
        warning = ""
        if self.google and appt["calendar_event_id"]:
            try:
                await asyncio.to_thread(self.google.update_event, appt["calendar_event_id"],
                                        str(self.settings.timezone), start=self._aware(begin),
                                        end=self._aware(end), location=location)
            except Exception as exc:
                log.exception("Calendar update failed")
                warning = f" (Couldn't update Google Calendar: {exc})"
        fields = {"start": begin.isoformat(), "end": end.isoformat(), "reminder_sent_at": None}
        if location:
            fields["location"] = location
        self.db.update_appointment(appointment_id, **fields)
        return (f"Moved {appt['horse_name']} to {begin.strftime('%a %-d %b %Y %H:%M')}–"
                f"{end.strftime('%H:%M')}.{warning}")

    async def t_cancel_appointment(self, appointment_id: int) -> str:
        appt = self.db.get_appointment(appointment_id)
        if not appt:
            raise ToolError(f"No appointment {appointment_id}.")
        warning = ""
        if self.google and appt["calendar_event_id"]:
            try:
                await asyncio.to_thread(self.google.delete_event, appt["calendar_event_id"])
            except Exception as exc:
                log.exception("Calendar delete failed")
                warning = f" (Couldn't remove it from Google Calendar: {exc})"
        self.db.update_appointment(appointment_id, status="cancelled")
        return f"Cancelled {appt['horse_name']}'s appointment on {appt['start'].replace('T', ' ')}.{warning}"

    def t_mark_appointment_done(self, appointment_id: int) -> str:
        if not self.db.get_appointment(appointment_id):
            raise ToolError(f"No appointment {appointment_id}.")
        self.db.update_appointment(appointment_id, status="done")
        return f"Appointment {appointment_id} marked as done."

    # ---- invoices ------------------------------------------------------
    async def t_create_invoice(self, owner_id: int, items: list[dict], issue_date: str | None = None,
                               due_in_days: int | None = None, notes: str | None = None) -> str:
        owner = self._owner(owner_id)
        if not items:
            raise ToolError("An invoice needs at least one line.")
        lines = []
        for item in items:
            if item.get("horse_id"):
                horse = self._horse(item["horse_id"])
                if horse["owner_id"] != owner_id:
                    raise ToolError(f"{horse['name']} belongs to {horse['owner_name']}, not {owner['name']}.")
            lines.append({"description": item["description"], "amount_pence": pounds_to_pence(item["amount_pounds"]),
                          "horse_id": item.get("horse_id"), "appointment_id": item.get("appointment_id")})
        issued = date.fromisoformat(issue_date) if issue_date else self._today()
        due = issued + timedelta(days=self.settings.payment_terms_days if due_in_days is None else due_in_days)
        inv = self.db.create_invoice(owner_id, lines, issued, due, notes)

        pdf = render_invoice(inv, self.settings, self.settings.files_dir / "invoices" / f"{inv['number']}.pdf")
        drive_id = await self._drive_upload(str(pdf), "Invoices", str(issued.year))
        self.db.update_invoice(inv["id"], pdf_path=str(pdf), drive_file_id=drive_id)

        warn = ""
        if not owner.get("address"):
            warn = " Note: there's no postal address on file for this owner, so the invoice shows their name only."
        return (f"Created invoice {inv['number']} for {owner['name']}: {pence_to_str(inv['total_pence'])}, "
                f"due {due.strftime('%-d %b %Y')}. Not sent yet.{warn}")

    def t_send_invoice(self, invoice_number: str, via: list[str], message: str | None = None) -> str:
        inv = self._invoice(invoice_number)
        if inv["status"] != "unpaid":
            raise ToolError(f"Invoice {inv['number']} is {inv['status']}.")
        first = inv["owner_name"].split(" ")[0]
        queued = []
        if "email" in via:
            if not inv["owner_email"]:
                raise ToolError(f"No email address for {inv['owner_name']}. Add one with update_owner, or send by text.")
            if not inv["pdf_path"] or not Path(inv["pdf_path"]).exists():
                pdf = render_invoice(inv, self.settings,
                                     self.settings.files_dir / "invoices" / f"{inv['number']}.pdf")
                self.db.update_invoice(inv["id"], pdf_path=str(pdf))
                inv["pdf_path"] = str(pdf)
            body = f"Hi {first},\n\n"
            body += (message.strip() + "\n\n") if message else ""
            body += (f"Please find attached invoice {inv['number']} for {pence_to_str(inv['total_pence'])}, "
                     f"due by {date.fromisoformat(inv['due_date']).strftime('%-d %B %Y')}.\n\n")
            if self.settings.bank_account_number:
                body += (f"Payment by bank transfer to:\n{self.settings.bank_account_name or self.settings.business_name}\n"
                         f"Sort code: {self.settings.bank_sort_code}\nAccount: {self.settings.bank_account_number}\n"
                         f"Reference: {inv['number']}\n\n")
            body += f"Many thanks,\n{self._sign_off()}"
            aid = self.queue.queue_email(inv["owner_email"], f"Invoice {inv['number']} – {self.settings.business_name}",
                                         body, [inv["pdf_path"]],
                                         after=[{"type": "invoice_sent", "invoice_id": inv["id"]}],
                                         ref=f"invoice_email:{inv['id']}")
            queued.append(f"email #{aid}")
        if "text" in via:
            if not inv["owner_phone"]:
                raise ToolError(f"No mobile number for {inv['owner_name']}.")
            text = (f"Hi {first}, here's your invoice {inv['number']} from {self.settings.business_name}: "
                    f"{pence_to_str(inv['total_pence'])}, due "
                    f"{date.fromisoformat(inv['due_date']).strftime('%-d %b')}.")
            if self.settings.bank_account_number:
                text += (f" Bank: {self.settings.bank_sort_code} / {self.settings.bank_account_number}, "
                         f"ref {inv['number']}.")
            if message:
                text += f" {message.strip()}"
            text += " Thank you!"
            aid = self.queue.queue_sms(inv["owner_phone"], text, recipient_name=inv["owner_name"],
                                       after=[{"type": "invoice_sent", "invoice_id": inv["id"]}],
                                       ref=f"invoice_text:{inv['id']}")
            queued.append(f"text #{aid}")
        if not queued:
            raise ToolError("Choose email and/or text.")
        return f"Prepared {' and '.join(queued)} for invoice {inv['number']}. Waiting for the owner to tap Send."

    def t_list_invoices(self, status: str, owner_id: int | None = None) -> Any:
        rows = self.db.list_invoices(status, owner_id, today=self._today())
        if not rows:
            return f"No {status} invoices."
        total = sum(r["total_pence"] for r in rows)
        return {"count": len(rows), "total": pence_to_str(total), "invoices": [
            {"number": r["number"], "owner": r["owner_name"], "issued": r["issue_date"], "due": r["due_date"],
             "amount": pence_to_str(r["total_pence"]), "status": r["status"], "paid_date": r["paid_date"],
             "sent": bool(r["sent_at"]), "reminders_sent": r["reminder_count"]} for r in rows]}

    def t_get_invoice(self, invoice_number: str) -> Any:
        inv = self._invoice(invoice_number)
        inv["total"] = pence_to_str(inv.pop("total_pence"))
        for it in inv["items"]:
            it["amount"] = pence_to_str(it.pop("amount_pence"))
        inv.pop("pdf_path", None)
        return inv

    def t_mark_invoice_paid(self, invoice_number: str, paid_date: str | None = None, method: str | None = None) -> str:
        inv = self._invoice(invoice_number)
        self.db.update_invoice(inv["id"], status="paid", paid_date=paid_date or self._today().isoformat(),
                               payment_method=method)
        return f"Invoice {inv['number']} ({pence_to_str(inv['total_pence'])}, {inv['owner_name']}) marked as paid."

    def t_void_invoice(self, invoice_number: str) -> str:
        inv = self._invoice(invoice_number)
        self.db.update_invoice(inv["id"], status="void")
        return f"Invoice {inv['number']} cancelled (void)."

    def t_prepare_payment_reminders(self, invoice_numbers: list[str] | None = None) -> str:
        today = self._today()
        if invoice_numbers:
            invoices = [self._invoice(n) for n in invoice_numbers]
            invoices = [i for i in invoices if i["status"] == "unpaid"]
        else:
            invoices = self.db.invoices_needing_reminder(today, self.settings.reminder_gap_days)
        return self.queue_reminders(invoices, today) or "No overdue invoices need a reminder right now."

    def queue_reminders(self, invoices: list[dict], today: date) -> str:
        done, skipped = [], []
        for inv in invoices:
            ref = f"invoice_reminder:{inv['id']}"
            if self.db.has_pending_ref(ref):
                skipped.append(f"{inv['number']} (already waiting for approval)")
                continue
            if not inv.get("owner_phone"):
                skipped.append(f"{inv['number']} (no mobile number for {inv['owner_name']})")
                continue
            aid = self.queue.queue_sms(inv["owner_phone"], invoice_reminder_text(inv, self.settings, today),
                                       recipient_name=inv["owner_name"], ref=ref,
                                       after=[{"type": "invoice_reminder", "invoice_id": inv["id"]}])
            done.append(f"{inv['number']} → text #{aid}")
        parts = []
        if done:
            parts.append("Prepared reminders: " + "; ".join(done))
        if skipped:
            parts.append("Skipped: " + "; ".join(skipped))
        return " ".join(parts)

    # ---- rehab ---------------------------------------------------------
    def t_save_rehab_template(self, name: str, content: str, summary: str | None = None) -> str:
        tid = self.db.save_rehab_template(name.strip(), content, summary)
        return f"Saved rehab template '{name}' (id {tid})."

    def t_list_rehab_templates(self) -> Any:
        return self.db.list_rehab_templates() or "No rehab templates saved yet."

    def t_get_rehab_template(self, name_or_id: str) -> Any:
        return self.db.get_rehab_template(name_or_id) or f"No template matching '{name_or_id}'."

    def t_delete_rehab_template(self, template_id: int) -> str:
        self.db.delete_rehab_template(template_id)
        return f"Deleted template {template_id}."

    async def t_send_rehab_programme(self, horse_id: int, title: str, content: str,
                                     template_id: int | None = None, email_message: str | None = None) -> str:
        horse = self._horse(horse_id)
        if not horse["owner_email"]:
            raise ToolError(f"No email address for {horse['owner_name']}. Add one with update_owner first.")
        today = self._today()
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip().replace(" ", "_")
        pdf = render_rehab(title, horse["name"], horse["owner_name"], content, self.settings,
                           self.settings.files_dir / "rehab" / f"{today.isoformat()}_{horse_id}_{safe}.pdf", today)
        await self._drive_upload(str(pdf), *self._horse_folder(horse))
        first = horse["owner_name"].split(" ")[0]
        body = email_message.strip() if email_message else (
            f"Hi {first},\n\nAs discussed, please find attached {horse['name']}'s rehab programme. "
            "Do get in touch if you have any questions, or if anything changes.")
        if not body.rstrip().endswith(self.settings.business_name):
            body += f"\n\nBest wishes,\n{self._sign_off()}"
        aid = self.queue.queue_email(horse["owner_email"], title, body, [str(pdf)],
                                     after=[{"type": "rehab_sent", "horse_id": horse_id, "title": title,
                                             "content": content, "template_id": template_id}])
        return f"Rehab PDF made and email #{aid} prepared for {horse['owner_name']}. Waiting for the owner to tap Send."

    # ---- reports -------------------------------------------------------
    async def t_save_report(self, horse_id: int, visit_date: str, summary: str, transcription: str,
                            upload_ids: list[int] | None = None, appointment_id: int | None = None) -> str:
        horse = self._horse(horse_id)
        drive_ids, missing = [], []
        for uid in upload_ids or []:
            up = self.db.get_upload(uid)
            if not up:
                missing.append(str(uid))
                continue
            ext = Path(up["local_path"]).suffix
            name = f"{visit_date} report {horse['name']} ({uid}){ext}"
            folder_parts = (*self._horse_folder(horse), "Reports")
            if self.google:
                try:
                    folder = await asyncio.to_thread(self.google.ensure_folder, *folder_parts)
                    drive_ids.append(await asyncio.to_thread(self.google.upload_file, up["local_path"], folder, name))
                except Exception:
                    log.exception("Report upload failed")
        rid = self.db.add_report(horse_id, visit_date, summary, transcription, drive_ids, appointment_id)
        msg = f"Saved report {rid} for {horse['name']} ({visit_date})."
        if drive_ids:
            msg += f" {len(drive_ids)} file(s) stored in Google Drive."
        elif upload_ids and not self.google:
            msg += " Photos kept on the bot only (Google Drive not connected)."
        if missing:
            msg += f" Couldn't find upload(s): {', '.join(missing)}."
        return msg

    # ---- email & text ----------------------------------------------------
    async def t_send_email(self, to: str, subject: str, body: str, upload_ids: list[int] | None = None,
                           reply_to_email_id: str | None = None) -> str:
        attachments = []
        for uid in upload_ids or []:
            up = self.db.get_upload(uid)
            if not up:
                raise ToolError(f"No upload {uid}.")
            attachments.append(up["local_path"])
        reply_header = thread_id = None
        if reply_to_email_id:
            if not self.google:
                raise ToolError("Google isn't connected.")
            original = await asyncio.to_thread(self.google.read_email, reply_to_email_id)
            reply_header, thread_id = original["message_id_header"], original["thread_id"]
        aid = self.queue.queue_email(to, subject, body, attachments, reply_to_message_id=reply_header,
                                     thread_id=thread_id)
        return f"Email #{aid} prepared. Waiting for the owner to tap Send."

    def t_send_text(self, to_phone: str, body: str, recipient_name: str | None = None) -> str:
        aid = self.queue.queue_sms(to_phone, body, recipient_name=recipient_name)
        note = "" if self.settings.twilio_configured else " (Twilio isn't set up yet, so sending will fail until it is.)"
        return f"Text #{aid} prepared. Waiting for the owner to tap Send.{note}"

    async def t_check_inbox(self, query: str = "in:inbox", max_results: int = 10) -> Any:
        if not self.google:
            raise ToolError("Google isn't connected yet.")
        return await asyncio.to_thread(self.google.search_inbox, query or "in:inbox", min(max_results or 10, 25))

    async def t_read_email(self, email_id: str) -> Any:
        if not self.google:
            raise ToolError("Google isn't connected yet.")
        return await asyncio.to_thread(self.google.read_email, email_id)
