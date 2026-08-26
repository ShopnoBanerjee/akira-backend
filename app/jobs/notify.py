"""Getting a message to a person.

Deliberately an interface with two implementations rather than an smtplib call
in the digest job. Stage 1 sends email because that is what is configured
(docs/DECISIONS.md A3), but staff in Indian F&B live on WhatsApp and Stage 2 is
expected to add it. A digest job that imports smtplib directly would have to be
rewritten to make that change; one that takes a Notifier does not.

The second reason is the one that matters today: **an unconfigured mailer must
be loud.** With no SMTP host set, this falls back to logging and returns
`"smtp_not_configured"` in its result, which lands in job_runs.detail and shows
on the jobs screen. A digest that silently stopped sending three weeks ago is
precisely the failure this epic exists to prevent, and the version of that
failure where the code "succeeded" every morning is the worst one.
"""

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    subject: str
    html: str
    #: Plain-text alternative. Some mail clients, and every log line, want it.
    text: str
    recipients: list[str] = field(default_factory=list)


class Notifier(Protocol):
    async def send(self, notification: Notification) -> dict[str, Any]:
        """Deliver, and describe what happened. Never raises: a failed digest
        must be recorded, not propagated into the scheduler."""
        ...


class LogNotifier:
    """Writes the message to the application log.

    Not a null object. The content is really emitted, so a local run or an
    unconfigured deployment still leaves the digest somewhere a person can
    read it.
    """

    channel = "log_only"

    async def send(self, notification: Notification) -> dict[str, Any]:
        logger.info(
            "notification [%s] to %s\n%s",
            notification.subject,
            ", ".join(notification.recipients) or "(nobody)",
            notification.text,
        )
        return {
            "channel": self.channel,
            "delivered": True,
            "recipients": notification.recipients,
        }


class EmailNotifier:
    """SMTP, synchronously, on a worker thread.

    smtplib blocks. The digest runs a handful of times a day, so a thread is
    the boring correct answer; an async SMTP library would be a dependency
    bought for nothing.
    """

    channel = "email"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _deliver(self, notification: Notification) -> None:
        s = self._settings
        message = EmailMessage()
        message["Subject"] = notification.subject
        message["From"] = s.SMTP_FROM
        message["To"] = ", ".join(notification.recipients)
        message.set_content(notification.text)
        message.add_alternative(notification.html, subtype="html")

        with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=30) as server:
            if s.SMTP_STARTTLS:
                server.starttls()
            if s.SMTP_USERNAME:
                server.login(s.SMTP_USERNAME, s.SMTP_PASSWORD)
            server.send_message(message)

    async def send(self, notification: Notification) -> dict[str, Any]:
        import asyncio

        if not notification.recipients:
            return {"channel": self.channel, "delivered": False, "reason": "no_recipients"}
        try:
            await asyncio.to_thread(self._deliver, notification)
        except Exception as exc:
            # Recorded, not raised. One outlet's digest failing must not stop
            # the other outlets' from going out.
            logger.exception("digest email failed")
            return {
                "channel": self.channel,
                "delivered": False,
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "recipients": notification.recipients,
            }
        return {
            "channel": self.channel,
            "delivered": True,
            "recipients": notification.recipients,
        }


def get_notifier(channel: str, settings: Settings | None = None) -> tuple[Notifier, str | None]:
    """The notifier for a configured channel, and why it was downgraded.

    Returns the fallback with a reason rather than raising, so a missing SMTP
    host produces a digest that was written and logged — plus a visible note —
    instead of a failed job and no digest at all.
    """
    settings = settings or get_settings()
    if channel == "email":
        if not settings.smtp_configured:
            return LogNotifier(), "smtp_not_configured"
        return EmailNotifier(settings), None
    return LogNotifier(), None
