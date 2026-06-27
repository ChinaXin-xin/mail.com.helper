import imaplib
import unittest
from email.message import EmailMessage

from ccgpt_mail_tool.imap_mail import (
    _build_snippet,
    _decode_header_value,
    _format_imap_error,
)


class ImapMailTests(unittest.TestCase):
    def test_decode_header_value_handles_encoded_subject(self) -> None:
        self.assertEqual(_decode_header_value("=?utf-8?b?5rWL6K+V?="), "测试")

    def test_build_snippet_prefers_plain_text(self) -> None:
        message = EmailMessage()
        message.set_content("Your login code is 123456.\n\nIgnore html.")
        message.add_alternative("<html><body>HTML fallback</body></html>", subtype="html")

        self.assertEqual(
            _build_snippet(message), "Your login code is 123456. Ignore html."
        )

    def test_format_imap_authentication_error_decodes_bytes(self) -> None:
        error = imaplib.IMAP4.error(b"authentication failed")

        self.assertIn("身份验证失败", _format_imap_error(error))


if __name__ == "__main__":
    unittest.main()
