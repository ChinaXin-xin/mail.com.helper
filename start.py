"""Win11 GUI for reading mail.com messages with direct HTTP requests.

The GUI posts accounts to a local 127.0.0.1 HTTP endpoint. The endpoint performs
the same mail.com web flow with requests only: login form, lightmailer startup,
folder list, message list, message detail, and message body.
"""

from __future__ import annotations

import html
import json
import re
import threading
import tkinter as tk
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import requests


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
MAIL_HOME_URL = "https://www.mail.com/"
LIGHT_START_URL = "https://lightmailer.mail.com/start?device=desktop&ott={ott}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class MailAccount:
    address: str
    password: str


@dataclass(frozen=True)
class LoginForm:
    action: str
    fields: dict[str, str]


class BasicHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.inputs: list[dict[str, str]] = []
        self.text: list[str] = []
        self._in_login_form = False
        self.login_action = ""
        self.login_inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.forms.append(values)
            action = values.get("action", "")
            self._in_login_form = "login.mail.com/login" in action
            if self._in_login_form:
                self.login_action = action
        if tag == "a":
            self.links.append(values)
        if tag == "input":
            self.inputs.append(values)
            if self._in_login_form:
                self.login_inputs.append(values)

    def handle_data(self, data: str) -> None:
        value = normalize_text(data)
        if value:
            self.text.append(value)


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


def normalize_text(value: str) -> str:
    value = html.unescape(value.replace("\xa0", " "))
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|tr|table|h[1-6])\s*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [normalize_text(line) for line in html.unescape(value).splitlines()]
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


def parse_login_form(home_html: str) -> LoginForm:
    parser = BasicHtmlParser()
    parser.feed(home_html)
    if not parser.login_action:
        raise RuntimeError("Could not find the mail.com login form.")

    fields: dict[str, str] = {}
    for input_tag in parser.login_inputs:
        name = input_tag.get("name")
        if name:
            fields[name] = input_tag.get("value", "")
    return LoginForm(action=parser.login_action, fields=fields)


def extract_ott(url: str, page_html: str) -> str:
    match = re.search(r"[?&]ott=([0-9a-f-]+)", url) or re.search(
        r"ott=([0-9a-f-]+)", page_html
    )
    if not match:
        raise RuntimeError(
            "mail.com did not return a lightmailer login token. "
            "The account may require CAPTCHA, extra verification, or cookies."
        )
    return match.group(1)


def extract_wicket_redirect(xml_text: str) -> str:
    match = re.search(r"<redirect><!\[CDATA\[(.*?)\]\]></redirect>", xml_text)
    if not match:
        raise RuntimeError("mail.com lightmailer did not return a startup redirect.")
    return match.group(1)


def unique_relative_urls(page_html: str, pattern: str) -> list[str]:
    found = [html.unescape(match) for match in re.findall(pattern, page_html)]
    output: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url not in seen:
            seen.add(url)
            output.append(url)
    return output


