"""GameJobs.co: a games-industry job aggregator. Slug = a search query.

The Atom feed only has "Title at Company", a link and a date. Location and
description come from the job page's schema.org JobPosting data in hydrate(),
which also finds the original application link so jobs from boards we already
poll can be recognized as duplicates.
"""

import json
import re
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import httpx

from ..models import Job
from . import html_to_text, parse_iso

FEED_URL = "https://gamejobs.co/search?q={query}&format=atom"
US_COUNTRY = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}
ATOM = "{http://www.w3.org/2005/Atom}"
LD_JSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
APPLY_RE = re.compile(r'href="(https?://(?!(?:www\.)?gamejobs\.co)[^"]+)"[^>]*>\s*Apply', re.I)


def split_title(text: str) -> tuple[str, str]:
    """'Game Security Analyst at Rockstar' -> ('Game Security Analyst', 'Rockstar')."""
    title, sep, company = text.rpartition(" at ")
    return (title, company) if sep else (text, "")


def parse_feed(xml: bytes) -> list[Job]:
    jobs = []
    for entry in ET.fromstring(xml).iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link").get("href")
        title, company = split_title((entry.findtext(f"{ATOM}title") or "").strip())
        jobs.append(Job(
            source="gamejobs",
            company=company,
            ext_id=link.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            location=None,  # unknown until hydrate()
            remote=False,
            url=link,
            posted_at=parse_iso(entry.findtext(f"{ATOM}updated")),
            description="",
        ))
    return jobs


async def fetch(client: httpx.AsyncClient, query: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    # No ETag support; the first page (newest 100) is ~35 KB.
    r = await client.get(FEED_URL.format(query=quote_plus(query)))
    r.raise_for_status()
    stats.bytes += len(r.content)
    return parse_feed(r.content), None


def _place(loc: dict) -> str:
    addr = loc.get("address") or {}
    if isinstance(addr, str):
        return addr
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
    return ", ".join(p for p in parts if isinstance(p, str) and p)


def apply_posting(job: Job, posting: dict) -> None:
    job.description = html_to_text(posting.get("description", ""))
    locations = posting.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    job.location = "; ".join(p for p in (_place(l) for l in locations) if p)
    if posting.get("jobLocationType") == "TELECOMMUTE":
        reqs = posting.get("applicantLocationRequirements") or []
        if isinstance(reqs, dict):
            reqs = [reqs]
        allowed = [r.get("name", "") for r in reqs if isinstance(r, dict)]
        countries = allowed + [
            (l.get("address") or {}).get("addressCountry", "") for l in locations if isinstance(l.get("address"), dict)
        ]
        # Remote only counts as remote-US if the US is among the allowed/listed countries (or none are given).
        job.remote = not [c for c in countries if c] or any(c.upper() in US_COUNTRY for c in countries)
        job.location = "; ".join(p for p in ("Remote", job.location, *allowed) if p)
    job.posted_at = parse_iso(posting.get("datePosted")) or job.posted_at


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    r = await client.get(job.url)
    r.raise_for_status()
    for block in LD_JSON_RE.findall(r.text):
        data = json.loads(block)
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            apply_posting(job, data)
            break
    else:
        raise ValueError("no JobPosting data on page")
    if m := APPLY_RE.search(r.text):
        job.apply_url = m.group(1)
    job.location = job.location or ""
