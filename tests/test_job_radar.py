import asyncio
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path
import anthropic
import httpx
import httpx2
import openai
import pytest

from job_radar import filters, notify
from job_radar.db import DB
from job_radar.models import FitScore, Job
from job_radar.resume import load_resume
from job_radar.scorer import AnthropicScorer, OpenAIScorer, Usage, make_scorer
from job_radar.sources import (Fetched, ashby, fetch_all, gamejobs, greenhouse, html_to_text, hydrate_all,
                               is_duplicate, lever, phenom, title_index, workday)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def make_job(title="Security Engineer", location="San Jose, CA", remote=False, posted=NOW, ext_id="1"):
    return Job("greenhouse", "acme", ext_id, title, location, remote, "https://x", posted, "desc")


@pytest.fixture(scope="module")
def flt():
    with open(ROOT / "config.example.toml", "rb") as f:
        return filters.Filters(tomllib.load(f)["filters"])


# ---- sources ----

def test_greenhouse_normalize():
    job = greenhouse.normalize("acme", fixture("greenhouse.json")["jobs"][0])
    assert job.key == "greenhouse:acme:111"
    assert job.title == "Associate SOC Analyst"
    assert job.remote and job.location == "Remote"
    assert job.posted_at.date().isoformat() == "2026-09-18"  # first_published, not updated_at
    assert "hunt threats" in job.description and "<" not in job.description
    assert "- Python" in job.description


def test_lever_normalize():
    job = lever.normalize("acme", fixture("lever.json")[0])
    assert job.location == "San Jose, California (US)"
    assert not job.remote
    assert job.posted_at.tzinfo is not None
    assert "Requirements" in job.description and "OSINT" in job.description
    assert job.comp == "USD 110,000-140,000 per-year-salary"


def test_ashby_normalize_and_skips_unlisted():
    data = fixture("ashby.json")

    def handler(request):
        return httpx.Response(200, json=data)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ashby.fetch(client, "acme", None, Fetched())

    jobs, _ = asyncio.run(run())
    assert len(jobs) == 1
    job = jobs[0]
    assert job.remote
    assert job.location == "Remote; United States (USA)"
    assert job.comp == "$130K – $160K"


def test_html_to_text_unescapes_greenhouse_double_escaping():
    assert html_to_text("&lt;p&gt;A &amp;amp; B&lt;/p&gt;") == "A & B"


# ---- filters ----

@pytest.mark.parametrize("title,keep", [
    ("Security Engineer, New Grad", True),
    ("Detection & Response Engineer", True),
    ("Associate SOC Analyst", True),
    ("Threat Intelligence Analyst", True),
    ("Security Engineer II", True),          # mid-level; left for Claude to judge
    ("Senior Detection Engineer", False),
    ("Sr. Security Engineer", False),
    ("Staff Security Engineer", False),
    ("Security Engineer III", False),
    ("Security Engineering Manager", False),
    ("Security Engineering Intern", False),
    ("Security Sales Engineer", False),
    ("Security GRC Analyst", True),           # GRC and SOC roles are in scope since 2026-10-07
    ("SOC Tier 1 Analyst", True),
    ("MDR Analyst", True),
    ("Cybersecurity Analyst in Santa Clara, CA", True),
    ("Social Media Manager", False),          # "soc" must be a whole word
    ("Software Engineer, Payments", False),
])
def test_title_filter(flt, title, keep):
    assert flt.title_ok(title) is keep


@pytest.mark.parametrize("location,remote,keep", [
    ("San Jose, CA", False, True),
    ("New York, NY; San Francisco, CA", False, True),
    ("Remote", True, True),
    ("US - Remote", True, True),
    ("Remote - Texas", True, True),
    ("Remote (EMEA)", True, False),
    ("Remote - UK", True, False),
    ("Remote, Canada; Remote, United States", True, True),
    ("Austin, TX", False, False),
    ("Tel Aviv", False, False),
])
def test_location_filter(flt, location, remote, keep):
    assert flt.location_ok(make_job(location=location, remote=remote)) is keep


