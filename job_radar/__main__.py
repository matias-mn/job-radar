"""job-radar CLI.

    python -m job_radar run [--dry-run] [--full]   fetch changed boards, filter, score, alert
    python -m job_radar rehydrate         re-read LinkedIn jobs stored without a description, re-score them
    python -m job_radar check-companies   verify every board slug in config.toml
    python -m job_radar test-slack        post a sample digest to the webhook
    python -m job_radar stats             totals and estimated API spend
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path

import anthropic
import httpx
import openai
from dotenv import load_dotenv

from . import filters, notify
from .db import DB
from .models import LIMITED_INFO
from .resume import load_resume
from .scorer import RateLimited, make_scorer
from .sources import fetch_all, hydrate_all, is_duplicate, linkedin, origin_board, title_index

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("job_radar")


def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


LINKEDIN_NOTICES = {
    "email_only": "⚠️ LinkedIn is refusing to show job pages, so LinkedIn alerts are now scored from the email alone "
                  "(title, company, location). Those scores show a `?`. I'll post again when full postings are back.",
    "full": "✅ LinkedIn job pages are back: LinkedIn alerts are scored from the full posting again.",
}
NO_SLACK = "Slack isn't set up: put SLACK_WEBHOOK_URL, or SLACK_BOT_TOKEN and SLACK_CHANNEL, in .env"


class Slack:
    """A bot token (can thread replies) when SLACK_BOT_TOKEN and SLACK_CHANNEL are set, else the webhook."""

    def __init__(self):
        token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL")
        self.bot = (token, channel) if token and channel else None
        self.webhook = os.environ.get("SLACK_WEBHOOK_URL")

    def __bool__(self) -> bool:
        return bool(self.bot or self.webhook)

    def alert(self, row: dict) -> None:
        """Raises httpx.HTTPError if the alert itself didn't go out."""
        if not self.bot:
            notify.post(self.webhook, notify.build_digest([row], with_qualifications=True))
            return
        ts = notify.post_bot(*self.bot, notify.build_digest([row]))
        if quals := notify.build_qualifications(row):
            try:
                notify.post_bot(*self.bot, quals, thread_ts=ts)
            except httpx.HTTPError as e:  # the alert went out; don't resend it over this
                log.error("couldn't post qualifications for %s: %s", row["key"], e)

    def message(self, text: str) -> None:
        if self.bot:
            notify.post_bot(*self.bot, {"text": text})
        else:
            notify.post(self.webhook, {"text": text})


class Alerter:
    """Sends each new match to Slack as soon as it's scored. In dry-run, collects and prints instead."""

    def __init__(self, db: DB, cfg: dict, dry_run: bool):
        self.db, self.dry_run = db, dry_run
        self.threshold = cfg["settings"]["alert_threshold"]
        self.budget = cfg["settings"]["max_alerts_per_run"]
        self.gaming = filters.Gaming(cfg.get("gaming"))
        self.gaming_threshold = self.gaming.alert_threshold or self.threshold
        self.slack = Slack()
        self.sent = 0
        self.printed: dict[str, dict] = {}
        self.broken = False

    def flush(self) -> None:
        if self.broken or self.sent >= self.budget:
            return
        rows = self.db.pending_alerts(self.threshold, self.budget - self.sent, self.gaming, self.gaming_threshold)
        if self.dry_run:
            self.printed.update((r["key"], r) for r in rows)
            return
        if rows and not self.slack:
            log.error("%s; %d match(es) waiting. Use --dry-run to see them.", NO_SLACK, len(rows))
            self.broken = True
            return
        for row in rows:
            try:
                self.slack.alert(row)
            except httpx.HTTPError as e:
                log.error("Slack post failed, will retry next run: %s", e)
                self.broken = True
                return
            self.db.mark_alerted([row["key"]])
            self.sent += 1
            log.info("alerted: %d%s %s — %s", row["fit_score"], " 🎮" if row["gaming"] else "", row["title"], row["company"])

    def notice(self, text: str) -> bool:
        """A status message. False if it wasn't posted (dry run, no Slack, or a failed post)."""
        if self.dry_run or not self.slack:
            print(f"\n[notice, not posted] {text}")
            return False
        try:
            self.slack.message(text)
        except httpx.HTTPError as e:
            log.error("Slack notice failed, will retry next run: %s", e)
            return False
        return True


