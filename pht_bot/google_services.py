"""Gmail, Google Calendar and Google Drive access for the business Google Workspace account."""

import base64
import json
import mimetypes
import re
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
    # drive.file only lets the bot see files and folders it created itself.
    "https://www.googleapis.com/auth/drive.file",
]

ROOT_FOLDER = "Performance Horse Therapy"


def load_credentials(token_json: str, token_file: Path) -> Credentials | None:
    info = None
    if token_json:
        info = json.loads(token_json)
    elif token_file.exists():
        info = json.loads(token_file.read_text())
    if not info:
        return None
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return creds


class GoogleServices:
    def __init__(self, creds: Credentials, sender_address: str, sender_name: str):
        self.gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sender_address = sender_address
        self.sender_name = sender_name
        self._folder_cache: dict[tuple[str, str], str] = {}

    # ---- Gmail ---------------------------------------------------------
    def send_email(self, to: str, subject: str, body: str, attachments: list[str] | None = None,
                   reply_to_message_id: str | None = None, thread_id: str | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = f"{self.sender_name} <{self.sender_address}>" if self.sender_address else self.sender_name
        msg["Subject"] = subject
        if reply_to_message_id:
            msg["In-Reply-To"] = reply_to_message_id
            msg["References"] = reply_to_message_id
        msg.set_content(body)
        for path in attachments or []:
            p = Path(path)
            mime, _ = mimetypes.guess_type(p.name)
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_json = {"raw": raw}
        if thread_id:
            body_json["threadId"] = thread_id
        sent = self.gmail.users().messages().send(userId="me", body=body_json).execute()
        return sent["id"]

    def search_inbox(self, query: str = "in:inbox", max_results: int = 10) -> list[dict]:
        listing = self.gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        results = []
        for ref in listing.get("messages", []):
            m = self.gmail.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
            results.append({
                "id": m["id"], "thread_id": m["threadId"], "from": headers.get("From"),
                "subject": headers.get("Subject"), "date": headers.get("Date"),
                "snippet": m.get("snippet"), "unread": "UNREAD" in m.get("labelIds", []),
            })
        return results

    def read_email(self, message_id: str) -> dict:
        m = self.gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
        return {
            "id": m["id"], "thread_id": m["threadId"], "from": headers.get("From"),
            "to": headers.get("To"), "subject": headers.get("Subject"), "date": headers.get("Date"),
            "message_id_header": headers.get("Message-ID") or headers.get("Message-Id"),
            "body": _plain_text(m["payload"])[:20000],
        }

    # ---- Calendar ------------------------------------------------------
    def create_event(self, summary: str, start: datetime, end: datetime, location: str | None,
                     description: str | None, timezone: str) -> str:
        event = {
            "summary": summary,
            "location": location or "",
            "description": description or "",
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        }
        created = self.calendar.events().insert(calendarId="primary", body=event).execute()
        return created["id"]

    def update_event(self, event_id: str, timezone: str, **changes) -> None:
        body = {}
        for key in ("summary", "location", "description"):
            if key in changes and changes[key] is not None:
                body[key] = changes[key]
        for key in ("start", "end"):
            if changes.get(key) is not None:
                body[key] = {"dateTime": changes[key].isoformat(), "timeZone": timezone}
        if body:
            self.calendar.events().patch(calendarId="primary", eventId=event_id, body=body).execute()

    def delete_event(self, event_id: str) -> None:
        self.calendar.events().delete(calendarId="primary", eventId=event_id).execute()

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        resp = self.calendar.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=250).execute()
        events = []
        for e in resp.get("items", []):
            events.append({
                "id": e["id"], "summary": e.get("summary", "(no title)"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
                "location": e.get("location"),
            })
        return events

    # ---- Drive ---------------------------------------------------------
    def ensure_folder(self, *path: str) -> str:
        """Return the id of ROOT_FOLDER/path..., creating folders as needed."""
        parent = "root"
        for name in (ROOT_FOLDER, *path):
            key = (parent, name)
            if key not in self._folder_cache:
                safe = name.replace("\\", "\\\\").replace("'", "\\'")
                q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
                     f"and '{parent}' in parents and trashed = false")
                found = self.drive.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
                if found:
                    self._folder_cache[key] = found[0]["id"]
                else:
                    created = self.drive.files().create(
                        body={"name": name, "mimeType": "application/vnd.google-apps.folder",
                              "parents": [parent]}, fields="id").execute()
                    self._folder_cache[key] = created["id"]
            parent = self._folder_cache[key]
        return parent

    def upload_file(self, local_path: str, folder_id: str, name: str | None = None) -> str:
        p = Path(local_path)
        mime, _ = mimetypes.guess_type(p.name)
        media = MediaFileUpload(str(p), mimetype=mime or "application/octet-stream", resumable=False)
        created = self.drive.files().create(
            body={"name": name or p.name, "parents": [folder_id]}, media_body=media, fields="id").execute()
        return created["id"]


def _plain_text(payload: dict) -> str:
    """Pull the readable text out of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    parts = payload.get("parts", [])
    for part in parts:
        text = _plain_text(part)
        if text:
            return text
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", html)
    return ""