def test_apply_drops_old_and_sorts_newest_first(flt):
    jobs = [
        make_job(ext_id="old", posted=datetime(2026, 9, 1, tzinfo=timezone.utc)),
        make_job(ext_id="mid", posted=datetime(2026, 9, 28, tzinfo=timezone.utc)),
        make_job(ext_id="new", posted=datetime(2026, 10, 4, tzinfo=timezone.utc)),
        make_job(ext_id="undated", posted=None),
    ]
    kept, counts = filters.apply(jobs, flt, max_age_days=14, now=NOW)
    assert [j.ext_id for j in kept] == ["new", "mid", "undated"]
    assert counts["dropped_age"] == 1


# ---- resume ----

RESUME = Path("~/path/to/resume.docx").expanduser()


@pytest.mark.skipif(not RESUME.exists(), reason="resume not on this machine")
def test_resume_strips_contact_info():
    text, digest = load_resume(RESUME)
    assert text.startswith("EDUCATION")
    assert "@" not in text.split("\n")[0]
    assert "@" not in text
    assert len(digest) == 12


# ---- db + notify ----

def usage():
    return Usage(input=100, output=50)


def fit(score, seniority="good"):
    return FitScore(fit_score=score, category="SOC / Security Operations", seniority_fit=seniority, matched_skills=["Python"], missing_skills=["Splunk"],
                    red_flags=[], shift_or_oncall=False, why="Good fit.")


def test_db_alerts_once_and_respects_threshold(tmp_path):
    db = DB(tmp_path / "t.db")
    jobs = [make_job(ext_id=str(i)) for i in range(4)]
    db.upsert_jobs(jobs)
    db.upsert_jobs(jobs)  # re-upsert is harmless
    db.save_score(jobs[0].key, fit(90), refused=False, model="m", resume_hash="h", usage=usage())
    db.save_score(jobs[1].key, fit(50), refused=False, model="m", resume_hash="h", usage=usage())
    db.save_score(jobs[2].key, fit(95, "too_senior"), refused=False, model="m", resume_hash="h", usage=usage())
    db.save_score(jobs[3].key, None, refused=True, model="m", resume_hash="h", usage=usage())
    assert db.scored_keys() == {j.key for j in jobs}

    rows = db.pending_alerts(threshold=70, limit=10)
    assert [r["key"] for r in rows] == [jobs[0].key]
    db.mark_alerted([jobs[0].key])
    assert db.pending_alerts(threshold=70, limit=10) == []


def test_gaming_jobs_use_lower_threshold_and_sort_first(tmp_path):
    db = DB(tmp_path / "t.db")
    normal = Job("greenhouse", "acme", "1", "Security Engineer", "SF", False, "u", NOW, "d")
    game = Job("greenhouse", "riotgames", "2", "Security Engineer", "SF", False, "u", NOW, "d")
    game_low = Job("greenhouse", "riotgames", "3", "Security Analyst", "SF", False, "u", NOW, "d")
    db.upsert_jobs([normal, game, game_low])
    db.save_score(normal.key, fit(80), refused=False, model="m", resume_hash="h", usage=usage())
    db.save_score(game.key, fit(62), refused=False, model="m", resume_hash="h", usage=usage())
    db.save_score(game_low.key, fit(55), refused=False, model="m", resume_hash="h", usage=usage())

    is_gaming = filters.Gaming({"companies": ["riotgames"]})
    rows = db.pending_alerts(70, 10, is_gaming, gaming_threshold=60)
    assert [(r["key"], r["gaming"]) for r in rows] == [(game.key, True), (normal.key, False)]
    assert db.pending_alerts(70, 1, is_gaming, gaming_threshold=60)[0]["key"] == game.key


