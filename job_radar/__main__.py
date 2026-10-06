"""job-radar CLI.

    python -m job_radar run [--dry-run] [--full]   fetch changed boards, filter, score, alert
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
from .resume import load_resume
from .scorer import RateLimited, make_scorer
from .sources import fetch_all, hydrate_all, is_duplicate, title_index

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("job_radar")


def load_config() -> dict:
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


class Alerter:
    """Sends each new match to Slack as soon as it's scored. In dry-run, collects and prints instead."""

    def __init__(self, db: DB, cfg: dict, dry_run: bool):
        self.db, self.dry_run = db, dry_run
        self.threshold = cfg["settings"]["alert_threshold"]
        self.budget = cfg["settings"]["max_alerts_per_run"]
        self.gaming = filters.Gaming(cfg.get("gaming"))
        self.gaming_threshold = self.gaming.alert_threshold or self.threshold
        self.webhook = os.environ.get("SLACK_WEBHOOK_URL")
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
        if rows and not self.webhook:
            log.error("SLACK_WEBHOOK_URL is not set; %d match(es) waiting. Use --dry-run to see them.", len(rows))
            self.broken = True
            return
        for row in rows:
            try:
                notify.post(self.webhook, notify.build_digest([row]))
            except httpx.HTTPError as e:
                log.error("Slack post failed, will retry next run: %s", e)
                self.broken = True
                return
            self.db.mark_alerted([row["key"]])
            self.sent += 1
            log.info("alerted: %d%s %s — %s", row["fit_score"], " 🎮" if row["gaming"] else "", row["title"], row["company"])


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
    skip = set() if full else db.throttled_boards(
        int(s.get("big_board_mb", 5) * 1e6), s.get("big_board_interval_minutes", 60)
    )
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
    titles = title_index(db.titles() + [(j.source, j.company, j.title) for j in fetched.jobs])
    late_drop = [j for j in new if is_duplicate(j, polled, seen, titles) or not flt.location_ok(j)]
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
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("SLACK_WEBHOOK_URL is not set in .env")
        return 1
    sample = {
        "key": "test", "title": "Detection & Response Engineer (New Grad)", "company": "example",
        "location": "San Jose, CA", "remote": 0, "url": "https://example.com", "comp": "$120K - $150K",
        "fit_score": 88, "why": "This is a test message from job-radar.",
        "matched_skills": ["Python automation", "threat intel"], "missing_skills": ["Splunk SPL"], "red_flags": [],
    }
    notify.post(webhook, notify.build_digest([sample]))
    print("Posted a test message.")
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
    sub.add_parser("check-companies", help="verify board slugs in config.toml")
    sub.add_parser("test-slack", help="post a sample message to Slack")
    sub.add_parser("stats", help="totals and estimated spend")
    args = parser.parse_args()

    cfg = load_config()
    if args.cmd == "run":
        return cmd_run(cfg, args.dry_run, args.full)
    if args.cmd == "check-companies":
        return cmd_check_companies(cfg)
    if args.cmd == "test-slack":
        return cmd_test_slack()
    return cmd_stats(cfg)


if __name__ == "__main__":
    sys.exit(main())
