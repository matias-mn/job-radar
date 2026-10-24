"""LinkedIn job-alert emails (jobalerts-noreply@linkedin.com), read by email_alerts.py.

Each email lists up to ~6 job cards: title, "Company · Location (Remote|Hybrid|On-site)",
and sometimes a salary. There's no description, so resolve.hydrate() first looks for
the same posting on the company's own job board, then falls back to LinkedIn's public
job page (fetch_description). That page needs no login and gives the full description
plus LinkedIn's seniority level. It's read one job at a time, a few seconds apart.
LinkedIn sometimes answers 429 even at that pace, so a refusal is retried after a
pause; after a few in a row, LinkedIn's pages are skipped for the rest of the run. Links in the emails carry login and
tracking tokens, so only the bare job ID is kept.
"""

import asyncio
import html
import logging
import re
import time
import weakref
from collections import Counter
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import httpx

from ..models import Job
from . import html_to_text

log = logging.getLogger(__name__)

CARD_RE = re.compile(r'<[^>]*data-test-id="job-card"[^>]*>')
JOB_ID_RE = re.compile(r"jobs/view/(\d+)")
WORKPLACE_RE = re.compile(r"\s*\((Remote|Hybrid|On-site)\)$")
SALARY_RE = re.compile(r"^\$[\d.,]+K?(?:\s*-\s*\$[\d.,]+K?)?\s*/\s*(?:year|hour)", re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>")


def _texts(chunk: str) -> list[str]:
    """Visible text pieces of an HTML fragment, in order."""
    pieces = TAG_RE.split(COMMENT_RE.sub("", chunk))
    return [t for t in (re.sub(r"\s+", " ", html.unescape(p)).strip() for p in pieces) if t]


def parse_card(chunk: str) -> dict | None:
    m = JOB_ID_RE.search(chunk)
    texts = _texts(chunk)
    if not m or len(texts) < 2 or " · " not in texts[1]:
        return None
    company, _, place = texts[1].rpartition(" · ")
    workplace = WORKPLACE_RE.search(place)
    return {
        "id": m[1],
        "title": texts[0],
        "company": company.strip(),
        "location": place.strip(),  # keeps "(Hybrid)" etc. for the alert
        "remote": bool(workplace) and workplace[1] == "Remote",
        "comp": next((t for t in texts[2:4] if SALARY_RE.match(t)), None),
    }


def parse_email(msg: EmailMessage) -> list[Job]:
    body = msg.get_body(("html",))
    if body is None:
        return []
    received = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    jobs = []
    for chunk in CARD_RE.split(body.get_content())[1:]:
        if card := parse_card(chunk):
            jobs.append(Job(
                source="linkedin",
                company=card["company"],
                ext_id=card["id"],
                title=card["title"],
                location=card["location"],
                remote=card["remote"],
                url=f"https://www.linkedin.com/jobs/view/{card['id']}/",
                posted_at=received,  # the email has no posting date; LinkedIn alerts on new postings
                description="",  # filled by resolve.hydrate()
                comp=card["comp"],
            ))
    return jobs


# ---- LinkedIn's public job page ----

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
MARKUP_RE = re.compile(r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S)
CRITERIA_RE = re.compile(r'job-criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', re.S)
CRITERIA = ("Seniority level", "Employment type")
CLOSED = "No longer accepting applications"
GAP_SECONDS = 3  # between page requests
PUSHBACK = {429, 999}  # 999 is LinkedIn's "looks like a bot"
BACKOFF_SECONDS = 15  # after a refusal: wait 15 s, then 30 s, then give up for the run
TRIES = 3


@dataclass
class _Pace:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last: float = 0.0
    blocked: bool = False


_pace: "weakref.WeakKeyDictionary[httpx.AsyncClient, _Pace]" = weakref.WeakKeyDictionary()  # per client (one per run)
# This process's page reads: "read" (got a page) and "blocked" (gave up after TRIES refusals).
# __main__ turns these into a Slack notice when LinkedIn starts or stops refusing.
page_stats: Counter = Counter()


def parse_posting(page: str) -> str:
    """The description, headed by LinkedIn's seniority level and employment type. '' if the page has none."""
    m = MARKUP_RE.search(page)
    body = html_to_text(m[1]) if m else ""
    if not body:
        return ""
    header = [f"{k}: {html.unescape(v)}" for k, v in CRITERIA_RE.findall(page) if k in CRITERIA and v != "Not Applicable"]
    if CLOSED in page:
        header.insert(0, "LinkedIn says this job is no longer accepting applications.")
    return "\n".join(header + ["", body]) if header else body


async def fetch_description(client: httpx.AsyncClient, job_id: str) -> str:
    """'' when the posting is gone, has no description, or LinkedIn is pushing back."""
    pace = _pace.setdefault(client, _Pace())
    async with pace.lock:
        if pace.blocked:
            return ""
        for attempt in range(1, TRIES + 1):
            await asyncio.sleep(max(0.0, pace.last + GAP_SECONDS - time.monotonic()))
            try:
                r = await client.get(GUEST_URL.format(id=job_id))
            finally:
                pace.last = time.monotonic()
            if r.status_code not in PUSHBACK:
                break
            if attempt < TRIES:
                await asyncio.sleep(BACKOFF_SECONDS * attempt)
        else:
            pace.blocked = True
            page_stats["blocked"] += 1
            log.warning("LinkedIn answered %d %d times in a row; skipping its job pages for the rest of this run",
                        r.status_code, TRIES)
            return ""
    if r.status_code == 404:
        return ""
    r.raise_for_status()
    page_stats["read"] += 1
    return parse_posting(r.text)
