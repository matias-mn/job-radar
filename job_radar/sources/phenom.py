"""Phenom career sites (Activision Blizzard, Blizzard, King). Slug = the site's
middle label: careers.{slug}.com. Search results only have a teaser, so the full
description comes from the linked Workday posting in hydrate()."""

import re

import httpx

from ..models import Job
from . import parse_iso, workday

# careers.blizzard.com/global/en/job/BLENGLOBALR028280EXTERNALENGLOBAL -> R028280 (the jobId)
JOB_URL_RE = re.compile(r"careers\.[\w-]+\.com/(?:[\w-]+/)*job/[A-Z]*?(R\d+)")

URL = "https://careers.{slug}.com/widgets"
PAGE = 50
# Most Phenom sites use en_us/us; a few global ones need en_global/global.
LOCALES = {"blizzard": ("en_global", "global")}


def _body(slug: str, offset: int) -> dict:
    lang, country = LOCALES.get(slug, ("en_us", "us"))
    return {
        "lang": lang, "country": country, "deviceType": "desktop", "pageName": "search-results",
        "ddoKey": "refineSearch", "from": offset, "size": PAGE, "keywords": "", "jobs": True,
        "siteType": "external", "global": True, "selected_fields": {},
    }


def req_id(url: str) -> str | None:
    """The jobId in a link to a Phenom posting page."""
    m = JOB_URL_RE.search(url or "")
    return m[1] if m else None


def normalize(slug: str, raw: dict) -> Job:
    location = "; ".join(raw.get("multi_location") or []) or raw.get("location") or ""
    return Job(
        source="phenom",
        company=slug,
        ext_id=raw.get("jobId") or raw["jobSeqNo"],
        title=(raw.get("title") or "").strip(),
        location=location,
        remote="remote" in location.lower(),
        url=workday.job_page(raw.get("applyUrl", "")),
        posted_at=parse_iso((raw.get("postedDate") or raw.get("dateCreated") or "").replace("+0000", "+00:00")),
        description="",  # filled by hydrate()
        apply_url=raw.get("applyUrl", ""),
        teaser=raw.get("descriptionTeaser") or "",
    )


async def fetch(client: httpx.AsyncClient, slug: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    # Phenom sends no ETag, so this is a full fetch every run (~200 KB for ~60 jobs).
    jobs: list[Job] = []
    offset, total = 0, None
    while total is None or offset < total:
        r = await client.post(URL.format(slug=slug), json=_body(slug, offset))
        r.raise_for_status()
        stats.bytes += len(r.content)
        result = r.json()["refineSearch"]
        total = min(result.get("totalHits") or 0, 1000)
        page = result["data"]["jobs"]
        jobs += [normalize(slug, j) for j in page]
        if len(page) < PAGE:
            break
        offset += PAGE
    return jobs, None


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    info = await workday.details(client, job.apply_url)
    if info is None:
        job.description = job.teaser  # not a Workday link; settle for the teaser
        return
    job.description = info["description"] or job.teaser
    job.remote = job.remote or info["remote"]
