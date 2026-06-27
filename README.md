# ccGpt Mail Receiver

Win11 local desktop tool for reading emails from user-owned mailboxes through IMAP.

## Current scope

- Paste accounts in `email@example.com----password` format.
- Parse and validate the account list locally.
- Read recent messages through IMAP SSL.
- Show sender, date, subject, and a short body preview.
- Do not save passwords, tokens, cookies, or mailbox contents to disk.

This tool is only for mailboxes you own or are explicitly authorized to access. It does not automate web login, bypass verification, solve captchas, or test unknown credentials.

## mail.com IMAP settings

The default preset follows mail.com Help Center settings:

- IMAP server: `imap.mail.com`
- Port: `993`
- Encryption: SSL/TLS

mail.com also notes that POP3/IMAP access may need to be enabled in mailbox settings first. Some mail.com help pages describe this feature under Premium account setup.

Official references:

- https://support.mail.com/premium/imap/server.html
- https://support.mail.com/pop-imap/imap/outlook.html
- https://support.mail.com/pop-imap/setup-emailprogram-fails.html

## Troubleshooting

If the result shows `身份验证失败`, the IMAP server rejected the login. Check these first:

- Log in to https://www.mail.com/ in a browser and confirm the mailbox and password work.
- Enable POP3/IMAP in the mail.com mailbox settings.
- Resolve any browser security checks, temporary locks, or account prompts before trying IMAP again.
- Confirm the account supports IMAP access.

## Run

If the virtual environment is already activated:

```powershell
python start.py
```

Or run it explicitly through the project virtual environment:

```powershell
.\.venv\Scripts\python.exe start.py
```
