"""Win11 GUI for reading mail.com messages with direct HTTP requests.

The GUI posts accounts to a local 127.0.0.1 HTTP endpoint. The endpoint performs
the same mail.com web flow with requests only: login form, lightmailer startup,
folder list, message list, message detail, and message body.
"""

from __future__ import annotations

import csv
import ctypes
import html
import json
import os
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from tkinterweb import HtmlFrame
except Exception:  # noqa: BLE001 - keep the app usable without optional HTML preview support.
    HtmlFrame = None


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765
MAIL_HOME_URL = "https://www.mail.com/"
LIGHT_START_URL = "https://lightmailer.mail.com/start?device=desktop&ott={ott}"
MAX_FETCH_WORKERS = 5
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
STATE_PATH = STATE_DIR / "ccGptMailReader" / "mail_reader_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
APP_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"


def enable_high_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            return


def colorref(hex_color: str) -> int:
    value = hex_color.lstrip("#")
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return red | (green << 8) | (blue << 16)


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
            errors.append(f"第 {line_number} 行：格式应为 email----密码")
            continue
        address, password = line.split("----", 1)
        address = address.strip()
        password = password.strip()
        if "@" not in address or not password:
            errors.append(f"第 {line_number} 行：账号或密码无效")
            continue
        accounts.append(MailAccount(address, password))

    if errors:
        raise ValueError("\n".join(errors))
    if not accounts:
        raise ValueError("请至少输入一个 email----密码 格式的账号。")
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


def count_account_lines(raw_text: str) -> tuple[int, int]:
    valid = 0
    invalid = 0
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "----" not in line:
            invalid += 1
            continue
        address, password = line.split("----", 1)
        if "@" in address.strip() and password.strip():
            valid += 1
        else:
            invalid += 1
    return valid, invalid


def account_lines_from_text(raw_text: str) -> dict[str, MailAccount]:
    output: dict[str, MailAccount] = {}
    for account in parse_accounts(raw_text):
        output[account.address] = account
    return output


def now_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_saved_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_saved_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_saved_state() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        return


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
        raise RuntimeError("未找到 mail.com 登录表单。")

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
            "mail.com 未返回 lightmailer 登录令牌。"
            "该账号可能需要验证码、额外验证或浏览器 Cookie。"
        )
    return match.group(1)