def test_unscored_jobs_queue(tmp_path):
    db = DB(tmp_path / "t.db")
    fresh = make_job(ext_id="fresh", posted=datetime.now(timezone.utc))
    stale = make_job(ext_id="stale", posted=datetime(2026, 1, 1, tzinfo=timezone.utc))
    scored = make_job(ext_id="scored", posted=datetime.now(timezone.utc))
    db.upsert_jobs([fresh, stale, scored])
    db.save_score(scored.key, fit(50), refused=False, model="m", resume_hash="h", usage=usage())
    assert [j.key for j in db.unscored_jobs(max_age_days=14, limit=10)] == [fresh.key]


def test_upsert_keeps_description_when_list_omits_it(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_jobs([make_job()])
    light = make_job()
    light.description = ""
    db.upsert_jobs([light])
    assert db.conn.execute("SELECT description FROM jobs").fetchone()[0] == "desc"


# ---- ETags + Greenhouse hydration (mocked HTTP) ----

def test_fetch_all_uses_etags_and_hydrates_greenhouse(monkeypatch):
    light = {"jobs": [{"id": 111, "title": "SOC Analyst", "location": {"name": "Remote"},
                       "absolute_url": "u", "first_published": "2026-10-01T00:00:00Z"}]}
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers.get("if-none-match")))
        if req.headers.get("if-none-match") == "v1":
            return httpx.Response(304)
        if req.url.path.endswith("/jobs/111"):
            return httpx.Response(200, json=fixture("greenhouse.json")["jobs"][0])
        return httpx.Response(200, json=light, headers={"etag": "v1"})

    from job_radar import sources
    real = httpx.AsyncClient
    monkeypatch.setattr(sources, "_client", lambda: real(transport=httpx.MockTransport(handler)))

    first = asyncio.run(fetch_all({"greenhouse": ["acme"]}))
    assert first.etags == {"greenhouse:acme": "v1"} and len(first.jobs) == 1
    assert first.jobs[0].description == ""  # list endpoint has no content
    assert asyncio.run(hydrate_all(first.jobs)) == set()
    assert "hunt threats" in first.jobs[0].description

    second = asyncio.run(fetch_all({"greenhouse": ["acme"]}, first.etags))
    assert second.unchanged == {"greenhouse:acme"} and second.jobs == [] and second.bytes == 0


def test_slack_single_gaming_alert():
    row = {"key": "k", "title": "Security Analyst", "company": "riotgames", "location": "LA", "remote": 0,
           "url": "https://x", "comp": None, "fit_score": 64, "why": "w", "gaming": True,
           "matched_skills": [], "missing_skills": [], "red_flags": []}
    payload = notify.build_digest([row])
    assert payload["blocks"][0]["text"]["text"] == "🎮 New gaming job match"
    assert payload["text"] == "64 · Security Analyst — riotgames"


def test_slack_digest_shape():
    row = {"key": "k", "title": "SOC <Analyst>", "company": "acme", "location": "Remote", "remote": 1,
           "url": "https://x", "comp": None, "fit_score": 88, "why": "Fits & then some.",
           "matched_skills": ["Python"], "missing_skills": [], "red_flags": ["on-call"]}
    payload = notify.build_digest([row])
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "header" and blocks[0]["text"]["text"] == "🎯 New job match"
    section = blocks[1]["text"]["text"]
    assert "<https://x|SOC &lt;Analyst&gt;>" in section and "Fits &amp; then some." in section
    assert "🚩 on-call" in blocks[2]["elements"][0]["text"]
    assert "🏷️ uncategorized · posted date unknown" in section
    assert len(blocks) <= 50


def test_slack_shows_category_date_and_shift():
    row = {"key": "k", "title": "SOC Analyst", "company": "acme", "location": "Remote", "remote": 1,
           "url": "https://x", "comp": None, "fit_score": 80, "why": "w", "category": "SOC / Security Operations",
           "posted_at": "2026-10-03T17:00:00+00:00", "shift_or_oncall": 1,
           "matched_skills": [], "missing_skills": [], "red_flags": []}
    section = notify.build_digest([row])["blocks"][1]["text"]["text"]
    assert "🏷️ SOC / Security Operations · posted Oct 3, 2026 · shift/on-call" in section
    assert "SOC / Security Operations · posted Oct 3, 2026" in notify.format_text([row])


