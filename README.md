# email_verify

Verifies a bulk email list before sending a campaign: syntax check, MX/domain check, and an
optional live SMTP (EHLO/RCPT TO) check, with resumable runs and CSV output split into
valid / invalid / unknown.

See `verify_emails.py --help` for all options.

```
pip install dnspython
python verify_emails.py --input emails.txt
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --resume
```

Note: `emails.txt` and all generated output files (`*.csv`, `*.log`, `summary.json`) are
git-ignored on purpose — an email list is real people's personal data and should never be
committed to this public repo.

## legacy/

The original, simpler version of this script (syntax + one-off SMTP check per address, no
resume/logging/rate-limiting). Kept for reference.
