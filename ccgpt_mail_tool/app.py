from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from .accounts import AccountCredential, parse_account_lines
from .imap_mail import (
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_MAILBOX,
    ImapMailFetcher,
    MailFetchError,
    MailSummary,
)


HELP_URL = "https://support.mail.com/pop-imap/setup-emailprogram-fails.html"


@dataclass(frozen=True)
class FetchSuccess:
    account: str
    messages: list[MailSummary]


@dataclass(frozen=True)
class FetchFailure:
    account: str
    error: str


class MailReceiverApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ccGpt 邮箱协议收信工具")
        self.geometry("1180x720")
        self.minsize(980, 620)
        self._result_queue: queue.Queue[FetchSuccess | FetchFailure | None] = (
            queue.Queue()
        )
        self._worker: threading.Thread | None = None

        self._host_var = tk.StringVar(value=DEFAULT_IMAP_HOST)
        self._port_var = tk.IntVar(value=DEFAULT_IMAP_PORT)
        self._mailbox_var = tk.StringVar(value=DEFAULT_MAILBOX)
        self._limit_var = tk.IntVar(value=5)
        self._status_var = tk.StringVar(value="准备就绪。仅限读取你拥有或获授权的邮箱。")

        self._build_layout()

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text="账号列表").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="格式：邮箱----密码。密码只在内存中用于本次 IMAP 登录，不会保存到磁盘。",
            foreground="#555555",
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))

        self._accounts_text = tk.Text(header, height=7, wrap="none", undo=True)
        self._accounts_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._accounts_text.insert(
            "1.0", "user@mail.com----你的邮箱密码\n"
        )

        settings = ttk.Frame(self, padding=(16, 8, 16, 8))
        settings.grid(row=1, column=0, sticky="ew")
        for column in range(8):
            settings.columnconfigure(column, weight=0)
        settings.columnconfigure(8, weight=1)

        ttk.Label(settings, text="IMAP").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self._host_var, width=28).grid(
            row=0, column=1, padx=(8, 16)
        )
        ttk.Label(settings, text="端口").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(
            settings,
            from_=1,
            to=65535,
            textvariable=self._port_var,
            width=8,
        ).grid(row=0, column=3, padx=(8, 16))
        ttk.Label(settings, text="目录").grid(row=0, column=4, sticky="w")
        ttk.Entry(settings, textvariable=self._mailbox_var, width=12).grid(
            row=0, column=5, padx=(8, 16)
        )
        ttk.Label(settings, text="每号邮件数").grid(row=0, column=6, sticky="w")
        ttk.Spinbox(
            settings, from_=1, to=20, textvariable=self._limit_var, width=6
        ).grid(row=0, column=7, padx=(8, 16))

        self._parse_button = ttk.Button(
            settings, text="解析账号", command=self._parse_accounts
        )
        self._parse_button.grid(row=0, column=9, padx=(8, 0))
        self._fetch_button = ttk.Button(
            settings, text="开始收信", command=self._start_fetch
        )
        self._fetch_button.grid(row=0, column=10, padx=(8, 0))
        ttk.Button(settings, text="清空结果", command=self._clear_results).grid(
            row=0, column=11, padx=(8, 0)
        )
        ttk.Button(settings, text="IMAP 帮助", command=self._open_help).grid(
            row=0, column=12, padx=(8, 0)
        )

        result_frame = ttk.Frame(self, padding=(16, 8, 16, 8))
        result_frame.grid(row=2, column=0, sticky="nsew")
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        columns = ("account", "status", "sender", "date", "subject", "snippet")
        self._results = ttk.Treeview(
            result_frame, columns=columns, show="headings", selectmode="browse"
        )
        self._results.heading("account", text="账号")
        self._results.heading("status", text="状态")
        self._results.heading("sender", text="发件人")
        self._results.heading("date", text="日期")
        self._results.heading("subject", text="主题")
        self._results.heading("snippet", text="内容预览")
        self._results.column("account", width=230, minwidth=180, stretch=False)
        self._results.column("status", width=90, minwidth=70, stretch=False)
        self._results.column("sender", width=210, minwidth=140, stretch=False)
        self._results.column("date", width=190, minwidth=120, stretch=False)
        self._results.column("subject", width=250, minwidth=160, stretch=False)
        self._results.column("snippet", width=360, minwidth=240, stretch=True)
        self._results.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            result_frame, orient="vertical", command=self._results.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._results.configure(yscrollcommand=scrollbar.set)

        footer = ttk.Frame(self, padding=(16, 4, 16, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self._status_var).grid(row=0, column=0, sticky="w")

    def _parse_accounts(self) -> None:
        result = parse_account_lines(self._accounts_text.get("1.0", "end"))
        status = f"解析到 {len(result.accounts)} 个账号"
        if result.errors:
            error_text = "\n".join(
                f"第 {error.line_number} 行：{error.message}"
                for error in result.errors[:10]
            )
            messagebox.showwarning("账号格式需要检查", error_text)
            status += f"，{len(result.errors)} 行有问题"
        self._status_var.set(status)

    def _start_fetch(self) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("正在运行", "当前收信任务还没有结束。")
            return

        result = parse_account_lines(self._accounts_text.get("1.0", "end"))
        if result.errors:
            self._parse_accounts()
            return
        if not result.accounts:
            messagebox.showwarning("没有账号", "请先粘贴至少一行账号。")
            return

        try:
            port = int(self._port_var.get())
            limit = int(self._limit_var.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("参数错误", "端口和每号邮件数必须是数字。")
            return

        fetcher = ImapMailFetcher(
            host=self._host_var.get().strip() or DEFAULT_IMAP_HOST,
            port=port,
            mailbox=self._mailbox_var.get().strip() or DEFAULT_MAILBOX,
        )

        self._clear_results()
        self._set_busy(True)
        self._status_var.set(f"开始收信：{len(result.accounts)} 个账号")
        self._worker = threading.Thread(
            target=self._fetch_worker,
            args=(fetcher, result.accounts, limit),
            daemon=True,
        )
        self._worker.start()
        self.after(120, self._drain_result_queue)

    def _fetch_worker(
        self,
        fetcher: ImapMailFetcher,
        accounts: tuple[AccountCredential, ...],
        limit: int,
    ) -> None:
        for account in accounts:
            try:
                messages = fetcher.fetch_latest(account, limit=limit)
                self._result_queue.put(FetchSuccess(account.email_address, messages))
            except MailFetchError as exc:
                self._result_queue.put(FetchFailure(account.email_address, str(exc)))
        self._result_queue.put(None)

    def _drain_result_queue(self) -> None:
        finished = False
        while True:
            try:
                item = self._result_queue.get_nowait()
            except queue.Empty:
                break

            if item is None:
                finished = True
                continue
            if isinstance(item, FetchFailure):
                self._insert_failure(item)
            else:
                self._insert_success(item)

        if finished:
            self._set_busy(False)
            row_count = len(self._results.get_children())
            self._status_var.set(f"收信结束，共显示 {row_count} 条结果")
            return
        self.after(120, self._drain_result_queue)

    def _insert_success(self, item: FetchSuccess) -> None:
        if not item.messages:
            self._results.insert(
                "",
                "end",
                values=(item.account, "无邮件", "", "", "", ""),
            )
            return

        for message in item.messages:
            self._results.insert(
                "",
                "end",
                values=(
                    message.account_email,
                    "成功",
                    message.sender,
                    message.date,
                    message.subject,
                    message.snippet,
                ),
            )

    def _insert_failure(self, item: FetchFailure) -> None:
        self._results.insert(
            "",
            "end",
            values=(item.account, "失败", "", "", "", item.error),
        )

    def _clear_results(self) -> None:
        for row_id in self._results.get_children():
            self._results.delete(row_id)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._parse_button.configure(state=state)
        self._fetch_button.configure(state=state)

    def _open_help(self) -> None:
        webbrowser.open(HELP_URL)


def main() -> None:
    app = MailReceiverApp()
    app.mainloop()
