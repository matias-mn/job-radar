"""Indeed job-alert emails (donotreply@jobalert.indeed.com), read by email_alerts.py.

The plain-text part lists each job as a block of lines:

    Cybersecurity Analyst in Santa Clara, CA        title
    Unisys - Santa Clara, CA                        company - location
    $72,000 - $96,000 a year                        pay (optional; Indeed estimates some)
    Easily apply                                    (optional)
    Assists in monitoring security events...        snippet from the posting
    Just posted                                     age
    https://www.indeed.com/rc/clk/dl?jk=...         link with personal tokens

Only the job key (jk) is kept from the link. Like LinkedIn, the full description
comes from the company's own board when resolve.py can find it; Indeed's pages are
never fetched.
"""

import re
from datetime import timedelta
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from ..models import Job

JK_RE = re.compile(r"https?://\S*[?&]jk=([0-9a-f]+)")
PAY_RE = re.compile(r"^\$[\d,.]+(?:\s*-\s*\$[\d,.]+)?\s+(?:a|an)\s+(?:year|month|week|day|hour)$", re.I)
AGE_RE = re.compile(r"^(?:just posted|today|(\d+)\+? days? ago)$", re.I)
SKIP = {"easily apply", "responsive employer", "urgently hiring", "new"}


def parse_block(lines: list[str], received) -> Job | None:
    m = JK_RE.search(lines[-1]) if lines else None
    if not m or len(lines) < 3 or " - " not in lines[1]:
        return None
    company, _, location = lines[1].rpartition(" - ")
    pay, snippet, days = None, [], 0
    for line in lines[2:-1]:
        if PAY_RE.match(line):
            pay = line
        elif age := AGE_RE.match(line):
            days = int(age[1] or 0)
        elif line.lower() not in SKIP:
            snippet.append(line)
    return Job(
        source="indeed",
        company=company.strip(),
        ext_id=m[1],
        title=lines[0],
        location=location.strip(),
        remote="remote" in location.lower(),
        url=f"https://www.indeed.com/viewjob?jk={m[1]}",
        posted_at=received - timedelta(days=days) if received else None,
        description="",  # filled by resolve.hydrate()
        comp=pay,
        teaser=" ".join(snippet),
    )


def parse_email(msg: EmailMessage) -> list[Job]:
    body = msg.get_body(("plain",))
    if body is None:
        return []
    received = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    jobs = []
    for block in re.split(r"\n\s*\n", body.get_content()):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if job := parse_block(lines, received):
            jobs.append(job)
    return jobs
