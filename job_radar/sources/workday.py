"""Workday career sites. Slug = "tenant.wdN/site", e.g. "crowdstrike.wd5/crowdstrikecareers".

Workday sends no ETags and lists 20 jobs per request, newest first (a few pinned
jobs can sit at the top). So instead of an ETag, each board remembers the job IDs
it has already shown, and a check reads pages until two in a row hold nothing new
or everything is older than MAX_AGE_DAYS. An unchanged board costs two requests.

The detail helpers here are also used by the Phenom (Activision) source.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..models import Job
from . import html_to_text, parse_iso

PAGE = 20  # Workday's maximum
MAX_PAGES = 60
MAX_AGE_DAYS = 14
SEEN_CAP = 3000  # job IDs remembered per board
POSTED_RE = re.compile(r"(\d+)\+? Days? Ago", re.I)
MULTI_LOCATION_RE = re.compile(r"^\d+ Locations?$", re.I)

# https://{tenant}.wd{N}.myworkdayjobs.com/[en-US/]{site}/job/{location}/{slug}_{R-id}[/apply]
URL_RE = re.compile(
    r"https://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"(?P<site>[^/]+)(?P<path>/job/[^?#]+?)(?:/apply)?/?(?:[?#].*)?$"
)
REQ_RE = re.compile(r"_(R\d+(?:-\d+)?)$")


def req_id(url: str) -> str | None:
    m = URL_RE.match(url or "")
    return (REQ_RE.search(m["path"]) or [None, None])[1] if m else None


def job_page(url: str) -> str:
    """Human-facing posting URL (drops the trailing /apply)."""
    m = URL_RE.match(url or "")
    return f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com/{m['site']}{m['path']}" if m else url


async def details(client: httpx.AsyncClient, url: str) -> dict | None:
    """Returns {description, location, remote, posted_at} from Workday's public JSON, or None."""
    m = URL_RE.match(url or "")
    if not m:
        return None
    api = f"https://{m['tenant']}.{m['wd']}.myworkdayjobs.com/wday/cxs/{m['tenant']}/{m['site']}{m['path']}"
    r = await client.get(api, headers={"Accept": "application/json"})
    r.raise_for_status()
    info = r.json().get("jobPostingInfo", {})
    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    location = "; ".join(l for l in locations if l)
    if info.get("country", {}).get("descriptor"):
        location = f"{location} ({info['country']['descriptor']})"
    return {
        "description": html_to_text(info.get("jobDescription", "")),
        "location": location,
        "remote": "remote" in location.lower() or bool(info.get("remoteType")),
        "posted_at": parse_iso(info.get("startDate")),
    }


def days_ago(posted_on: str) -> int:
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 3 Days Ago' -> 3, 'Posted 30+ Days Ago' -> 31."""
    text = posted_on or ""
    if "today" in text.lower():
        return 0
    if "yesterday" in text.lower():
        return 1
    m = POSTED_RE.search(text)
    return (int(m[1]) + ("+" in text)) if m else 0


def _parse_slug(slug: str) -> tuple[str, str, str]:
    host, site = slug.split("/", 1)
    tenant, wd = host.split(".", 1)
    return tenant, wd, site


def normalize(slug: str, raw: dict, now: datetime) -> Job:
    tenant, wd, site = _parse_slug(slug)
    path = raw["externalPath"]
    ext_id = (raw.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1]
    text = raw.get("locationsText") or ""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"
    return Job(
        source="workday",
        company=tenant,
        ext_id=ext_id,
        title=(raw.get("title") or "").strip(),
        location=None if MULTI_LOCATION_RE.match(text) or not text else text,  # "3 Locations": hydrate to learn them
        remote="remote" in text.lower(),
        url=url,
        posted_at=now - timedelta(days=days_ago(raw.get("postedOn", ""))),
        description="",
        apply_url=url,
    )


async def fetch(client: httpx.AsyncClient, slug: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    tenant, wd, site = _parse_slug(slug)
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    seen: list[str] = json.loads(etag) if etag else []
    seen_set = set(seen)
    now = datetime.now(timezone.utc)
    new: list[Job] = []
    page_ids: list[str] = []
    quiet_pages = 0
    for page in range(MAX_PAGES):
        r = await client.post(api, json={"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": ""},
                              headers={"Accept": "application/json"})
        r.raise_for_status()
        stats.bytes += len(r.content)
        postings = r.json().get("jobPostings") or []
        # Occasionally a posting has no path (Nvidia); skip it rather than fail the board.
        jobs = [normalize(slug, p, now) for p in postings if p.get("externalPath")]
        fresh = [j for j in jobs if j.ext_id not in seen_set]
        new += fresh
        page_ids += [j.ext_id for j in jobs]
        quiet_pages = 0 if fresh else quiet_pages + 1
        all_old = page > 0 and jobs and all(days_ago(p.get("postedOn", "")) > MAX_AGE_DAYS for p in postings)
        if len(postings) < PAGE or quiet_pages >= 2 or all_old:
            break
    if not new:
        return None, etag
    remembered = list(dict.fromkeys(page_ids + seen))[:SEEN_CAP]
    return new, json.dumps(remembered)


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    info = await details(client, job.url)
    if info is None:
        raise ValueError(f"not a Workday URL: {job.url}")
    job.description = info["description"]
    job.location = info["location"] or job.location or ""
    job.remote = job.remote or info["remote"]
    job.posted_at = info["posted_at"] or job.posted_at
