"""Slack digest via an incoming webhook."""

from datetime import datetime

import httpx


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge(score: int) -> str:
    return "🟢" if score >= 85 else "🟡" if score >= 70 else "⚪"


def _posted(posted_at: str | None) -> str:
    if not posted_at:
        return "posted date unknown"
    d = datetime.fromisoformat(posted_at)
    return f"posted {d:%b} {d.day}, {d.year}"


def _tags(r: dict) -> str:
    """Category, listing date, and a shift/on-call marker."""
    tags = [r.get("category") or "uncategorized", _posted(r.get("posted_at"))]
    if r.get("shift_or_oncall"):
        tags.append("shift/on-call")
    return " · ".join(tags)


def build_digest(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 1:
        header = "🎮 New gaming job match" if rows[0].get("gaming") else "🎯 New job match"
    else:
        header = f"🎯 {n} new job matches"
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": header}}]
    for r in rows:
        where = _esc(r["location"] or "location n/a") + (" · remote" if r["remote"] else "")
        lines = [
            f"{_badge(r['fit_score'])} *{r['fit_score']}*  <{r['url']}|{_esc(r['title'])}>" + ("  🎮" if r.get("gaming") else ""),
            f"*{_esc(r['company'])}* · {where}" + (f" · {_esc(r['comp'])}" if r.get("comp") else ""),
            f"🏷️ {_esc(_tags(r))}",
            _esc(r["why"] or ""),
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}})
        details = []
        if r["matched_skills"]:
            details.append("✅ " + _esc(", ".join(r["matched_skills"][:4])))
        if r["missing_skills"]:
            details.append("⚠️ " + _esc(", ".join(r["missing_skills"][:4])))
        if r["red_flags"]:
            details.append("🚩 " + _esc(", ".join(r["red_flags"][:3])))
        if details:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(details)[:3000]}]})
        blocks.append({"type": "divider"})
    first = rows[0]
    text = f"{first['fit_score']} · {first['title']} — {first['company']}" if n == 1 else f"{n} new job matches"
    return {"text": text, "blocks": blocks}  # `text` is the push-notification preview


def format_text(rows: list[dict]) -> str:
    """Terminal version of the digest, for --dry-run."""
    if not rows:
        return "No new matches above the threshold."
    out = []
    for r in rows:
        out.append(f"[{r['fit_score']:>3}]{' 🎮' if r.get('gaming') else ''} {r['title']} — {r['company']} — {r['location']}")
        out.append(f"      {_tags(r)}")
        out.append(f"      {r['url']}")
        out.append(f"      {r['why']}")
        if r["matched_skills"]:
            out.append(f"      + {', '.join(r['matched_skills'])}")
        if r["missing_skills"]:
            out.append(f"      - {', '.join(r['missing_skills'])}")
        if r["red_flags"]:
            out.append(f"      ! {', '.join(r['red_flags'])}")
    return "\n".join(out)


def post(webhook_url: str, payload: dict) -> None:
    r = httpx.post(webhook_url, json=payload, timeout=15)
    r.raise_for_status()
