from __future__ import annotations

from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
import imaplib
import re

from .accounts import AccountCredential


DEFAULT_IMAP_HOST = "imap.mail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX = "INBOX"


@dataclass(frozen=True)
class MailSummary:
    account_email: str
    sender: str
    date: str
    subject: str
    snippet: str


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        return " ".join(self._parts)


class MailFetchError(RuntimeError):
    """Raised when an IMAP mailbox cannot be read."""


class ImapMailFetcher:
    def __init__(
        self,
        host: str = DEFAULT_IMAP_HOST,
        port: int = DEFAULT_IMAP_PORT,
        mailbox: str = DEFAULT_MAILBOX,
        timeout_seconds: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.mailbox = mailbox
        self.timeout_seconds = timeout_seconds

    def fetch_latest(
        self, account: AccountCredential, limit: int = 5
    ) -> list[MailSummary]:
        limit = max(1, min(limit, 20))
        try:
            with imaplib.IMAP4_SSL(
                self.host, self.port, timeout=self.timeout_seconds
            ) as client:
                client.login(account.email_address, account.password)
                status, _ = client.select(self.mailbox, readonly=True)
                if status != "OK":
                    raise MailFetchError(f"无法打开邮箱目录: {self.mailbox}")

                status, payload = client.search(None, "ALL")
                if status != "OK" or not payload:
                    raise MailFetchError("无法搜索邮箱邮件")

                message_ids = payload[0].split()
                latest_ids = list(reversed(message_ids[-limit:]))
                summaries = [
                    self._fetch_summary(client, account.email_address, message_id)
                    for message_id in latest_ids
                ]
                client.close()
                return summaries
        except imaplib.IMAP4.error as exc:
            raise MailFetchError(_format_imap_error(exc)) from exc
        except OSError as exc:
            raise MailFetchError(f"网络连接失败: {exc}") from exc

    def _fetch_summary(
        self, client: imaplib.IMAP4_SSL, account_email: str, message_id: bytes
    ) -> MailSummary:
        status, payload = client.fetch(message_id, "(BODY.PEEK[])")
        if status != "OK" or not payload:
            raise MailFetchError("读取邮件失败")

        raw_message = next(
            (part[1] for part in payload if isinstance(part, tuple) and part[1]), None
        )
        if raw_message is None:
            raise MailFetchError("邮件内容为空")

        message = message_from_bytes(raw_message)
        sender_name, sender_addr = parseaddr(_decode_header_value(message.get("From")))
        sender = sender_addr or sender_name or "(未知发件人)"

        return MailSummary(
            account_email=account_email,
            sender=sender,
            date=_decode_header_value(message.get("Date")),
            subject=_decode_header_value(message.get("Subject")) or "(无主题)",
            snippet=_build_snippet(message),
        )


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeDecodeError, ValueError):
        return value.strip()


def _format_imap_error(exc: imaplib.IMAP4.error) -> str:
    message = _decode_exception_message(exc)
    lowered = message.lower()
    if "authentication failed" in lowered or "login failed" in lowered:
        return (
            "身份验证失败：请先用浏览器登录 mail.com 确认邮箱和密码正确；"
            "再到邮箱设置里开启 POP3/IMAP。若网页提示临时锁定、安全验证"
            "或账号限制，请按网页提示处理后再试。"
        )
    return message or "IMAP 服务返回未知错误"


def _decode_exception_message(exc: BaseException) -> str:
    raw = exc.args[0] if exc.args else str(exc)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def _build_snippet(message: Message, max_length: int = 160) -> str:
    text = _extract_body_text(message)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _extract_body_text(message: Message) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in message.walk():
            content_disposition = part.get_content_disposition()
            if content_disposition == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(_decode_payload(part))
            elif content_type == "text/html":
                html_parts.append(_html_to_text(_decode_payload(part)))
        return " ".join(part for part in plain_parts if part) or " ".join(
            part for part in html_parts if part
        )

    if message.get_content_type() == "text/html":
        return _html_to_text(_decode_payload(message))
    return _decode_payload(message)


def _decode_payload(message: Message) -> str:
    payload = message.get_payload(decode=True)
    if payload is None:
        raw = message.get_payload()
        return raw if isinstance(raw, str) else ""

    charset = message.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()