def check_linkedin_mode(db: DB, alerter: Alerter) -> None:
    """Post once when LinkedIn starts refusing job pages (alerts fall back to the email) and once when it stops."""
    stats = linkedin.page_stats
    mode = "email_only" if stats["blocked"] else "full" if stats["read"] else None  # None: no pages tried
    if mode and mode != (db.get_meta("linkedin_mode") or "full") and alerter.notice(LINKEDIN_NOTICES[mode]):
        db.set_meta("linkedin_mode", mode)


def filters_fingerprint(cfg: dict) -> str:
    """Boards answering 304 aren't re-filtered, so a config change must force a full fetch."""
    relevant = {k: cfg.get(k) for k in ("filters", "companies", "gaming")} | {"max_age_days": cfg["settings"]["max_age_days"]}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]


def cmd_run(cfg: dict, dry_run: bool, full: bool) -> int:
    s = cfg["settings"]
    db = DB(ROOT / s["db_path"])
    resume_text, resume_hash = load_resume(s["resume_path"])
    gaming = filters.Gaming(cfg.get("gaming"))
    flt = filters.Filters(cfg["filters"], gaming)

    fingerprint = filters_fingerprint(cfg)
    if full or db.get_meta("filters_fingerprint") != fingerprint:
        db.clear_etags()  # first run, --full, or config changed: re-check every board

    # Boards with huge full-description payloads (e.g. OpenAI's ~14 MB Ashby board) change
    # nearly every check, so ETags don't help; download those at most once per interval.
    skip = set() if full else {b for b in db.throttled_boards(
        int(s.get("big_board_mb", 5) * 1e6), s.get("big_board_interval_minutes", 60)
    ) if not b.startswith("email:")}  # a big batch of emails is a one-off, not a big board
    fetched = asyncio.run(fetch_all(cfg["companies"], db.etags(), skip))
    db.save_board_sizes(fetched.board_bytes)
    kept, counts = filters.apply(fetched.jobs, flt, s["max_age_days"])
    known = db.known_keys()
    new = [j for j in kept if j.key not in known]
    failed_boards = asyncio.run(hydrate_all(new))
    new = [j for j in new if j.description]
    # Hydration can reveal a location (aggregators) or a posting we already poll directly.
    polled = {f"{src}:{slug}" for src, slugs in cfg["companies"].items() for slug in slugs}
    seen = known | {j.key for j in fetched.jobs}
    all_titles = db.titles() + [(j.source, j.company, j.title) for j in fetched.jobs]
    titles, linkedin_titles = title_index(all_titles), title_index(all_titles, "linkedin")
    late_drop = [j for j in new if is_duplicate(j, polled, seen, titles, linkedin_titles) or not flt.location_ok(j)]
    for board in sorted({o[0] for j in new if j.source in ("linkedin", "indeed") and (o := origin_board(j.apply_url))} - polled):
        log.info("Email alert job found on %s, which config.toml doesn't poll yet; consider adding it", board)
    new = [j for j in new if j not in late_drop]
    db.upsert_jobs(new)
    db.upsert_jobs(late_drop, status="filtered")
    counts.update(dropped_after_details=len(late_drop))
    # Only remember a board's ETag once its new jobs are safely stored.
    db.save_etags({b: e for b, e in fetched.etags.items() if b not in failed_boards})
    db.set_meta("filters_fingerprint", fingerprint)

    batch = db.unscored_jobs(s["max_age_days"], s["max_llm_calls_per_run"])
    for job in batch:
        job.gaming = gaming(job.source, job.company)
    counts.update(boards_changed=len(fetched.etags), boards_unchanged=len(fetched.unchanged), boards_throttled=len(skip),
                  boards_failed=len(fetched.errors), new=len(new), scoring_now=len(batch))
    counts["mb_downloaded"] = round(fetched.bytes / 1e6, 1)
    log.info("pipeline: %s", {k: v for k, v in counts.items() if v})

    alerter = Alerter(db, cfg, dry_run)
    check_linkedin_mode(db, alerter)
    alerter.flush()  # anything left over from a previous run (e.g. a failed Slack post)
    if batch:
        try:
            scorer = make_scorer(s["provider"], resume_text, s["model"], s["effort"])
        except (openai.OpenAIError, anthropic.AnthropicError) as e:
            log.error("can't create the %s client (is its API key in .env?): %s", s["provider"], e)
            return 1
        tally = Counter()
        for i, job in enumerate(batch, 1):
            try:
                result = scorer.score(job)
            except RateLimited as e:
                log.error("rate limited, stopping scoring for this run: %s", e)
                break
            if result is None:
                tally["errors"] += 1
                continue
            cap_limited(result.fit, job, s)
            db.save_score(job.key, result.fit, refused=result.refused, model=result.model,
                          resume_hash=resume_hash, usage=result.usage)
            u = result.usage
            tally.update(input=u.input, output=u.output, cache_read=u.cache_read, cache_write=u.cache_write)
            if result.refused:
                tally["refused"] += 1
                continue
            log.info("[%d/%d] %3d  %s — %s", i, len(batch), result.fit.fit_score, job.title, job.company)
            alerter.flush()
        log.info("scoring usage: %s", dict(tally))

    if dry_run:
        print("\n" + notify.format_text(list(alerter.printed.values())))
    return 1 if alerter.broken else 0


