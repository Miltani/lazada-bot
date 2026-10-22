"""Console + optional Telegram notifications."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import urllib.parse
import urllib.request

log = logging.getLogger("lazada_bot")


class Notifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str, urgent: bool = False) -> None:
        log.warning(message) if urgent else log.info(message)
        if urgent:
            sys.stdout.write("\a")  # terminal bell
            sys.stdout.flush()
        if self.bot_token and self.chat_id:
            try:
                data = urllib.parse.urlencode({
                    "chat_id": self.chat_id,
                    "text": f"[lazada-bot {dt.datetime.now():%H:%M:%S}] {message}",
                }).encode()
                url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                with urllib.request.urlopen(url, data=data, timeout=10) as resp:
                    json.load(resp)
            except Exception as exc:  # never let a notification failure stop buying
                log.error("Telegram notification failed: %s", exc)
