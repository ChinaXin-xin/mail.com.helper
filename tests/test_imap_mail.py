import unittest
from email.message import EmailMessage

from ccgpt_mail_tool.imap_mail import _build_snippet, _decode_header_value


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


if __name__ == "__main__":
    unittest.main()