def cap_limited(fit, job, s: dict) -> None:
    """A title-only guess shouldn't outrank a job whose full posting was read."""
    if fit and job.description.startswith(LIMITED_INFO):
        fit.fit_score = min(fit.fit_score, s.get("limited_info_max_score", 75))


def cmd_rehydrate(cfg: dict) -> int:
    """One-off backfill for LinkedIn jobs stored before LinkedIn's job pages were read."""
    s = cfg["settings"]
    db = DB(ROOT / s["db_path"])
    rows = db.limited_jobs("linkedin")
    if not rows:
        print("No LinkedIn jobs without a description.")
        return 0
    jobs = [job for job, _ in rows]
    print(f"Looking up {len(jobs)} LinkedIn job(s), a few seconds apart...")
    for job in jobs:
        job.description = ""
    asyncio.run(hydrate_all(jobs))
    found = [(job, old) for job, old in rows if not job.description.startswith(LIMITED_INFO)]
    print(f"Found a description for {len(found)} of {len(jobs)}.")
    if not found:
        return 0
    db.upsert_jobs([job for job, _ in found])

    resume_text, resume_hash = load_resume(s["resume_path"])
    try:
        scorer = make_scorer(s["provider"], resume_text, s["model"], s["effort"])
    except (openai.OpenAIError, anthropic.AnthropicError) as e:
        log.error("can't create the %s client (is its API key in .env?): %s", s["provider"], e)
        return 1
    gaming = filters.Gaming(cfg.get("gaming"))
    report = []
    for job, old in found:
        job.gaming = gaming(job.source, job.company)
        try:
            result = scorer.score(job)
        except RateLimited as e:
            log.error("rate limited; run rehydrate again later to finish: %s", e)
            break
        if result is None or result.refused:
            continue  # keeps its old score
        db.save_score(job.key, result.fit, refused=False, model=result.model, resume_hash=resume_hash, usage=result.usage)
        report.append((old, result.fit, job))

    print(f"\nRe-scored {len(report)} job(s). Old score (no description) -> new score:")
    for old, fit, job in sorted(report, key=lambda r: r[1].fit_score, reverse=True):
        before = f"{old['fit_score']:>3} {old['seniority_fit'] or '':<10}" if old["fit_score"] is not None else "  -" + " " * 11
        print(f"  {before} -> {fit.fit_score:>3} {fit.seniority_fit:<10} {'alerted ' if old['alerted'] else '        '}"
              f"{job.title} — {job.company}")
    print("\nJobs not alerted before that now clear the threshold will be alerted on the next run.")
    return 0


