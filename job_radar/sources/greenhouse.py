import httpx

from ..models import Job
from . import get_json, html_to_text, parse_iso

# The list endpoint without ?content=true is ~4x smaller. Descriptions are fetched
# per job by hydrate(), only for new jobs that pass the filters.
LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}"


def normalize(slug: str, raw: dict) -> Job:
    location = (raw.get("location") or {}).get("name") or ""
    offices = [o.get("name") for o in raw.get("offices") or [] if o.get("name")]
    extra = [o for o in offices if o.lower() not in location.lower()]
    if extra:
        location = "; ".join([location, *extra]) if location else "; ".join(extra)
    return Job(
        source="greenhouse",
        company=slug,
        ext_id=str(raw["id"]),
        title=raw.get("title", "").strip(),
        location=location,
        remote="remote" in location.lower(),
        url=raw.get("absolute_url", ""),
        posted_at=parse_iso(raw.get("first_published") or raw.get("updated_at")),
        description=html_to_text(raw.get("content", "")),
    )


async def fetch(client: httpx.AsyncClient, slug: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    data, etag = await get_json(client, LIST_URL.format(slug=slug), etag, stats)
    if data is None:
        return None, etag
    return [normalize(slug, j) for j in data.get("jobs", [])], etag


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    r = await client.get(JOB_URL.format(slug=job.company, id=job.ext_id))
    r.raise_for_status()
    full = normalize(job.company, r.json())
    job.description = full.description
    job.location = full.location or job.location  # detail view adds office names
    job.remote = job.remote or full.remote
