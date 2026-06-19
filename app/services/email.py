"""In-house transactional email.

Two transports, chosen by ``settings.email_transport``:

* ``console`` (default) — renders the message and writes it to the application
  log. Needs no credentials, so dev and the test suite work out of the box and
  no real mail is sent unless a transport is explicitly configured.
* ``resend`` — sends through `Resend <https://resend.com>`_ via its Python SDK,
  authenticated with ``RESEND_API_KEY``.

Sending is best-effort: callers (e.g. team invites) treat failures as
non-fatal, so a provider outage never blocks the request that triggered the mail.
"""

from __future__ import annotations

import asyncio
import logging

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
        if self.transport == "resend":
            return await self._send_resend(to=to, subject=subject, text=text, html=html)
        return self._send_console(to=to, subject=subject, text=text)

    def _send_console(self, *, to: str, subject: str, text: str) -> bool:
        logger.info(
            "[email:console] to=%s from=%s subject=%s\n%s",
            to,
            self._settings.email_from,
            subject,
            text,
        )
        return True

    async def _send_resend(self, *, to: str, subject: str, text: str, html: str | None) -> bool:
        # Imported lazily so the dependency is only required when Resend is the
        # active transport (the test suite runs on the console transport).
        try:
            import resend
        except ImportError:  # pragma: no cover - depends on deploy env
            logger.error("EMAIL_TRANSPORT=resend but the resend package is not installed; dropping email to %s", to)
            return False

        s = self._settings
        if not s.resend_api_key:
            logger.error("EMAIL_TRANSPORT=resend but RESEND_API_KEY is unset; dropping email to %s", to)
            return False

        params: dict = {
            "from": s.email_from,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        if html:
            params["html"] = html

        try:
            resend.api_key = s.resend_api_key
            # The SDK call is synchronous (blocking HTTP); run it off the event loop.
            await asyncio.to_thread(resend.Emails.send, params)
            logger.info("[email:resend] sent to=%s subject=%s", to, subject)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("[email:resend] failed to send to=%s subject=%s: %s", to, subject, exc)
            return False


def render_team_invite(*, team_name: str, accept_url: str, inviter_label: str | None = None, product_name: str | None = None) -> tuple[str, str, str]:
    """Return (subject, text, html) for a team invite email."""
    product = product_name or get_settings().product_name
    inviter = f"{inviter_label} invited you" if inviter_label else "You've been invited"
    subject = f"{inviter} to join {team_name} on {product}"
    text = (
        f"{inviter} to join the team \"{team_name}\" on {product}.\n\n"
        f"Accept the invitation:\n{accept_url}\n\n"
        f"If you weren't expecting this, you can ignore this email."
    )
    html = (
        f"<p>{inviter} to join the team <strong>{team_name}</strong> on {product}.</p>"
        f'<p><a href="{accept_url}">Accept the invitation</a></p>'
        f'<p style="color:#6b7280;font-size:12px">'
        f"If you weren't expecting this, you can ignore this email.</p>"
    )
    return subject, text, html


# Module-level singleton; cheap to construct but convenient to share.
email_service = EmailService()
