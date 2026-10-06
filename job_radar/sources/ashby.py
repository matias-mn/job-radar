import httpx

from ..models import Job
from . import get_json, html_to_text, parse_iso

URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def _location(raw: dict) -> str:
    names = [raw.get("location") or ""]
    names += [s.get("location", "") for s in raw.get("secondaryLocations") or []]
    location = "; ".join(dict.fromkeys(n for n in names if n))
    country = (((raw.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
    if country and country.lower() not in location.lower():
        location = f"{location} ({country})" if location else country
    return location


def _comp(raw: dict) -> str | None:
    comp = raw.get("compensation") or {}
    return comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")


def normalize(slug: str, raw: dict) -> Job:
    location = _location(raw)
    return Job(
        source="ashby",
        company=slug,
        ext_id=raw["id"],
        title=raw.get("title", "").strip(),
        location=location,
        remote=bool(raw.get("isRemote")) or "remote" in location.lower(),
        url=raw.get("jobUrl", ""),
        posted_at=parse_iso(raw.get("publishedAt")),
        description=raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml", "")),
        comp=_comp(raw),
    )


async def fetch(client: httpx.AsyncClient, slug: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    data, etag = await get_json(client, URL.format(slug=slug), etag, stats)
    if data is None:
        return None, etag
    return [normalize(slug, j) for j in data.get("jobs", []) if j.get("isListed", True)], etag
