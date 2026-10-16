"""Cheap, no-LLM filters. Everything tunable lives in config.toml [filters]."""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .models import Job


def _pattern(terms: list[str]) -> re.Pattern:
    # Whole-word match; lookarounds instead of \b so terms like "d&r" and "u.s." work.
    alts = "|".join(re.escape(t.lower()) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])")


US_STATES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho "
    "illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota "
    "mississippi missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah "
    "vermont virginia washington wisconsin wyoming"
).split() + ["new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
             "rhode island", "south carolina", "south dakota", "west virginia"]
# "Seattle, WA" style. Case-sensitive on purpose so words like "in" or "or" don't match.
STATE_CODE_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
    r"NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)


class Gaming:
    """Config [gaming]: which jobs count as gaming, and how they're treated."""

    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.companies = set(cfg.get("companies", []))
        self._slugs = {re.sub(r"[^a-z0-9]", "", c.lower()) for c in self.companies}
        self.sources = set(cfg.get("sources", []))
        self.alert_threshold = cfg.get("alert_threshold")
        self.any_us_location = cfg.get("any_us_location", False)

    def __call__(self, source: str, company: str) -> bool:
        if company in self.companies or source in self.sources:
            return True
        # Email alerts give a company name, not a board slug: "Riot Games" ~ riotgames,
        # "Sony Interactive Entertainment" ~ sonyinteractiveentertainmentglobal.
        name = re.sub(r"[^a-z0-9]", "", company.lower())
        return len(name) >= 4 and any(slug.startswith(name) for slug in self._slugs)


class Filters:
    def __init__(self, cfg: dict, gaming: Gaming | None = None):
        self.include = _pattern(cfg["title_include"])
        self.exclude = _pattern(cfg["title_exclude"])
        self.bay_area = _pattern(cfg["bay_area"])
        self.us = _pattern(cfg["us_markers"])
        self.non_us = _pattern(cfg["non_us_markers"])
        self.states = _pattern(US_STATES)
        self.gaming = gaming or Gaming(None)

    def title_ok(self, title: str) -> bool:
        t = title.lower()
        return bool(self.include.search(t)) and not self.exclude.search(t)

    def in_us(self, location: str) -> bool:
        loc = location.lower()
        if self.non_us.search(loc) and not self.us.search(loc):
            return False  # e.g. "Bengaluru, IN" isn't Indiana
        return bool(self.us.search(loc) or self.states.search(loc) or STATE_CODE_RE.search(location))

    def location_ok(self, job: Job) -> bool:
        if job.location is None:
            return True  # unknown until hydrated; checked again afterwards
        loc = job.location.lower()
        if self.bay_area.search(loc):
            return True
        if job.remote and not (self.non_us.search(loc) and not self.us.search(loc)):
            return True
        # Dream-industry exception: gaming jobs anywhere in the US.
        return self.gaming.any_us_location and self.gaming(job.source, job.company) and self.in_us(job.location)


def is_recent(job: Job, max_age_days: int, now: datetime) -> bool:
    if job.posted_at is None:
        return True  # can't tell; let the scorer see it
    posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
    return now - posted <= timedelta(days=max_age_days)


def apply(
    jobs: list[Job], filters: Filters, max_age_days: int, now: datetime | None = None
) -> tuple[list[Job], Counter]:
    """Returns (kept jobs newest first, counts of why jobs were dropped)."""
    now = now or datetime.now(timezone.utc)
    counts: Counter = Counter(fetched=len(jobs))
    kept: dict[str, Job] = {}
    for job in jobs:
        if not filters.title_ok(job.title):
            counts["dropped_title"] += 1
        elif not filters.location_ok(job):
            counts["dropped_location"] += 1
        elif not is_recent(job, max_age_days, now):
            counts["dropped_age"] += 1
        else:
            kept[job.key] = job
    counts["kept"] = len(kept)
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(kept.values(), key=lambda j: j.posted_at or oldest, reverse=True)
    return ordered, counts
