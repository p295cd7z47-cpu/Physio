"""Outgoing messages that wait for the owner's approval before they are sent.

Nothing is ever emailed or texted to a client without the owner tapping "Send" in Telegram.
"""

import asyncio
import logging

from .db import Database, now_iso

log = logging.getLogger(__name__)


class ActionQueue:
    def __init__(self, db: Database, google=None, sms=None):
        self.db = db
        self.google = google
        self.sms = sms

    # ---- queueing ------------------------------------------------------
    def queue_email(self, to: str, subject: str, body: str, attachments: list[str] | None = None,
                    after: list[dict] | None = None, ref: str | None = None,
                    reply_to_message_id: str | None = None, thread_id: str | None = None) -> int:
        attach_names = ", ".join(p.rsplit("/", 1)[-1] for p in attachments or [])
        preview = f"📧 Email to {to}\nSubject: {subject}\n\n{body}"
        if attach_names:
            preview += f"\n\n📎 {attach_names}"
        payload = {"to": to, "subject": subject, "body": body, "attachments": attachments or [],
                   "after": after or [], "ref": ref, "reply_to_message_id": reply_to_message_id,
                   "thread_id": thread_id}
        return self.db.add_pending("email", payload, preview)

    def queue_sms(self, to: str, body: str, after: list[dict] | None = None, ref: str | None = None,
                  recipient_name: str | None = None) -> int:
        who = f"{recipient_name} ({to})" if recipient_name else to
        preview = f"💬 Text to {who}\n\n{body}"
        payload = {"to": to, "body": body, "after": after or [], "ref": ref}
        return self.db.add_pending("sms", payload, preview)

    # ---- running -------------------------------------------------------
    async def approve(self, action_id: int) -> str:
        action = self.db.get_pending(action_id)
        if not action:
            return "I can't find that message any more."
        if action["status"] != "pending":
            return f"That message was already {action['status']}."
        # Claim it first so a double tap can't send twice.
        self.db.finish_pending(action_id, "sending")
        p = action["payload"]
        try:
            if action["kind"] == "email":
                if not self.google:
                    raise RuntimeError("Google isn't connected yet, so I can't send emails.")
                await asyncio.to_thread(self.google.send_email, p["to"], p["subject"], p["body"],
                                        p.get("attachments"), p.get("reply_to_message_id"), p.get("thread_id"))
                done = f"Email sent to {p['to']} ✅"
            elif action["kind"] == "sms":
                if not self.sms:
                    raise RuntimeError("Twilio isn't set up yet, so I can't send texts.")
                await asyncio.to_thread(self.sms.send, p["to"], p["body"])
                done = f"Text sent to {p['to']} ✅"
            else:
                raise RuntimeError(f"Unknown action type {action['kind']}")
        except Exception as exc:  # report any failure back to the owner rather than crashing
            log.exception("Sending action %s failed", action_id)
            self.db.finish_pending(action_id, "failed", str(exc))
            return f"⚠️ Couldn't send: {exc}"
        self.db.finish_pending(action_id, "sent", done)
        self._apply_after(p.get("after", []))
        return done

    def cancel(self, action_id: int) -> str:
        action = self.db.get_pending(action_id)
        if not action or action["status"] != "pending":
            return "Nothing to cancel."
        self.db.finish_pending(action_id, "cancelled")
        return "Cancelled – nothing was sent."

    def _apply_after(self, steps: list[dict]) -> None:
        """Book-keeping once a message has actually gone out."""
        for step in steps:
            kind = step.get("type")
            if kind == "invoice_sent":
                self.db.update_invoice(step["invoice_id"], sent_at=now_iso())
            elif kind == "invoice_reminder":
                self.db.record_invoice_reminder(step["invoice_id"])
            elif kind == "rehab_sent":
                self.db.record_rehab_sent(step["horse_id"], step["title"], step["content"],
                                          step.get("template_id"))
            elif kind == "appointment_reminder":
                self.db.update_appointment(step["appointment_id"], reminder_sent_at=now_iso())
