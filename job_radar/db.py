import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import FitScore, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    source TEXT, company TEXT, ext_id TEXT, title TEXT, location TEXT,
    remote INTEGER, url TEXT, posted_at TEXT, description TEXT, comp TEXT,
    first_seen TEXT,
    status TEXT NOT NULL DEFAULT 'new'  -- for the v1 UI: new/saved/applied/dismissed
);
CREATE TABLE IF NOT EXISTS scores (
    key TEXT PRIMARY KEY REFERENCES jobs(key),
    fit_score INTEGER, seniority_fit TEXT, matched_skills TEXT, missing_skills TEXT,
    red_flags TEXT, shift_or_oncall INTEGER, why TEXT,
    refused INTEGER NOT NULL DEFAULT 0,
    model TEXT, resume_hash TEXT, scored_at TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY REFERENCES jobs(key),
    alerted_at TEXT
);
CREATE TABLE IF NOT EXISTS boards (
    board TEXT PRIMARY KEY,  -- 'source:slug'
    etag TEXT,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(boards)")}
        for col, kind in (("bytes", "INTEGER"), ("fetched_at", "TEXT")):  # added in v0.3
            if col not in columns:
                self.conn.execute(f"ALTER TABLE boards ADD COLUMN {col} {kind}")

    def upsert_jobs(self, jobs: list[Job], status: str = "new") -> None:
        """status='filtered' records jobs dropped after hydration so they aren't fetched again."""
        self.conn.executemany(
            """INSERT INTO jobs (key, source, company, ext_id, title, location, remote, url,
                                 posted_at, description, comp, first_seen, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET title=excluded.title, location=excluded.location,
                 url=excluded.url, comp=excluded.comp,
                 description=COALESCE(NULLIF(excluded.description, ''), jobs.description)""",
            [
                (j.key, j.source, j.company, j.ext_id, j.title, j.location, int(j.remote), j.url,
                 j.posted_at.astimezone(timezone.utc).isoformat() if j.posted_at else None, j.description, j.comp,
                 _now(), status)
                for j in jobs
            ],
        )
        self.conn.commit()

    def titles(self) -> list[tuple[str, str, str]]:
        return [(r["source"], r["company"], r["title"]) for r in self.conn.execute("SELECT source, company, title FROM jobs")]

    def known_keys(self) -> set[str]:
        return {r["key"] for r in self.conn.execute("SELECT key FROM jobs")}

    def unscored_jobs(self, max_age_days: int, limit: int) -> list[Job]:
        """Stored jobs still waiting for a score, newest first. Undated jobs count as new."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM jobs
               WHERE key NOT IN (SELECT key FROM scores) AND description != '' AND status != 'filtered'
                 AND (posted_at IS NULL OR posted_at >= ?)
               ORDER BY posted_at IS NULL, posted_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [
            Job(r["source"], r["company"], r["ext_id"], r["title"], r["location"], bool(r["remote"]), r["url"],
                datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None, r["description"], r["comp"])
            for r in rows
        ]

    def etags(self) -> dict[str, str]:
        return {r["board"]: r["etag"] for r in self.conn.execute("SELECT board, etag FROM boards") if r["etag"]}

    def save_etags(self, etags: dict[str, str]) -> None:
        self.conn.executemany(
            """INSERT INTO boards (board, etag, checked_at) VALUES (?, ?, ?)
               ON CONFLICT(board) DO UPDATE SET etag=excluded.etag, checked_at=excluded.checked_at""",
            [(b, e, _now()) for b, e in etags.items()],
        )
        self.conn.commit()

    def clear_etags(self) -> None:
        self.conn.execute("UPDATE boards SET etag = NULL")
        self.conn.commit()

    def save_board_sizes(self, sizes: dict[str, int]) -> None:
        self.conn.executemany(
            """INSERT INTO boards (board, bytes, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(board) DO UPDATE SET bytes=excluded.bytes, fetched_at=excluded.fetched_at""",
            [(b, n, _now()) for b, n in sizes.items()],
        )
        self.conn.commit()

    def throttled_boards(self, min_bytes: int, minutes: int) -> set[str]:
        """Big boards downloaded within the last `minutes`: skip them this run."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT board FROM boards WHERE bytes >= ? AND fetched_at >= ?", (min_bytes, cutoff)
        )
        return {r["board"] for r in rows}

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.conn.commit()

    def scored_keys(self) -> set[str]:
        return {r["key"] for r in self.conn.execute("SELECT key FROM scores")}

    def save_score(self, key: str, fit: FitScore | None, *, refused: bool, model: str, resume_hash: str, usage) -> None:
        f = fit.model_dump() if fit else {}
        self.conn.execute(
            """INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, f.get("fit_score"), f.get("seniority_fit"),
                json.dumps(f.get("matched_skills", [])), json.dumps(f.get("missing_skills", [])),
                json.dumps(f.get("red_flags", [])), int(f.get("shift_or_oncall", False)), f.get("why"),
                int(refused), model, resume_hash, _now(),
                usage.input, usage.output, usage.cache_read, usage.cache_write,
            ),
        )
        self.conn.commit()

    def pending_alerts(self, threshold: int, limit: int, is_gaming=None, gaming_threshold: int | None = None) -> list[dict]:
        """Unalerted matches. Jobs where is_gaming(source, company) use `gaming_threshold` and sort first."""
        floor = min(threshold, gaming_threshold) if is_gaming and gaming_threshold is not None else threshold
        rows = self.conn.execute(
            """SELECT j.*, s.fit_score, s.seniority_fit, s.matched_skills, s.missing_skills,
                      s.red_flags, s.shift_or_oncall, s.why
               FROM scores s JOIN jobs j USING (key)
               WHERE s.refused = 0 AND s.fit_score >= ? AND s.seniority_fit != 'too_senior'
                 AND j.status != 'dismissed'
                 AND key NOT IN (SELECT key FROM alerts)
               ORDER BY s.fit_score DESC, j.posted_at DESC""",
            (floor,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["gaming"] = bool(is_gaming and is_gaming(d["source"], d["company"]))
            if d["fit_score"] < (gaming_threshold if d["gaming"] else threshold):
                continue
            for field in ("matched_skills", "missing_skills", "red_flags"):
                d[field] = json.loads(d[field] or "[]")
            out.append(d)
        out.sort(key=lambda d: (d["gaming"], d["fit_score"]), reverse=True)
        return out[:limit]

    def mark_alerted(self, keys: list[str]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO alerts VALUES (?, ?)", [(k, _now()) for k in keys])
        self.conn.commit()

    def stats(self, threshold: int) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) AS scored, SUM(refused) AS refused,
                      SUM(fit_score >= ?) AS strong,
                      COALESCE(SUM(input_tokens),0) AS input_tokens,
                      COALESCE(SUM(output_tokens),0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                      COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens
               FROM scores""",
            (threshold,),
        ).fetchone()
        d = dict(row)
        d["jobs"] = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        d["alerted"] = self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        return d