class MailComHttpReader:
    def __init__(self, status: list[str]) -> None:
        self.status = status

    def fetch_account(self, account: MailAccount, max_messages: int) -> dict[str, Any]:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        self._set_status(f"Logging in with direct HTTP: {account.address}")

        folder_url, folder_html = self._login(session, account)
        inbox_url = self._find_inbox_url(folder_url, folder_html)
        messages = self._fetch_messages(session, inbox_url, max_messages)

        return {
            "account": account.address,
            "ok": True,
            "messages": messages,
            "total": len(messages),
        }

    def _set_status(self, value: str) -> None:
        self.status.append(value)

    def _login(self, session: requests.Session, account: MailAccount) -> tuple[str, str]:
        home = session.get(MAIL_HOME_URL, timeout=30)
        home.raise_for_status()
        login_form = parse_login_form(home.text)

        fields = dict(login_form.fields)
        fields["username"] = account.address
        fields["password"] = account.password
        login = session.post(login_form.action, data=fields, timeout=60, allow_redirects=True)
        login.raise_for_status()

        ott = extract_ott(login.url, login.text)
        light = session.get(LIGHT_START_URL.format(ott=ott), timeout=60, allow_redirects=True)
        light.raise_for_status()

        ajax_headers = {
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": "start?0&device=desktop",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Referer": light.url,
        }
        startup = session.get(
            urljoin(light.url, "./start?0-1.0-&device=desktop"),
            headers=ajax_headers,
            timeout=60,
        )
        startup.raise_for_status()
        redirect = extract_wicket_redirect(startup.text)

        folder = session.get(urljoin(light.url, redirect), timeout=60)
        folder.raise_for_status()
        if "FolderListPage" not in folder.text:
            raise RuntimeError("Login succeeded, but the folder list did not load.")
        self._set_status(f"Logged in: {account.address}")
        return folder.url, folder.text

    def _find_inbox_url(self, folder_url: str, folder_html: str) -> str:
        match = re.search(
            r'href="(\./messagelist\?folderId=[^"]+)"[^>]+data-webdriver="INBOX:Inbox"',
            folder_html,
        )
        if not match:
            raise RuntimeError("Could not find the Inbox folder link.")
        return urljoin(folder_url, html.unescape(match.group(1)))

    def _fetch_messages(
        self,
        session: requests.Session,
        inbox_url: str,
        max_messages: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        page_url = inbox_url

        while page_url:
            listing = session.get(page_url, timeout=60)
            listing.raise_for_status()
            detail_urls = unique_relative_urls(
                listing.text,
                r'href="(\./messagedetail\?[^"]+)"',
            )
            if not detail_urls and not messages:
                self._set_status("Inbox is empty.")
                return []

            for detail_url in detail_urls:
                if max_messages and len(messages) >= max_messages:
                    return messages
                index = len(messages) + 1
                self._set_status(f"Reading message #{index}")
                messages.append(
                    self._fetch_message_detail(
                        session,
                        urljoin(listing.url, detail_url),
                        index,
                    )
                )

            page_url = self._find_next_page_url(listing.url, listing.text)
            if page_url and max_messages and len(messages) >= max_messages:
                page_url = ""

        return messages

    def _find_next_page_url(self, current_url: str, listing_html: str) -> str:
        parser = BasicHtmlParser()
        parser.feed(listing_html)
        for link in parser.links:
            label = " ".join(
                value for key, value in link.items() if key in {"title", "aria-label", "data-webdriver"}
            ).lower()
            href = link.get("href", "")
            if href and ("next" in label or "paging=next" in href.lower()):
                return urljoin(current_url, href)
        return ""

    def _fetch_message_detail(
        self,
        session: requests.Session,
        detail_url: str,
        index: int,
    ) -> dict[str, Any]:
        detail = session.get(detail_url, timeout=60)
        detail.raise_for_status()

        body_match = re.search(r'<iframe id="bodyIFrame"[^>]+src="([^"]+)"', detail.text)
        body = ""
        if body_match:
            body_url = urljoin(detail.url, html.unescape(body_match.group(1)))
            body_response = session.get(body_url, headers={"Referer": detail.url}, timeout=60)
            body_response.raise_for_status()
            body = html_to_text(body_response.text)

        metadata = self._extract_detail_metadata(detail.text)
        metadata["index"] = index
        metadata["body"] = body
        return metadata

    def _extract_detail_metadata(self, detail_html: str) -> dict[str, str]:
        parser = BasicHtmlParser()
        parser.feed(detail_html)
        tokens = parser.text

        subject = self._next_after(tokens, "Subject")
        sender = self._next_after(tokens, "From:") or self._next_after(tokens, "Sender")
        date = next((token for token in tokens if token.startswith("Received ")), "")

        title_match = re.search(r"<title[^>]*>mail\.com - E-Mail: (.*?)</title>", detail_html)
        if not subject and title_match:
            subject = normalize_text(title_match.group(1))

        return {
            "from": sender,
            "to": "",
            "date": date,
            "subject": subject,
        }

    def _next_after(self, tokens: list[str], label: str) -> str:
        for index, token in enumerate(tokens[:-1]):
            if token == label:
                return tokens[index + 1]
        return ""


def fetch_all(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = [
        MailAccount(str(item["address"]), str(item["password"])) for item in payload["accounts"]
    ]
    max_messages = int(payload.get("max_messages") or 0)
    status: list[str] = []
    reader = MailComHttpReader(status=status)

    results: list[dict[str, Any]] = []
    for account in accounts:
        try:
            results.append(reader.fetch_account(account, max_messages))
        except Exception as exc:  # noqa: BLE001 - return per-account error to GUI.
            results.append({"account": account.address, "ok": False, "error": str(exc)})
    return {"ok": True, "status": status, "results": results}


class LocalMailApiHandler(BaseHTTPRequestHandler):
    server_version = "LocalMailComHttpApi/1.0"

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
        self.title("mail.com HTTP Mail Reader")
        self.geometry("980x720")
        self.minsize(860, 620)

        self.api = LocalMailApi()
        self.api.start()
        self.max_messages = tk.IntVar(value=0)
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
        actions.columnconfigure(3, weight=1)
        ttk.Label(actions, text="Max mails, 0 = all").grid(row=0, column=0, padx=(0, 6))
        ttk.Spinbox(actions, from_=0, to=99999, textvariable=self.max_messages, width=8).grid(
            row=0, column=1, padx=(0, 14)
        )
        self.fetch_button = ttk.Button(actions, text="Fetch all emails", command=self.fetch_emails)
        self.fetch_button.grid(row=0, column=2, padx=(0, 10))
        ttk.Label(actions, textvariable=self.status).grid(row=0, column=3, sticky=tk.W)

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
        }
        self.busy = True
        self.fetch_button.configure(state=tk.DISABLED)
        self.status.set("Fetching through direct HTTP requests...")
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
