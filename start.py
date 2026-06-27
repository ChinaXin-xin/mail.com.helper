"""Win11 GUI for reading mail.com messages with direct HTTP requests.

The GUI posts accounts to a local 127.0.0.1 HTTP endpoint. The endpoint performs
the same mail.com web flow with requests only: login form, lightmailer startup,
folder list, message list, message detail, and message body.
"""

from __future__ import annotations

import csv
import html
import json
import queue
import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import requests

try:
    from tkinterweb import HtmlFrame
except Exception:  # noqa: BLE001 - keep the app usable without optional HTML preview support.
    HtmlFrame = None


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


ProgressCallback = Callable[[dict[str, Any]], None]


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


def load_accounts_file(path: str) -> str:
    account_path = Path(path)
    content = account_path.read_text(encoding="utf-8-sig", errors="replace")
    if account_path.suffix.lower() != ".csv":
        return content

    rows: list[str] = []
    for row in csv.reader(StringIO(content)):
        if not row or len(row) < 2:
            continue
        address = row[0].strip()
        password = row[1].strip()
        if "@" in address and password:
            rows.append(f"{address}----{password}")
    return "\n".join(rows) if rows else content


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


def parse_message_timestamp(value: str) -> float | None:
    cleaned = value.strip()
    if cleaned.lower().startswith("received "):
        cleaned = cleaned[9:].strip()
    if not cleaned:
        return None
    try:
        parsed = parsedate_to_datetime(cleaned)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def message_sort_key(message: dict[str, Any]) -> tuple[int, float]:
    timestamp = parse_message_timestamp(str(message.get("date", "")))
    if timestamp is not None:
        return (1, timestamp)
    try:
        index = int(message.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    return (0, -float(index))


def strip_unsafe_html(value: str) -> str:
    value = re.sub(r"(?is)<script\b.*?>.*?</script>", "", value)
    value = re.sub(r"(?is)<object\b.*?>.*?</object>", "", value)
    value = re.sub(r"(?is)<embed\b.*?>", "", value)
    value = re.sub(r"(?is)<form\b.*?>.*?</form>", "", value)
    return value


def body_fragment(value: str) -> str:
    match = re.search(r"(?is)<body[^>]*>(.*?)</body>", value)
    return match.group(1) if match else value


def build_preview_html(message: dict[str, Any]) -> str:
    body_html = str(message.get("body_html") or "")
    if body_html:
        content = strip_unsafe_html(body_fragment(body_html))
    else:
        content = html.escape(str(message.get("body", ""))).replace("\n", "<br>")

    subject = html.escape(str(message.get("subject", "")))
    sender = html.escape(str(message.get("from", "")))
    date = html.escape(str(message.get("date", "")))
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    margin: 0;
    padding: 18px;
    color: #1f2933;
    background: #ffffff;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 14px;
    line-height: 1.55;
}}
.mail-header {{
    border-bottom: 1px solid #d7dde5;
    margin-bottom: 18px;
    padding-bottom: 14px;
}}
.mail-subject {{
    color: #0b4f8a;
    font-size: 20px;
    font-weight: 650;
    margin-bottom: 8px;
}}
.mail-meta {{
    color: #596775;
    margin: 3px 0;
}}
img {{
    max-width: 100%;
    height: auto;
}}
</style>
</head>
<body>
<section class="mail-header">
    <div class="mail-subject">{subject}</div>
    <div class="mail-meta"><strong>From:</strong> {sender}</div>
    <div class="mail-meta"><strong>Date:</strong> {date}</div>