def test_score_saves_category(tmp_path):
    db = DB(tmp_path / "t.db")
    job = make_job()
    db.upsert_jobs([job])
    db.save_score(job.key, fit(90), refused=False, model="m", resume_hash="h", usage=usage())
    assert db.pending_alerts(threshold=70, limit=10)[0]["category"] == "SOC / Security Operations"


# ---- scorers (mocked HTTP; both SDKs use httpx2) ----

FIT_JSON = json.dumps(fit(82).model_dump())


def mock_client(cls, handler, **kw):
    return cls(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **kw)


def openai_response(content):
    return {
        "id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5-mini", "status": "completed",
        "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [content]}],
        "usage": {"input_tokens": 3000, "input_tokens_details": {"cached_tokens": 2048},
                  "output_tokens": 300, "output_tokens_details": {"reasoning_tokens": 200}, "total_tokens": 3300},
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    }


def test_openai_scorer_request_and_parse():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=openai_response({"type": "output_text", "text": FIT_JSON, "annotations": []}))

    scorer = OpenAIScorer("RESUME TEXT", "gpt-5-mini", "low", client=mock_client(openai.OpenAI, handler))
    result = scorer.score(make_job())

    body = seen["body"]
    assert body["model"] == "gpt-5-mini" and body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert "RESUME TEXT" in body["instructions"]
    assert body["text"]["format"]["type"] == "json_schema"
    assert result.fit.fit_score == 82 and not result.refused
    assert result.usage == Usage(input=952, output=300, cache_read=2048, cache_write=0)


def test_openai_scorer_refusal():
    def handler(req):
        return httpx2.Response(200, json=openai_response({"type": "refusal", "refusal": "no"}))

    scorer = OpenAIScorer("r", "gpt-5-mini", "", client=mock_client(openai.OpenAI, handler))
    result = scorer.score(make_job())
    assert result.refused and result.fit is None


def test_anthropic_scorer_request_and_parse():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": FIT_JSON}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 120, "cache_read_input_tokens": 2600,
                      "cache_creation_input_tokens": 0},
        })

    scorer = AnthropicScorer("RESUME TEXT", "claude-opus-5-5", "low", client=mock_client(anthropic.Anthropic, handler))
    result = scorer.score(make_job())

    body = seen["body"]
    assert seen["beta"] == "server-side-fallback-2026-07-01" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.fit.fit_score == 82
    assert result.usage == Usage(input=900, output=120, cache_read=2600, cache_write=0)


def test_make_scorer_rejects_unknown_provider():
    with pytest.raises(ValueError):
        make_scorer("gemini", "r", "m", "low")


# ---- GameJobs.co, Phenom (Activision), Workday ----

def test_gamejobs_feed_and_page():
    jobs = gamejobs.parse_feed((FIXTURES / "gamejobs.atom").read_bytes())
    # Splits on the last " at ": rare company names containing " at " come out wrong,
    # which only affects the display name.
    assert [(j.title, j.company) for j in jobs] == [
        ("Game Security Analyst", "Rockstar"), ("Anti-Cheat Analyst at Studio", "Large")
    ]
    job = jobs[0]
    assert job.location is None and job.ext_id == "Game-Security-Analyst-at-Rockstar-4115"

    page = (FIXTURES / "gamejobs_page.html").read_text()

    def handler(req):
        return httpx.Response(200, text=page)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await gamejobs.hydrate(client, job)

    asyncio.run(run())
    assert job.location == "Carlsbad, California, US"
    assert "cheaters" in job.description
    assert job.apply_url == "https://job-boards.greenhouse.io/rockstargames/jobs/8009072003"


