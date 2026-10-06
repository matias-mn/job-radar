import httpx

from ..models import Job
from . import get_json, html_to_text, parse_epoch_ms

URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _description(raw: dict) -> str:
    parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description", ""))]
    for section in raw.get("lists") or []:
        parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content', ''))}")
    if raw.get("additionalPlain"):
        parts.append(raw["additionalPlain"])
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _comp(raw: dict) -> str | None:
    salary = raw.get("salaryRange")
    if not salary or salary.get("min") is None:
        return None
    return f"{salary.get('currency', '')} {salary['min']:,}-{salary.get('max', salary['min']):,} {salary.get('interval', '')}".strip()


def normalize(slug: str, raw: dict) -> Job:
    cats = raw.get("categories") or {}
    locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
    location = "; ".join(locations)
    workplace = (raw.get("workplaceType") or "").lower()
    if raw.get("country"):
        location = f"{location} ({raw['country']})" if location else raw["country"]
    return Job(
        source="lever",
        company=slug,
        ext_id=raw["id"],
        title=raw.get("text", "").strip(),
        location=location,
        remote=workplace == "remote" or "remote" in location.lower(),
        url=raw.get("hostedUrl", ""),
        posted_at=parse_epoch_ms(raw.get("createdAt")),
        description=_description(raw),
        comp=_comp(raw),
    )


async def fetch(client: httpx.AsyncClient, slug: str, etag: str | None, stats) -> tuple[list[Job] | None, str | None]:
    data, etag = await get_json(client, URL.format(slug=slug), etag, stats)
    if data is None:
        return None, etag
    return [normalize(slug, j) for j in data], etag
