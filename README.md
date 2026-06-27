# ccGpt Mail Receiver

Win11 local desktop tool for reading user-owned mail.com inboxes through the
mail.com web UI.

## Current Scope

- Paste accounts in `email@example.com----password` format.
- Start a local HTTP endpoint at `http://127.0.0.1:8765/fetch-mails`.
- The HTTP endpoint opens mail.com in Chrome, logs in, downloads messages as
  `.eml` through the web UI, parses them locally, and returns all message text.
- `Max mails = 0` means read all messages reachable by the current inbox flow.
- Passwords, cookies, and message files are kept in memory or temporary browser
  folders only and are not written to this repository.

This tool is only for mailboxes you own or are explicitly authorized to access.
It does not bypass CAPTCHA, security checks, paywalls, or account restrictions.
If mail.com shows a CAPTCHA or extra verification prompt, complete it manually in
the visible browser window and then let the flow continue.

## Run

Use the project virtual environment:

```powershell
.\.venv\Scripts\python.exe start.py
```

The GUI calls the local HTTP endpoint with a payload like:

```json
{
  "accounts": [{"address": "email@example.com", "password": "password"}],
  "max_messages": 0,
  "headless": false
}
```

## Dependencies

Install dependencies only into `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The script uses the installed Google Chrome on Windows. It was designed for
Windows 11 and Python 3.11.

## BitBrowser GUI

Start the local BitBrowser API first, then run:

```powershell
.\.venv\Scripts\python.exe bitbrowser_gui.py
```

The S5 proxy can be entered in the window, or prefilled with `S5_HOST`, `S5_PORT`,
`S5_USERNAME`, and `S5_PASSWORD` environment variables.
