"""Find an email-alert job (LinkedIn, Indeed) on the company's own job board, for its full description.

Alert emails only give title, company and location. This guesses the company's
Greenhouse, Lever and Ashby board slugs from its name ("Duck Creek Technologies" ->
duckcreektechnologies, duck-creek-technologies, duckcreek) and looks for the same title.
A match gives a full description and an application link on the company's board,
which also lets is_duplicate() drop jobs from boards we already poll. No match: a
LinkedIn job takes its description from LinkedIn's public job page instead
(linkedin.fetch_description); Indeed's pages block scripts. Still nothing: the job is
scored on what the email said, flagged as limited.
"""

import asyncio
import logging
import re
import weakref

import httpx

from ..models import LIMITED_INFO, Job
from . import Fetched, _norm, ashby, greenhouse, lever, linkedin

log = logging.getLogger(__name__)

ATS = {"greenhouse": greenhouse.fetch, "lever": lever.fetch, "ashby": ashby.fetch}
# Canonical links, so sources._ORIGIN_RES recognizes the board.
CANONICAL = {
    "greenhouse": "https://boards.greenhouse.io/{slug}/jobs/{id}",
    "lever": "https://jobs.lever.co/{slug}/{id}",
    "ashby": "https://jobs.ashbyhq.com/{slug}/{id}",
}
SUFFIX_RE = re.compile(r"\b(?:inc|llc|ltd|corp|corporation|co|company|technologies|technology|group|labs?)\b\.?", re.I)

# Per client (one per run): (ats, slug) -> task loading that board, so concurrent jobs
# from the same company share one download.
_boards: "weakref.WeakKeyDictionary[httpx.AsyncClient, dict]" = weakref.WeakKeyDictionary()


def slug_guesses(company: str) -> list[str]:
    name = re.sub(r"\(.*?\)", " ", company)  # "Amazon Web Services (AWS)", "Bitdeer (NASDAQ: BTDR)"
    words = re.findall(r"[a-z0-9]+", name.lower())
    short = re.findall(r"[a-z0-9]+", SUFFIX_RE.sub(" ", name).lower())
    guesses = ["".join(words), "-".join(words), "".join(short)]
    if short and len(short[0]) >= 4:
        guesses.append(short[0])
    return [g for g in dict.fromkeys(guesses) if g]


async def _load(client: httpx.AsyncClient, ats: str, slug: str) -> list[Job]:
    try:
        jobs, _ = await ATS[ats](client, slug, None, Fetched())
    except (httpx.HTTPError, ValueError, KeyError):
        return []  # usually a 404: no such board
    return jobs or []


def _board(client: httpx.AsyncClient, ats: str, slug: str) -> asyncio.Task:
    cache = _boards.setdefault(client, {})
    if (ats, slug) not in cache:
        cache[(ats, slug)] = asyncio.ensure_future(_load(client, ats, slug))
    return cache[(ats, slug)]


def _pick(job: Job, candidates: list[Job]) -> Job:
    """Same title can be posted once per office; prefer the one in the email's city."""
    city = (job.location or "").split(",")[0].strip().lower()
    return next((c for c in candidates if city and city in (c.location or "").lower()), candidates[0])


async def find(client: httpx.AsyncClient, job: Job) -> Job | None:
    title = _norm(job.title)
    for slug in slug_guesses(job.company):
        for ats in ATS:
            matches = [j for j in await _board(client, ats, slug) if _norm(j.title) == title]
            if matches:
                return _pick(job, matches)
    return None


async def hydrate(client: httpx.AsyncClient, job: Job) -> None:
    """Never raises: a job we can't find is still worth scoring from the email alone."""
    try:
        found = await find(client, job)
        if found and not found.description:
            await greenhouse.hydrate(client, found)  # Greenhouse lists omit descriptions
    except httpx.HTTPError as e:
        log.info("couldn't look up %s at %s: %s", job.title, job.company, e)
        found = None
    if found and found.description:
        job.description = found.description
        job.apply_url = CANONICAL[found.source].format(slug=found.company, id=found.ext_id)
        job.url = found.url or job.apply_url  # apply on the company's site, not through LinkedIn
        job.comp = job.comp or found.comp
        job.posted_at = found.posted_at or job.posted_at
        return
    if job.source == "linkedin":
        try:
            job.description = await linkedin.fetch_description(client, job.ext_id)
        except httpx.HTTPError as e:
            log.info("couldn't read LinkedIn's page for %s: %s", job.key, e)
    if not job.description:
        job.description = LIMITED_INFO + (f"\n\nSnippet from the alert: {job.teaser}" if job.teaser else "")
