from __future__ import annotations

import asyncio
import imaplib
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from html import unescape
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
FL_HOSTS = {"fl.ru", "www.fl.ru"}
MAX_PREVIEW_LENGTH = 700


@dataclass(frozen=True, slots=True)
class FlMailNotification:
    key: str
    subject: str
    preview: str
    url: str


@dataclass(frozen=True, slots=True)
class MailBatch:
    uid_validity: int
    last_uid: int
    notifications: tuple[FlMailNotification, ...]
    initialized: bool = False


def _decoded_header(value: str | None, default: str = "") -> str:
    if not value:
        return default
    try:
        return str(make_header(decode_header(value))).strip() or default
    except (LookupError, UnicodeError):
        return value.strip() or default


def _message_bodies(message: Message) -> tuple[str, str]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        if content_type == "text/html":
            html_parts.append(content)
        else:
            plain_parts.append(content)
    return "\n".join(plain_parts), "\n".join(html_parts)


def _compact_preview(plain: str, html: str) -> str:
    text = plain
    if not text.strip() and html:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = " ".join(unescape(text).split())
    if len(text) > MAX_PREVIEW_LENGTH:
        return text[: MAX_PREVIEW_LENGTH - 1].rstrip() + "…"
    return text


def _is_safe_fl_url(value: str) -> bool:
    try:
        parsed = urlparse(unescape(value))
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").casefold() in FL_HOSTS


def _extract_fl_url(plain: str, html: str) -> str:
    candidates: list[str] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        candidates.extend(
            str(node.get("href", "")).strip()
            for node in soup.select("a[href]")
        )
    candidates.extend(re.findall(r"https://(?:www\.)?fl\.ru/[^\s<>\"]+", plain))
    safe = [candidate.rstrip(".,);]") for candidate in candidates if _is_safe_fl_url(candidate)]
    if not safe:
        return "https://www.fl.ru/contacts/"

    def rank(url: str) -> tuple[int, int]:
        path = urlparse(url).path.casefold()
        priority = 0 if any(word in path for word in ("contact", "message", "chat")) else 1
        return priority, len(url)

    return min(safe, key=rank)


def parse_fl_notification(raw: bytes, *, uid: int, uid_validity: int) -> FlMailNotification | None:
    message = message_from_bytes(raw, policy=policy.default)
    _, sender_address = parseaddr(_decoded_header(message.get("From")))
    sender_domain = sender_address.rpartition("@")[2].casefold()
    if sender_domain != "fl.ru" and not sender_domain.endswith(".fl.ru"):
        return None
    plain, html = _message_bodies(message)
    subject = _decoded_header(message.get("Subject"), "Новое сообщение на FL.ru")
    if len(subject) > 300:
        subject = subject[:299].rstrip() + "…"
    preview = _compact_preview(plain, html) or "Откройте диалог, чтобы прочитать сообщение."
    message_id = _decoded_header(message.get("Message-ID"))
    key = message_id if message_id else f"gmail:{uid_validity}:{uid}"
    return FlMailNotification(
        key=key,
        subject=subject,
        preview=preview,
        url=_extract_fl_url(plain, html),
    )


def format_fl_notification(notification: FlMailNotification) -> str:
    return "\n".join(
        (
            "💬 Новое сообщение на FL.ru",
            "",
            notification.subject,
            "",
            notification.preview,
            "",
            notification.url,
        )
    )


def _status_values(client: imaplib.IMAP4_SSL) -> tuple[int, int]:
    status, data = client.status("INBOX", "(UIDVALIDITY UIDNEXT)")
    if status != "OK" or not data or not data[0]:
        raise RuntimeError("Gmail не вернул состояние папки INBOX")
    raw = data[0].decode("ascii", errors="replace") if isinstance(data[0], bytes) else str(data[0])
    validity_match = re.search(r"UIDVALIDITY\s+(\d+)", raw)
    next_match = re.search(r"UIDNEXT\s+(\d+)", raw)
    if validity_match is None or next_match is None:
        raise RuntimeError("Gmail вернул неизвестный формат состояния INBOX")
    return int(validity_match.group(1)), int(next_match.group(1))


def _raw_message(fetch_data: list[Any]) -> bytes:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("Gmail не вернул содержимое письма")


class GmailFlMailbox:
    def __init__(self, address: str, app_password: str) -> None:
        self._address = address
        self._app_password = app_password

    async def fetch(
        self,
        *,
        last_uid: int | None,
        uid_validity: int | None,
    ) -> MailBatch:
        return await asyncio.to_thread(
            self._fetch_sync,
            last_uid=last_uid,
            uid_validity=uid_validity,
        )

    def _fetch_sync(
        self,
        *,
        last_uid: int | None,
        uid_validity: int | None,
    ) -> MailBatch:
        client = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, timeout=30)
        try:
            client.login(self._address, self._app_password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Gmail не разрешил открыть папку INBOX")
            current_validity, next_uid = _status_values(client)
            current_last_uid = max(0, next_uid - 1)
            if last_uid is None or uid_validity != current_validity:
                return MailBatch(
                    uid_validity=current_validity,
                    last_uid=current_last_uid,
                    notifications=(),
                    initialized=True,
                )
            if current_last_uid <= last_uid:
                return MailBatch(current_validity, current_last_uid, ())

            start_uid = last_uid + 1
            search_status, search_data = client.uid(
                "search",
                None,
                f"UID {start_uid}:{current_last_uid}",
                'FROM "fl.ru"',
            )
            if search_status != "OK" or not search_data:
                raise RuntimeError("Gmail не выполнил поиск новых писем")
            notifications: list[FlMailNotification] = []
            for raw_uid in search_data[0].split():
                uid = int(raw_uid)
                if not start_uid <= uid <= current_last_uid:
                    continue
                fetch_status, fetch_data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if fetch_status != "OK" or not isinstance(fetch_data, list):
                    raise RuntimeError("Gmail не вернул новое письмо FL.ru")
                notification = parse_fl_notification(
                    _raw_message(fetch_data),
                    uid=uid,
                    uid_validity=current_validity,
                )
                if notification is not None:
                    notifications.append(notification)
            return MailBatch(current_validity, current_last_uid, tuple(notifications))
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
