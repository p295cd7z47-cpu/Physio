"""Settings, read from environment variables (or a .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    # Telegram
    telegram_token: str
    owner_telegram_id: int | None

    # Claude
    anthropic_api_key: str
    model: str

    # Business details printed on invoices and used in messages
    business_name: str
    owner_name: str
    business_address: str
    business_phone: str
    business_email: str
    bank_account_name: str
    bank_sort_code: str
    bank_account_number: str
    payment_terms_days: int
    default_session_minutes: int
    timezone: ZoneInfo

    # Twilio (text messages)
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from: str

    # Google
    google_token_json: str
    google_token_file: Path

    # Storage
    data_dir: Path

    # Daily jobs (24h clock, local time)
    overdue_check_time: str
    appointment_reminder_time: str
    backup_time: str
    reminder_gap_days: int
    appointment_reminders_enabled: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pht.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)


def load_settings() -> Settings:
    load_dotenv()
    owner_id = _get("OWNER_TELEGRAM_ID")
    data_dir = Path(_get("DATA_DIR", "data"))
    return Settings(
        telegram_token=_get("TELEGRAM_BOT_TOKEN"),
        owner_telegram_id=int(owner_id) if owner_id else None,
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        model=_get("CLAUDE_MODEL", "claude-opus-5-5"),
        business_name=_get("BUSINESS_NAME", "Performance Horse Therapy"),
        owner_name=_get("OWNER_NAME"),
        business_address=_get("BUSINESS_ADDRESS").replace("\\n", "\n"),
        business_phone=_get("BUSINESS_PHONE"),
        business_email=_get("BUSINESS_EMAIL"),
        bank_account_name=_get("BANK_ACCOUNT_NAME"),
        bank_sort_code=_get("BANK_SORT_CODE"),
        bank_account_number=_get("BANK_ACCOUNT_NUMBER"),
        payment_terms_days=int(_get("PAYMENT_TERMS_DAYS", "14")),
        default_session_minutes=int(_get("DEFAULT_SESSION_MINUTES", "60")),
        timezone=ZoneInfo(_get("TIMEZONE", "Europe/London")),
        twilio_account_sid=_get("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_get("TWILIO_AUTH_TOKEN"),
        twilio_from=_get("TWILIO_FROM"),
        google_token_json=_get("GOOGLE_TOKEN_JSON"),
        google_token_file=Path(_get("GOOGLE_TOKEN_FILE", str(data_dir / "google_token.json"))),
        data_dir=data_dir,
        overdue_check_time=_get("OVERDUE_CHECK_TIME", "09:00"),
        appointment_reminder_time=_get("APPOINTMENT_REMINDER_TIME", "18:00"),
        backup_time=_get("BACKUP_TIME", "23:30"),
        reminder_gap_days=int(_get("REMINDER_GAP_DAYS", "7")),
        appointment_reminders_enabled=_get("APPOINTMENT_REMINDERS", "on").lower() in ("on", "true", "yes", "1"),
    )
