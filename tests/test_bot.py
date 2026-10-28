import json
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pht_bot.actions import ActionQueue
from pht_bot.agent import Agent
from pht_bot.config import Settings
from pht_bot.db import Database, pence_to_str, pounds_to_pence
from pht_bot.main import _chunks
from pht_bot.sms import normalise_uk_number
from pht_bot.tools import TOOLS, Toolbox, ToolError


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        telegram_token="x", owner_telegram_id=1, anthropic_api_key="k", model="claude-opus-5-5",
        business_name="Performance Horse Therapy", owner_name="Jo Rider",
        business_address="1 Stable Lane\nNewmarket", business_phone="07700 900000",
        business_email="hello@example.co.uk", bank_account_name="Jo Rider", bank_sort_code="12-34-56",
        bank_account_number="12345678", payment_terms_days=14, default_session_minutes=60,
        timezone=ZoneInfo("Europe/London"), twilio_account_sid="", twilio_auth_token="", twilio_from="",
        google_token_json="", google_token_file=tmp_path / "token.json", data_dir=tmp_path,
        overdue_check_time="09:00", appointment_reminder_time="18:00", backup_time="23:30",
        reminder_gap_days=7, appointment_reminders_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


class FakeGoogle:
    def __init__(self):
        self.sent, self.events, self.uploads = [], {}, []

    def send_email(self, to, subject, body, attachments=None, reply_to_message_id=None, thread_id=None):
        self.sent.append({"to": to, "subject": subject, "body": body, "attachments": attachments})
        return "msg1"

    def create_event(self, summary, start, end, location, description, timezone):
        eid = f"ev{len(self.events) + 1}"
        self.events[eid] = {"summary": summary, "start": start, "end": end}
        return eid

    def update_event(self, event_id, timezone, **changes):
        self.events[event_id].update({k: v for k, v in changes.items() if v is not None})

    def delete_event(self, event_id):
        del self.events[event_id]

    def list_events(self, start, end):
        return [{"id": "personal", "summary": "Dentist", "start": start.isoformat(), "end": end.isoformat()}]

    def ensure_folder(self, *path):
        return "/".join(path)

    def upload_file(self, local_path, folder_id, name=None):
        self.uploads.append((local_path, folder_id, name))
        return f"drive{len(self.uploads)}"


class FakeSms:
    def __init__(self):
        self.sent = []

    def send(self, to, body):
        self.sent.append((to, body))
        return "SM1"


@pytest.fixture
def env(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(":memory:")
    google, sms = FakeGoogle(), FakeSms()
    queue = ActionQueue(db, google, sms)
    toolbox = Toolbox(db, settings, queue, google)
    owner_id = db.add_owner(name="Sarah Jones", phone="07700 900123", email="sarah@example.com",
                            address="Oak Farm\nSuffolk")
    horse_id = db.add_horse(owner_id, name="Bella", yard_address="Oak Farm")
    return SimpleNamespace(settings=settings, db=db, google=google, sms=sms, queue=queue, tools=toolbox,
                           owner_id=owner_id, horse_id=horse_id)


def test_money_helpers():
    assert pounds_to_pence("£65.50") == 6550
    assert pounds_to_pence(45) == 4500
    assert pence_to_str(123456) == "£1,234.56"


def test_uk_numbers():
    assert normalise_uk_number("07700 900123") == "+447700900123"
    assert normalise_uk_number("+44 7700 900123") == "+447700900123"
    assert normalise_uk_number("0044 7700 900123") == "+447700900123"


def test_tool_schemas_match_handlers(env):
    for tool in TOOLS:
        assert hasattr(env.tools, f"t_{tool['name']}"), tool["name"]
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])


async def test_search_by_horse_name(env):
    out = json.loads(await env.tools.run("find_clients", {"query": "bell"}))
    assert out[0]["name"] == "Sarah Jones"
    assert out[0]["horses"][0]["name"] == "Bella"


async def test_invoice_flow_needs_approval(env):
    msg = await env.tools.run("create_invoice", {
        "owner_id": env.owner_id,
        "items": [{"description": "Physio session", "amount_pounds": 65, "horse_id": env.horse_id}]})
    assert "PHT-0001" in msg and "£65.00" in msg
    inv = env.db.get_invoice("PHT-0001")
    assert inv["pdf_path"].endswith("PHT-0001.pdf")
    with open(inv["pdf_path"], "rb") as f:
        assert f.read(4) == b"%PDF"
    assert inv["drive_file_id"]

    await env.tools.run("send_invoice", {"invoice_number": "pht-0001", "via": ["email", "text"]})
    assert env.google.sent == [] and env.sms.sent == []  # nothing goes out before approval
    pending = env.db.list_pending()
    assert [p["kind"] for p in pending] == ["email", "sms"]
    assert "12-34-56" in pending[1]["payload"]["body"]

    assert "sent" in await env.queue.approve(pending[0]["id"])
    assert env.google.sent[0]["to"] == "sarah@example.com"
    assert env.db.get_invoice("PHT-0001")["sent_at"]
    assert "already" in await env.queue.approve(pending[0]["id"])  # no double send
    assert env.queue.cancel(pending[1]["id"]).startswith("Cancelled")
    assert env.sms.sent == []

    await env.tools.run("mark_invoice_paid", {"invoice_number": "PHT-0001"})
    assert env.db.get_invoice("PHT-0001")["status"] == "paid"


async def test_invoice_rejects_other_owners_horse(env):
    other = env.db.add_owner(name="Tom Smith")
    with pytest.raises(ToolError):
        await env.tools.run("create_invoice", {
            "owner_id": other, "items": [{"description": "x", "amount_pounds": 10, "horse_id": env.horse_id}]})


