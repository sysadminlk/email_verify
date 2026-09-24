#!/usr/bin/env python3
"""
Bulk email list verifier.

Pipeline (each stage only runs on emails that passed the previous one):
  1. Syntax check         - regex, catches obvious typos              (fast, 100% safe)
  2. MX / domain check    - does the domain even accept mail?         (fast, 100% safe)
  3. SMTP RCPT TO check   - full ESMTP handshake (EHLO, opportunistic  (slow, best-effort,
                            STARTTLS, MAIL FROM, RCPT TO) asking the    can be blocked/rate
                            receiving server if the mailbox exists,     limited by the server)
                            without actually sending mail

Output (all three always written; role/disposable/risk flags on every row):
  valid.csv    - email, domain, status, role_based, disposable, risky_for_spreadsheet
  invalid.csv  - email, domain, reason, role_based, disposable, risky_for_spreadsheet
  unknown.csv  - email, domain, reason, role_based, disposable, risky_for_spreadsheet
                 (couldn't be confirmed either way: catch-all domain, greylisting,
                 timeout, DNS lookup timed out, verification blocked, etc.)

Notes on the flag columns:
  - role_based: local part looks like info@/admin@/sales@/noreply@ etc. Not a validity
    signal, just useful for excluding from marketing sends.
  - disposable: domain matches a known temp-mail provider (mailinator, guerrillamail...).
    The built-in list is small; pass --disposable-domains-file for a maintained list.
  - risky_for_spreadsheet: local part starts with =, +, - or @, which some spreadsheet
    apps (Excel/Sheets) may misinterpret as the start of a formula if you open the CSV
    there. The email itself is left untouched (so it still works as a mailing list) -
    this is just a warning flag so you know to double-check before opening in Excel.

Resuming a long run (--resume):
  Emails already recorded in valid.csv or invalid.csv from a previous run are skipped.
  Rows in unknown.csv are NOT treated as settled and are always retried, since "unknown"
  means the previous run genuinely couldn't tell. All three files are written to
  incrementally (flushed after every result), so killing the process mid-run only loses
  whatever was in flight, not everything.

IMPORTANT — read before relying on this for bounce prevention:
  - Steps 1-2 are safe and cheap. They will catch typos, dead domains, and most of what
    causes hard bounces from junk data.
  - Step 3 (--smtp) is the only step that can approximate "mailbox not found", but it is
    NOT reliable at scale:
      * Many providers (Outlook/Hotmail, Yahoo in many configs, lots of corporate mail,
        and notably Gmail) either refuse to answer at RCPT time, greylist you, or accept
        everything ("catch-all") and bounce later instead. Those all land in unknown.csv,
        not invalid.csv, because we genuinely don't know. If your list is mostly
        gmail.com/yahoo.com addresses, expect the SMTP step to add little beyond the MX
        check for that chunk of the list.
      * Probing thousands of addresses via SMTP from one IP looks like spammer behaviour.
        Providers may rate-limit, block, or blacklist the sending IP if you push this too
        hard. Keep --workers and --delay conservative, and expect it to be slow for huge
        lists.
      * For a genuinely huge list (tens/hundreds of thousands), a reputable paid bulk
        verification API (ZeroBounce, NeverBounce, Kickbox, Bouncer, etc.) will get you
        much better accuracy and won't put your own IP's reputation at risk. This script
        is a good free first pass (steps 1-2) to strip out obvious junk before you pay to
        verify the rest.
      * Outbound TCP port 25 is blocked by most ISPs, home routers, corporate firewalls,
        and cloud/VPS providers by default (anti-spam measure) - --smtp will simply hang
        or fail for everything if you're on a network like that. Run it from a machine/
        server that has port 25 explicitly open outbound (test with:
        `python -c "import socket; socket.create_connection(('aspmx.l.google.com',25),8)"`).

Usage:
    pip install dnspython
    python verify_emails.py --input emails.txt
    python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com
    python verify_emails.py --input emails.txt --smtp --sender you@yourdomain.com --resume
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import smtplib
import socket
import string
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from math import ceil

try:
    import dns.resolver
except ImportError:
    print("Missing dependency. Install it with:\n    pip install dnspython", file=sys.stderr)
    sys.exit(1)

_LOCAL_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+"
EMAIL_RE = re.compile(
    r"^" + _LOCAL_ATOM + r"(?:\." + _LOCAL_ATOM + r")*"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}$"
)

ROLE_LOCAL_PARTS = frozenset({
    "info", "admin", "administrator", "support", "sales", "contact", "help",
    "webmaster", "postmaster", "hostmaster", "noreply", "no-reply", "donotreply",
    "do-not-reply", "abuse", "marketing", "billing", "accounts", "hr", "jobs",
    "careers", "office", "mail", "root", "security", "privacy", "press", "media",
    "enquiries", "inquiries", "feedback", "newsletter", "subscribe", "unsubscribe",
})

DEFAULT_DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "trashmail.com", "yopmail.com", "throwawaymail.com", "getnada.com",
    "sharklasers.com", "dispostable.com", "fakeinbox.com", "maildrop.cc",
    "mintemail.com", "tempinbox.com", "mohmal.com", "moakt.com",
    "emailondeck.com", "temp-mail.org", "guerrillamailblock.com", "spam4.me",
    "mailnesia.com", "mailcatch.com", "tempmailaddress.com", "burnermail.io",
    "temp-mail.io", "33mail.com", "spamgourmet.com",
})

_RISKY_LEADING_CHARS = ("=", "+", "-", "@")

# Consumer providers well known to either block/ignore RCPT-time probing almost
# universally or behave unpredictably enough that probing them wastes connections
# without a trustworthy answer. Skipped straight to unknown.csv unless overridden.
KNOWN_UNVERIFIABLE_DOMAINS = frozenset({
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "hotmail.co.uk", "hotmail.fr", "hotmail.it", "hotmail.es", "hotmail.de",
    "outlook.fr", "outlook.de", "outlook.jp", "outlook.co.uk",
    "icloud.com", "me.com", "mac.com",
})

_mx_cache = {}
logger = logging.getLogger("verify_emails")


def extract_email(line):
    """Pull the email address out of a line that may have a leading index/tab/etc."""
    match = re.search(r"[^\s,;]+@[^\s,;]+", line)
    return match.group(0).strip().strip(".,;") if match else None


def is_valid_syntax(email):
    return bool(EMAIL_RE.match(email))


def classify_flags(email, disposable_domains):
    """Returns (role_based, disposable, risky_for_spreadsheet) - informational, not validity."""
    try:
        local, domain = email.rsplit("@", 1)
    except ValueError:
        return False, False, False
    role = local.strip().lower() in ROLE_LOCAL_PARTS
    disposable = domain.strip().lower() in disposable_domains
    risky = bool(local) and local[0] in _RISKY_LEADING_CHARS
    return role, disposable, risky


def load_disposable_domains(extra_file):
    domains = set(DEFAULT_DISPOSABLE_DOMAINS)
    if extra_file:
        with open(extra_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    domains.add(line)
    return domains


def load_unverifiable_domains(extra_file):
    domains = set(KNOWN_UNVERIFIABLE_DOMAINS)
    if extra_file:
        with open(extra_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    domains.add(line)
    return domains


class RateLimiter:
    """Thread-safe sliding-window limiter: at most max_per_minute calls to wait() in any 60s window."""

    def __init__(self, max_per_minute):
        self.max_per_minute = max_per_minute
        self._lock = threading.Lock()
        self._timestamps = deque()

    def wait(self):
        if not self.max_per_minute:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return
                sleep_for = 60 - (now - self._timestamps[0])
            time.sleep(max(sleep_for, 0.05))


class RunTimeBudget:
    """Tracks an optional wall-clock deadline for the whole run."""

    def __init__(self, max_runtime_minutes):
        self.deadline = time.monotonic() + max_runtime_minutes * 60 if max_runtime_minutes else None

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline


def _resolve(domain, rtype, retries, retry_delay):
    """Returns ('ok', records) | ('empty', None) | ('timeout', None)."""
    for attempt in range(retries + 1):
        try:
            answers = dns.resolver.resolve(domain, rtype, lifetime=10)
            return "ok", list(answers)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return "empty", None
        except dns.resolver.Timeout:
            if attempt < retries:
                time.sleep(retry_delay)
            continue
        except Exception:
            return "empty", None
    return "timeout", None


def get_mx_hosts(domain, retries=2, retry_delay=1.0):
    """
    Returns:
      list[str] - MX (or fallback A-record) hosts, sorted by preference
      []        - CONFIRMED: domain has no mail server (NXDOMAIN / no MX / no A)
      None      - DNS lookups timed out repeatedly; genuinely unknown, not "no server"
    Cached per domain for the life of the process.
    """
    if domain in _mx_cache:
        return _mx_cache[domain]

    status, answers = _resolve(domain, "MX", retries, retry_delay)
    if status == "ok":
        hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
        _mx_cache[domain] = hosts
        return hosts
    if status == "timeout":
        _mx_cache[domain] = None
        return None

    # status == "empty": no MX record, fall back to an A record per RFC 5321.
    status, _ = _resolve(domain, "A", retries, retry_delay)
    if status == "ok":
        hosts = [domain]
    elif status == "timeout":
        hosts = None
    else:
        hosts = []

    _mx_cache[domain] = hosts
    return hosts


def random_probe_local_part():
    return "verify-probe-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def open_smtp_session(host, sender_domain, timeout, use_starttls):
    """
    Connect to host:25 and perform a proper ESMTP handshake:
    EHLO -> (STARTTLS -> EHLO again) if the server offers it -> fall back to
    plain HELO only if the server doesn't understand EHLO at all.
    Raises on failure; returns a connected, ehlo'd smtplib.SMTP on success.

    Must construct SMTP with the host in the same call rather than SMTP() + .connect()
    separately: smtplib only sets the internal _host attribute (used as the TLS SNI
    server_hostname by starttls()) inside __init__, so a bare .connect() call afterward
    leaves it empty and starttls() fails with "server_hostname cannot be an empty string",
    silently breaking every command after it.
    """
    server = smtplib.SMTP(host, 25, timeout=timeout)

    code, _ = server.ehlo(sender_domain)
    if code < 200 or code >= 300:
        # Ancient / broken server that doesn't speak ESMTP at all.
        code, msg = server.helo(sender_domain)
        if code < 200 or code >= 300:
            raise smtplib.SMTPHeloError(code, msg)
        return server

    if use_starttls and server.has_extn("starttls"):
        try:
            server.starttls()
            # RFC 3207: session state resets after STARTTLS, must EHLO again.
            code, _ = server.ehlo(sender_domain)
            if code < 200 or code >= 300:
                raise smtplib.SMTPHeloError(code, "EHLO after STARTTLS failed")
        except Exception:
            # Couldn't upgrade to TLS; carry on with the plaintext session
            # we already have rather than failing the whole domain over it.
            pass

    return server


def connect_any(mx_hosts, sender_domain, timeout, use_starttls):
    """Try each MX host in preference order; return (server, host) for the first that works."""
    last_exc = RuntimeError("no MX hosts to try")
    for host in mx_hosts:
        try:
            return open_smtp_session(host, sender_domain, timeout, use_starttls), host
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc


def probe_catch_all(domain, mx_hosts, sender, sender_domain, timeout, use_starttls, rate_limiter=None):
    """
    One lightweight connection per domain: ask for a bogus address. If the server says
    250, the whole domain accepts everything at RCPT time and per-address checks are
    meaningless. Returns True / False / None (couldn't even connect to check).
    """
    try:
        server, _ = connect_any(mx_hosts, sender_domain, timeout, use_starttls)
    except Exception:
        return None
    try:
        if rate_limiter:
            rate_limiter.wait()
        server.mail(sender)
        code, _ = server.rcpt(f"{random_probe_local_part()}@{domain}")
        return code == 250
    except Exception:
        return None
    finally:
        try:
            server.quit()
        except Exception:
            pass


def smtp_check_shard(mx_hosts, emails, sender, sender_domain, timeout, delay, use_starttls,
                      rate_limiter=None, jitter=0.0):
    """
    One ESMTP session for a chunk ("shard") of a domain's address list.
    Returns a dict: email -> ("valid" | "invalid" | "unknown", reason)
    Caller must already know the domain is not catch-all before calling this.
    """
    results = {}
    try:
        server, _ = connect_any(mx_hosts, sender_domain, timeout, use_starttls)
    except Exception:
        for e in emails:
            results[e] = ("unknown", "could_not_complete_ehlo_handshake")
        return results

    def rcpt(addr):
        try:
            server.mail(sender)
            code, msg = server.rcpt(addr)
            try:
                server.rset()
            except Exception:
                pass
            return code, msg
        except (smtplib.SMTPServerDisconnected, socket.error):
            raise
        except Exception as exc:
            return None, str(exc)

    for addr in emails:
        if delay or jitter:
            time.sleep(delay + (random.uniform(0, jitter) if jitter else 0))
        if rate_limiter:
            rate_limiter.wait()
        try:
            code, msg = rcpt(addr)
        except Exception:
            # connection dropped mid-shard; try one reconnect
            try:
                server, _ = connect_any(mx_hosts, sender_domain, timeout, use_starttls)
                code, msg = rcpt(addr)
            except Exception:
                results[addr] = ("unknown", "connection_dropped_during_check")
                continue

        if code is None:
            results[addr] = ("unknown", f"smtp_error: {msg}")
        elif code == 250:
            results[addr] = ("valid", "smtp_accepted")
        elif code in (550, 551, 553):
            results[addr] = ("invalid", "mailbox_not_found")
        elif code in (450, 451, 452):
            results[addr] = ("unknown", "greylisted_or_temporary_failure")
        else:
            results[addr] = ("unknown", f"smtp_code_{code}")

    try:
        server.quit()
    except Exception:
        pass

    return results


class ResultWriter:
    """
    Owns the three output CSVs, writes rows incrementally (flushed immediately, so a
    crash mid-run only loses whatever was in flight), and - with resume=True - reads
    prior valid/invalid rows on startup so they're skipped instead of rechecked.
    unknown.csv rows are never treated as settled: they're always retried.
    """

    HEADER = ["email", "domain", "status_or_reason", "role_based", "disposable", "risky_for_spreadsheet"]

    def __init__(self, valid_path, invalid_path, unknown_path, resume):
        self.done = set()
        self._files = {}
        self._writers = {}
        self.counts = {"valid": 0, "invalid": 0, "unknown": 0}

        self._open("valid", valid_path, resume, track_done=True)
        self._open("invalid", invalid_path, resume, track_done=True)
        # unknown.csv is always a fresh snapshot of "still undetermined after this run" -
        # never appended to, so retried domains don't pile up duplicate rows across resumes.
        self._open("unknown", unknown_path, resume=False, track_done=False)

    def _open(self, bucket, path, resume, track_done):
        exists = os.path.exists(path)
        if resume and exists and track_done:
            with open(path, "r", newline="", encoding="utf-8") as rf:
                reader = csv.reader(rf)
                next(reader, None)
                for row in reader:
                    if row:
                        self.done.add(row[0].lstrip("'").strip().lower())

        mode = "a" if (resume and exists) else "w"
        f = open(path, mode, newline="", encoding="utf-8")
        writer = csv.writer(f)
        if mode == "w":
            writer.writerow(self.HEADER)
            f.flush()
        self._files[bucket] = f
        self._writers[bucket] = writer

    def write(self, bucket, email, domain, status_or_reason, role, disposable, risky):
        self._writers[bucket].writerow([email, domain, status_or_reason, role, disposable, risky])
        self._files[bucket].flush()
        self.counts[bucket] += 1

    def close(self):
        for f in self._files.values():
            f.close()


def setup_logging(log_file, quiet):
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if not quiet:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)


def cancel_pending(futures):
    """Cancel futures that haven't started yet; already-running ones finish normally."""
    cancelled = 0
    for fut in futures:
        if fut.cancel():
            cancelled += 1
    return cancelled


def main():
    ap = argparse.ArgumentParser(description="Verify a bulk email list (syntax + MX + optional SMTP).")
    ap.add_argument("--input", required=True, help="Path to input file (one email per line, extra columns ok).")
    ap.add_argument("--valid-out", default="valid.csv")
    ap.add_argument("--invalid-out", default="invalid.csv")
    ap.add_argument("--unknown-out", default="unknown.csv")
    ap.add_argument("--smtp", action="store_true", help="Also do a live SMTP RCPT TO check (slow, best-effort).")
    ap.add_argument("--sender", default=None, help="MAIL FROM address to use for --smtp (required if --smtp).")
    ap.add_argument("--workers", type=int, default=8, help="Max parallel SMTP connections overall.")
    ap.add_argument("--timeout", type=int, default=10, help="SMTP connection timeout in seconds.")
    ap.add_argument("--delay", type=float, default=0.5, help="Seconds to wait between RCPT checks on one connection.")
    ap.add_argument("--jitter", type=float, default=0.0,
                     help="Extra random 0..N seconds added on top of --delay, so timing isn't perfectly uniform.")
    ap.add_argument("--max-rcpt-per-minute", type=int, default=None,
                     help="Global cap on RCPT attempts per minute across ALL connections combined (unlimited if unset).")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N emails (for testing).")
    ap.add_argument("--no-starttls", action="store_true",
                     help="Skip opportunistic STARTTLS upgrade after EHLO and stay in plaintext.")
    ap.add_argument("--resume", action="store_true",
                     help="Skip emails already settled (valid/invalid) in existing output files; retry unknowns.")
    ap.add_argument("--max-conn-per-domain", type=int, default=4,
                     help="Max parallel SMTP connections for a single large domain (e.g. gmail.com).")
    ap.add_argument("--min-per-connection", type=int, default=25,
                     help="Don't open an extra connection for a domain unless it has at least this many addresses "
                          "per shard.")
    ap.add_argument("--dns-retries", type=int, default=2, help="Retries for a timed-out DNS lookup before giving up.")
    ap.add_argument("--dns-retry-delay", type=float, default=1.0, help="Seconds between DNS retry attempts.")
    ap.add_argument("--disposable-domains-file", default=None,
                     help="Optional newline-delimited file of extra disposable-email domains to flag.")
    ap.add_argument("--no-skip-unverifiable", action="store_true",
                     help="Don't skip known-unverifiable providers (Outlook/Hotmail/iCloud etc.) - probe them anyway.")
    ap.add_argument("--unverifiable-domains-file", default=None,
                     help="Optional newline-delimited file of extra domains to treat as unverifiable (skip probing).")
    ap.add_argument("--max-runtime-minutes", type=float, default=None,
                     help="Stop cleanly after this many minutes (e.g. for a cron window). Rerun with --resume "
                          "to continue. Unlimited if unset.")
    ap.add_argument("--log-file", default=None, help="Also write timestamped log lines to this file.")
    ap.add_argument("--quiet", action="store_true", help="Suppress console output (only --log-file, if given).")
    ap.add_argument("--summary-out", default="summary.json", help="Where to write the final run summary as JSON.")
    args = ap.parse_args()

    if args.smtp and not args.sender:
        ap.error("--smtp requires --sender you@yourdomain.com")

    setup_logging(args.log_file, args.quiet)
    run_started = datetime.now(timezone.utc)
    budget = RunTimeBudget(args.max_runtime_minutes)
    rate_limiter = RateLimiter(args.max_rcpt_per_minute) if args.max_rcpt_per_minute else None

    disposable_domains = load_disposable_domains(args.disposable_domains_file)
    unverifiable_domains = (
        set() if args.no_skip_unverifiable else load_unverifiable_domains(args.unverifiable_domains_file)
    )
    writer = ResultWriter(args.valid_out, args.invalid_out, args.unknown_out, args.resume)
    if args.resume and writer.done:
        logger.info(f"Resuming: {len(writer.done)} emails already settled in a previous run will be skipped.")

    def emit(bucket, email, domain, status_or_reason):
        role, disposable, risky = classify_flags(email, disposable_domains)
        writer.write(bucket, email, domain, status_or_reason, role, disposable, risky)

    # ---- Load + dedupe + syntax check ----
    seen = set()
    syntax_ok = []
    skipped_resume = 0

    with open(args.input, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            email = extract_email(line)
            if not email:
                continue
            key = email.lower()
            if key in seen:
                continue
            seen.add(key)

            if key in writer.done:
                skipped_resume += 1
                continue

            if is_valid_syntax(email):
                syntax_ok.append(email)
            else:
                emit("invalid", email, "", "invalid_syntax")

    if args.limit:
        syntax_ok = syntax_ok[: args.limit]

    logger.info(f"Loaded {len(seen)} unique emails."
                + (f" Skipped {skipped_resume} already settled from a previous run." if skipped_resume else ""))

    # ---- MX / domain check ----
    domains = {}
    for email in syntax_ok:
        domain = email.rsplit("@", 1)[1].lower()
        domains.setdefault(domain, []).append(email)

    logger.info(f"Checking MX records for {len(domains)} unique domains...")
    domain_mx = {}
    with ThreadPoolExecutor(max_workers=max(args.workers, 4)) as pool:
        futures = {
            pool.submit(get_mx_hosts, d, args.dns_retries, args.dns_retry_delay): d for d in domains
        }
        for fut in as_completed(futures):
            d = futures[fut]
            domain_mx[d] = fut.result()

    mx_ok_emails = []
    for domain, emails in domains.items():
        hosts = domain_mx.get(domain)
        if hosts:
            mx_ok_emails.extend(emails)
        elif hosts is None:
            for e in emails:
                emit("unknown", e, domain, "dns_lookup_timed_out")
        else:
            for e in emails:
                emit("invalid", e, domain, "no_mail_server_for_domain")

    logger.info(f"{len(mx_ok_emails)} emails have a domain that accepts mail.")

    if not args.smtp:
        for e in mx_ok_emails:
            domain = e.rsplit("@", 1)[1].lower()
            emit("valid", e, domain, "valid_domain_mx_only")
    elif budget.expired():
        logger.warning("Max runtime already reached before the SMTP stage started; leaving remaining "
                        "emails unprocessed for the next --resume run.")
    else:
        by_domain = {}
        for e in mx_ok_emails:
            domain = e.rsplit("@", 1)[1].lower()
            by_domain.setdefault(domain, []).append(e)

        checkable_domains = {d: emails for d, emails in by_domain.items() if domain_mx[d]}
        sender_domain = args.sender.split("@")[1]
        use_starttls = not args.no_starttls

        # Known-unverifiable providers: skip straight to unknown, no connection wasted.
        probe_domains = {}
        for d, emails in checkable_domains.items():
            if d in unverifiable_domains:
                for e in emails:
                    emit("unknown", e, d, "provider_blocks_verification_skipped")
            else:
                probe_domains[d] = emails

        logger.info(f"Probing {len(probe_domains)} domains for catch-all behaviour "
                    f"({len(checkable_domains) - len(probe_domains)} skipped as known-unverifiable providers)...")
        catch_all_status = {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(probe_catch_all, d, domain_mx[d], args.sender, sender_domain, args.timeout,
                            use_starttls, rate_limiter): d
                for d in probe_domains
            }
            for fut in as_completed(futures):
                if budget.expired():
                    cancel_pending(futures)
                    logger.warning("Max runtime reached during catch-all probing; stopping early.")
                    break
                catch_all_status[futures[fut]] = fut.result()

        # Build per-connection shards: large domains get split across up to
        # --max-conn-per-domain connections instead of bottlenecking on one.
        tasks = []
        for domain, emails in probe_domains.items():
            if domain not in catch_all_status:
                continue  # probing didn't finish (runtime budget) - leave unprocessed for next --resume
            catch_all = catch_all_status[domain]
            if catch_all is None:
                for e in emails:
                    emit("unknown", e, domain, "could_not_complete_ehlo_handshake")
                continue
            if catch_all:
                for e in emails:
                    emit("unknown", e, domain, "catch_all_domain_cannot_verify_mailbox")
                continue

            shard_count = min(args.max_conn_per_domain, max(1, len(emails) // args.min_per_connection))
            shard_size = ceil(len(emails) / shard_count)
            for i in range(0, len(emails), shard_size):
                tasks.append((domain, emails[i:i + shard_size]))

        total_smtp_emails = sum(len(shard) for _, shard in tasks)
        logger.info(f"Running SMTP checks: {len(tasks)} connection(s) across "
                    f"{len({d for d, _ in tasks})} domains ({total_smtp_emails} addresses)...")

        if budget.expired():
            logger.warning("Max runtime reached before SMTP checks could start; rerun with --resume.")
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(
                        smtp_check_shard, domain_mx[domain], shard, args.sender, sender_domain,
                        args.timeout, args.delay, use_starttls, rate_limiter, args.jitter,
                    ): (domain, shard)
                    for domain, shard in tasks
                }
                done = 0
                for fut in as_completed(futures):
                    if budget.expired():
                        cancel_pending(futures)
                        logger.warning(f"Max runtime reached after {done}/{len(tasks)} connections; "
                                       "remaining addresses left for the next --resume run.")
                        break
                    domain, shard = futures[fut]
                    done += 1
                    try:
                        results = fut.result()
                    except Exception as exc:
                        results = {e: ("unknown", f"checker_error: {exc}") for e in shard}
                    for email, (status, reason) in results.items():
                        emit(status, email, domain, reason)
                    logger.info(f"  [{done}/{len(tasks)} connections] {domain} shard done ({len(shard)} addresses)")

    writer.close()

    run_finished = datetime.now(timezone.utc)
    summary = {
        "started_at": run_started.isoformat(),
        "finished_at": run_finished.isoformat(),
        "duration_seconds": round((run_finished - run_started).total_seconds(), 1),
        "input_file": args.input,
        "smtp_check_enabled": args.smtp,
        "unique_emails_loaded": len(seen),
        "skipped_already_settled": skipped_resume,
        "counts": dict(writer.counts),
        "outputs": {"valid": args.valid_out, "invalid": args.invalid_out, "unknown": args.unknown_out},
    }
    with open(args.summary_out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info("Done.")
    logger.info(f"  valid:   {writer.counts['valid']}  -> {args.valid_out}")
    logger.info(f"  invalid: {writer.counts['invalid']}  -> {args.invalid_out}")
    logger.info(f"  unknown: {writer.counts['unknown']}  -> {args.unknown_out}  (couldn't be confirmed either way)")
    logger.info(f"  summary: {args.summary_out}")


if __name__ == "__main__":
    main()