def test_is_duplicate_matches_polled_boards_and_phenom():
    job = Job("gamejobs", "Rockstar", "x", "Game Security Analyst", "", False, "u", NOW, "d")
    job.apply_url = "https://job-boards.greenhouse.io/rockstargames/jobs/8009072003"
    assert is_duplicate(job, {"greenhouse:rockstargames"}, set())
    assert not is_duplicate(job, {"greenhouse:other"}, set())
    job.apply_url = "https://xboxgaming.wd1.myworkdayjobs.com/External/job/Sherman-Oaks/Game-Security-Analyst_R028045-1/apply"
    assert is_duplicate(job, set(), {"phenom:activisionblizzard:R028045-1"})
    assert not is_duplicate(job, set(), {"phenom:activisionblizzard:R999"})


def test_is_duplicate_by_title_when_no_link():
    job = Job("gamejobs", "Roblox", "x", "Safety Threat Investigator, Safety Investigations", "", False, "u", NOW, "d")
    titles = title_index([("greenhouse", "roblox", "Safety Threat Investigator, Safety Investigations")])
    assert is_duplicate(job, set(), set(), titles)
    job.company = "Rockstar"
    assert not is_duplicate(job, set(), set(), titles)
    assert is_duplicate(Job("gamejobs", "Rockstar", "y", "Game Security Analyst", "", False, "u", NOW, "d"),
                        set(), set(), title_index([("greenhouse", "rockstargames", "Game Security Analyst")]))


def test_gamejobs_remote_requires_us():
    job = Job("gamejobs", "Tripledot", "1", "Fraud Analyst", None, False, "u", NOW, "")
    loc = {"@type": "Place", "address": {"addressLocality": "Warsaw", "addressCountry": "PL"}}
    gamejobs.apply_posting(job, {"jobLocationType": "TELECOMMUTE", "jobLocation": loc,
                                 "applicantLocationRequirements": {"@type": "Country", "name": "PL"}})
    assert not job.remote and job.location.startswith("Remote")
    gamejobs.apply_posting(job, {"jobLocationType": "TELECOMMUTE",
                                 "applicantLocationRequirements": [{"@type": "Country", "name": "USA"}]})
    assert job.remote


def test_workday_url_helpers():
    url = "https://xboxgaming.wd1.myworkdayjobs.com/External/job/Sherman-Oaks/Game-Security-Analyst_R028045-1/apply"
    assert workday.req_id(url) == "R028045-1"
    assert workday.job_page(url) == url.removesuffix("/apply")


def test_phenom_normalize():
    raw = {"jobId": "R028045-1", "title": "Game Security Analyst - Ricochet Anti-Cheat ",
           "multi_location": ["Sherman Oaks, California, United States of America"],
           "postedDate": "2026-09-15T00:00:00.000+0000", "descriptionTeaser": "Teaser",
           "applyUrl": "https://xboxgaming.wd1.myworkdayjobs.com/External/job/Sherman-Oaks/X_R028045-1/apply"}
    job = phenom.normalize("activisionblizzard", raw)
    assert job.key == "phenom:activisionblizzard:R028045-1"
    assert job.posted_at.date().isoformat() == "2026-09-15"
    assert job.url.endswith("X_R028045-1") and job.teaser == "Teaser"


@pytest.mark.parametrize("location,any_us,keep", [
    ("Sherman Oaks, California, United States of America", True, True),
    ("Seattle, WA", True, True),
    ("Bengaluru, IN", True, False),
    ("Montreal, Quebec, CA", True, False),
    ("Dundee, Scotland, GB", True, False),
    ("Sherman Oaks, California, United States of America", False, False),
])
def test_gaming_any_us_location(location, any_us, keep):
    with open(ROOT / "config.example.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    gaming = filters.Gaming({"sources": ["gamejobs"], "any_us_location": any_us})
    f = filters.Filters(cfg["filters"], gaming)
    job = Job("gamejobs", "Studio", "1", "Security Analyst", location, False, "u", NOW, "d")
    assert f.location_ok(job) is keep
    job.source, job.company = "greenhouse", "acme"  # not gaming: normal rules
    assert f.location_ok(job) is False


def test_unknown_location_passes_until_hydrated(flt):
    assert flt.location_ok(make_job(location=None))


# ---- Workday listing ----

@pytest.mark.parametrize("text,days", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 31), ("", 0),
])
def test_workday_days_ago(text, days):
    assert workday.days_ago(text) == days


