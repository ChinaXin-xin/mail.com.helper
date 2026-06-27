"""Win11 GUI for reading mail.com webmail through a local HTTP API.

The GUI posts accounts to a local 127.0.0.1 HTTP endpoint. That endpoint opens
mail.com in a real browser, logs in, downloads each visible message as .eml
through the web UI, parses it locally, and returns the message contents.
"""

from __future__ import annotations

import email
import html
import json
import re
import shutil
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from email.message import Message
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
MAIL_HOME_URL = "https://www.mail.com/"
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


@dataclass(frozen=True)
class MailAccount:
    address: str
    password: str


def parse_accounts(raw_text: str) -> list[MailAccount]:
    accounts: list[MailAccount] = []
    errors: list[str] = []
    for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if "----" not in line:
            errors.append(f"Line {line_number}: expected email----password")
            continue
        address, password = line.split("----", 1)
        address = address.strip()
        password = password.strip()
        if "@" not in address or not password:
            errors.append(f"Line {line_number}: invalid account or password")
            continue
        accounts.append(MailAccount(address, password))

    if errors:
        raise ValueError("\n".join(errors))
    if not accounts:
        raise ValueError("Enter at least one account in email----password format.")
    return accounts


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("chrome") or shutil.which("chrome.exe")


def decode_payload(message: Message) -> str:
    payload = message.get_payload(decode=True)
    charset = message.get_content_charset() or "utf-8"
    if payload is None:
        value = message.get_payload()
        return value if isinstance(value, str) else ""
    return payload.decode(charset, errors="replace")


def html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_text(html.unescape(text))


def normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    output: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                output.append("")
            blank = True
            continue
        output.append(line)
        blank = False
    return "\n".join(output).strip()


