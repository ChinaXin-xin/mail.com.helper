# ccGpt Mail Receiver

Win11 local desktop tool for reading user-owned mail.com inboxes through direct
HTTP requests to the mail.com lightmailer web flow.

## Current Scope

- Paste accounts in `email@example.com----password` format or import `.txt` /
  `.csv` account files, with a sample placeholder shown in the input box.
- Show the valid / invalid account count after text is pasted or imported.
- Validate the imported account list before fetching.
- Fetch multiple accounts concurrently with a configurable worker count.
- Show accounts, inbox messages, and the selected message in a business-style
  three-pane Chinese GUI with Windows high-DPI awareness.
- Make each account available in the Inbox panel as soon as that account
  finishes, without waiting for the whole batch.
- Refresh or delete selected account tasks from the account list, including
  multi-select batches.
- Copy selected account email addresses from the account task right-click menu.
- Show a progress bar and per-account status while direct HTTP fetching is
  running.
- Start or stop a 5-second auto-fetch loop from the GUI to keep all imported
  accounts refreshed.
- Restore the previous local session when the app opens again, with a manual
  clear button for saved results.
- Start local HTTP search endpoints on `http://127.0.0.1:8913`.
- The local search endpoint accepts `email`, `keyword`, and `regex`, then returns
  the first matching string from the locally fetched or restored messages. If
  nothing matches, it returns `NullX`.
- The live search endpoint also accepts `password`; it logs in for that request,
  searches the mailbox directly, and returns only the first matching string or
  `NullX`.
- The GUI uses the captured HTML body for a styled message preview when the
  optional Tkinter HTML component is available.
- `Max mails = 0` means read all messages reachable by the current inbox flow.
- Passwords and cookies are kept in memory only and are not written to this
  repository.
- Saved sessions are stored outside the repository at the current Windows
  user's local app-data path and do not include passwords.

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

Search cached messages with either GET or POST:

```powershell
Invoke-RestMethod "http://127.0.0.1:8913/search-mail?email=weatherallmayalyn761@mail.com&keyword=ChatGPT&regex=\d{6}"
```

```json
{
  "email": "email@example.com",
  "keyword": "ChatGPT",
  "regex": "\\d{6}"
}
```

Search a mailbox directly with the account password:

```powershell
Invoke-RestMethod "http://127.0.0.1:8913/search-mail-live?email=weatherallmayalyn761@mail.com&password=xxx&keyword=ChatGPT&regex=\d{6}"
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
