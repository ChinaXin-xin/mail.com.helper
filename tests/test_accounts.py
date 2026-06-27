import unittest

from ccgpt_mail_tool.accounts import parse_account_lines


class AccountParsingTests(unittest.TestCase):
    def test_parse_account_lines_accepts_email_separator_password(self) -> None:
        result = parse_account_lines(
            """
            # comment
            sample@mail.com----sample-password
            other@example.com----secret----with-separator
            """
        )

        self.assertEqual(len(result.accounts), 2)
        self.assertEqual(result.accounts[0].email_address, "sample@mail.com")
        self.assertEqual(result.accounts[0].password, "sample-password")
        self.assertEqual(result.accounts[1].password, "secret----with-separator")
        self.assertEqual(result.errors, ())

    def test_parse_account_lines_reports_invalid_lines(self) -> None:
        result = parse_account_lines(
            """
            missing-separator
            bad-address----pw
            empty@example.com----
            """
        )

        self.assertEqual(result.accounts, ())
        self.assertEqual([error.line_number for error in result.errors], [2, 3, 4])

    def test_account_repr_does_not_expose_password(self) -> None:
        result = parse_account_lines("user@example.com----very-secret-password")

        self.assertNotIn("very-secret-password", repr(result.accounts[0]))


if __name__ == "__main__":
    unittest.main()
