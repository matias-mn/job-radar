"""freehire.me: an open-source index of ~3M postings crawled from 120+ applicant tracking systems.
Slug = a filter string for its public search API (https://freehire.me/docs/api), e.g.
"category=security&requires_clearance=false". No key needed for personal, non-commercial use.

There are no ETags, so results are sorted by when freehire first recorded each job, and
a check reads pages until it reaches jobs the previous check saw. The board's ETag slot
holds the newest created_at seen plus the jobs recorded at that moment, since freehire
records a crawl's jobs in batches that share a timestamp. Search results carry a truncated description, so a new job that
passes the filters gets the full posting in hydrate().

freehire re-lists postings from boards we poll and from LinkedIn/Indeed/GameJobs alerts;
sources.is_duplicate() drops those.
"""

import asyncio
import json
from urllib.parse import parse_qsl

import httpx

from ..models import Job
from . import html_to_text, parse_iso

API = "https://freehire.me/api/v1"
PAGE = 100  # the API's maximum; used for the first scan
CHECK_PAGE = 25  # later checks usually find a handful of new jobs, and each row carries a ~1 KB preview
MAX_ROWS = 10_000  # offset + limit may not exceed this
FIRST_SCAN_DAYS = 15  # keep in step with max_age_days in config.toml
BAY_AREA_METRO = "San Francisco Bay Area (US)"
DEFAULTS = {"countries": "US", "sort": "created_at", "order": "desc", "open_within_days": FIRST_SCAN_DAYS}


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None, stats=None) -> dict:
    """GET with a short wait on 429 (the free tier allows 600 requests a minute)."""
    for attempt in range(3):
        r = await client.get(f"{API}{path}", params=params)
        if r.status_code != 429 or attempt == 2:
            break
        await asyncio.sleep(min(float(r.headers.get("retry-after") or 5), 30))
    r.raise_for_status()
    if stats is not None:
        stats.bytes += len(r.content)
    return r.json()


def _location(raw: dict) -> str:
    location = raw.get("location") or ", ".join(raw.get("cities") or [])
    # freehire knows Alameda or Dublin, CA is in the Bay Area; config's city list may not.
    if BAY_AREA_METRO in (raw.get("metros") or []):
        location = f"{location} (San Francisco Bay Area)" if location else "San Francisco Bay Area"
    return location


def _comp(raw: dict) -> str | None:
    e = raw.get("enrichment") or {}
    low, high, currency = e.get("salary_min"), e.get("salary_max"), e.get("salary_currency")
    if not (low or high) or not currency:
        return None
    pay = f"{low:,}-{high:,}" if low and high and low != high else f"{low or high:,}"
    return f"{currency} {pay} per {e.get('salary_period') or 'year'}"


def normalize(raw: dict) -> Job:
    return Job(
        source="freehire",
        company=raw.get("company") or raw.get("company_slug") or "",
        ext_id=raw["public_slug"],
        title=(raw.get("title") or "").strip(),
        location=_location(raw),
        remote=raw.get("work_mode") == "remote",
        url=raw.get("url", ""),
        posted_at=parse_iso(raw.get("posted_at")) or parse_iso(raw.get("created_at")),
        description="",  # search has a preview only; hydrate() reads the full posting
        comp=_comp(raw),
        apply_url=raw.get("url", ""),  # the company's own posting, so is_duplicate() can match polled boards
    )


async def fetch(client: httpx.AsyncClient, query: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    last = json.loads(etag) if etag else {}
    since, seen = parse_iso(last.get("created_at")), set(last.get("slugs", []))
    limit = CHECK_PAGE if since else PAGE
    params = DEFAULTS | dict(parse_qsl(query)) | {"limit": limit}

    def is_new(raw: dict) -> bool:
        created = parse_iso(raw["created_at"])
        return not since or created > since or (created == since and raw["public_slug"] not in seen)

    jobs: list[Job] = []
    rows_seen: list[dict] = []
    for offset in range(0, MAX_ROWS, limit):
        data = await _get(client, "/jobs/search", params | {"offset": offset}, stats)
        if ignored := (data.get("meta") or {}).get("ignored_params"):
            # freehire skips filters it doesn't know, which would quietly widen the search.
            raise ValueError(f"freehire ignored {ignored}")
        rows = data.get("data") or []
        rows_seen += rows
        fresh = [raw for raw in rows if is_new(raw)]
        jobs += [normalize(raw) for raw in fresh]
        if len(rows) < limit or (since and parse_iso(rows[-1]["created_at"]) < since):  # reached the last check
            break
    if not jobs:
        return None, etag
    newest = rows_seen[0]["created_at"]
    slugs = [raw["public_slug"] for raw in rows_seen if raw["created_at"] == newest]
    if newest == last.get("created_at"):
        slugs += seen  # more jobs recorded in the same batch since the last check
    return jobs, json.dumps({"created_at": newest, "slugs": sorted(set(slugs))})


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    data = (await _get(client, f"/jobs/{job.ext_id}"))["data"]
    job.description = html_to_text(data.get("description") or "")