def extract_wicket_redirect(xml_text: str) -> str:
    match = re.search(r"<redirect><!\[CDATA\[(.*?)\]\]></redirect>", xml_text)
    if not match:
        raise RuntimeError("mail.com lightmailer 未返回启动跳转。")
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
    font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
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
    <div class="mail-meta"><strong>发件人：</strong> {sender}</div>
    <div class="mail-meta"><strong>时间：</strong> {date}</div>
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
        self._set_status(f"正在登录：{account.address}")

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
            raise RuntimeError("登录成功，但文件夹列表未加载。")
        self._set_status(f"登录成功：{account.address}")
        return folder.url, folder.text

    def _find_inbox_url(self, folder_url: str, folder_html: str) -> str:
        match = re.search(
            r'href="(\./messagelist\?folderId=[^"]+)"[^>]+data-webdriver="INBOX:Inbox"',
            folder_html,
        )
        if not match:
            raise RuntimeError("未找到收件箱链接。")
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
                self._set_status("收件箱为空。")
                return []

            for detail_url in detail_urls:
                if max_messages and len(messages) >= max_messages:
                    return messages
                index = len(messages) + 1
                self._set_status(
                    f"正在读取第 {index} 封邮件",
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
    worker_count = max(1, min(int(payload.get("max_workers") or MAX_FETCH_WORKERS), len(accounts)))
    status: list[str] = []
    if progress is not None:
        progress(
            {
                "event": "batch_start",
                "message": f"开始处理 {len(accounts)} 个账号，并发数 {worker_count}。",
                "total_accounts": len(accounts),
                "max_workers": worker_count,
            }
        )

    def fetch_one(index: int, account: MailAccount) -> tuple[int, dict[str, Any]]:
        if progress is not None:
            progress(
                {
                    "event": "account_start",
                    "message": f"正在收取 {account.address}（{index}/{len(accounts)}）",
                    "account": account.address,
                    "current_account": index,
                    "total_accounts": len(accounts),
                }
            )
        try:
            reader = MailComHttpReader(status=status, progress=progress)
            account_result = reader.fetch_account(account, max_messages)
            if progress is not None:
                progress(
                    {
                        "event": "account_done",
                        "message": f"{account.address} 已完成：{account_result['total']} 封邮件。",
                        "account": account.address,
                        "messages": account_result["total"],
                        "current_account": index,
                        "total_accounts": len(accounts),
                        "result": account_result,
                    }
                )
            return index, account_result
        except Exception as exc:  # noqa: BLE001 - return per-account error to GUI.
            account_result = {"account": account.address, "ok": False, "error": str(exc)}
            if progress is not None:
                progress(
                    {
                        "event": "account_error",
                        "message": f"{account.address} 失败：{exc}",
                        "account": account.address,
                        "error": str(exc),
                        "current_account": index,
                        "total_accounts": len(accounts),
                        "result": account_result,
                    }
                )
            return index, account_result

    results_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_one, index, account)
            for index, account in enumerate(accounts, start=1)
        ]
        for future in as_completed(futures):
            index, account_result = future.result()
            results_by_index[index] = account_result

    results = [results_by_index[index] for index in sorted(results_by_index)]
    if progress is not None:
        progress({"event": "batch_done", "message": "收取完成。", "results": results})
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
        enable_high_dpi_awareness()
        super().__init__()
        self.title("mail.com 邮件管理台")
        self.geometry("1440x860")
        self.minsize(1180, 740)
        self.configure(bg="#f5f7fb")
        self.tk.call("tk", "scaling", max(1.0, min(1.6, self.winfo_fpixels("1i") / 72)))

        self.api = LocalMailApi()
        self.api.start()
        self.max_messages = tk.IntVar(value=0)
        self.max_workers = tk.IntVar(value=MAX_FETCH_WORKERS)
        self.status = tk.StringVar(value=f"就绪。本地接口：{self.api.url}/fetch-mails")
        self.import_summary = tk.StringVar(value="粘贴 email----密码，或导入账号文件。")
        self.total_accounts_summary = tk.StringVar(value="0")
        self.loaded_accounts_summary = tk.StringVar(value="0")
        self.message_summary = tk.StringVar(value="0")
        self.error_summary = tk.StringVar(value="0")
        self.saved_summary = tk.StringVar(value="暂无已恢复会话。")
        self.progress_events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.busy = False
        self.accounts: list[MailAccount] = []
        self.active_accounts: set[str] = set()
        self.completed_jobs = 0
        self.current_job_total = 0
        self.results_by_account: dict[str, dict[str, Any]] = {}
        self.account_by_iid: dict[str, str] = {}
        self.account_iid_by_address: dict[str, str] = {}
        self.message_by_iid: dict[str, dict[str, Any]] = {}
        self.preview_after_id: str | None = None
        self.preview_html: Any | None = None
        self.preview_text: tk.Text | None = None

        self._configure_fonts()
        self._configure_style()
        self._build_ui()
        self._load_state_into_ui()
        self._apply_window_chrome()
        self.after(100, self._drain_progress_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure(".", font=(APP_FONT, 10))
        style.configure("TFrame", background="#f5f7fb")
        style.configure("Panel.TFrame", background="#ffffff", relief=tk.FLAT)
        style.configure("TLabel", background="#f5f7fb", foreground="#1f2937")
        style.configure("Panel.TLabel", background="#ffffff", foreground="#111827")
        style.configure("Muted.TLabel", background="#ffffff", foreground="#64748b")
        style.configure("KpiTitle.TLabel", background="#ffffff", foreground="#667085", font=(APP_FONT, 9))
        style.configure("KpiValue.TLabel", background="#ffffff", foreground="#101828", font=(APP_FONT, 20, "bold"))
        style.configure("TLabelframe", background="#f5f7fb", bordercolor="#e5e7eb", relief=tk.FLAT)
        style.configure("TLabelframe.Label", background="#f5f7fb", foreground="#344054", font=(APP_FONT, 11, "bold"))
        style.configure("TButton", padding=(14, 7), font=(APP_FONT, 10))
        style.configure("Accent.TButton", padding=(16, 8), font=(APP_FONT, 10, "bold"))
        style.configure("Treeview", rowheight=32, background="#ffffff", fieldbackground="#ffffff", foreground="#1f2937", font=(APP_FONT, 10))
        style.configure("Treeview.Heading", background="#f2f4f7", foreground="#344054", font=(APP_FONT, 10, "bold"))
        style.configure("Horizontal.TProgressbar", thickness=12)

    def _configure_fonts(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family=APP_FONT, size=10)
            except tk.TclError:
                continue
        try:
            tkfont.nametofont("TkFixedFont").configure(family=MONO_FONT, size=10)
        except tk.TclError:
            pass

    def _apply_window_chrome(self) -> None:
        if os.name != "nt":
            return
        self.update_idletasks()
        try:
            hwnd = self.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            caption = ctypes.c_int(colorref("#0b5cad"))
            text = ctypes.c_int(colorref("#ffffff"))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), ctypes.sizeof(text))
        except (AttributeError, OSError, tk.TclError):
            return

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        header = tk.Frame(root, bg="#0b5cad", height=78)
        header.grid(row=0, column=0, sticky=tk.EW)
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="邮件批量管理台",
            bg="#0b5cad",
            fg="#ffffff",
            font=(APP_FONT, 22, "bold"),
        ).grid(row=0, column=0, sticky=tk.W, padx=18, pady=(12, 0))
        tk.Label(
            header,
            text="多账号并发收取、实时收件箱预览、本地会话恢复",
            bg="#0b5cad",
            fg="#dbeafe",
            font=(APP_FONT, 10),
        ).grid(row=1, column=0, sticky=tk.W, padx=18, pady=(0, 12))

        kpis = ttk.Frame(root, style="Panel.TFrame", padding=10)
        kpis.grid(row=1, column=0, sticky=tk.EW, pady=(10, 0))
        for column in range(4):
            kpis.columnconfigure(column, weight=1)
        self._kpi(kpis, 0, "账号总数", self.total_accounts_summary)
        self._kpi(kpis, 1, "已加载", self.loaded_accounts_summary)
        self._kpi(kpis, 2, "邮件数量", self.message_summary)
        self._kpi(kpis, 3, "异常账号", self.error_summary)

        import_frame = ttk.LabelFrame(root, text="批量导入")
        import_frame.grid(row=2, column=0, sticky=tk.NSEW, pady=(10, 0))
        import_frame.columnconfigure(0, weight=1)
        import_frame.rowconfigure(0, weight=1)
        self.accounts_text = tk.Text(
            import_frame,
            height=4,
            wrap=tk.NONE,
            bg="#ffffff",
            fg="#111827",
            insertbackground="#111827",
            relief=tk.FLAT,
            padx=10,
            pady=8,
            font=(MONO_FONT, 10),
        )
        self.accounts_text.grid(row=0, column=0, sticky=tk.NSEW, padx=(8, 0), pady=(8, 2))
        self.accounts_text.bind("<<Modified>>", self._on_accounts_text_modified)
        account_scrollbar = ttk.Scrollbar(import_frame, command=self.accounts_text.yview)
        account_scrollbar.grid(row=0, column=1, sticky=tk.NS, pady=(8, 2))
        self.accounts_text.configure(yscrollcommand=account_scrollbar.set)
        ttk.Label(import_frame, textvariable=self.import_summary).grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=8,
            pady=(0, 8),
        )

        import_actions = ttk.Frame(import_frame)
        import_actions.grid(row=0, column=2, rowspan=2, sticky=tk.N, padx=10, pady=8)
        self.import_button = ttk.Button(import_actions, text="导入文件", command=self.import_accounts)
        self.import_button.grid(row=0, column=0, sticky=tk.EW, pady=(0, 6))
        self.validate_button = ttk.Button(import_actions, text="验证列表", command=self.validate_accounts)
        self.validate_button.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        self.clear_button = ttk.Button(import_actions, text="清除缓存", command=self.clear_saved_data)
        self.clear_button.grid(row=2, column=0, sticky=tk.EW)

        panes = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panes.grid(row=3, column=0, sticky=tk.NSEW, pady=(10, 0))

        accounts_frame = ttk.LabelFrame(panes, text="账号任务")
        accounts_frame.columnconfigure(0, weight=1)
        accounts_frame.rowconfigure(1, weight=1)
        task_toolbar = ttk.Frame(accounts_frame, padding=(6, 6, 6, 2))
        task_toolbar.grid(row=0, column=0, columnspan=2, sticky=tk.EW)
        self.refresh_button = ttk.Button(task_toolbar, text="刷新选中", command=self.refresh_selected_account)
        self.refresh_button.grid(row=0, column=0, padx=(0, 6))
        self.delete_button = ttk.Button(task_toolbar, text="删除选中", command=self.delete_selected_account)
        self.delete_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Label(task_toolbar, textvariable=self.saved_summary).grid(row=0, column=2, sticky=tk.W, padx=(8, 0))
        task_toolbar.columnconfigure(2, weight=1)

        self.account_tree = ttk.Treeview(
            accounts_frame,
            columns=("status", "messages", "updated"),
            show="tree headings",
            selectmode="browse",
        )
        self.account_tree.heading("#0", text="账号")
        self.account_tree.heading("status", text="状态")
        self.account_tree.heading("messages", text="邮件")
        self.account_tree.heading("updated", text="更新时间")
        self.account_tree.column("#0", width=230, minwidth=180, stretch=True)
        self.account_tree.column("status", width=110, anchor=tk.CENTER, stretch=False)
        self.account_tree.column("messages", width=62, anchor=tk.CENTER, stretch=False)
        self.account_tree.column("updated", width=140, minwidth=120, stretch=False)
        self.account_tree.grid(row=1, column=0, sticky=tk.NSEW, padx=(6, 0), pady=(0, 6))
        account_tree_scrollbar = ttk.Scrollbar(accounts_frame, command=self.account_tree.yview)
        account_tree_scrollbar.grid(row=1, column=1, sticky=tk.NS, pady=(0, 6), padx=(0, 6))
        self.account_tree.configure(yscrollcommand=account_tree_scrollbar.set)
        self.account_tree.bind("<<TreeviewSelect>>", self._on_account_select)
        self.account_tree.bind("<Button-3>", self._show_account_menu)
        self.account_tree.tag_configure("done", foreground="#166534")
        self.account_tree.tag_configure("error", foreground="#b91c1c")
        self.account_tree.tag_configure("running", foreground="#0f5f8f")
        self.account_tree.tag_configure("saved", foreground="#475569")

        self.account_menu = tk.Menu(self, tearoff=0)
        self.account_menu.add_command(label="刷新选中", command=self.refresh_selected_account)
        self.account_menu.add_command(label="删除选中", command=self.delete_selected_account)

        inbox_frame = ttk.LabelFrame(panes, text="收件箱")
        inbox_frame.columnconfigure(0, weight=1)
        inbox_frame.rowconfigure(0, weight=1)
        self.message_tree = ttk.Treeview(
            inbox_frame,
            columns=("date", "sender", "subject"),
            show="headings",
            selectmode="browse",
        )
        self.message_tree.heading("date", text="时间")
        self.message_tree.heading("sender", text="发件人")
        self.message_tree.heading("subject", text="主题")
        self.message_tree.column("date", width=165, minwidth=130, stretch=False)
        self.message_tree.column("sender", width=180, minwidth=130, stretch=False)
        self.message_tree.column("subject", width=350, minwidth=220, stretch=True)
        self.message_tree.grid(row=0, column=0, sticky=tk.NSEW, padx=(6, 0), pady=6)
        message_tree_scrollbar = ttk.Scrollbar(inbox_frame, command=self.message_tree.yview)
        message_tree_scrollbar.grid(row=0, column=1, sticky=tk.NS, pady=6, padx=(0, 6))
        self.message_tree.configure(yscrollcommand=message_tree_scrollbar.set)
        self.message_tree.bind("<<TreeviewSelect>>", self._on_message_select)

        preview_frame = ttk.LabelFrame(panes, text="邮件预览")
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
            self.preview_html.grid(row=0, column=0, sticky=tk.NSEW, padx=6, pady=6)
        else:
            self.preview_text = tk.Text(preview_frame, wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT)
            self.preview_text.grid(row=0, column=0, sticky=tk.NSEW, padx=(6, 0), pady=6)
            preview_scrollbar = ttk.Scrollbar(preview_frame, command=self.preview_text.yview)
            preview_scrollbar.grid(row=0, column=1, sticky=tk.NS, pady=6, padx=(0, 6))
            self.preview_text.configure(yscrollcommand=preview_scrollbar.set)

        panes.add(accounts_frame, weight=1)
        panes.add(inbox_frame, weight=2)
        panes.add(preview_frame, weight=3)

        actions = ttk.Frame(root)
        actions.grid(row=4, column=0, sticky=tk.EW, pady=(10, 0))
        actions.columnconfigure(8, weight=1)
        ttk.Label(actions, text="每号最多邮件，0=全部").grid(row=0, column=0, padx=(0, 6))
        self.max_messages_spinbox = ttk.Spinbox(actions, from_=0, to=99999, textvariable=self.max_messages, width=8)
        self.max_messages_spinbox.grid(row=0, column=1, padx=(0, 14))
        ttk.Label(actions, text="并发数").grid(row=0, column=2, padx=(0, 6))
        self.max_workers_spinbox = ttk.Spinbox(actions, from_=1, to=10, textvariable=self.max_workers, width=5)
        self.max_workers_spinbox.grid(row=0, column=3, padx=(0, 14))
        self.fetch_button = ttk.Button(
            actions,
            text="开始收取",
            style="Accent.TButton",
            command=self.fetch_emails,
        )
        self.fetch_button.grid(row=0, column=4, padx=(0, 12))
        self.progress_bar = ttk.Progressbar(actions, mode="determinate", length=240)
        self.progress_bar.grid(row=0, column=5, padx=(0, 12), sticky=tk.EW)
        ttk.Label(actions, textvariable=self.status).grid(row=0, column=8, sticky=tk.W)

        self._set_empty_preview("请选择账号和邮件。")

    def _kpi(self, parent: ttk.Frame, column: int, title: str, value: tk.StringVar) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(16, 10))
        frame.grid(row=0, column=column, sticky=tk.EW, padx=6)
        ttk.Label(frame, text=title, style="KpiTitle.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=value, style="KpiValue.TLabel").grid(row=1, column=0, sticky=tk.W)

    def fetch_emails(self) -> None:
        if self.busy:
            return
        try:
            accounts = self._validate_accounts()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("输入有误", str(exc))
            return
        self._start_fetch(accounts)

    def refresh_selected_account(self) -> None:
        if self.busy:
            messagebox.showinfo("正在收取", "请等待当前收取任务完成后再刷新。")
            return
        address = self._selected_account_address()
        if not address:
            messagebox.showinfo("未选择账号", "请选择一个需要刷新的账号。")
            return
        try:
            credentials = account_lines_from_text(self.accounts_text.get("1.0", tk.END))
        except ValueError as exc:
            messagebox.showwarning("输入有误", str(exc))
            return
        account = credentials.get(address)
        if account is None:
            messagebox.showwarning(
                "需要密码",
                "刷新前请先在导入框中粘贴该账号的 email----密码。",
            )
            return
        self._start_fetch([account])

    def delete_selected_account(self) -> None:
        address = self._selected_account_address()
        if not address:
            messagebox.showinfo("未选择账号", "请选择一个需要删除的账号。")
            return
        if address in self.active_accounts:
            messagebox.showinfo("账号运行中", "该账号正在收取邮件，请完成后再删除。")
            return
        iid = self.account_iid_by_address.pop(address, "")
        if iid and self.account_tree.exists(iid):
            self.account_tree.delete(iid)
        self.account_by_iid = {row: account for row, account in self.account_by_iid.items() if account != address}
        self.results_by_account.pop(address, None)
        self.accounts = [account for account in self.accounts if account.address != address]
        self._remove_account_from_input(address)
        self._clear_messages()
        self._save_state()
        self._refresh_summaries()
        self.status.set(f"已删除 {address}。")

    def clear_saved_data(self) -> None:
        if self.busy:
            messagebox.showinfo("正在收取", "请等待当前收取任务完成后再清除缓存。")
            return
        if not messagebox.askyesno("清除缓存", "确定清除本机保存的邮件结果和账号任务吗？"):
            return
        clear_saved_state()
        self.accounts_text.delete("1.0", tk.END)
        self.accounts = []
        self.results_by_account = {}
        self.account_by_iid = {}
        self.account_iid_by_address = {}
        self.account_tree.delete(*self.account_tree.get_children())
        self._clear_messages()
        self._refresh_summaries()
        self.saved_summary.set("已清除本地会话。")
        self.status.set("缓存已清除。")
        self.import_summary.set("粘贴 email----密码，或导入账号文件。")

    def import_accounts(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="导入账号",
            filetypes=[
                ("账号文件", "*.txt *.csv"),
                ("文本文件", "*.txt"),
                ("CSV 文件", "*.csv"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        try:
            content = load_accounts_file(path)
            self.accounts_text.delete("1.0", tk.END)
            self.accounts_text.insert(tk.END, content)
            accounts = self._validate_accounts()
            self.status.set(f"已从 {Path(path).name} 导入 {len(accounts)} 个账号。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc))

    def validate_accounts(self) -> None:
        try:
            accounts = self._validate_accounts()
            self.status.set(f"已验证 {len(accounts)} 个账号。")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("输入有误", str(exc))

    def _validate_accounts(self) -> list[MailAccount]:
        accounts = parse_accounts(self.accounts_text.get("1.0", tk.END))
        self.accounts = accounts
        for account in accounts:
            result = self.results_by_account.get(account.address)
            if result and result.get("ok"):
                self._ensure_account_row(
                    account.address,
                    status="完成",
                    messages=str(result.get("total", 0)),
                    updated=str(result.get("updated_at", "")),
                )
            elif result:
                self._ensure_account_row(account.address, status="失败", messages="0", updated=str(result.get("updated_at", "")))
            else:
                self._ensure_account_row(account.address, status="就绪", messages="0", updated="")
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=max(1, len(accounts)), value=0)
        self._update_import_summary()
        self._refresh_summaries()
        return accounts

    def _start_fetch(self, accounts: list[MailAccount]) -> None:
        if not accounts:
            return
        max_messages = int(self.max_messages.get())
        max_workers = max(1, min(int(self.max_workers.get()), len(accounts)))
        payload = {
            "accounts": [
                {"address": account.address, "password": account.password} for account in accounts
            ],
            "max_messages": max_messages,
            "max_workers": max_workers,
        }
        self.busy = True
        self.completed_jobs = 0
        self.current_job_total = len(accounts)
        self.active_accounts = {account.address for account in accounts}
        for account in accounts:
            self._ensure_account_row(account.address, status="排队中", messages="0", updated="")
        self._set_controls_state(tk.DISABLED)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=len(accounts), value=0)
        self.status.set(f"开始收取 {len(accounts)} 个账号，并发数 {max_workers}...")
        threading.Thread(target=self._worker, args=(payload,), daemon=True).start()

    def _worker(self, payload: dict[str, Any]) -> None:
        try:
            response = fetch_all(payload, progress=self.progress_events.put)
            self.progress_events.put({"event": "results", "response": response})
        except Exception as exc:  # noqa: BLE001
            self.progress_events.put({"event": "fatal_error", "error": str(exc)})

    def _set_controls_state(self, state: str) -> None:
        for widget in (
            self.import_button,
            self.validate_button,
            self.clear_button,
            self.refresh_button,
            self.delete_button,
            self.fetch_button,
            self.max_messages_spinbox,
            self.max_workers_spinbox,
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
            self._update_account_row(account, status="收取中", updated="")
        elif event_name == "message" and account:
            self._update_account_row(account, status=f"读取第 {event.get('current_message', '')} 封")
        elif event_name == "account_done" and account:
            result = dict(event.get("result") or {})
            result["updated_at"] = now_label()
            self.results_by_account[account] = result
            self.active_accounts.discard(account)
            self._mark_job_completed()
            self._update_account_row(
                account,
                status="完成",
                messages=str(result.get("total", 0)),
                updated=str(result.get("updated_at", "")),
            )
            self._save_state()
            self._refresh_summaries()
            if self._selected_account_address() == account or not self.message_tree.get_children():
                self._select_account(account)
                self._populate_messages(result)
        elif event_name == "account_error" and account:
            error_result = dict(event.get("result") or {})
            error_result["updated_at"] = now_label()
            existing = self.results_by_account.get(account)
            if existing and existing.get("ok"):
                existing["last_error"] = error_result.get("error", "Unknown error")
                existing["updated_at"] = error_result["updated_at"]
                result = existing
            else:
                result = error_result
                self.results_by_account[account] = result
            self.active_accounts.discard(account)
            self._mark_job_completed()
            self._update_account_row(
                account,
                status="失败",
                messages=str(result.get("total", 0) if result.get("ok") else 0),
                updated=str(result.get("updated_at", "")),
            )
            self._save_state()
            self._refresh_summaries()
            if self._selected_account_address() == account:
                self._populate_messages(result)
        elif event_name == "results":
            self._finish_fetch(event.get("response", {}))
        elif event_name == "fatal_error":
            self._finish_fetch_error(str(event.get("error") or "Unknown error"))

    def _mark_job_completed(self) -> None:
        self.completed_jobs = min(self.current_job_total, self.completed_jobs + 1)
        self.progress_bar.configure(value=self.completed_jobs)

    def _finish_fetch(self, response: dict[str, Any]) -> None:
        for result in response.get("results", []):
            account = str(result.get("account", ""))
            if account and account not in self.results_by_account:
                result["updated_at"] = now_label()
                self.results_by_account[account] = result
        self.busy = False
        self.active_accounts = set()
        self.progress_bar.configure(value=self.current_job_total)
        self._set_controls_state(tk.NORMAL)
        self._save_state()
        self._refresh_summaries()
        self.status.set("收取完成。已完成账号可直接在收件箱中查看。")

    def _finish_fetch_error(self, error: str) -> None:
        self.busy = False
        self.active_accounts = set()
        self._set_controls_state(tk.NORMAL)
        self.status.set(f"失败：{error}")
        messagebox.showerror("收取失败", error)

    def _ensure_account_row(
        self,
        account: str,
        *,
        status: str,
        messages: str,
        updated: str,
    ) -> str:
        iid = self.account_iid_by_address.get(account)
        if iid and self.account_tree.exists(iid):
            self.account_tree.item(iid, text=account, tags=(self._status_tag(status),))
            self.account_tree.set(iid, "status", status)
            self.account_tree.set(iid, "messages", messages)
            self.account_tree.set(iid, "updated", updated)
            return iid
        iid = f"account-{len(self.account_iid_by_address) + 1}"
        while self.account_tree.exists(iid):
            iid = f"account-{len(self.account_iid_by_address) + 1}-{len(self.account_tree.get_children())}"
        self.account_tree.insert(
            "",
            tk.END,
            iid=iid,
            text=account,
            values=(status, messages, updated),
            tags=(self._status_tag(status),),
        )
        self.account_by_iid[iid] = account
        self.account_iid_by_address[account] = iid
        return iid

    def _update_account_row(
        self,
        account: str,
        *,
        status: str | None = None,
        messages: str | None = None,
        updated: str | None = None,
    ) -> None:
        iid = self.account_iid_by_address.get(account)
        if not iid or not self.account_tree.exists(iid):
            iid = self._ensure_account_row(
                account,
                status=status or "就绪",
                messages=messages or "0",
                updated=updated or "",
            )
        if status is not None:
            self.account_tree.set(iid, "status", status)
            self.account_tree.item(iid, tags=(self._status_tag(status),))
        if messages is not None:
            self.account_tree.set(iid, "messages", messages)
        if updated is not None:
            self.account_tree.set(iid, "updated", updated)

    def _status_tag(self, status: str) -> str:
        lowered = status.lower()
        if lowered in {"done", "完成"}:
            return "done"
        if "error" in lowered or "失败" in status:
            return "error"
        if (
            "fetch" in lowered
            or "reading" in lowered
            or "queued" in lowered
            or "收取" in status
            or "读取" in status
            or "排队" in status
        ):
            return "running"
        return "saved"

    def _show_account_menu(self, event: tk.Event) -> None:
        row = self.account_tree.identify_row(event.y)
        if row:
            self.account_tree.selection_set(row)
            self.account_tree.focus(row)
        self.account_menu.tk_popup(event.x_root, event.y_root)

    def _on_accounts_text_modified(self, _event: tk.Event | None = None) -> None:
        if not self.accounts_text.edit_modified():
            return
        self.accounts_text.edit_modified(False)
        if self.preview_after_id is not None:
            self.after_cancel(self.preview_after_id)
        self.preview_after_id = self.after(250, self._update_import_summary)

    def _update_import_summary(self) -> None:
        valid, invalid = count_account_lines(self.accounts_text.get("1.0", tk.END))
        if valid and invalid:
            self.import_summary.set(f"可导入 {valid} 个账号；{invalid} 行需要检查。")
        elif valid:
            self.import_summary.set(f"可导入 {valid} 个账号。")
        elif invalid:
            self.import_summary.set(f"暂无有效账号；{invalid} 行需要检查。")
        else:
            self.import_summary.set("粘贴 email----密码，或导入账号文件。")

    def _load_state_into_ui(self) -> None:
        state = load_saved_state()
        account_addresses = state.get("accounts", []) if isinstance(state, dict) else []
        if isinstance(account_addresses, list):
            for account in account_addresses:
                address = str(account)
                if address:
                    self._ensure_account_row(address, status="已保存", messages="0", updated="")
        results = state.get("results", []) if isinstance(state, dict) else []
        if not isinstance(results, list):
            results = []
        for result in results:
            if not isinstance(result, dict):
                continue
            account = str(result.get("account", ""))
            if not account:
                continue
            self.results_by_account[account] = result
            status = "完成" if result.get("ok") else "失败"
            self._ensure_account_row(
                account,
                status=status,
                messages=str(result.get("total", 0) if result.get("ok") else 0),
                updated=str(result.get("updated_at", state.get("saved_at", ""))),
            )
        if results:
            self.saved_summary.set(f"已恢复本地会话：{state.get('saved_at', '')}")
            first = self.account_tree.get_children()[0]
            self.account_tree.selection_set(first)
            self.account_tree.focus(first)
            self._show_selected_account()
        else:
            self.saved_summary.set("暂无已恢复会话。")
        self._refresh_summaries()

    def _save_state(self) -> None:
        results = []
        for account in sorted(self.results_by_account):
            result = dict(self.results_by_account[account])
            result.pop("password", None)
            results.append(result)
        payload = {
            "version": 1,
            "saved_at": now_label(),
            "accounts": sorted(self.account_iid_by_address),
            "results": results,
        }
        save_saved_state(payload)
        self.saved_summary.set(f"已本地保存：{payload['saved_at']}")

    def _refresh_summaries(self) -> None:
        total_accounts = len(self.account_iid_by_address)
        loaded = sum(1 for result in self.results_by_account.values() if result.get("ok"))
        errors = 0
        messages = 0
        for result in self.results_by_account.values():
            if result.get("ok"):
                messages += int(result.get("total") or 0)
                if result.get("last_error"):
                    errors += 1
            else:
                errors += 1
        self.total_accounts_summary.set(str(total_accounts))
        self.loaded_accounts_summary.set(str(loaded))
        self.message_summary.set(str(messages))
        self.error_summary.set(str(errors))

    def _selected_account_address(self) -> str:
        selection = self.account_tree.selection()
        if not selection:
            return ""
        return self.account_by_iid.get(selection[0], "")

    def _select_account(self, account: str) -> None:
        iid = self.account_iid_by_address.get(account)
        if not iid or not self.account_tree.exists(iid):
            return
        self.account_tree.selection_set(iid)
        self.account_tree.focus(iid)
        self.account_tree.see(iid)

    def _remove_account_from_input(self, address: str) -> None:
        lines = []
        for line in self.accounts_text.get("1.0", tk.END).splitlines():
            if line.strip().startswith(f"{address}----"):
                continue
            lines.append(line)
        self.accounts_text.delete("1.0", tk.END)
        self.accounts_text.insert(tk.END, "\n".join(lines).strip())
        self._update_import_summary()

    def _on_account_select(self, _event: tk.Event | None = None) -> None:
        self._show_selected_account()

    def _show_selected_account(self) -> None:
        account = self._selected_account_address()
        if not account:
            return
        result = self.results_by_account.get(account)
        self._populate_messages(result)

    def _populate_messages(self, result: dict[str, Any] | None) -> None:
        self.message_tree.delete(*self.message_tree.get_children())
        self.message_by_iid = {}
        if not result:
            self._set_empty_preview("该账号已就绪，点击开始收取或刷新后查看收件箱。")
            return
        if not result.get("ok"):
            self._set_empty_preview(f"错误：{result.get('error', '未知错误')}")
            return

        messages = sorted(result.get("messages", []), key=message_sort_key, reverse=True)
        if not messages:
            self._set_empty_preview("收件箱为空。")
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
                    f"主题：{message.get('subject', '')}",
                    f"发件人：{message.get('from', '')}",
                    f"时间：{message.get('date', '')}",
                    "",
                    str(message.get("body", "")),
                ]
            )
        )

    def _clear_messages(self) -> None:
        self.message_tree.delete(*self.message_tree.get_children())
        self.message_by_iid = {}
        self._set_empty_preview("请选择账号和邮件。")

    def _set_empty_preview(self, text: str) -> None:
        if self.preview_html is not None:
            self.preview_html.load_html(
                f"""<!doctype html>
<html><body style="font-family: Microsoft YaHei UI, Segoe UI, Arial, sans-serif; color: #64748b; padding: 22px;">
<h2 style="color:#334155; font-size:18px; margin:0 0 10px 0;">邮件预览</h2>
<p>{html.escape(text)}</p>
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
        lines.append(f"账号：{account_result.get('account', '')}")
        if not account_result.get("ok"):
            lines.append(f"错误：{account_result.get('error', '未知错误')}")
            lines.append("")
            continue

        lines.append(f"返回邮件数：{account_result.get('total', 0)}")
        lines.append("=" * 80)
        for message in account_result.get("messages", []):
            lines.append(f"#{message.get('index', '')}")
            lines.append(f"发件人：{message.get('from', '')}")
            lines.append(f"收件人：{message.get('to', '')}")
            lines.append(f"时间：{message.get('date', '')}")
            lines.append(f"主题：{message.get('subject', '')}")
            lines.append("")
            lines.append(str(message.get("body", "")))
            lines.append("-" * 80)
        lines.append("")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    app = MailReaderApp()
    app.mainloop()
