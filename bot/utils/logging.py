"""Logging setup: rotating file + console, redaction, per-update context.

Three things the production bot needed and did not have:

* **rotation** — logs went to stdout only, so the supervisor file grew until the
  disk filled (and that same disk is where /tmp holds product images);
* **redaction** — the WooCommerce/WordPress credentials travel as query-string
  auth and inside URLs, so one logged exception could put a consumer secret in a
  plaintext log file;
* **context** — with several admins clicking at once, a bare
  «create_draft failed» is useless. Every line now carries the acting user and,
  inside a product flow, the current step.
"""
from __future__ import annotations

import contextvars
import os
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"

_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3

# Secrets that must never reach a log file (they appear in URLs and in
# exception messages raised by httpx).
_SECRETS = (
    # Telegram bot tokens: "<digits>:<43 url-safe chars>".
    re.compile(r"(?P<prefix>\d{5,14}:)(?P<token>[A-Za-z0-9_-]{30,})"),
    re.compile(r"(?i)(consumer_key|consumer_secret|api_key|token)=(?P<secret>[^&\s\"']+)"),
    re.compile(r"(?i)(Bearer\s+)(?P<secret>[A-Za-z0-9._\-]{12,})"),
)

_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("tisa_user", default="")


def set_current_user(user_id: object) -> None:
    """Tag every log line handled while this context is active."""
    _current_user.set(str(user_id) if user_id else "")


def redact(text: str) -> str:
    """Replace anything that looks like a token/secret with «***»."""

    def _mask(match: re.Match[str]) -> str:
        whole = match.group(0)
        for name in ("token", "secret"):
            if match.groupdict().get(name):
                head = whole[: match.start(name) - match.start()]
                return f"{head}***"
        return whole

    for pattern in _SECRETS:
        text = pattern.sub(_mask, text)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tag = _current_user.get()
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if tag:
            record.msg = f"[user {tag}] {record.msg}"
        return True


class _RedactingFormatter(logging.Formatter):
    """Redact the final output, including cached exception and stack text.

    Filtering only LogRecord.msg is insufficient: Formatter appends the
    traceback later, and may reuse exc_text between several handlers.
    """
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO", *, to_files: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        # Idempotent on purpose (--check-config, tests, a second call after a
        # config fix) — and the handlers are closed, because keeping them open
        # leaked a file descriptor on every re-configuration.
        root.removeHandler(handler)
        if isinstance(handler, (RotatingFileHandler, logging.FileHandler)):
            handler.close()

    formatter = _RedactingFormatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    redactor = _RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if to_files:
        try:
            LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            LOG_DIR.chmod(0o700)
            file_handler = RotatingFileHandler(
                LOG_DIR / "bot.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
            )
            os.chmod(LOG_DIR / "bot.log", 0o600)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)
        except OSError:                     # a read-only /logs must not stop the bot
            logging.getLogger(__name__).warning(
                "log file disabled: %s is not writable", LOG_DIR
            )

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # httpx (used by python-telegram-bot) is very chatty at INFO and prints URLs.
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


__all__ = ["LOG_DIR", "redact", "set_current_user", "setup_logging"]
