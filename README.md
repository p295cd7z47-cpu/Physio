# Performance Horse Therapy – business assistant

A private Telegram bot for running an equine physiotherapy practice. You message it in plain English and it:

- **Keeps your client list**: owners (who pay) and their horses, including vet details and vet consent.
- **Runs your diary**: books, moves and cancels appointments in Google Calendar. Clients can't book themselves.
- **Does invoices**: creates PDF invoices per client (UK, not VAT registered), saves them to Google Drive, and emails or texts them.
- **Chases payment**: every morning it checks for overdue invoices and prepares polite reminder texts.
- **Sends appointment reminders**: every evening it prepares reminder texts for tomorrow's appointments.
- **Files report sheets**: send a photo of a paper sheet and it types it up and files the photo in the horse's Drive folder.
- **Handles rehab programmes**: keeps a library of templates, personalises one for a horse, and emails it as a PDF.
- **Handles email**: reads the business inbox, and drafts and sends emails and replies.

**Nothing goes to a client without your OK.** Every email and text appears in Telegram with **✅ Send** and **✖️ Cancel** buttons.

Things you can say:

> New client: Sarah Jones, 07700 900123, sarah@example.com, Oak Farm, Long Melford. Horse Bella, 12yo ISH, eventing, vet Dr Patel at Oakfield Vets, consent given today
>
> Book Bella in for Tuesday at 10
>
> What have I got on this week?
>
> Invoice Sarah £65 for Bella's session today and email it to her
>
> Who owes me money?
>
> *(photo of a report sheet)* This is Bella's from this morning
>
> Save this as a template called "Hind limb strengthening – 4 week": …
>
> Send Bella the kissing spine programme, but walk only in week 1
>
> Any new emails from clients?

---

## Setting it up

There are five accounts to create, then the bot gets put online. Steps 3 and 6 are the technical ones, and Claude can walk you through them.

### 1. Telegram bot (5 minutes, free)
1. Install **Telegram** on your phone.
2. Search for **@BotFather**, send `/newbot`, and follow the prompts. For example, name it *Performance Horse Therapy Assistant* with username `pht_assistant_bot` (the username must end in `bot`).
3. BotFather gives you a **token** that looks like `123456:ABC-…`. That's your `TELEGRAM_BOT_TOKEN`. Keep it secret.

### 2. Claude API key (5 minutes, pay as you go)
1. Go to **console.anthropic.com** and sign up with your work email.
2. Under **Billing**, add a card and buy some credit. £10–20 lasts a long time for one person. Set a **monthly spend limit** too.
3. Under **API keys**, create a key. That's your `ANTHROPIC_API_KEY`.

### 3. Connect Google (15 minutes, free)
Do this while signed in to your **work** Google account (e.g. hello@performancehorsetherapy.co.uk).
1. Go to **console.cloud.google.com** and create a project called *PHT Assistant*.
2. Under **APIs & Services → Library**, enable the **Gmail API**, **Google Calendar API** and **Google Drive API**.
3. Under **APIs & Services → OAuth consent screen**, choose **Internal**, fill in the app name and your email, then save.
4. Under **APIs & Services → Credentials → Create credentials → OAuth client ID**, choose **Desktop app** and download the JSON file.
5. On a computer with Python installed, run:
   ```
   pip install -r requirements.txt
   python scripts/google_login.py path/to/downloaded.json
   ```
   A browser opens. Sign in with your work account and allow access. The script prints a long line of text, which is your `GOOGLE_TOKEN_JSON`.

The bot can only see Drive files it created itself (in a **Performance Horse Therapy** folder). It can't see the rest of your Drive.

### 4. Twilio, for text messages (15 minutes, about £1/month plus about 4p per text)
1. Sign up at **twilio.com** and upgrade from the trial (trial accounts can only text your own number).
2. Choose how texts appear to clients:
   - **A sender name**, e.g. `PerfHorse` (up to 11 characters). It's quick to set up, but clients can't reply.
   - **A UK mobile number** from Twilio. Clients can reply, but UK numbers need proof of address.
3. From the Twilio console, copy the **Account SID** and **Auth Token**. Your sender name or number is `TWILIO_FROM`.

### 5. Your business details
Copy `.env.example` to `.env` and fill it in: business name, your name, address, bank details for invoices, and the keys from steps 1–4.

### 6. Put it online (about £5/month)
The bot needs to run all the time. The simplest option is **Railway** (railway.app):
1. Sign in with GitHub and choose **New project → Deploy from GitHub repo**, then pick this repository. It builds from the `Dockerfile`.
2. Add a **Volume** mounted at `/data`. This is where your client records live, so don't skip it.
3. Under **Variables**, add everything from your `.env` file.
4. Deploy. In Telegram, open your bot and send `/start`. It replies with your Telegram id. Add that as `OWNER_TELEGRAM_ID`, then redeploy.

Any always-on server that runs Docker works too (Fly.io, a small VPS, or a Raspberry Pi at home).

---

## Day to day

| Command | What it does |
|---|---|
| `/start` | Help, and a list of anything not yet set up |
| `/new` | Start a fresh conversation (it also does this by itself after 3 hours' quiet) |
| `/pending` | Show messages still waiting for your Send/Cancel |

**Automatic jobs** (times can be changed in the settings):
- **09:00**: overdue invoices get reminder texts prepared (at most one per invoice every 7 days).
- **18:00**: a list of tomorrow's appointments, plus a reminder text for each owner.
- **23:30**: your records are backed up to Google Drive → *Performance Horse Therapy / Backups*.

## Good to know
- **Only you can use it.** The bot ignores everyone except your Telegram account.
- **Vet consent:** under the Veterinary Surgeons Act, you need the vet's consent before treating. The bot records it per horse and reminds you if it's missing.
- **Data protection:** you're holding clients' personal details. Check whether you need to pay the ICO data protection fee (ico.org.uk). Most small businesses do, and it's about £40–60 a year.
- **Running costs:** roughly £5 for hosting, a few pounds for Claude, £1 for Twilio plus about 4p per text. That's about £10–20 a month.

## For developers
```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                         # tests use fakes – nothing is sent
python -m pht_bot.main         # run locally with a filled-in .env
```
- Code lives in `pht_bot/`. `tools.py` has everything the assistant can do. `agent.py` is the Claude loop. `main.py` is the Telegram side and the daily jobs. `actions.py` holds outgoing messages until they're approved.
- Data is stored in SQLite at `$DATA_DIR/pht.sqlite3`, with files under `$DATA_DIR/files`.