async def test_overdue_reminders(env):
    today = date.today()
    env.db.create_invoice(env.owner_id, [{"description": "Session", "amount_pence": 6000}],
                          today - timedelta(days=30), today - timedelta(days=16))
    env.db.create_invoice(env.owner_id, [{"description": "Session", "amount_pence": 6000}],
                          today, today + timedelta(days=14))
    due = env.db.invoices_needing_reminder(today, 7)
    assert [i["number"] for i in due] == ["PHT-0001"]

    out = env.tools.queue_reminders(due, today)
    assert "PHT-0001" in out
    again = env.tools.queue_reminders(due, today)
    assert "already waiting" in again
    action = env.db.list_pending()[0]
    assert "16 days ago" in action["payload"]["body"]
    await env.queue.approve(action["id"])
    assert env.sms.sent[0][0] == "07700 900123"
    assert env.db.invoices_needing_reminder(today, 7) == []  # reminded today
    assert env.db.get_invoice("PHT-0001")["reminder_count"] == 1


async def test_booking_and_diary(env):
    msg = await env.tools.run("book_appointment", {"horse_id": env.horse_id, "start": "2030-03-05T10:00"})
    assert "Booked Bella" in msg and "no vet consent" in msg
    appt = env.db.get_appointment(1)
    assert appt["location"] == "Oak Farm" and appt["end"] == "2030-03-05T11:00:00"
    assert env.google.events["ev1"]["start"].tzinfo is not None

    clash = await env.tools.run("book_appointment", {"horse_id": env.horse_id, "start": "2030-03-05T10:30"})
    assert "Overlaps" in clash

    await env.tools.run("reschedule_appointment", {"appointment_id": 1, "new_start": "2030-03-06 14:00"})
    assert env.db.get_appointment(1)["end"] == "2030-03-06T15:00:00"

    diary = json.loads(await env.tools.run("list_diary", {"start_date": "2030-03-06", "end_date": "2030-03-06"}))
    assert [a["appointment_id"] for a in diary["client_appointments"]] == [1]
    assert diary["other_calendar_events"][0]["summary"] == "Dentist"

    await env.tools.run("cancel_appointment", {"appointment_id": 1})
    assert "ev1" not in env.google.events


async def test_rehab_templates_and_sending(env):
    await env.tools.run("save_rehab_template", {"name": "Back – 6 week", "summary": "Kissing spine",
                                                "content": "# Week 1\n- Walk 10 mins in hand"})
    found = json.loads(await env.tools.run("get_rehab_template", {"name_or_id": "back"}))
    assert found["content"].startswith("# Week 1")
    await env.tools.run("send_rehab_programme", {"horse_id": env.horse_id, "title": "Bella – back rehab",
                                                 "content": found["content"], "template_id": found["id"]})
    action = env.db.list_pending()[0]
    assert action["payload"]["attachments"][0].endswith(".pdf")
    await env.queue.approve(action["id"])
    assert env.db.horse_history(env.horse_id)["rehab_sent"][0]["title"] == "Bella – back rehab"


async def test_save_report_uploads_photo(env, tmp_path):
    photo = tmp_path / "sheet.jpg"
    photo.write_bytes(b"\xff\xd8fake")
    uid = env.db.add_upload(str(photo), "image/jpeg", "sheet.jpg")
    msg = await env.tools.run("save_report", {"horse_id": env.horse_id, "visit_date": "2030-03-05",
                                              "summary": "Tight left lumbar", "transcription": "...",
                                              "upload_ids": [uid]})
    assert "1 file(s)" in msg
    assert env.google.uploads[0][1].endswith("Bella (#1)/Reports")


def test_chunks():
    assert _chunks("short") == ["short"]
    long_text = "\n".join(["line"] * 2000)
    parts = _chunks(long_text)
    assert all(len(p) <= 4000 for p in parts) and len(parts) > 1


# ---- agent loop with a fake Claude ---------------------------------------

def block(**kw):
    return SimpleNamespace(**kw)


class FakeClaude:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


async def test_agent_runs_tools_and_replies(env):
    claude = FakeClaude([
        block(stop_reason="tool_use", content=[
            block(type="text", text="Let me look."),
            block(type="tool_use", id="t1", name="find_clients", input={"query": "Bella"}),
            block(type="tool_use", id="t2", name="get_horse_history", input={"horse_id": 999}),
        ]),
        block(stop_reason="end_turn", content=[block(type="text", text="Bella belongs to Sarah Jones.")]),
    ])
    agent = Agent(env.settings, env.tools, client=claude)
    reply = await agent.handle(42, "Whose horse is Bella?")
    assert reply == "Let me look.\n\nBella belongs to Sarah Jones."

    second = claude.calls[1]
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # all results in one user message
    assert "Sarah Jones" in results[0]["content"] and not results[0].get("is_error")
    assert results[1]["is_error"] is True
    assert second["fallbacks"] == "default"
    assert "temperature" not in second
    assert "[Now:" in claude.calls[0]["messages"][0]["content"][-1]["text"]


async def test_agent_passes_on_approval_notes(env):
    claude = FakeClaude([block(stop_reason="end_turn", content=[block(type="text", text="Hi")]),
                         block(stop_reason="end_turn", content=[block(type="text", text="Great")])])
    agent = Agent(env.settings, env.tools, client=claude)
    await agent.handle(1, "hello")
    agent.add_note(1, "#3: Email sent to sarah@example.com ✅")
    await agent.handle(1, "thanks")
    last_user = claude.calls[1]["messages"][-1]["content"][-1]["text"]
    assert "\n[Since your last reply: #3: Email sent" in last_user
