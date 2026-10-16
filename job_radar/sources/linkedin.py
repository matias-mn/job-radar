"""LinkedIn job-alert emails (jobalerts-noreply@linkedin.com), read by email_alerts.py.

Each email lists up to ~6 job cards: title, "Company · Location (Remote|Hybrid|On-site)",
and sometimes a salary. There's no description, so hydrate() looks for the same
posting on the company's own job board (resolve.py). LinkedIn's job pages are never
fetched. Links in the emails carry login and tracking tokens, so only the bare job
ID is kept.
"""

import html
import re
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from ..models import Job

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