def _wd_posting(i, posted="Posted Today", loc="Santa Clara, CA"):
    return {"title": f"Security Engineer {i}", "locationsText": loc, "postedOn": posted,
            "externalPath": f"/job/US-CA/Security-Engineer_JR{i}", "bulletFields": [f"JR{i}"]}


def _run_workday_fetch(pages, etag=None):
    calls = []

    def handler(req):
        offset = json.loads(req.content)["offset"]
        calls.append(offset)
        page = pages[offset // 20] if offset // 20 < len(pages) else []
        return httpx.Response(200, json={"total": 999, "jobPostings": page})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await workday.fetch(client, "acme.wd5/careers", etag, Fetched())

    jobs, new_etag = asyncio.run(run())
    return jobs, new_etag, calls


def test_workday_first_run_stops_at_old_jobs():
    pages = [[_wd_posting(p * 20 + i) for i in range(20)] for p in range(3)]
    pages.append([_wd_posting(100 + i, "Posted 30+ Days Ago") for i in range(20)])
    pages.append([_wd_posting(200 + i) for i in range(20)])  # never reached
    jobs, etag, calls = _run_workday_fetch(pages)
    assert calls == [0, 20, 40, 60]
    assert len(jobs) == 80 and len(json.loads(etag)) == 80
    j = jobs[0]
    assert j.key == "workday:acme:JR0" and j.url == "https://acme.wd5.myworkdayjobs.com/careers/job/US-CA/Security-Engineer_JR0"
    assert j.location == "Santa Clara, CA"


def test_workday_unchanged_board_costs_two_requests_and_survives_pinned_jobs():
    pinned = [_wd_posting(900 + i, "Posted 30+ Days Ago") for i in range(20)]
    newest = [_wd_posting(i) for i in range(20)]
    older = [_wd_posting(20 + i) for i in range(20)]
    seen = json.dumps([f"JR{n}" for n in [*range(900, 920), *range(0, 40)]])
    jobs, etag, calls = _run_workday_fetch([pinned, newest, older], seen)
    assert jobs is None and calls == [0, 20]  # two quiet pages -> stop, nothing new

    newest[0] = _wd_posting(5000)  # one new job hides behind the pinned page
    jobs, etag, calls = _run_workday_fetch([pinned, newest, older], seen)
    assert [j.ext_id for j in jobs] == ["JR5000"] and calls == [0, 20, 40, 60]  # two quiet pages after it
    assert "JR5000" in json.loads(etag)


def test_workday_multi_location_waits_for_hydration():
    job = workday.normalize("acme.wd5/careers", _wd_posting(1, loc="3 Locations"), NOW)
    assert job.location is None


def test_big_boards_are_throttled(tmp_path):
    db = DB(tmp_path / "t.db")
    db.save_etags({"ashby:openai": "v1"})
    db.save_board_sizes({"ashby:openai": 14_000_000, "greenhouse:small": 50_000})
    assert db.throttled_boards(5_000_000, 60) == {"ashby:openai"}
    db.conn.execute("UPDATE boards SET fetched_at = '2026-01-01T00:00:00+00:00'")  # long ago
    assert db.throttled_boards(5_000_000, 60) == set()
    db.save_board_sizes({"ashby:openai": 14_000_000})
    db.clear_etags()
    assert db.etags() == {} and db.throttled_boards(5_000_000, 60) == {"ashby:openai"}
