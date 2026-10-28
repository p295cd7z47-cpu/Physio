"""The Claude conversation loop: turns the owner's messages into tool calls and replies."""

import logging
import time
from datetime import datetime

import anthropic

from .config import Settings
from .tools import TOOLS, Toolbox, ToolError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 25
IDLE_RESET_SECONDS = 3 * 60 * 60
MAX_HISTORY_MESSAGES = 80

SYSTEM_PROMPT = """You are the business assistant for {business}, an equine (horse) veterinary \
physiotherapy practice run by {owner}. You talk to {owner} through Telegram. They are often at a yard, \
on their phone, between horses – keep replies short, warm and practical, in British English. Use plain \
text with the odd emoji; no markdown tables or headings.

What you look after:
- Clients: horse owners (who pay) and their horses. Each horse has an id; invoices go to the owner.
- The diary: appointments live in Google Calendar. Clients can never book themselves – only {owner}, through you.
- Invoices: per client, in pounds. The business is not VAT registered, so never add VAT. Payment terms are \
{terms} days unless {owner} says otherwise.
- Payment chasing: polite reminder texts for overdue invoices.
- Report sheets: {owner} photographs paper treatment/assessment sheets. Transcribe them faithfully \
(mark anything you can't read as [illegible] – never guess clinical details), work out which horse it is, \
then save it with save_report including the upload ids.
- Rehab programmes: a library of reusable templates, personalised per horse and emailed as a PDF.
- Email: the business Gmail inbox – reading, drafting and replying.

How to work:
- Look things up rather than asking: use find_clients to get ids, list_diary before booking.
- If a name matches more than one client or horse, ask which one.
- When something is ambiguous and getting it wrong would matter (amounts, dates, who it's for), ask first. \
For small things, make a sensible choice and say what you did.
- Emails and texts to clients are never sent straight away. The send_* tools prepare them, and {owner} gets \
Send/Cancel buttons underneath your reply. Tell them it's ready to check; don't say it has been sent.
- Write client messages in a friendly, professional tone, signed off as {owner_first} at {business}.
- In the UK, a vet must agree before a physio treats an animal. If a horse has no vet consent recorded, \
mention it once when booking or invoicing – don't nag.
- Never invent prices: if you don't know what to charge, ask. Use the same prices {owner} has used before \
for the same kind of session when they say "the usual".
- Dates: today's date and time are given at the start of each message. "Tuesday" means the next Tuesday.
- Uploaded photos/files arrive with an upload id, e.g. [Upload #12]. Use those ids with save_report or send_email.
- Client records are confidential: only share a client's details with that client or their vet."""


class Conversation:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.notes: list[str] = []
        self.last_active = time.monotonic()

    def is_stale(self) -> bool:
        return (time.monotonic() - self.last_active > IDLE_RESET_SECONDS
                or len(self.messages) > MAX_HISTORY_MESSAGES)


class Agent:
    def __init__(self, settings: Settings, toolbox: Toolbox, client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.toolbox = toolbox
        self.client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
        owner = settings.owner_name or "the owner"
        self.system = SYSTEM_PROMPT.format(
            business=settings.business_name, owner=owner, owner_first=owner.split(" ")[0],
            terms=settings.payment_terms_days)
        self.conversations: dict[int, Conversation] = {}

    def add_note(self, chat_id: int, note: str) -> None:
        """Something that happened outside the chat (e.g. a message approved), passed on with the next turn."""
        convo = self.conversations.get(chat_id)
        if convo:
            convo.notes.append(note)

    def reset(self, chat_id: int) -> None:
        self.conversations.pop(chat_id, None)

    def _conversation(self, chat_id: int) -> Conversation:
        convo = self.conversations.get(chat_id)
        # Start afresh rather than trimming old turns: everything that matters is in the database.
        if convo is None or convo.is_stale():
            convo = Conversation()
            self.conversations[chat_id] = convo
        return convo

    async def handle(self, chat_id: int, text: str, attachments: list[dict] | None = None) -> str:
        """Process one message from the owner. `attachments` are ready-made image/document content blocks."""
        convo = self._conversation(chat_id)
        now = datetime.now(self.settings.timezone)
        if convo.notes:
            text = "[Since your last reply: " + "; ".join(convo.notes) + "]\n" + text
            convo.notes = []
        content: list[dict] = [*(attachments or []),
                               {"type": "text", "text": f"[Now: {now.strftime('%A %-d %B %Y, %H:%M')}]\n{text}"}]
        convo.messages.append({"role": "user", "content": content})

        replies: list[str] = []
        first_round = True
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self.client.beta.messages.create(
                    model=self.settings.model,
                    max_tokens=16000,
                    system=self.system,
                    tools=TOOLS,
                    messages=convo.messages,
                    output_config={"effort": "medium"},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIError as exc:
                log.exception("Claude API error")
                self._recover(chat_id, convo, first_round)
                if isinstance(exc, anthropic.RateLimitError):
                    problem = "I'm a bit overloaded right now – please try again in a minute."
                elif isinstance(exc, anthropic.APIConnectionError):
                    problem = "I can't reach Claude at the moment – please try again shortly."
                else:
                    problem = "Something went wrong talking to Claude. Please try again."
                if not first_round:
                    problem += " I'd already started on it, so ask me what got done before repeating it."
                return "\n\n".join([*replies, problem])
            first_round = False

            convo.messages.append({"role": "assistant", "content": response.content})
            convo.last_active = time.monotonic()
            replies.extend(b.text for b in response.content if b.type == "text" and b.text.strip())

            if response.stop_reason == "refusal":
                return "\n\n".join(replies) or "Sorry, I can't help with that one."
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "max_tokens":
                replies.append("(I ran out of space for that reply – ask me to carry on.)")
                break
            tool_calls = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_calls:
                break

            results = []
            for call in tool_calls:
                results.append(await self._run_tool(call))
            convo.messages.append({"role": "user", "content": results})
        else:
            replies.append("(That took more steps than I expected – tell me if anything is left to do.)")

        return "\n\n".join(replies).strip() or "Done."

    def _recover(self, chat_id: int, convo: Conversation, first_round: bool) -> None:
        """After an API error, drop the unanswered message, or start afresh if a tool round was in flight."""
        if first_round:
            convo.messages.pop()
        else:
            self.reset(chat_id)

    async def _run_tool(self, call) -> dict:
        args = call.input if isinstance(call.input, dict) else {}
        log.info("Tool %s %s", call.name, args)
        try:
            output = await self.toolbox.run(call.name, args)
            return {"type": "tool_result", "tool_use_id": call.id, "content": output}
        except ToolError as exc:
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(exc), "is_error": True}
        except TypeError as exc:
            log.exception("Bad arguments for %s", call.name)
            return {"type": "tool_result", "tool_use_id": call.id,
                    "content": f"Invalid arguments: {exc}", "is_error": True}
        except Exception as exc:
            log.exception("Tool %s failed", call.name)
            return {"type": "tool_result", "tool_use_id": call.id,
                    "content": f"That failed: {exc}", "is_error": True}