def message_body(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/plain":
                return normalize_text(decode_payload(part))
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return html_to_text(decode_payload(part))

    payload = decode_payload(message)
    if message.get_content_type() == "text/html":
        return html_to_text(payload)
    return normalize_text(payload)


def parse_eml(path: Path, index: int) -> dict[str, Any]:
    parsed = email.message_from_bytes(path.read_bytes(), policy=default)
    return {
        "index": index,
        "from": str(parsed.get("From", "")),
        "to": str(parsed.get("To", "")),
        "date": str(parsed.get("Date", "")),
        "subject": str(parsed.get("Subject", "")),
        "body": message_body(parsed),
    }


class MailComWebReader:
    def __init__(self, headless: bool, status: list[str]) -> None:
        self.headless = headless
        self.status = status

    def fetch_account(self, account: MailAccount, max_messages: int) -> dict[str, Any]:
        chrome_path = find_chrome()
        if not chrome_path:
            raise RuntimeError("Google Chrome was not found on this computer.")

        with tempfile.TemporaryDirectory(prefix="ccgpt-mailcom-") as user_data_dir:
            downloads = Path(user_data_dir) / "downloads"
            downloads.mkdir(exist_ok=True)
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir,
                    executable_path=chrome_path,
                    headless=self.headless,
                    accept_downloads=True,
                    downloads_path=str(downloads),
                    viewport={"width": 1280, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    self._login(page, account)
                    messages = self._download_messages(page, downloads, max_messages)
                    return {
                        "account": account.address,
                        "ok": True,
                        "messages": messages,
                        "total": len(messages),
                    }
                finally:
                    context.close()

    def _set_status(self, text: str) -> None:
        self.status.append(text)

    def _login(self, page: Page, account: MailAccount) -> None:
        self._set_status(f"Opening mail.com for {account.address}")
        page.goto(MAIL_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        self._close_optional_popups(page)

        login = page.get_by_role("link", name="Log in", exact=True)
        login.wait_for(timeout=30_000)
        login.click()
        page.get_by_label("Email address", exact=True).fill(account.address)
        page.get_by_label("Password", exact=True).fill(account.password)
        page.get_by_role("button", name="Log in", exact=True).click()

        page.wait_for_url(re.compile(r"https://navigator-lxa\.mail\.com/mail.*"), timeout=60_000)
        page.locator("#thirdPartyFrame_mail").wait_for(timeout=60_000)
        self._mail_frame(page).locator("text=Inbox").first.wait_for(timeout=60_000)
        self._set_status(f"Logged in: {account.address}")

    def _close_optional_popups(self, page: Page) -> None:
        for label in ("Close", "I agree", "Accept all"):
            try:
                locator = page.get_by_role("button", name=label, exact=True)
                if locator.count():
                    locator.first.click(timeout=1000)
                    page.wait_for_timeout(300)
            except PlaywrightTimeoutError:
                continue

    def _mail_frame(self, page: Page):
        return page.frame_locator("#thirdPartyFrame_mail")

    def _download_messages(
        self,
        page: Page,
        downloads_dir: Path,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        mail = self._mail_frame(page)
        self._open_first_message(mail)
        messages: list[dict[str, Any]] = []

        while True:
            index = len(messages) + 1
            self._set_status(f"Downloading message #{index}")
            messages.append(self._save_current_message(page, mail, downloads_dir, index))

            if max_messages and len(messages) >= max_messages:
                break
            if not self._go_next_message(mail):
                break

        return messages

    def _open_first_message(self, mail: Any) -> None:
        container = mail.locator("mail-list-container")
        container.wait_for(timeout=60_000)
        box = container.bounding_box(timeout=10_000)
        if not box:
            raise RuntimeError("Cannot locate the mail list.")
        container.click(position={"x": min(260, box["width"] - 20), "y": 145}, timeout=10_000)
        mail.get_by_role("button", name="Back", exact=True).wait_for(timeout=30_000)

    def _save_current_message(
        self,
        page: Page,
        mail: Any,
        downloads_dir: Path,
        index: int,
    ) -> dict[str, Any]:
        more = mail.locator('button[title="Show further actions"]')
        more.wait_for(timeout=30_000)
        more.click()
        save_eml = mail.get_by_role("button", name="Save (.eml)", exact=True)
        save_eml.wait_for(timeout=10_000)

        with page.expect_download(timeout=30_000) as download_info:
            save_eml.click()
        download = download_info.value
        target = downloads_dir / f"message-{index}.eml"
        download.save_as(str(target))
        return parse_eml(target, index)

    def _go_next_message(self, mail: Any) -> bool:
        next_button = mail.locator('button[title="Next email"]')
        try:
            if next_button.count() == 0 or not next_button.first.is_enabled(timeout=1000):
                return False
            next_button.first.click(timeout=10_000)
            mail.locator('button[title="Show further actions"]').wait_for(timeout=20_000)
            time.sleep(0.5)
            return True
        except PlaywrightTimeoutError:
            return False


def fetch_all(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = [
        MailAccount(str(item["address"]), str(item["password"])) for item in payload["accounts"]
    ]
    max_messages = int(payload.get("max_messages") or 0)
    headless = bool(payload.get("headless", False))
    status: list[str] = []
    reader = MailComWebReader(headless=headless, status=status)

    results: list[dict[str, Any]] = []
    for account in accounts:
        try:
            results.append(reader.fetch_account(account, max_messages))
        except Exception as exc:  # noqa: BLE001 - return per-account error to GUI.
            results.append({"account": account.address, "ok": False, "error": str(exc)})
    return {"ok": True, "status": status, "results": results}


class LocalMailApiHandler(BaseHTTPRequestHandler):
    server_version = "LocalMailComWebApi/1.0"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/fetch-mails":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            self._send_json(200, fetch_all(payload))
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalMailApi:
    def __init__(self, host: str = DEFAULT_API_HOST, port: int = DEFAULT_API_PORT) -> None:
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer((self.host, self.port), LocalMailApiHandler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


class MailReaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("mail.com Web Mail Reader")
        self.geometry("980x720")
        self.minsize(860, 620)

        self.api = LocalMailApi()
        self.api.start()
        self.max_messages = tk.IntVar(value=0)
        self.show_browser = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value=f"Local HTTP API ready: {self.api.url}/fetch-mails")
        self.busy = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(root, text="Accounts, one per line: email----password")
        top.grid(row=0, column=0, sticky=tk.NSEW)
        top.columnconfigure(0, weight=1)
        self.accounts_text = tk.Text(top, height=5, wrap=tk.NONE)
        self.accounts_text.grid(row=0, column=0, sticky=tk.NSEW, padx=8, pady=8)

        output_frame = ttk.LabelFrame(root, text="All emails")
        output_frame.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output_text = tk.Text(output_frame, wrap=tk.WORD)
        self.output_text.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar = ttk.Scrollbar(output_frame, command=self.output_text.yview)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.output_text.configure(yscrollcommand=scrollbar.set)

        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky=tk.EW, pady=(10, 0))
        actions.columnconfigure(4, weight=1)
        ttk.Label(actions, text="Max mails, 0 = all").grid(row=0, column=0, padx=(0, 6))
        ttk.Spinbox(actions, from_=0, to=99999, textvariable=self.max_messages, width=8).grid(
            row=0, column=1, padx=(0, 14)
        )
        ttk.Checkbutton(actions, text="Show browser", variable=self.show_browser).grid(
            row=0, column=2, padx=(0, 14)
        )
        self.fetch_button = ttk.Button(actions, text="Fetch all emails", command=self.fetch_emails)
        self.fetch_button.grid(row=0, column=3, padx=(0, 10))
        ttk.Label(actions, textvariable=self.status).grid(row=0, column=4, sticky=tk.W)

    def fetch_emails(self) -> None:
        if self.busy:
            return
        try:
            accounts = parse_accounts(self.accounts_text.get("1.0", tk.END))
            max_messages = int(self.max_messages.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Invalid input", str(exc))
            return

        payload = {
            "accounts": [
                {"address": account.address, "password": account.password} for account in accounts
            ],
            "max_messages": max_messages,
            "headless": not self.show_browser.get(),
        }
        self.busy = True
        self.fetch_button.configure(state=tk.DISABLED)
        self.status.set("Fetching through mail.com web login...")
        threading.Thread(target=self._worker, args=(payload,), daemon=True).start()

    def _worker(self, payload: dict[str, Any]) -> None:
        try:
            response = self._post_json(f"{self.api.url}/fetch-mails", payload)
            rendered = render_results(response)
            self.after(0, lambda: self._set_output(rendered))
            self.after(0, lambda: self.status.set("Done. Passwords were not saved."))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: messagebox.showerror("Fetch failed", str(exc)))
            self.after(0, lambda: self.status.set(f"Failed: {exc}"))
        finally:
            self.after(0, lambda: self.fetch_button.configure(state=tk.NORMAL))
            self.busy = False

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=600) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise RuntimeError(f"Cannot call local HTTP API: {exc}") from exc
        if not parsed.get("ok"):
            raise RuntimeError(parsed.get("error") or "Local HTTP API returned an error.")
        return parsed

    def _set_output(self, value: str) -> None:
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert(tk.END, value)

    def _on_close(self) -> None:
        self.api.stop()
        self.destroy()


def render_results(response: dict[str, Any]) -> str:
    lines: list[str] = []
    for account_result in response.get("results", []):
        lines.append(f"Account: {account_result.get('account', '')}")
        if not account_result.get("ok"):
            lines.append(f"ERROR: {account_result.get('error', 'Unknown error')}")
            lines.append("")
            continue

        lines.append(f"Messages returned: {account_result.get('total', 0)}")
        lines.append("=" * 80)
        for message in account_result.get("messages", []):
            lines.append(f"#{message.get('index', '')}")
            lines.append(f"From: {message.get('from', '')}")
            lines.append(f"To: {message.get('to', '')}")
            lines.append(f"Date: {message.get('date', '')}")
            lines.append(f"Subject: {message.get('subject', '')}")
            lines.append("")
            lines.append(str(message.get("body", "")))
            lines.append("-" * 80)
        lines.append("")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    app = MailReaderApp()
    app.mainloop()
