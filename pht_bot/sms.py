"""Text messages to clients via Twilio."""

import re

from twilio.rest import Client


def normalise_uk_number(number: str) -> str:
    """Turn '07700 900123' or '+44 7700 900123' into '+447700900123'."""
    digits = re.sub(r"[^\d+]", "", number or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0"):
        digits = "+44" + digits[1:]
    if digits and not digits.startswith("+"):
        digits = "+" + digits
    return digits


class SmsSender:
    def __init__(self, account_sid: str, auth_token: str, sender: str):
        self.client = Client(account_sid, auth_token)
        self.sender = sender

    def send(self, to: str, body: str) -> str:
        message = self.client.messages.create(to=normalise_uk_number(to), from_=self.sender, body=body)
        return message.sid
