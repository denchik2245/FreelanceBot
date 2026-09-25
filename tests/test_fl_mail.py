from email.message import EmailMessage

import pytest

from freelance_bot import fl_mail
from freelance_bot.fl_mail import GmailFlMailbox, format_fl_notification, parse_fl_notification


def _message(
    *,
    sender: str = "FL.ru <notify@fl.ru>",
    subject: str = "Новое сообщение от заказчика",
    plain: str = "",
    html: str = "",
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "freelancer@gmail.com"
    message["Subject"] = subject
    message["Message-ID"] = "<fl-message-42@example>"
    message.set_content(plain or "Откройте письмо")
    if html:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def test_parses_fl_message_without_marking_external_link_as_safe() -> None:
    raw = _message(
        plain="Алексей написал: Добрый день, когда сможете начать?",
        html=(
            '<p>Алексей написал: Добрый день, когда сможете начать?</p>'
            '<a href="https://tracking.example/steal">Открыть</a>'
            '<a href="https://www.fl.ru/contacts/client-42/">Ответить</a>'
        ),
    )

    notification = parse_fl_notification(raw, uid=7, uid_validity=3)

    assert notification is not None
    assert notification.key == "<fl-message-42@example>"
    assert notification.subject == "Новое сообщение от заказчика"
    assert "когда сможете начать" in notification.preview
    assert notification.url == "https://www.fl.ru/contacts/client-42/"
    formatted = format_fl_notification(notification)
    assert formatted.startswith("💬 Новое сообщение на FL.ru")
    assert "tracking.example" not in formatted


def test_rejects_mail_from_non_fl_sender() -> None:
    raw = _message(sender="FL Support <notify@example.com>")

    assert parse_fl_notification(raw, uid=7, uid_validity=3) is None


def test_falls_back_to_fl_contacts_when_message_has_no_safe_link() -> None:
    raw = _message(plain="Вам пришло новое сообщение")

    notification = parse_fl_notification(raw, uid=8, uid_validity=3)

    assert notification is not None
    assert notification.url == "https://www.fl.ru/contacts/"


@pytest.mark.asyncio
async def test_gmail_fetch_uses_readonly_and_body_peek(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _message(plain="Новое сообщение")

    class FakeImap:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            assert (host, port, timeout) == ("imap.gmail.com", 993, 30)

        def login(self, address: str, password: str) -> None:
            assert (address, password) == ("freelancer@gmail.com", "app-password")

        def select(self, folder: str, *, readonly: bool) -> tuple[str, list[bytes]]:
            assert folder == "INBOX"
            assert readonly is True
            return "OK", [b"1"]

        def status(self, folder: str, fields: str) -> tuple[str, list[bytes]]:
            assert (folder, fields) == ("INBOX", "(UIDVALIDITY UIDNEXT)")
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 6)"]

        def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
            if command == "search":
                assert args == (None, "UID 5:5", 'FROM "fl.ru"')
                return "OK", [b"5"]
            assert command == "fetch"
            assert args == ("5", "(BODY.PEEK[])")
            return "OK", [(b"5 (BODY[])", raw), b")"]

        def logout(self) -> None:
            return None

    monkeypatch.setattr(fl_mail.imaplib, "IMAP4_SSL", FakeImap)

    batch = await GmailFlMailbox("freelancer@gmail.com", "app-password").fetch(
        last_uid=4,
        uid_validity=9,
    )

    assert batch.uid_validity == 9
    assert batch.last_uid == 5
    assert len(batch.notifications) == 1
