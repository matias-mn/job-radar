import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

from ..models import Job

log = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Greenhouse double-escapes its HTML, so unescape first, then strip tags."""
    parser = _TextExtractor()
    parser.feed(html.unescape(raw or ""))
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_epoch_ms(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc) if value else None


@dataclass
class Fetched:
    jobs: list[Job] = field(default_factory=list)
    etags: dict[str, str] = field(default_factory=dict)  # board -> new ETag, for boards that changed
    unchanged: set[str] = field(default_factory=set)  # boards that answered 304 Not Modified
    errors: dict[str, str] = field(default_factory=dict)
    bytes: int = 0
    board_bytes: dict[str, int] = field(default_factory=dict)  # per board, for boards that sent a body


async def get_json(client: httpx.AsyncClient, url: str, etag: str | None, stats: Fetched):
    """GET with If-None-Match. Returns (data, etag); data is None when the board is unchanged."""
    r = await client.get(url, headers={"If-None-Match": etag} if etag else {})
    if r.status_code == 304:
        return None, etag
    r.raise_for_status()
    stats.bytes += len(r.content)
    return r.json(), r.headers.get("etag")


# Imported after the helpers above, which the source modules use.
from . import workday  # noqa: E402
from . import ashby, gamejobs, greenhouse, lever, phenom  # noqa: E402

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "phenom": phenom.fetch,
    "gamejobs": gamejobs.fetch,
    "workday": workday.fetch,
}
AGGREGATORS = {"gamejobs"}  # multi-company sources that may re-list jobs from boards we poll
# Sources whose listing omits the description (and, for gamejobs, the location).
HYDRATORS = {
    "greenhouse": greenhouse.hydrate,
    "phenom": phenom.hydrate,
    "gamejobs": gamejobs.hydrate,
    "workday": workday.hydrate,
}

# Application links on boards we can poll directly -> that board's job key.
_ORIGIN_RES = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})")),
]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def title_index(jobs) -> dict[str, set[str]]:
    """normalized title -> normalized company slugs, for jobs from boards we poll directly."""
    index: dict[str, set[str]] = {}
    for source, company, title in jobs:
        if source not in AGGREGATORS:
            index.setdefault(_norm(title), set()).add(_norm(company))
    return index


def is_duplicate(job: Job, polled_boards: set[str], seen_keys: set[str], titles: dict[str, set[str]] | None = None) -> bool:
    """True if an aggregator job is a posting we already get from the company's own board."""
    if job.source not in AGGREGATORS:
        return False
    for source, pattern in _ORIGIN_RES:
        if m := pattern.search(job.apply_url):
            return f"{source}:{m[1]}" in polled_boards or f"{source}:{m[1]}:{m[2]}" in seen_keys
    if (req := workday.req_id(job.apply_url)) and any(
        k.startswith("phenom:") and k.endswith(f":{req}") for k in seen_keys
    ):
        return True
    # No usable link: same title at a company whose name matches a polled board's slug
    # ("Roblox" ~ roblox, "Rockstar" ~ rockstargames, "Take-Two Interactive" ~ taketwo).
    name = _norm(job.company)
    return len(name) >= 3 and any(
        slug.startswith(name) or name.startswith(slug) for slug in (titles or {}).get(_norm(job.title), ())
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "job-radar/0.2"})


async def fetch_all(
    companies: dict[str, list[str]], etags: dict[str, str] | None = None, skip: set[str] = frozenset(), concurrency: int = 10
) -> Fetched:
    """Fetch every configured board except `skip`, skipping unchanged ones via ETags. Boards are keyed 'source:slug'."""
    etags = etags or {}
    out = Fetched()
    sem = asyncio.Semaphore(concurrency)

    async def one(client: httpx.AsyncClient, source: str, slug: str) -> None:
        board = f"{source}:{slug}"
        counter = Fetched()  # per-board byte count; fetchers add to .bytes
        async with sem:
            try:
                jobs, etag = await FETCHERS[source](client, slug, etags.get(board), counter)
            except httpx.HTTPStatusError as e:
                out.errors[board] = f"HTTP {e.response.status_code}"
                return
            except (httpx.HTTPError, ValueError, KeyError) as e:
                out.errors[board] = f"{type(e).__name__}: {e}"
                return
            finally:
                out.bytes += counter.bytes
                if counter.bytes:
                    out.board_bytes[board] = counter.bytes
        if jobs is None:
            out.unchanged.add(board)
        else:
            for job in jobs:
                job.board = board
            out.jobs.extend(jobs)
            if etag:
                out.etags[board] = etag

    async with _client() as client:
        await asyncio.gather(*[
            one(client, source, slug)
            for source, slugs in companies.items() if source in FETCHERS
            for slug in slugs if f"{source}:{slug}" not in skip
        ])
    for board, err in out.errors.items():
        log.warning("fetch failed for %s: %s", board, err)
    return out


async def hydrate_all(jobs: list[Job], concurrency: int = 10) -> set[str]:
    """Fill in descriptions for jobs whose source list omits them. Returns boards with a failure."""
    sem = asyncio.Semaphore(concurrency)
    failed: set[str] = set()

    async def one(client: httpx.AsyncClient, job: Job) -> None:
        async with sem:
            try:
                await HYDRATORS[job.source](client, job)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                failed.add(f"{job.source}:{job.company}")
                log.warning("couldn't fetch details for %s: %s", job.key, e)

    todo = [j for j in jobs if j.source in HYDRATORS and not j.description]
    if todo:
        async with _client() as client:
            await asyncio.gather(*[one(client, j) for j in todo])
    return failed
