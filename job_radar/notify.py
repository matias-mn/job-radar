"""Slack alerts: through a bot token when SLACK_BOT_TOKEN and SLACK_CHANNEL are set, else an incoming webhook.

A bot can reply in a thread, so the posting's qualifications go in a reply under the alert.
A webhook can't, so they're appended to the alert itself.
"""

from datetime import datetime

import httpx

from .models import LIMITED_INFO

EMAIL_SOURCES = {"linkedin": "LinkedIn", "indeed": "Indeed"}


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge(score: int) -> str:
    return "🟢" if score >= 85 else "🟡" if score >= 70 else "⚪"


def _limited(r: dict) -> bool:
    """Scored without the posting's description (e.g. from an alert email alone)."""
    return (r.get("description") or "").startswith(LIMITED_INFO)


def _score(r: dict) -> str:
    """'88', or '88?' when the score is a guess from limited information."""
    return f"{r['fit_score']}{'?' if _limited(r) else ''}"


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
    if r.get("source") in EMAIL_SOURCES:
        tags.append(f"📧 {EMAIL_SOURCES[r['source']]} alert")
    if _limited(r):
        tags.append("limited info, score is a guess")
    return " · ".join(tags)


def _qual_sections(r: dict) -> list[str]:
    out = []
    for field, heading in (("min_qualifications", "Minimum qualifications"),
                           ("preferred_qualifications", "Preferred qualifications")):
        if items := r.get(field):
            out.append(f"*{heading}*\n" + "\n".join(f"• {_esc(q)}" for q in items))
    return out


def build_qualifications(r: dict) -> dict | None:
    """Thread reply listing the posting's minimum and preferred qualifications. None if it lists neither."""
    sections = _qual_sections(r)
    if not sections:
        return None
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}} for text in sections]
    return {"text": f"Qualifications: {r['title']}", "blocks": blocks}


def build_digest(rows: list[dict], with_qualifications: bool = False) -> dict:
    """with_qualifications: append them to each job (webhooks can't post a thread reply)."""
    n = len(rows)
    if n == 1:
        header = "🎮 New gaming job match" if rows[0].get("gaming") else "🎯 New job match"
    else:
        header = f"🎯 {n} new job matches"
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": header}}]
    for r in rows:
        where = _esc(r["location"] or "location n/a") + (" · remote" if r["remote"] else "")
        lines = [
            f"{_badge(r['fit_score'])} *{_score(r)}*  <{r['url']}|{_esc(r['title'])}>" + ("  🎮" if r.get("gaming") else ""),
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
        if with_qualifications:
            blocks += [{"type": "section", "text": {"type": "mrkdwn", "text": t[:3000]}} for t in _qual_sections(r)]
        blocks.append({"type": "divider"})
    first = rows[0]
    text = f"{_score(first)} · {first['title']} — {first['company']}" if n == 1 else f"{n} new job matches"
    return {"text": text, "blocks": blocks}  # `text` is the push-notification preview


def format_text(rows: list[dict]) -> str:
    """Terminal version of the digest, for --dry-run."""
    if not rows:
        return "No new matches above the threshold."
    out = []
    for r in rows:
        out.append(f"[{_score(r):>4}]{' 🎮' if r.get('gaming') else ''} {r['title']} — {r['company']} — {r['location']}")
        out.append(f"      {_tags(r)}")
        out.append(f"      {r['url']}")
        out.append(f"      {r['why']}")
        if r["matched_skills"]:
            out.append(f"      + {', '.join(r['matched_skills'])}")
        if r["missing_skills"]:
            out.append(f"      - {', '.join(r['missing_skills'])}")
        if r["red_flags"]:
            out.append(f"      ! {', '.join(r['red_flags'])}")
        for label, field in (("min", "min_qualifications"), ("preferred", "preferred_qualifications")):
            for q in r.get(field) or []:
                out.append(f"      {label}: {q}")
    return "\n".join(out)


def post(webhook_url: str, payload: dict) -> None:
    r = httpx.post(webhook_url, json=payload, timeout=15)
    r.raise_for_status()


def post_bot(token: str, channel: str, payload: dict, thread_ts: str | None = None) -> str:
    """chat.postMessage; returns the message's ts, for threading replies under it."""
    body = {"channel": channel, **payload} | ({"thread_ts": thread_ts} if thread_ts else {})
    r = httpx.post("https://slack.com/api/chat.postMessage", json=body, timeout=15,
                   headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):  # Slack reports errors in a 200 response
        raise httpx.HTTPError(f"Slack chat.postMessage: {data.get('error')}")
    return data["ts"]