def cmd_check_companies(cfg: dict) -> int:
    f = filters.Filters(cfg["filters"], filters.Gaming(cfg.get("gaming")))
    fetched = asyncio.run(fetch_all(cfg["companies"]))
    errors = fetched.errors
    total, relevant = Counter(), Counter()
    for j in fetched.jobs:
        total[j.board] += 1
        relevant[j.board] += f.title_ok(j.title) and f.location_ok(j)
    for source, slugs in cfg["companies"].items():
        for slug in slugs:
            if err := errors.get(f"{source}:{slug}"):
                print(f"  FAIL  {source:<10} {slug:<20} {err}")
                continue
            n = total[f"{source}:{slug}"]
            flag = "ok" if n else "EMPTY"
            print(f"  {flag:<5} {source:<10} {slug:<20} {n:>4} open, {relevant[f'{source}:{slug}']:>3} pass title+location")
    print(f"\n{len(errors)} board(s) failed." if errors else "\nAll boards OK.")
    return 1 if errors else 0


def cmd_test_slack() -> int:
    slack = Slack()
    if not slack:
        print(NO_SLACK)
        return 1
    sample = {
        "key": "test", "title": "Detection & Response Engineer (New Grad)", "company": "example",
        "location": "San Jose, CA", "remote": 0, "url": "https://example.com", "comp": "$120K - $150K",
        "fit_score": 88, "why": "This is a test message from job-radar.",
        "matched_skills": ["Python automation", "threat intel"], "missing_skills": ["Splunk SPL"], "red_flags": [],
        "min_qualifications": ["Bachelor's degree or equivalent practical experience",
                               "1 year of experience with Python"],
        "preferred_qualifications": ["Experience writing detections in a SIEM"],
    }
    try:
        slack.alert(sample)
    except httpx.HTTPError as e:
        print(f"Slack refused the test message: {e}")  # e.g. channel_not_found, not_in_channel, missing_scope
        return 1
    print("Posted a test message" + (" with a threaded reply." if slack.bot else " (webhook: qualifications inline)."))
    return 0


def cmd_stats(cfg: dict) -> int:
    s = cfg["settings"]
    st = DB(ROOT / s["db_path"]).stats(s["alert_threshold"])
    p = cfg.get("prices", {}).get(s["model"])
    print(f"jobs stored:   {st['jobs']}")
    print(f"jobs scored:   {st['scored']}  (refused: {st['refused'] or 0}, >= {s['alert_threshold']}: {st['strong'] or 0})")
    print(f"alerts sent:   {st['alerted']}")
    print(f"tokens:        in {st['input_tokens']:,}  out {st['output_tokens']:,}  "
          f"cache read {st['cache_read_tokens']:,}  cache write {st['cache_write_tokens']:,}")
    if p:
        cost = (st["input_tokens"] * p["input"] + st["output_tokens"] * p["output"]
                + st["cache_read_tokens"] * p["cache_read"] + st["cache_write_tokens"] * p["cache_write"]) / 1e6
        print(f"est. spend:    ${cost:.2f}  (at {s['model']} prices from config.toml)")
    else:
        print(f"est. spend:    add [prices.\"{s['model']}\"] to config.toml to estimate")
    return 0


def main() -> int:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("httpx", "httpx2"):  # our fetchers use httpx; the LLM SDKs use httpx2
        logging.getLogger(noisy).setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="job_radar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="fetch, filter, score, alert")
    run.add_argument("--dry-run", action="store_true", help="print matches instead of posting to Slack")
    run.add_argument("--full", action="store_true", help="ignore ETags and re-check every board")
    sub.add_parser("rehydrate", help="re-read LinkedIn jobs stored without a description and re-score them")
    sub.add_parser("check-companies", help="verify board slugs in config.toml")
    sub.add_parser("test-slack", help="post a sample message to Slack")
    sub.add_parser("stats", help="totals and estimated spend")
    args = parser.parse_args()

    cfg = load_config()
    if args.cmd == "run":
        return cmd_run(cfg, args.dry_run, args.full)
    if args.cmd == "rehydrate":
        return cmd_rehydrate(cfg)
    if args.cmd == "check-companies":
        return cmd_check_companies(cfg)
    if args.cmd == "test-slack":
        return cmd_test_slack()
    return cmd_stats(cfg)


if __name__ == "__main__":
    sys.exit(main())
