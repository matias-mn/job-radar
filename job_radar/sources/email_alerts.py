"""Job-alert emails forwarded to a dedicated Gmail. Slug = which sender to read ("linkedin", "indeed").

Logs in over IMAP with an app password (ALERT_EMAIL, ALERT_EMAIL_APP_PASSWORD in .env)
and reads All Mail read-only, so archiving or labeling emails doesn't hide them. Like
Workday, there's no ETag: each board remembers the highest email UID it has read.
Without one (first run, or after a config change) it reads the last MAX_AGE_DAYS.
"""

import asyncio
import email
import imaplib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email import policy

from . import indeed, linkedin

log = logging.getLogger(__name__)

HOST = "imap.gmail.com"
MAILBOX = '"[Gmail]/All Mail"'
MAX_AGE_DAYS = 15  # keep in step with max_age_days in config.toml
SENDERS = {"linkedin": "jobalerts-noreply@linkedin.com", "indeed": "donotreply@jobalert.indeed.com"}
PARSERS = {"linkedin": linkedin.parse_email, "indeed": indeed.parse_email}


def _connect() -> imaplib.IMAP4:
    user, password = os.environ.get("ALERT_EMAIL"), os.environ.get("ALERT_EMAIL_APP_PASSWORD")
    if not user or not password:
        raise ValueError("ALERT_EMAIL and ALERT_EMAIL_APP_PASSWORD must be set in .env")
    conn = imaplib.IMAP4_SSL(HOST, 993, timeout=30)
    conn.login(user, password)
    return conn


def _read(slug: str, etag: str | None, stats) -> tuple[list | None, str | None]:
    sender = SENDERS[slug]
    mark = json.loads(etag) if etag else {}
    conn = _connect()
    try:
        conn.select(MAILBOX, readonly=True)
        validity = conn.response("UIDVALIDITY")[1][0].decode()
        last = mark.get("uid", 0) if mark.get("validity") == validity else 0
        if last:
            query = ["FROM", f'"{sender}"', "UID", f"{last + 1}:*"]
        else:
            since = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
            query = ["FROM", f'"{sender}"', "SINCE", since.strftime("%d-%b-%Y")]
        _, data = conn.uid("SEARCH", None, *query)
        uids = sorted(u for u in map(int, data[0].split()) if u > last)  # "N:*" always returns the newest UID
        if not uids:
            return None, etag
        jobs = []
        for uid in uids:
            _, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK: leave it unread
            raw = parts[0][1]
            stats.bytes += len(raw)
            found = PARSERS[slug](email.message_from_bytes(raw, policy=policy.default))
            if not found:
                log.warning("no jobs found in %s email UID %d; has the format changed?", slug, uid)
            jobs += found
        return jobs, json.dumps({"validity": validity, "uid": uids[-1]})
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


async def fetch(client, slug: str, etag: str | None, stats):
    """Same signature as the job-board fetchers; `client` is unused (IMAP, not HTTP)."""
    if slug not in SENDERS:
        raise ValueError(f"unknown email source {slug!r}; known: {', '.join(SENDERS)}")
    try:
        return await asyncio.to_thread(_read, slug, etag, stats)
    except (imaplib.IMAP4.error, OSError) as e:
        raise ValueError(f"IMAP: {e}") from e  # fetch_all reports ValueErrors as a failed board
