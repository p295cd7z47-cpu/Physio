"""SQLite storage for clients, horses, bookings, invoices, rehab programmes and reports.

Money is stored as whole pence to avoid rounding errors.
"""

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    address TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    name TEXT NOT NULL,
    breed TEXT,
    age TEXT,
    discipline TEXT,
    yard_address TEXT,
    vet_name TEXT,
    vet_practice TEXT,
    vet_consent_date TEXT,
    notes TEXT,
    drive_folder_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    location TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    calendar_event_id TEXT,
    reminder_sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL UNIQUE,
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    total_pence INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid',
    paid_date TEXT,
    payment_method TEXT,
    notes TEXT,
    pdf_path TEXT,
    drive_file_id TEXT,
    sent_at TEXT,
    last_reminder_at TEXT,
    reminder_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    horse_id INTEGER REFERENCES horses(id),
    appointment_id INTEGER REFERENCES appointments(id),
    description TEXT NOT NULL,
    amount_pence INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rehab_templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    summary TEXT,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rehab_sent (
    id INTEGER PRIMARY KEY,
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    template_id INTEGER REFERENCES rehab_templates(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    appointment_id INTEGER REFERENCES appointments(id),
    visit_date TEXT NOT NULL,
    summary TEXT,
    transcription TEXT,
    drive_file_ids TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    local_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    original_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    preview TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    announced INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def pounds_to_pence(amount: float | int | str) -> int:
    return int(round(float(str(amount).replace("£", "").replace(",", "")) * 100))


def pence_to_str(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def backup_to(self, target: Path) -> None:
        dest = sqlite3.connect(str(target))
        with dest:
            self.conn.backup(dest)
        dest.close()

    # ---- helpers -------------------------------------------------------
    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _insert(self, table: str, values: dict) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        self.conn.commit()
        return cur.lastrowid

    def _update(self, table: str, row_id: int, values: dict) -> None:
        if not values:
            return
        sets = ", ".join(f"{c} = ?" for c in values)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))
        self.conn.commit()

    # ---- owners & horses ----------------------------------------------
    OWNER_FIELDS = ("name", "phone", "email", "address", "notes")
    HORSE_FIELDS = ("name", "breed", "age", "discipline", "yard_address", "vet_name",
                    "vet_practice", "vet_consent_date", "notes", "drive_folder_id", "owner_id")

    def add_owner(self, **fields: Any) -> int:
        values = {k: fields.get(k) for k in self.OWNER_FIELDS}
        values["created_at"] = now_iso()
        return self._insert("owners", values)

    def update_owner(self, owner_id: int, **fields: Any) -> None:
        self._update("owners", owner_id, {k: v for k, v in fields.items() if k in self.OWNER_FIELDS})

    def get_owner(self, owner_id: int) -> dict | None:
        return self._one("SELECT * FROM owners WHERE id = ?", (owner_id,))

    def add_horse(self, owner_id: int, **fields: Any) -> int:
        values = {k: fields.get(k) for k in self.HORSE_FIELDS if k != "owner_id"}
        values["owner_id"] = owner_id
        values["created_at"] = now_iso()
        return self._insert("horses", values)

    def update_horse(self, horse_id: int, **fields: Any) -> None:
        self._update("horses", horse_id, {k: v for k, v in fields.items() if k in self.HORSE_FIELDS})

    def get_horse(self, horse_id: int) -> dict | None:
        return self._one(
            "SELECT h.*, o.name AS owner_name, o.phone AS owner_phone, o.email AS owner_email "
            "FROM horses h JOIN owners o ON o.id = h.owner_id WHERE h.id = ?",
            (horse_id,),
        )

    def horses_for_owner(self, owner_id: int) -> list[dict]:
        return self._all("SELECT * FROM horses WHERE owner_id = ? ORDER BY name", (owner_id,))

    def search_clients(self, query: str = "") -> list[dict]:
        """Owners matching by owner name, phone, email or any of their horses' names."""
        q = f"%{query.strip()}%"
        owners = self._all(
            "SELECT DISTINCT o.* FROM owners o LEFT JOIN horses h ON h.owner_id = o.id "
            "WHERE o.name LIKE ? OR o.phone LIKE ? OR o.email LIKE ? OR h.name LIKE ? "
            "ORDER BY o.name LIMIT 50",
            (q, q, q, q),
        )
        for o in owners:
            o["horses"] = self.horses_for_owner(o["id"])
        return owners

    # ---- appointments --------------------------------------------------
    def add_appointment(self, horse_id: int, start: str, end: str, location: str | None,
                        notes: str | None, calendar_event_id: str | None) -> int:
        return self._insert("appointments", {
            "horse_id": horse_id, "start": start, "end": end, "location": location,
            "notes": notes, "calendar_event_id": calendar_event_id, "status": "booked",
            "created_at": now_iso(),
        })

    def update_appointment(self, appt_id: int, **fields: Any) -> None:
        allowed = ("start", "end", "location", "notes", "status", "calendar_event_id", "reminder_sent_at")
        self._update("appointments", appt_id, {k: v for k, v in fields.items() if k in allowed})

    def get_appointment(self, appt_id: int) -> dict | None:
        return self._one(
            "SELECT a.*, h.name AS horse_name, h.owner_id, o.name AS owner_name, o.phone AS owner_phone, "
            "o.email AS owner_email FROM appointments a JOIN horses h ON h.id = a.horse_id "
            "JOIN owners o ON o.id = h.owner_id WHERE a.id = ?",
            (appt_id,),
        )

    def appointments_between(self, start: str, end: str, include_cancelled: bool = False) -> list[dict]:
        status_clause = "" if include_cancelled else "AND a.status != 'cancelled'"
        return self._all(
            "SELECT a.*, h.name AS horse_name, h.owner_id, o.name AS owner_name, o.phone AS owner_phone, "
            "o.email AS owner_email FROM appointments a JOIN horses h ON h.id = a.horse_id "
            f"JOIN owners o ON o.id = h.owner_id WHERE a.start >= ? AND a.start < ? {status_clause} "
            "ORDER BY a.start",
            (start, end),
        )

    def appointments_for_horse(self, horse_id: int) -> list[dict]:
        return self._all("SELECT * FROM appointments WHERE horse_id = ? ORDER BY start DESC", (horse_id,))

    # ---- invoices ------------------------------------------------------
    def next_invoice_number(self, prefix: str = "PHT") -> str:
        row = self._one("SELECT COUNT(*) AS n FROM invoices")
        n = (row["n"] if row else 0) + 1
        while self._one("SELECT id FROM invoices WHERE number = ?", (f"{prefix}-{n:04d}",)):
            n += 1
        return f"{prefix}-{n:04d}"

    def create_invoice(self, owner_id: int, items: list[dict], issue_date: date, due_date: date,
                       notes: str | None = None) -> dict:
        number = self.next_invoice_number()
        total = sum(i["amount_pence"] for i in items)
        invoice_id = self._insert("invoices", {
            "number": number, "owner_id": owner_id, "issue_date": issue_date.isoformat(),
            "due_date": due_date.isoformat(), "total_pence": total, "status": "unpaid",
            "notes": notes, "created_at": now_iso(),
        })
        for item in items:
            self._insert("invoice_items", {
                "invoice_id": invoice_id, "horse_id": item.get("horse_id"),
                "appointment_id": item.get("appointment_id"), "description": item["description"],
                "amount_pence": item["amount_pence"],
            })
        return self.get_invoice(number)

    def get_invoice(self, number: str) -> dict | None:
        inv = self._one(
            "SELECT i.*, o.name AS owner_name, o.phone AS owner_phone, o.email AS owner_email, "
            "o.address AS owner_address FROM invoices i JOIN owners o ON o.id = i.owner_id "
            "WHERE i.number = ? COLLATE NOCASE",
            (number.strip(),),
        )
        if inv:
            inv["items"] = self._all(
                "SELECT it.*, h.name AS horse_name FROM invoice_items it "
                "LEFT JOIN horses h ON h.id = it.horse_id WHERE it.invoice_id = ? ORDER BY it.id",
                (inv["id"],),
            )
        return inv

    def update_invoice(self, invoice_id: int, **fields: Any) -> None:
        allowed = ("status", "paid_date", "payment_method", "pdf_path", "drive_file_id", "sent_at",
                   "last_reminder_at", "reminder_count", "notes")
        self._update("invoices", invoice_id, {k: v for k, v in fields.items() if k in allowed})

    def record_invoice_reminder(self, invoice_id: int) -> None:
        self.conn.execute(
            "UPDATE invoices SET last_reminder_at = ?, reminder_count = reminder_count + 1 WHERE id = ?",
            (now_iso(), invoice_id))
        self.conn.commit()

    def list_invoices(self, status: str = "unpaid", owner_id: int | None = None,
                      today: date | None = None) -> list[dict]:
        today = today or date.today()
        clauses, params = [], []
        if status == "overdue":
            clauses.append("i.status = 'unpaid' AND i.due_date < ?")
            params.append(today.isoformat())
        elif status in ("unpaid", "paid", "void"):
            clauses.append("i.status = ?")
            params.append(status)
        if owner_id:
            clauses.append("i.owner_id = ?")
            params.append(owner_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._all(
            "SELECT i.*, o.name AS owner_name, o.phone AS owner_phone, o.email AS owner_email "
            f"FROM invoices i JOIN owners o ON o.id = i.owner_id {where} ORDER BY i.issue_date DESC, i.id DESC",
            tuple(params),
        )

    def invoices_needing_reminder(self, today: date, gap_days: int) -> list[dict]:
        """Overdue invoices that haven't had a reminder in the last `gap_days` days."""
        result = []
        for inv in self.list_invoices("overdue", today=today):
            last = inv["last_reminder_at"]
            if last and (today - date.fromisoformat(last[:10])).days < gap_days:
                continue
            result.append(inv)
        return result

    # ---- rehab ---------------------------------------------------------
    def save_rehab_template(self, name: str, content: str, summary: str | None = None) -> int:
        existing = self._one("SELECT id FROM rehab_templates WHERE name = ?", (name,))
        if existing:
            self._update("rehab_templates", existing["id"],
                         {"content": content, "summary": summary, "updated_at": now_iso()})
            return existing["id"]
        return self._insert("rehab_templates", {
            "name": name, "summary": summary, "content": content, "updated_at": now_iso(),
        })

    def list_rehab_templates(self) -> list[dict]:
        return self._all("SELECT id, name, summary, updated_at FROM rehab_templates ORDER BY name")

    def get_rehab_template(self, name_or_id: str | int) -> dict | None:
        if str(name_or_id).isdigit():
            found = self._one("SELECT * FROM rehab_templates WHERE id = ?", (int(name_or_id),))
            if found:
                return found
        return (self._one("SELECT * FROM rehab_templates WHERE name = ?", (str(name_or_id),))
                or self._one("SELECT * FROM rehab_templates WHERE name LIKE ? ORDER BY name",
                             (f"%{name_or_id}%",)))

    def delete_rehab_template(self, template_id: int) -> None:
        self.conn.execute("UPDATE rehab_sent SET template_id = NULL WHERE template_id = ?", (template_id,))
        self.conn.execute("DELETE FROM rehab_templates WHERE id = ?", (template_id,))
        self.conn.commit()

    def record_rehab_sent(self, horse_id: int, title: str, content: str, template_id: int | None) -> int:
        return self._insert("rehab_sent", {
            "horse_id": horse_id, "template_id": template_id, "title": title,
            "content": content, "sent_at": now_iso(),
        })

    # ---- reports & uploads ----------------------------------------------
    def add_upload(self, local_path: str, mime_type: str, original_name: str | None = None) -> int:
        return self._insert("uploads", {
            "local_path": local_path, "mime_type": mime_type, "original_name": original_name,
            "created_at": now_iso(),
        })

    def get_upload(self, upload_id: int) -> dict | None:
        return self._one("SELECT * FROM uploads WHERE id = ?", (upload_id,))

    def add_report(self, horse_id: int, visit_date: str, summary: str, transcription: str,
                   drive_file_ids: list[str], appointment_id: int | None = None) -> int:
        return self._insert("reports", {
            "horse_id": horse_id, "appointment_id": appointment_id, "visit_date": visit_date,
            "summary": summary, "transcription": transcription,
            "drive_file_ids": json.dumps(drive_file_ids), "created_at": now_iso(),
        })

    def horse_history(self, horse_id: int) -> dict:
        return {
            "horse": self.get_horse(horse_id),
            "appointments": self.appointments_for_horse(horse_id)[:20],
            "reports": self._all(
                "SELECT id, visit_date, summary, transcription FROM reports WHERE horse_id = ? "
                "ORDER BY visit_date DESC LIMIT 10", (horse_id,)),
            "rehab_sent": self._all(
                "SELECT id, title, sent_at FROM rehab_sent WHERE horse_id = ? ORDER BY sent_at DESC LIMIT 10",
                (horse_id,)),
            "invoices": self._all(
                "SELECT DISTINCT i.number, i.issue_date, i.total_pence, i.status FROM invoices i "
                "JOIN invoice_items it ON it.invoice_id = i.id WHERE it.horse_id = ? "
                "ORDER BY i.issue_date DESC LIMIT 10", (horse_id,)),
        }

    # ---- pending actions (things waiting for the owner's OK) -------------
    def add_pending(self, kind: str, payload: dict, preview: str) -> int:
        return self._insert("pending_actions", {
            "kind": kind, "payload": json.dumps(payload), "preview": preview,
            "status": "pending", "created_at": now_iso(),
        })

    def get_pending(self, action_id: int) -> dict | None:
        row = self._one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
        if row:
            row["payload"] = json.loads(row["payload"])
        return row

    def list_pending(self, only_unannounced: bool = False) -> list[dict]:
        sql = "SELECT * FROM pending_actions WHERE status = 'pending'"
        if only_unannounced:
            sql += " AND announced = 0"
        rows = self._all(sql + " ORDER BY id")
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def mark_announced(self, action_id: int) -> None:
        self._update("pending_actions", action_id, {"announced": 1})

    def finish_pending(self, action_id: int, status: str, result: str = "") -> None:
        self._update("pending_actions", action_id, {"status": status, "result": result})

    def has_pending_ref(self, ref: str) -> bool:
        """True if an action tagged with `ref` (e.g. "invoice_reminder:12") is still waiting."""
        return any(row["payload"].get("ref") == ref for row in self.list_pending())
