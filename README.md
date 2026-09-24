# email_verify

Verify a bulk email list before sending a campaign, so you stop wasting sends (and sender
reputation) on addresses that don't exist.

The script runs three stages, each only checking what survived the previous one:

1. **Syntax check** — regex, catches obvious typos. Fast, 100% safe.
2. **MX / domain check** — does the domain even have a mail server? Fast, 100% safe.
3. **SMTP check** (`--smtp`, optional) — a real ESMTP handshake (EHLO, opportunistic
   STARTTLS, MAIL FROM, RCPT TO) that asks the receiving server whether the mailbox exists,
   without actually sending mail. Slow, best-effort, and the only stage that can catch
   "mailbox not found" — see [Limitations](#limitations-read-this) before relying on it.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How it decides valid / invalid / unknown](#how-it-decides-valid--invalid--unknown)
- [Output files](#output-files)
- [Sample run](#sample-run)
- [All options](#all-options)
- [Recipes](#recipes)
- [Running on a VPS](#running-on-a-vps)
- [Limitations — read this](#limitations-read-this)
- [legacy/](#legacy)

## Install

```bash
pip install dnspython
```

Python 3.8+ (uses only `dnspython` as a third-party dependency; everything else is stdlib).

## Quick start

```bash
# Fast, safe pass: syntax + MX check only. No network probing of mailboxes.
python verify_emails.py --input emails.txt

# Also probe mailbox existence over SMTP (slow, best-effort — read the limitations section)
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com

# Re-run after an interrupted/partial run: skips everything already settled
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --resume
```

`--input` accepts one email per line; a leading index/tab/comma (e.g. `1\tjohn@x.com`) is
fine, the script extracts the address.

## How it decides valid / invalid / unknown

| Stage | Outcome | Bucket |
|---|---|---|
| Syntax | doesn't look like an email | `invalid` |
| MX/domain | NXDOMAIN, no MX, no A record | `invalid` |
| MX/domain | DNS lookup timed out repeatedly | `unknown` (retry later, not a dead domain) |
| MX/domain only (no `--smtp`) | domain accepts mail | `valid` (`valid_domain_mx_only`) |
| SMTP | server returns 250 | `valid` (`smtp_accepted`) |
| SMTP | server returns 550/551/553 | `invalid` (`mailbox_not_found`) |
| SMTP | domain is catch-all (accepts everything) | `unknown` — can't verify a specific mailbox |
| SMTP | 450/451/452 (greylisted), timeout, connection refused | `unknown` |
| SMTP | known-unverifiable provider (Outlook/Hotmail/iCloud etc.) | `unknown`, skipped without probing |

`unknown` is a real, distinct outcome — it means "we genuinely couldn't tell," not "probably
bad." Don't treat it the same as `invalid`.

## Output files

Three CSVs, always written, sharing the same columns:

```
email, domain, status_or_reason, role_based, disposable, risky_for_spreadsheet
```

- **valid.csv** — safe to send to (`valid_domain_mx_only` or `smtp_accepted`)
- **invalid.csv** — don't send (`invalid_syntax`, `no_mail_server_for_domain`,
  `mailbox_not_found`)
- **unknown.csv** — undetermined; always a fresh snapshot of *this run* (never accumulates
  duplicates across `--resume` retries), since these can be retried

Extra flag columns on every row (informational, not a validity signal):

- `role_based` — local part looks like `info@`/`admin@`/`sales@`/`noreply@` etc. Usually
  excluded from marketing sends.
- `disposable` — domain matches a known temp-mail provider (mailinator, guerrillamail, ...).
  Built-in list is small; extend with `--disposable-domains-file`.
- `risky_for_spreadsheet` — local part starts with `=`, `+`, `-` or `@`. Some spreadsheet
  apps may misread that as the start of a formula if you open the CSV in Excel/Sheets. The
  email itself is **not** modified (so it still works as a mailing list) — this is just a
  warning to double check before opening in a spreadsheet.

A `summary.json` is also written every run: start/end time, duration, counts, how many
emails were skipped via `--resume`.

## Sample run

Example `emails.txt` (a leading index/tab is fine, the script extracts the address):

```
1	jane.doe@example.com
2	not-an-email
3	john@totally-made-up-domain-xyz123.com
4	sales@example.org
5	random123@mailinator.com
6	+weird@example.com
```

```bash
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --log-file run.log
```

Console / `run.log` output:

```
2026-09-24 17:34:42 INFO Loaded 6 unique emails.
2026-09-24 17:34:42 INFO Checking MX records for 4 unique domains...
2026-09-24 17:34:44 INFO 4 emails have a domain that accepts mail.
2026-09-24 17:34:44 INFO Probing 3 domains for catch-all behaviour (0 skipped as known-unverifiable providers)...
2026-09-24 17:34:45 INFO Running SMTP checks: 2 connection(s) across 2 domains (3 addresses)...
2026-09-24 17:34:46 INFO   [1/2 connections] example.com shard done (2 addresses)
2026-09-24 17:34:46 INFO   [2/2 connections] example.org shard done (1 addresses)
2026-09-24 17:34:46 INFO Done.
2026-09-24 17:34:46 INFO   valid:   2  -> valid.csv
2026-09-24 17:34:46 INFO   invalid: 2  -> invalid.csv
2026-09-24 17:34:46 INFO   unknown: 2  -> unknown.csv  (couldn't be confirmed either way)
2026-09-24 17:34:46 INFO   summary: summary.json
```

`valid.csv`:

```
email,domain,status_or_reason,role_based,disposable,risky_for_spreadsheet
jane.doe@example.com,example.com,smtp_accepted,False,False,False
sales@example.org,example.org,smtp_accepted,True,False,False
```

`invalid.csv`:

```
email,domain,status_or_reason,role_based,disposable,risky_for_spreadsheet
not-an-email,,invalid_syntax,False,False,False
john@totally-made-up-domain-xyz123.com,totally-made-up-domain-xyz123.com,no_mail_server_for_domain,False,False,False
```

`unknown.csv`:

```
email,domain,status_or_reason,role_based,disposable,risky_for_spreadsheet
random123@mailinator.com,mailinator.com,catch_all_domain_cannot_verify_mailbox,False,True,False
+weird@example.com,example.com,greylisted_or_temporary_failure,False,False,True
```

`summary.json`:

```json
{
  "started_at": "2026-09-24T12:04:42.938508+00:00",
  "finished_at": "2026-09-24T12:04:46.201933+00:00",
  "duration_seconds": 3.3,
  "input_file": "emails.txt",
  "smtp_check_enabled": true,
  "unique_emails_loaded": 6,
  "skipped_already_settled": 0,
  "counts": {"valid": 2, "invalid": 2, "unknown": 2},
  "outputs": {"valid": "valid.csv", "invalid": "invalid.csv", "unknown": "unknown.csv"}
}
```

A few things to notice in that output:
- `sales@example.org` is flagged `role_based=True` but still counted `valid` — the flag is informational, you decide whether to exclude it from a send.
- `+weird@example.com` is flagged `risky_for_spreadsheet=True` because the local part starts with `+` — the address itself isn't touched, just watch it if you open the CSV in Excel.
- `random123@mailinator.com` landed in `unknown.csv` because mailinator.com is catch-all, not because it's flagged `disposable=True` — the disposable flag is informational and never overrides the actual SMTP verdict.

## All options

```
--input PATH                  Required. One email per line (extra columns/index ok).
--valid-out FILE              Default: valid.csv
--invalid-out FILE            Default: invalid.csv
--unknown-out FILE            Default: unknown.csv
--summary-out FILE            Default: summary.json

--smtp                        Enable the live SMTP mailbox check (see Limitations).
--sender EMAIL                MAIL FROM address for --smtp. Required if --smtp is set.
--no-starttls                 Skip opportunistic STARTTLS after EHLO; stay in plaintext.

--workers N                   Max parallel SMTP connections overall (default 8).
--max-conn-per-domain N       Max parallel connections for one large domain, e.g. gmail.com
                               (default 4) — avoids one huge domain bottlenecking the run.
--min-per-connection N        Don't open an extra connection for a domain unless it has at
                               least this many addresses per shard (default 25).
--timeout SECONDS             SMTP connection timeout (default 10).
--delay SECONDS                Pause between RCPT checks on one connection (default 0.5).
--jitter SECONDS               Extra random 0..N seconds added on top of --delay, so timing
                               isn't perfectly uniform.
--max-rcpt-per-minute N       Global cap on RCPT attempts per minute across ALL connections
                               combined (unlimited if unset).

--resume                      Skip emails already settled (valid/invalid) from a previous
                               run's output files; unknowns are always retried.
--max-runtime-minutes N       Stop cleanly after N minutes (e.g. a cron window). Rerun with
                               --resume to continue. Unlimited if unset.
--limit N                     Only process the first N emails (for testing).

--dns-retries N               Retries for a timed-out DNS lookup before giving up (default 2).
--dns-retry-delay SECONDS     Seconds between DNS retry attempts (default 1.0).

--disposable-domains-file F   Extra newline-delimited disposable domains to flag.
--no-skip-unverifiable        Don't skip known-unverifiable providers; probe them anyway.
--unverifiable-domains-file F Extra newline-delimited domains to treat as unverifiable.

--log-file FILE               Also write timestamped log lines to this file.
--quiet                       Suppress console output (use with --log-file).
```

Run `python verify_emails.py --help` for this same list from the script itself.

## Recipes

**Just clean obvious junk, no network probing of mailboxes (fast, always safe):**
```bash
python verify_emails.py --input emails.txt
```

**Full SMTP verification, conservative pace, logged to a file:**
```bash
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com \
  --workers 5 --delay 1 --jitter 0.5 --max-rcpt-per-minute 30 \
  --log-file run.log --resume
```

**Nightly cron window that picks up where it left off:**
```bash
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com \
  --resume --max-runtime-minutes 120 --log-file run.log
```
Schedule it daily; each run stops after 2 hours and the next day's run continues from
`unknown.csv`/unsettled addresses automatically.

**Test on a handful of addresses first:**
```bash
python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --limit 20
```

## Running on a VPS

Two things will block `--smtp` before the script itself is even the issue:

1. **Outbound TCP port 25 is blocked by default on almost every cloud/VPS provider**
   (AWS, DigitalOcean, Vultr, Linode, GCP, Azure, ...) as an anti-spam measure. Test it:
   ```bash
   python3 -c "import socket; socket.create_connection(('aspmx.l.google.com',25),8); print('OPEN')"
   ```
   If that hangs or fails, ask your provider to unblock port 25 outbound (usually a support
   ticket). The MX-only pass (no `--smtp`) works fine regardless.
2. **Reverse DNS (PTR record).** Gmail/Yahoo/Outlook heavily distrust a connecting IP with
   no PTR record or a generic hosting one. Check with `dig -x <your-vps-ip> +short`, and set
   a real PTR in your provider's control panel if it's missing.

Rocky Linux setup:
```bash
sudo dnf install -y python3 python3-pip
python3 -m venv ~/emailverify-venv
source ~/emailverify-venv/bin/activate
pip install dnspython
```

Run it detached so it survives an SSH disconnect:
```bash
tmux new -s verify
source ~/emailverify-venv/bin/activate
python3 verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --resume \
    --log-file run.log
# Ctrl+b, d to detach; tmux attach -t verify to come back
```
Always pass `--resume` on a long VPS run — if the process dies or SSH drops, rerunning the
same command continues instead of rechecking everything.

## Limitations — read this

- **Steps 1-2 (syntax + MX) are safe and cheap** and catch typos, dead domains, and most
  of what causes hard bounces from junk data.
- **Step 3 (`--smtp`) is best-effort, not authoritative:**
  - Many providers — notably Gmail, and Outlook/Hotmail/iCloud (skipped by default, see
    `--no-skip-unverifiable`) — either refuse to answer at RCPT time, greylist you, or
    accept everything ("catch-all") and bounce later instead. Those land in `unknown.csv`,
    not `invalid.csv`, because the script genuinely doesn't know. If your list is mostly
    gmail.com/yahoo.com, expect `--smtp` to add little beyond the MX check.
  - Probing thousands of addresses via SMTP from one IP looks like spammer behaviour.
    Providers may rate-limit, block, or blacklist the sending IP if pushed too hard — keep
    `--workers`, `--delay`, and `--max-rcpt-per-minute` conservative.
  - A mailbox that exists today can still be a **spam trap** — providers recycle
    long-abandoned addresses specifically to catch senders with stale lists. "Verified" is
    a snapshot, not a lifetime guarantee, especially on an old/unused list.
  - For a genuinely huge list, a reputable paid bulk verification API (ZeroBounce,
    NeverBounce, Kickbox, Bouncer, ...) will get better accuracy without risking your own
    IP's reputation. Use this script's fast MX-only pass first to strip obvious junk for
    free, then verify only the survivors through a paid API if the list is large.
- **This script verifies mailboxes exist — it does not manage sender reputation.** Actually
  sending bulk mail via raw SMTP from a bare VPS/script is a fast way to get blacklisted
  regardless of list quality. Use a real ESP (SendGrid, Mailgun, Amazon SES, Postmark) for
  the actual send, with SPF/DKIM/DMARC set up on your sending domain, and feed real
  bounce/complaint data back into your suppression list over time.

## legacy/

The original, simpler version of this script: one SMTP connection per email (no reuse), no
MX caching, no catch-all detection, and a bare `except: return False` that lumps timeouts
and greylisting in with confirmed-bad addresses. Kept for reference, not recommended for
actual use — see the comparison in the project history for why.
