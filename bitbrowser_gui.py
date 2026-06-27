"""Small Tkinter controller for BitBrowser local profiles."""

from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


BITBROWSER_API = os.getenv("BITBROWSER_API", "http://127.0.0.1:54345")
TARGET_URLS = [
    "https://chatgpt.com/",
    "https://www.mail.com/",
    "https://redeemgpt.com/",
]


class BitBrowserClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._request_json(request)

    def _request_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except URLError as exc:
            raise RuntimeError(f"无法连接比特浏览器本地接口: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"接口返回不是 JSON: {body[:200]}") from exc

        if isinstance(parsed, dict):
            return parsed
        return {"success": True, "data": parsed}


class BitBrowserApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("比特浏览器无痕控制器")
        self.geometry("560x440")
        self.minsize(520, 420)

        self.client = BitBrowserClient(BITBROWSER_API)
        self.profile_ids: list[str] = []
        self.busy = False

        self.proxy_host = tk.StringVar(value=os.getenv("S5_HOST", ""))
        self.proxy_port = tk.StringVar(value=os.getenv("S5_PORT", ""))
        self.proxy_user = tk.StringVar(value=os.getenv("S5_USERNAME", ""))
        self.proxy_password = tk.StringVar(value=os.getenv("S5_PASSWORD", ""))
        self.browser_count = tk.IntVar(value=3)
        self.status = tk.StringVar(value="请输入 S5 代理信息，然后点击开启。")

        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="S5 主机").grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Entry(root, textvariable=self.proxy_host).grid(row=0, column=1, sticky=tk.EW, pady=5)

        ttk.Label(root, text="端口").grid(row=1, column=0, sticky=tk.W, pady=5)
        ttk.Entry(root, textvariable=self.proxy_port).grid(row=1, column=1, sticky=tk.EW, pady=5)

        ttk.Label(root, text="账号").grid(row=2, column=0, sticky=tk.W, pady=5)
        ttk.Entry(root, textvariable=self.proxy_user).grid(row=2, column=1, sticky=tk.EW, pady=5)

        ttk.Label(root, text="密码").grid(row=3, column=0, sticky=tk.W, pady=5)
        ttk.Entry(root, textvariable=self.proxy_password, show="*").grid(
            row=3, column=1, sticky=tk.EW, pady=5
        )

        ttk.Label(root, text="浏览器数量").grid(row=4, column=0, sticky=tk.W, pady=5)
        ttk.Spinbox(root, from_=1, to=20, textvariable=self.browser_count, width=8).grid(
            row=4, column=1, sticky=tk.W, pady=5
        )

        urls_frame = ttk.LabelFrame(root, text="每个浏览器打开")
        urls_frame.grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(12, 8))
        urls_frame.columnconfigure(0, weight=1)
        for index, url in enumerate(TARGET_URLS):
            ttk.Label(urls_frame, text=url).grid(row=index, column=0, sticky=tk.W, padx=10, pady=4)

        actions = ttk.Frame(root)
        actions.grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=10)
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(actions, text="开启", command=self.start_browsers)
        self.start_button.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))

        self.destroy_button = ttk.Button(actions, text="销毁", command=self.destroy_browsers)
        self.destroy_button.grid(row=0, column=1, sticky=tk.EW, padx=(6, 0))

        status_box = ttk.LabelFrame(root, text="状态")
        status_box.grid(row=7, column=0, columnspan=2, sticky=tk.NSEW, pady=(8, 0))
        root.rowconfigure(7, weight=1)
        status_box.columnconfigure(0, weight=1)
        ttk.Label(status_box, textvariable=self.status, wraplength=500, justify=tk.LEFT).grid(
            row=0, column=0, sticky=tk.NSEW, padx=10, pady=10
        )

    def start_browsers(self) -> None:
        if self.busy:
            return
        self._run_worker(self._start_browsers)

    def destroy_browsers(self) -> None:
        if self.busy:
            return
        self._run_worker(self._destroy_browsers)

    def _run_worker(self, target: Any) -> None:
        self.busy = True
        self._set_buttons(False)
        thread = threading.Thread(target=self._worker_wrapper, args=(target,), daemon=True)
        thread.start()

    def _worker_wrapper(self, target: Any) -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001 - show GUI error for any API failure.
            self.after(0, lambda: messagebox.showerror("操作失败", str(exc)))
            self.after(0, lambda: self.status.set(f"失败: {exc}"))
        finally:
            self.after(0, lambda: self._set_buttons(True))
            self.busy = False

    def _set_buttons(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.start_button.configure(state=state)
        self.destroy_button.configure(state=state)

    def _start_browsers(self) -> None:
        proxy = self._proxy_payload()
        count = self._browser_count()

        if self.profile_ids:
            self._destroy_browsers()

        self._set_status("正在检测代理...")
        check_payload = {**proxy, "checkExists": 0}
        check_response = self.client.post("/checkagent", check_payload)
        self._ensure_success(check_response, "代理检测失败")

        profile_ids: list[str] = []
        for index in range(1, count + 1):
            self._set_status(f"正在创建第 {index}/{count} 个浏览器配置...")
            profile_id = self._create_profile(index, proxy)
            profile_ids.append(profile_id)

            self._set_status(f"正在打开第 {index}/{count} 个无痕浏览器...")
            open_response = self.client.post(
                "/browser/open",
                {
                    "id": profile_id,
                    "args": ["--incognito"],
                    "queue": True,
                    "loadExtensions": False,
                    "extractIp": True,
                },
            )
            self._ensure_success(open_response, f"打开第 {index} 个浏览器失败")

        self.profile_ids = profile_ids
        self._set_status(f"已开启 {count} 个无痕浏览器，每个浏览器已打开 3 个网页。")

    def _destroy_browsers(self) -> None:
        if not self.profile_ids:
            self._set_status("没有可销毁的浏览器。")
            return

        for index, profile_id in enumerate(list(self.profile_ids), start=1):
            self._set_status(f"正在关闭第 {index}/{len(self.profile_ids)} 个浏览器...")
            close_response = self.client.post("/browser/close", {"id": profile_id})
            if not close_response.get("success", False):
                self._set_status(f"关闭 {profile_id} 未成功，继续尝试删除。")
            time.sleep(0.5)

        self._set_status("正在删除浏览器配置...")
        delete_response = self.client.post("/browser/delete/ids", {"ids": list(self.profile_ids)})
        self._ensure_success(delete_response, "删除浏览器配置失败")

        self.profile_ids = []
        self._set_status("已关闭并销毁本次创建的浏览器。")

    def _proxy_payload(self) -> dict[str, str | int]:
        host = self.proxy_host.get().strip()
        port = self.proxy_port.get().strip()
        username = self.proxy_user.get().strip()
        password = self.proxy_password.get()

        if not all([host, port, username, password]):
            raise ValueError("请填写完整的 S5 主机、端口、账号和密码。")
        if not port.isdigit():
            raise ValueError("端口必须是数字。")

        return {
            "proxyMethod": 2,
            "proxyType": "socks5",
            "host": host,
            "port": port,
            "proxyUserName": username,
            "proxyPassword": password,
            "ipCheckService": "ip123in",
        }

    def _browser_count(self) -> int:
        count = self.browser_count.get()
        if count < 1 or count > 20:
            raise ValueError("浏览器数量必须在 1 到 20 之间。")
        return count

    def _create_profile(self, index: int, proxy: dict[str, str | int]) -> str:
        payload: dict[str, Any] = {
            "groupId": "",
            "platform": TARGET_URLS[0],
            "platformIcon": "other",
            "url": ",".join(TARGET_URLS),
            "name": f"Codex-S5-Incognito-{index}",
            "remark": "created by local GUI",
            "userName": "",
            "password": "",
            "cookie": "",
            "ignoreCookieError": 0,
            "browserFingerPrint": {},
            **proxy,
        }
        response = self.client.post("/browser/update", payload)
        self._ensure_success(response, "创建浏览器配置失败")

        data = response.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        raise RuntimeError(f"创建成功但未返回浏览器 ID: {response}")

    def _ensure_success(self, response: dict[str, Any], message: str) -> None:
        if not response.get("success", False):
            raise RuntimeError(f"{message}: {response.get('msg') or response}")

    def _set_status(self, text: str) -> None:
        self.after(0, lambda: self.status.set(text))


if __name__ == "__main__":
    app = BitBrowserApp()
    app.mainloop()
