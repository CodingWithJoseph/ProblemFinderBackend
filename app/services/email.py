"""In-house transactional email.

Two transports, chosen by ``settings.email_transport``:

* ``console`` (default) — renders the message and writes it to the application
  log. Needs no credentials, so dev and the test suite work out of the box.
* ``smtp`` — relays through the configured SMTP server via ``aiosmtplib``.

Sending is best-effort: callers (e.g. team invites) treat failures as
non-fatal, so a relay outage never blocks the request that triggered the mail.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmailService:
    def __init__(self, settings=None) -> None:
        self._settings = settings or get_settings()

    @property
    def transport(self) -> str:
        return (self._settings.email_transport or "console").strip().lower()

    async def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> bool:
        """Send one message. Returns True on success, False on failure.

        Never raises — failures are logged and reported via the return value so
        callers can stay non-fatal.
        """
        message = EmailMessage()
        message["From"] = self._settings.email_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        if self.transport == "smtp":
            return await self._send_smtp(message, to=to, subject=subject)
        return self._send_console(message, to=to, subject=subject, text=text)

    def _send_console(self, message: EmailMessage, *, to: str, subject: str, text: str) -> bool:
        logger.info(
            "[email:console] to=%s from=%s subject=%s\n%s",
            to,
            self._settings.email_from,
            subject,
            text,
        )
        return True

    async def _send_smtp(self, message: EmailMessage, *, to: str, subject: str) -> bool:
        # Imported lazily so the dependency is only required when SMTP is the
        # active transport (the test suite runs on the console transport).
        try:
            import aiosmtplib
        except ImportError:  # pragma: no cover - depends on deploy env
            logger.error("EMAIL_TRANSPORT=smtp but aiosmtplib is not installed; dropping email to %s", to)
            return False

        s = self._settings
        if not s.smtp_host:
            logger.error("EMAIL_TRANSPORT=smtp but SMTP_HOST is unset; dropping email to %s", to)
            return False

        try:
            await aiosmtplib.send(
                message,
                hostname=s.smtp_host,
                port=s.smtp_port,
                username=s.smtp_user or None,
                password=s.smtp_password or None,
                start_tls=s.smtp_starttls,
            )
            logger.info("[email:smtp] sent to=%s subject=%s", to, subject)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("[email:smtp] failed to send to=%s subject=%s: %s", to, subject, exc)
            return False


def render_team_invite(*, team_name: str, accept_url: str, inviter_label: str | None = None) -> tuple[str, str, str]:
    """Return (subject, text, html) for a team invite email."""
    inviter = f"{inviter_label} invited you" if inviter_label else "You've been invited"
    subject = f"{inviter} to join {team_name} on Immensity"
    text = (
        f"{inviter} to join the team \"{team_name}\" on Immensity.\n\n"
        f"Accept the invitation:\n{accept_url}\n\n"
        f"If you weren't expecting this, you can ignore this email."
    )
    html = (
        f"<p>{inviter} to join the team <strong>{team_name}</strong> on Immensity.</p>"
        f'<p><a href="{accept_url}">Accept the invitation</a></p>'
        f'<p style="color:#6b7280;font-size:12px">'
        f"If you weren't expecting this, you can ignore this email.</p>"
    )
    return subject, text, html


# Module-level singleton; cheap to construct but convenient to share.
email_service = EmailService()