</section>
<main>{content}</main>
</body>
</html>"""


class MailComHttpReader:
    def __init__(self, status: list[str], progress: ProgressCallback | None = None) -> None:
        self.status = status
        self.progress = progress
        self.current_account = ""

    def fetch_account(self, account: MailAccount, max_messages: int) -> dict[str, Any]:
        self.current_account = account.address
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

    def _set_status(self, value: str, event: str = "status", **fields: Any) -> None:
        self.status.append(value)
        if self.progress is None:
            return
        payload = {
            "event": event,
            "message": value,
            "account": self.current_account,
        }
        payload.update(fields)
        self.progress(payload)

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
                self._set_status(
                    f"Reading message #{index}",
                    event="message",
                    current_message=index,
                )
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
        body_html = ""
        body_url = ""
        if body_match:
            body_url = urljoin(detail.url, html.unescape(body_match.group(1)))
            body_response = session.get(body_url, headers={"Referer": detail.url}, timeout=60)
            body_response.raise_for_status()
            body_html = body_response.text
            body = html_to_text(body_html)

        metadata = self._extract_detail_metadata(detail.text)
        metadata["index"] = index
        metadata["body"] = body
        metadata["body_html"] = body_html
        metadata["body_url"] = body_url
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


def fetch_all(payload: dict[str, Any], progress: ProgressCallback | None = None) -> dict[str, Any]:
    accounts = [
        MailAccount(str(item["address"]), str(item["password"])) for item in payload["accounts"]
    ]
    max_messages = int(payload.get("max_messages") or 0)
    status: list[str] = []
    reader = MailComHttpReader(status=status, progress=progress)

    results: list[dict[str, Any]] = []
    if progress is not None:
        progress(
            {
                "event": "batch_start",
                "message": f"Starting {len(accounts)} account(s).",
                "total_accounts": len(accounts),
            }
        )

    for index, account in enumerate(accounts, start=1):
        if progress is not None:
            progress(
                {
                    "event": "account_start",
                    "message": f"Fetching {account.address} ({index}/{len(accounts)})",
                    "account": account.address,
                    "current_account": index,
                    "total_accounts": len(accounts),
                }
            )
        try:
            account_result = reader.fetch_account(account, max_messages)
            results.append(account_result)
            if progress is not None:
                progress(
                    {
                        "event": "account_done",
                        "message": f"Finished {account.address}: {account_result['total']} message(s).",
                        "account": account.address,
                        "messages": account_result["total"],
                        "current_account": index,
                        "total_accounts": len(accounts),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - return per-account error to GUI.
            results.append({"account": account.address, "ok": False, "error": str(exc)})
            if progress is not None:
                progress(
                    {
                        "event": "account_error",
                        "message": f"{account.address}: {exc}",
                        "account": account.address,
                        "error": str(exc),
                        "current_account": index,
                        "total_accounts": len(accounts),
                    }
                )
    if progress is not None:
        progress({"event": "batch_done", "message": "Fetch finished."})
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
        self.geometry("1280x780")
        self.minsize(1080, 680)

        self.api = LocalMailApi()
        self.api.start()
        self.max_messages = tk.IntVar(value=0)
        self.status = tk.StringVar(value=f"Local HTTP API ready: {self.api.url}/fetch-mails")
        self.progress_events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.busy = False
        self.accounts: list[MailAccount] = []
        self.results_by_account: dict[str, dict[str, Any]] = {}
        self.account_by_iid: dict[str, str] = {}
        self.account_iid_by_address: dict[str, str] = {}
        self.message_by_iid: dict[str, dict[str, Any]] = {}
        self.preview_html: Any | None = None
        self.preview_text: tk.Text | None = None

        self._build_ui()
        self.after(100, self._drain_progress_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(root, text="Batch accounts")
        top.grid(row=0, column=0, sticky=tk.NSEW)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(0, weight=1)
        self.accounts_text = tk.Text(top, height=4, wrap=tk.NONE)
        self.accounts_text.grid(row=0, column=0, sticky=tk.NSEW, padx=(8, 0), pady=8)
        account_scrollbar = ttk.Scrollbar(top, command=self.accounts_text.yview)
        account_scrollbar.grid(row=0, column=1, sticky=tk.NS, pady=8)
        self.accounts_text.configure(yscrollcommand=account_scrollbar.set)

        import_actions = ttk.Frame(top)
        import_actions.grid(row=0, column=2, sticky=tk.N, padx=8, pady=8)
        self.import_button = ttk.Button(
            import_actions,
            text="Import file",
            command=self.import_accounts,
        )
        self.import_button.grid(row=0, column=0, sticky=tk.EW, pady=(0, 6))
        self.validate_button = ttk.Button(
            import_actions,
            text="Validate list",
            command=self.validate_accounts,
        )
        self.validate_button.grid(row=1, column=0, sticky=tk.EW)

        panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))

        accounts_frame = ttk.LabelFrame(panes, text="Accounts")
        accounts_frame.columnconfigure(0, weight=1)
        accounts_frame.rowconfigure(0, weight=1)
        self.account_tree = ttk.Treeview(
            accounts_frame,
            columns=("status", "messages"),
            show="tree headings",
            selectmode="browse",
        )
        self.account_tree.heading("#0", text="Account")
        self.account_tree.heading("status", text="Status")
        self.account_tree.heading("messages", text="Mail")
        self.account_tree.column("#0", width=190, minwidth=150, stretch=True)
        self.account_tree.column("status", width=95, anchor=tk.CENTER, stretch=False)
        self.account_tree.column("messages", width=60, anchor=tk.CENTER, stretch=False)
        self.account_tree.grid(row=0, column=0, sticky=tk.NSEW)
        account_tree_scrollbar = ttk.Scrollbar(accounts_frame, command=self.account_tree.yview)
        account_tree_scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.account_tree.configure(yscrollcommand=account_tree_scrollbar.set)
        self.account_tree.bind("<<TreeviewSelect>>", self._on_account_select)

        inbox_frame = ttk.LabelFrame(panes, text="Inbox")
        inbox_frame.columnconfigure(0, weight=1)
        inbox_frame.rowconfigure(0, weight=1)
        self.message_tree = ttk.Treeview(
            inbox_frame,
            columns=("date", "sender", "subject"),
            show="headings",
            selectmode="browse",
        )
        self.message_tree.heading("date", text="Date")
        self.message_tree.heading("sender", text="From")
        self.message_tree.heading("subject", text="Subject")
        self.message_tree.column("date", width=150, minwidth=120, stretch=False)
        self.message_tree.column("sender", width=170, minwidth=130, stretch=False)
        self.message_tree.column("subject", width=310, minwidth=180, stretch=True)
        self.message_tree.grid(row=0, column=0, sticky=tk.NSEW)
        message_tree_scrollbar = ttk.Scrollbar(inbox_frame, command=self.message_tree.yview)
        message_tree_scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.message_tree.configure(yscrollcommand=message_tree_scrollbar.set)
        self.message_tree.bind("<<TreeviewSelect>>", self._on_message_select)

        preview_frame = ttk.LabelFrame(panes, text="Message")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        if HtmlFrame is not None:
            self.preview_html = HtmlFrame(
                preview_frame,
                messages_enabled=False,
                javascript_enabled=False,
                forms_enabled=False,
                objects_enabled=False,
                images_enabled=True,
                stylesheets_enabled=True,
                vertical_scrollbar=True,
                horizontal_scrollbar="auto",
            )
            self.preview_html.grid(row=0, column=0, sticky=tk.NSEW)
        else:
            self.preview_text = tk.Text(preview_frame, wrap=tk.WORD, state=tk.DISABLED)
            self.preview_text.grid(row=0, column=0, sticky=tk.NSEW)
            preview_scrollbar = ttk.Scrollbar(preview_frame, command=self.preview_text.yview)
            preview_scrollbar.grid(row=0, column=1, sticky=tk.NS)
            self.preview_text.configure(yscrollcommand=preview_scrollbar.set)

        panes.add(accounts_frame, weight=1)
        panes.add(inbox_frame, weight=2)
        panes.add(preview_frame, weight=3)

        actions = ttk.Frame(root)
        actions.grid(row=2, column=0, sticky=tk.EW, pady=(10, 0))
        actions.columnconfigure(5, weight=1)
        ttk.Label(actions, text="Max mails, 0 = all").grid(row=0, column=0, padx=(0, 6))
        self.max_messages_spinbox = ttk.Spinbox(
            actions,
            from_=0,
            to=99999,
            textvariable=self.max_messages,
            width=8,
        )
        self.max_messages_spinbox.grid(
            row=0, column=1, padx=(0, 14)
        )
        self.fetch_button = ttk.Button(actions, text="Fetch all emails", command=self.fetch_emails)
        self.fetch_button.grid(row=0, column=2, padx=(0, 12))
        self.progress_bar = ttk.Progressbar(actions, mode="determinate", length=210)
        self.progress_bar.grid(row=0, column=3, padx=(0, 12), sticky=tk.EW)
        ttk.Label(actions, textvariable=self.status).grid(row=0, column=5, sticky=tk.W)

        self._set_empty_preview("Select an account and message.")

    def fetch_emails(self) -> None:
        if self.busy:
            return
        try:
            accounts = self._validate_accounts()
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
        self.results_by_account = {}
        for account in accounts:
            self._update_account_row(account.address, status="Queued", messages="0")
        self._clear_messages()
        self._set_controls_state(tk.DISABLED)
        self.progress_bar.configure(mode="indeterminate", maximum=100, value=0)
        self.progress_bar.start(12)
        self.status.set("Starting direct HTTP fetch...")
        threading.Thread(target=self._worker, args=(payload,), daemon=True).start()

    def _worker(self, payload: dict[str, Any]) -> None:
        try:
            response = fetch_all(payload, progress=self.progress_events.put)
            self.progress_events.put({"event": "results", "response": response})
        except Exception as exc:  # noqa: BLE001
            self.progress_events.put({"event": "fatal_error", "error": str(exc)})

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

    def import_accounts(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Import accounts",
            filetypes=[
                ("Account files", "*.txt *.csv"),
                ("Text files", "*.txt"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        try:
            content = load_accounts_file(path)
            self.accounts_text.delete("1.0", tk.END)
            self.accounts_text.insert(tk.END, content)
            self._validate_accounts()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import failed", str(exc))

    def validate_accounts(self) -> None:
        try:
            self._validate_accounts()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Invalid input", str(exc))

    def _validate_accounts(self) -> list[MailAccount]:
        accounts = parse_accounts(self.accounts_text.get("1.0", tk.END))
        self.accounts = accounts
        self.results_by_account = {}
        self._populate_account_rows(accounts)
        self._clear_messages()
        self.progress_bar.stop()
        self.progress_bar.configure(
            mode="determinate",
            maximum=max(1, len(accounts)),
            value=len(accounts),
        )
        self.status.set(f"Validated {len(accounts)} account(s).")
        return accounts

    def _populate_account_rows(self, accounts: list[MailAccount]) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        self.account_by_iid = {}
        self.account_iid_by_address = {}
        for index, account in enumerate(accounts, start=1):
            iid = f"account-{index}"
            self.account_tree.insert("", tk.END, iid=iid, text=account.address, values=("Ready", "0"))
            self.account_by_iid[iid] = account.address
            self.account_iid_by_address[account.address] = iid

    def _set_controls_state(self, state: str) -> None:
        for widget in (
            self.import_button,
            self.validate_button,
            self.fetch_button,
            self.max_messages_spinbox,
        ):
            widget.configure(state=state)

    def _drain_progress_events(self) -> None:
        try:
            while True:
                event = self.progress_events.get_nowait()
                self._apply_progress_event(event)
        except queue.Empty:
            pass
        self.after(100, self._drain_progress_events)

    def _apply_progress_event(self, event: dict[str, Any]) -> None:
        event_name = event.get("event")
        message = str(event.get("message") or "")
        account = str(event.get("account") or "")
        if message:
            self.status.set(message)

        if event_name == "account_start" and account:
            self._update_account_row(account, status="Fetching")
        elif event_name == "message" and account:
            self._update_account_row(
                account,
                status=f"Reading #{event.get('current_message', '')}",
            )
        elif event_name == "account_done" and account:
            self._update_account_row(
                account,
                status="Done",
                messages=str(event.get("messages", 0)),
            )
        elif event_name == "account_error" and account:
            self._update_account_row(account, status="Error")
        elif event_name == "results":
            self._finish_fetch(event.get("response", {}))
        elif event_name == "fatal_error":
            self._finish_fetch_error(str(event.get("error") or "Unknown error"))

    def _update_account_row(
        self,
        account: str,
        *,
        status: str | None = None,
        messages: str | None = None,
    ) -> None:
        iid = self.account_iid_by_address.get(account)
        if not iid or not self.account_tree.exists(iid):
            return
        if status is not None:
            self.account_tree.set(iid, "status", status)
        if messages is not None:
            self.account_tree.set(iid, "messages", messages)

    def _finish_fetch(self, response: dict[str, Any]) -> None:
        self.progress_bar.stop()
        total = max(1, len(self.accounts))
        self.progress_bar.configure(mode="determinate", maximum=total, value=total)
        self.results_by_account = {
            str(result.get("account", "")): result for result in response.get("results", [])
        }
        for result in response.get("results", []):
            account = str(result.get("account", ""))
            if result.get("ok"):
                self._update_account_row(
                    account,
                    status="Done",
                    messages=str(result.get("total", 0)),
                )
            else:
                self._update_account_row(account, status="Error")
        self.busy = False
        self._set_controls_state(tk.NORMAL)
        self.status.set("Done. Passwords were not saved.")

        children = self.account_tree.get_children()
        if children:
            self.account_tree.selection_set(children[0])
            self.account_tree.focus(children[0])
            self._show_selected_account()

    def _finish_fetch_error(self, error: str) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.busy = False
        self._set_controls_state(tk.NORMAL)
        self.status.set(f"Failed: {error}")
        messagebox.showerror("Fetch failed", error)

    def _on_account_select(self, _event: tk.Event | None = None) -> None:
        self._show_selected_account()

    def _show_selected_account(self) -> None:
        selection = self.account_tree.selection()
        if not selection:
            return
        account = self.account_by_iid.get(selection[0], "")
        result = self.results_by_account.get(account)
        self._populate_messages(result)

    def _populate_messages(self, result: dict[str, Any] | None) -> None:
        self.message_tree.delete(*self.message_tree.get_children())
        self.message_by_iid = {}
        if not result:
            self._set_empty_preview("No messages loaded for this account.")
            return
        if not result.get("ok"):
            self._set_empty_preview(f"ERROR: {result.get('error', 'Unknown error')}")
            return

        messages = sorted(
            result.get("messages", []),
            key=message_sort_key,
            reverse=True,
        )
        if not messages:
            self._set_empty_preview("Inbox is empty.")
            return

        for index, message in enumerate(messages, start=1):
            iid = f"message-{index}"
            self.message_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    str(message.get("date", "")),
                    str(message.get("from", "")),
                    str(message.get("subject", "")),
                ),
            )
            self.message_by_iid[iid] = message

        first = self.message_tree.get_children()[0]
        self.message_tree.selection_set(first)
        self.message_tree.focus(first)
        self._show_selected_message()

    def _on_message_select(self, _event: tk.Event | None = None) -> None:
        self._show_selected_message()

    def _show_selected_message(self) -> None:
        selection = self.message_tree.selection()
        if not selection:
            return
        message = self.message_by_iid.get(selection[0])
        if not message:
            return
        html_document = build_preview_html(message)
        if self.preview_html is not None:
            try:
                self.preview_html.load_html(
                    html_document,
                    base_url=str(message.get("body_url") or "") or None,
                )
                return
            except Exception as exc:  # noqa: BLE001
                self.status.set(f"HTML preview failed: {exc}")

        self._set_plain_preview(
            "\n".join(
                [
                    f"Subject: {message.get('subject', '')}",
                    f"From: {message.get('from', '')}",
                    f"Date: {message.get('date', '')}",
                    "",
                    str(message.get("body", "")),
                ]
            )
        )

    def _clear_messages(self) -> None:
        self.message_tree.delete(*self.message_tree.get_children())
        self.message_by_iid = {}
        self._set_empty_preview("Select an account and message.")

    def _set_empty_preview(self, text: str) -> None:
        if self.preview_html is not None:
            self.preview_html.load_html(
                f"""<!doctype html>
<html><body style="font-family: Segoe UI, Arial, sans-serif; color: #667085; padding: 18px;">
{html.escape(text)}
</body></html>"""
            )
            return
        self._set_plain_preview(text)

    def _set_plain_preview(self, text: str) -> None:
        if self.preview_text is None:
            return
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, text)
        self.preview_text.configure(state=tk.DISABLED)

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
