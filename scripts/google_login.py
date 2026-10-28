"""One-off: sign the bot in to your Google Workspace account.

Run this on your own computer (it opens a browser window):

    python scripts/google_login.py path/to/client_secret.json

It saves data/google_token.json and prints the value to paste into GOOGLE_TOKEN_JSON
on your hosting service.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from pht_bot.google_services import SCOPES  # noqa: E402


def main() -> None:
    secret = Path(sys.argv[1] if len(sys.argv) > 1 else "client_secret.json")
    if not secret.exists():
        raise SystemExit(f"Can't find {secret}. Download it from Google Cloud (see README, step 3).")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    out = Path("data/google_token.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(creds.to_json())
    print(f"\nSigned in. Saved {out}.")
    print("\nIf you host the bot online, copy everything on the next line into GOOGLE_TOKEN_JSON:\n")
    print(creds.to_json())


if __name__ == "__main__":
    main()
