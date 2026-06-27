# ccGpt Mail Receiver

Win11 local desktop tool for reading user-owned mail.com inboxes through direct
HTTP requests to the mail.com lightmailer web flow.

## Current Scope

- Paste accounts in `email@example.com----password` format.
- Start a local HTTP endpoint at `http://127.0.0.1:8765/fetch-mails`.
- The HTTP endpoint logs in through mail.com web forms, enters the lightmailer
  flow, reads the folder list, message list, message details, and message body
  pages with HTTP requests, then returns all message text.
- `Max mails = 0` means read all messages reachable by the current inbox flow.
- Passwords, cookies, and message files are kept in memory or temporary browser
  folders only and are not written to this repository.

This tool is only for mailboxes you own or are explicitly authorized to access.
It does not bypass CAPTCHA, security checks, paywalls, or account restrictions.
If mail.com shows a CAPTCHA or extra verification prompt, direct HTTP cannot
complete it; log in manually in a browser first to resolve the account prompt,
then try again.

## Run

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe start.py
```

The GUI calls the local HTTP endpoint with a payload like:

```json
{
  "accounts": [{"address": "email@example.com", "password": "password"}],
  "max_messages": 0
}
```

## Dependencies

Install dependencies only into `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The script uses direct HTTP requests and does not start a browser. It was
designed for Windows 11 and Python 3.11.

## BitBrowser GUI

Start the local BitBrowser API first, then run:

```powershell
.\.venv\Scripts\python.exe bitbrowser_gui.py
```

The S5 proxy can be entered in the window, or prefilled with `S5_HOST`, `S5_PORT`,
`S5_USERNAME`, and `S5_PASSWORD` environment variables.
