from types import SimpleNamespace

import pytest

from app.services.email import EmailService, render_team_invite


def _settings(**kw):
    defaults = dict(
        email_transport="console",
        email_from="Immensity <no-reply@useimmensity.com>",
        smtp_host="",
        smtp_port=587,
        smtp_user="",
        smtp_password="",
        smtp_starttls=True,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_render_team_invite_includes_link_and_team():
    subject, text, html = render_team_invite(
        team_name="Launch Lab", accept_url="https://app.example.com/invite/tok"
    )
    assert "Launch Lab" in subject
    assert "https://app.example.com/invite/tok" in text
    assert "https://app.example.com/invite/tok" in html


async def test_console_transport_succeeds_without_credentials():
    service = EmailService(settings=_settings(email_transport="console"))
    ok = await service.send(to="a@b.com", subject="Hi", text="body")
    assert ok is True


async def test_smtp_transport_without_host_fails_gracefully():
    service = EmailService(settings=_settings(email_transport="smtp", smtp_host=""))
    ok = await service.send(to="a@b.com", subject="Hi", text="body")
    assert ok is False
