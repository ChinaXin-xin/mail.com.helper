from __future__ import annotations

from dataclasses import dataclass, field
import re


ACCOUNT_SEPARATOR = "----"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AccountCredential:
    email_address: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class AccountParseError:
    line_number: int
    message: str


@dataclass(frozen=True)
class AccountParseResult:
    accounts: tuple[AccountCredential, ...]
    errors: tuple[AccountParseError, ...]


def parse_account_lines(raw_text: str) -> AccountParseResult:
    accounts: list[AccountCredential] = []
    errors: list[AccountParseError] = []

    for line_number, original_line in enumerate(raw_text.splitlines(), start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue

        if ACCOUNT_SEPARATOR not in line:
            errors.append(
                AccountParseError(line_number, f"缺少分隔符 {ACCOUNT_SEPARATOR}")
            )
            continue

        email_address, password = line.split(ACCOUNT_SEPARATOR, maxsplit=1)
        email_address = email_address.strip()
        password = password.strip()

        if not EMAIL_RE.fullmatch(email_address):
            errors.append(AccountParseError(line_number, "邮箱格式不正确"))
            continue

        if not password:
            errors.append(AccountParseError(line_number, "密码不能为空"))
            continue

        accounts.append(AccountCredential(email_address=email_address, password=password))

    return AccountParseResult(accounts=tuple(accounts), errors=tuple(errors))

