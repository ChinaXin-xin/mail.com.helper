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

## Run

```powershell
.\.venv\Scripts\python.exe -m ccgpt_mail_tool
```

