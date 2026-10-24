import asyncio
import json
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import anthropic
import httpx
import httpx2
import openai
import pytest

from job_radar import filters, notify
from job_radar.db import DB
from job_radar.models import LIMITED_INFO, FitScore, Job
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

def make_docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def test_resume_strips_contact_info(tmp_path):
    resume = tmp_path / "resume.docx"
    make_docx(resume, ["Jane Doe", "(555) 010-0000 | jane@example.com | linkedin.com/in/janedoe",
                       "EDUCATION", "B.S. Computer Science", "EXPERIENCE", "Built a phishing-domain detection service"])
    text, digest = load_resume(resume)
    assert text.startswith("EDUCATION")
    assert "Jane" not in text and "555" not in text and "@" not in text
    assert "phishing-domain detection" in text
    assert len(digest) == 12


# ---- db + notify ----

def usage():
    return Usage(input=100, output=50)


def fit(score, seniority="good"):
    return FitScore(fit_score=score, category="SOC / Security Operations", seniority_fit=seniority, matched_skills=["Python"], missing_skills=["Splunk"],
                    red_flags=[], shift_or_oncall=False, why="Good fit.",
                    min_qualifications=["BS in CS or related"], preferred_qualifications=["Splunk SPL", "Sigma rules"])


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
    assert rows[0]["min_qualifications"] == ["BS in CS or related"]
    assert rows[0]["preferred_qualifications"] == ["Splunk SPL", "Sigma rules"]
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


# ---- email alerts (LinkedIn) ----

def linkedin_email():
    import email
    from email import policy
    return email.message_from_bytes((FIXTURES / "linkedin_alert.eml").read_bytes(), policy=policy.default)


def test_linkedin_parse_email():
    from job_radar.sources import linkedin
    jobs = linkedin.parse_email(linkedin_email())
    assert [j.company for j in jobs] == ["Acme Robotics", "Globex", "Initech", "Hooli", "Umbrella Labs", "Pied Piper"]
    acme, globex, _, hooli, umbrella, _ = jobs
    assert acme.key == "linkedin:Acme Robotics:1000000001" and acme.title == "Security Engineer"
    assert acme.location == "United States (Remote)" and acme.remote
    assert globex.title == "Detection & Response Engineer"
    assert globex.location == "Foster City, CA (Hybrid)" and not globex.remote
    assert globex.comp == "$130K-$160K / year" and umbrella.comp is None
    assert hooli.location == "Palo Alto, CA" and not hooli.remote  # no workplace type given
    assert acme.url == "https://www.linkedin.com/jobs/view/1000000001/"  # no tracking tokens
    assert acme.posted_at.date().isoformat() == "2026-10-12" and acme.description == ""


def test_linkedin_jobs_pass_filters(flt):
    from job_radar.sources import linkedin
    jobs = linkedin.parse_email(linkedin_email())
    kept, _ = filters.apply(jobs, flt, 15, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    assert {j.company for j in kept} == {"Acme Robotics", "Globex", "Initech", "Hooli", "Umbrella Labs", "Pied Piper"}


class FakeIMAP:
    def __init__(self, messages, validity="1"):
        self.messages, self.validity, self.searches = messages, validity, []

    def select(self, mailbox, readonly):
        assert readonly
        return "OK", [b"%d" % len(self.messages)]

    def response(self, code):
        return code, [self.validity.encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args[1:])
            if "UID" in args:
                low = int(args[args.index("UID") + 1].split(":")[0])
                hits = [u for u in self.messages if u >= low] or [max(self.messages)]  # IMAP quirk for "N:*"
            else:
                hits = list(self.messages)
            return "OK", [" ".join(map(str, hits)).encode()]
        return "OK", [(b"BODY[]", self.messages[int(args[0])]), b")"]

    def logout(self):
        pass


def test_email_fetch_remembers_last_uid(monkeypatch):
    from job_radar.sources import email_alerts
    raw = (FIXTURES / "linkedin_alert.eml").read_bytes()
    imap = FakeIMAP({5: raw})
    monkeypatch.setattr(email_alerts, "_connect", lambda: imap)

    jobs, etag = asyncio.run(email_alerts.fetch(None, "linkedin", None, Fetched()))
    assert len(jobs) == 6 and json.loads(etag) == {"validity": "1", "uid": 5}
    assert "SINCE" in imap.searches[0]  # first run: recent mail only

    assert asyncio.run(email_alerts.fetch(None, "linkedin", etag, Fetched())) == (None, etag)
    assert imap.searches[1][-2:] == ("UID", "6:*")

    imap.messages[9] = raw
    jobs, etag = asyncio.run(email_alerts.fetch(None, "linkedin", etag, Fetched()))
    assert len(jobs) == 6 and json.loads(etag)["uid"] == 9

    imap.validity = "2"  # mailbox rebuilt: UIDs mean something else now, start over
    asyncio.run(email_alerts.fetch(None, "linkedin", etag, Fetched()))
    assert "SINCE" in imap.searches[-1]


def test_email_fetch_needs_credentials(monkeypatch):
    from job_radar.sources import email_alerts
    monkeypatch.delenv("ALERT_EMAIL", raising=False)
    with pytest.raises(ValueError, match="ALERT_EMAIL"):
        asyncio.run(email_alerts.fetch(None, "linkedin", None, Fetched()))


def test_slug_guesses():
    from job_radar.sources.resolve import slug_guesses
    assert slug_guesses("Duck Creek Technologies") == [
        "duckcreektechnologies", "duck-creek-technologies", "duckcreek", "duck"]
    assert slug_guesses("Amazon Web Services (AWS)")[0] == "amazonwebservices"
    assert slug_guesses("23andMe") == ["23andme"]


def _resolve(job, routes):
    from job_radar.sources import resolve
    calls = []

    def handler(req):
        calls.append(str(req.url))
        for prefix, body in routes.items():
            if str(req.url).startswith(prefix):
                if isinstance(body, int):
                    return httpx.Response(body)
                return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await resolve.hydrate(client, job)

    asyncio.run(run())
    return calls


def linkedin_job(title="Security Engineer", company="Etched"):
    return Job("linkedin", company, "42", title, "San Jose, CA (On-site)", False,
               "https://www.linkedin.com/jobs/view/42/", NOW, "", comp="$150K-$330K / year")


def test_resolve_finds_posting_on_company_board():
    data = fixture("ashby.json")
    listed = next(j for j in data["jobs"] if j.get("isListed", True))
    job = linkedin_job(title=listed["title"])
    calls = _resolve(job, {"https://api.ashbyhq.com/posting-api/job-board/etched": data})
    assert job.description and job.description != LIMITED_INFO
    assert job.apply_url == f"https://jobs.ashbyhq.com/etched/{listed['id']}"
    assert job.url == listed["jobUrl"]
    assert job.comp == "$150K-$330K / year"  # the email's pay is kept
    assert is_duplicate(job, {"ashby:etched"}, set())  # we already poll that board
    assert not is_duplicate(job, {"greenhouse:other"}, set())
    assert len(calls) == 3  # greenhouse, lever, then ashby hit


def test_resolve_hydrates_greenhouse_match():
    detail = fixture("greenhouse.json")["jobs"][0]
    light = {"jobs": [{k: v for k, v in detail.items() if k != "content"}]}
    job = linkedin_job(title=detail["title"], company="Acme")
    _resolve(job, {"https://boards-api.greenhouse.io/v1/boards/acme/jobs/111": detail,
                   "https://boards-api.greenhouse.io/v1/boards/acme/jobs": light})
    assert "hunt threats" in job.description
    assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/111"


def test_resolve_falls_back_to_limited_info():
    job = linkedin_job(company="Nobody Inc")
    calls = _resolve(job, {})
    assert job.description == LIMITED_INFO and job.apply_url == ""
    assert job.url == "https://www.linkedin.com/jobs/view/42/"
    assert len(calls) == 10  # 3 slug guesses x 3 boards, then LinkedIn's page, all 404


LINKEDIN_PAGE = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/42"


def test_linkedin_parse_posting():
    from job_radar.sources import linkedin
    text = linkedin.parse_posting((FIXTURES / "linkedin_posting.html").read_text())
    assert text.startswith("Employment type: Full-time\n\n")  # "Seniority level: Not Applicable" is left out
    assert "- 1 year of experience with security assessments" in text
    assert "Industries" not in text and "Show less" not in text
    page = ('<h3 class="description__job-criteria-subheader">Seniority level</h3><span class="x">Mid-Senior level</span>'
            'No longer accepting applications<div class="show-more-less-html__markup x">8+ years of SOC work</div>')
    assert linkedin.parse_posting(page) == ("LinkedIn says this job is no longer accepting applications.\n"
                                            "Seniority level: Mid-Senior level\n\n8+ years of SOC work")
    assert linkedin.parse_posting("<html>sign in</html>") == ""


def test_resolve_falls_back_to_linkedin_page():
    job = linkedin_job(company="Nobody Inc")
    _resolve(job, {LINKEDIN_PAGE: (FIXTURES / "linkedin_posting.html").read_text()})
    assert "security design reviews" in job.description
    assert job.url == "https://www.linkedin.com/jobs/view/42/" and job.apply_url == ""


def test_linkedin_pushback_stops_page_reads_for_the_run(monkeypatch):
    from job_radar.sources import linkedin, resolve
    monkeypatch.setattr(linkedin, "GAP_SECONDS", 0)
    monkeypatch.setattr(linkedin, "BACKOFF_SECONDS", 0)
    pages = []

    def handler(req):
        if "jobs-guest" in str(req.url):
            pages.append(str(req.url))
            return httpx.Response(999)
        return httpx.Response(404)

    jobs = [linkedin_job(company="Nobody Inc"), linkedin_job(title="SOC Analyst", company="Nobody Inc")]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for job in jobs:
                await resolve.hydrate(client, job)

    monkeypatch.setattr(linkedin, "page_stats", type(linkedin.page_stats)())
    asyncio.run(run())
    assert len(pages) == linkedin.TRIES  # all for the first job; the second doesn't try
    assert linkedin.page_stats == {"blocked": 1}
    assert all(job.description == LIMITED_INFO for job in jobs)


def test_linkedin_retries_a_passing_429(monkeypatch):
    from job_radar.sources import linkedin
    monkeypatch.setattr(linkedin, "GAP_SECONDS", 0)
    monkeypatch.setattr(linkedin, "BACKOFF_SECONDS", 0)
    page = (FIXTURES / "linkedin_posting.html").read_text()
    answers = [httpx.Response(429), httpx.Response(200, text=page)]

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: answers.pop(0))) as client:
            return await linkedin.fetch_description(client, "42")

    assert "security design reviews" in asyncio.run(run())


def test_indeed_job_also_on_linkedin_is_a_duplicate():
    linkedin_titles = title_index([("linkedin", "THINK Surgical", "Cybersecurity Operations Analyst"),
                                   ("indeed", "Acme", "SOC Analyst")], "linkedin")
    job = Job("indeed", "THINK Surgical, Inc.", "abc", "Cybersecurity Operations Analyst", "Fremont, CA", False, "u", NOW, "d")
    assert is_duplicate(job, set(), set(), {}, linkedin_titles)
    assert not is_duplicate(Job("indeed", "Acme", "b", "SOC Analyst", "", False, "u", NOW, "d"), set(), set(), {}, linkedin_titles)
    job.source = "linkedin"  # LinkedIn's own copy is never dropped for this
    assert not is_duplicate(job, set(), set(), {}, linkedin_titles)


def test_limited_info_score_is_capped():
    from job_radar.__main__ import cap_limited
    fit = FitScore(fit_score=90, category="Other", seniority_fit="good", matched_skills=[], missing_skills=[],
                   red_flags=[], shift_or_oncall=False, why="w", min_qualifications=[], preferred_qualifications=[])
    cap_limited(fit, linkedin_job(), {"limited_info_max_score": 75})
    assert fit.fit_score == 90  # no description yet means not limited
    job = linkedin_job()
    job.description = LIMITED_INFO
    cap_limited(fit, job, {"limited_info_max_score": 75})
    assert fit.fit_score == 75
    cap_limited(None, job, {})  # refusals have no score


def test_db_limited_jobs(tmp_path):
    db = DB(tmp_path / "t.db")
    limited, full = linkedin_job(), linkedin_job(title="SOC Analyst")
    limited.description, full.description = LIMITED_INFO, "full posting"
    full.ext_id = "43"
    db.upsert_jobs([limited, full])
    db.mark_alerted([limited.key])
    [(job, old)] = db.limited_jobs("linkedin")
    assert job.key == limited.key and old == {"fit_score": None, "seniority_fit": None, "alerted": 1}
    assert db.limited_jobs("indeed") == []


def test_gaming_matches_company_names_from_emails():
    is_gaming = filters.Gaming({"companies": ["riotgames", "sonyinteractiveentertainmentglobal", "niantic-spatial", "2k"]})
    assert is_gaming("linkedin", "Riot Games")
    assert is_gaming("linkedin", "Sony Interactive Entertainment")
    assert is_gaming("linkedin", "Niantic Spatial")
    assert not is_gaming("linkedin", "Google")
    assert not is_gaming("linkedin", "2")  # too short to match on a prefix


def test_slack_tags_linkedin_limited_info():
    row = {"key": "k", "title": "Security Engineer", "company": "Etched", "location": "San Jose, CA", "remote": 0,
           "url": "https://x", "comp": None, "fit_score": 72, "why": "w", "source": "linkedin",
           "description": LIMITED_INFO, "matched_skills": [], "missing_skills": [], "red_flags": []}
    assert "📧 LinkedIn alert · limited info" in notify.build_digest([row])["blocks"][1]["text"]["text"]
    row["description"] = "full posting"
    section = notify.build_digest([row])["blocks"][1]["text"]["text"]
    assert "📧 LinkedIn alert" in section and "limited info" not in section


# ---- email alerts (Indeed) ----

def test_indeed_parse_email():
    import email
    from email import policy
    from job_radar.sources import indeed
    msg = email.message_from_bytes((FIXTURES / "indeed_alert.eml").read_bytes(), policy=policy.default)
    jobs = indeed.parse_email(msg)
    assert len(jobs) == 8
    initech, _, _, umbrella, acme = jobs[:5]
    assert initech.key == "indeed:Initech:5fa85475740879fa"
    assert initech.title == "Cybersecurity Analyst in Santa Clara, CA" and initech.location == "Santa Clara, CA"
    assert initech.comp == "$72,000 - $96,000 a year" and "security events" in initech.teaser
    assert initech.url == "https://www.indeed.com/viewjob?jk=5fa85475740879fa"  # no personal tokens
    assert initech.posted_at.date().isoformat() == "2026-10-16"  # "Just posted"
    assert umbrella.comp is None and "Easily apply" not in umbrella.teaser
    assert acme.company == "Acme Robotics" and acme.location == "Sunnyvale, CA"
    assert jobs[6].company == "Soylent Memory Solutions America Inc."


def test_indeed_block_age_and_remote():
    from job_radar.sources import indeed
    lines = ["SOC Analyst", "Acme - Remote", "Monitor alerts.", "3 days ago", "https://www.indeed.com/rc/clk/dl?jk=abc123&x=1"]
    job = indeed.parse_block(lines, NOW)
    assert job.remote and job.posted_at == NOW.replace(day=2)
    assert indeed.parse_block(["See all", "https://www.indeed.com/jobs?q=x"], NOW) is None


def test_limited_info_keeps_indeed_snippet():
    job = Job("indeed", "Nobody Inc", "abc", "SOC Analyst", "San Jose, CA", False, "u", NOW, "", teaser="Monitor SIEM alerts.")
    _resolve(job, {})
    assert job.description.startswith(LIMITED_INFO) and job.description.endswith("Monitor SIEM alerts.")
    row = {"key": "k", "title": "SOC Analyst", "company": "Nobody Inc", "location": "San Jose, CA", "remote": 0,
           "url": "u", "comp": None, "fit_score": 72, "why": "w", "source": "indeed", "description": job.description,
           "matched_skills": [], "missing_skills": [], "red_flags": []}
    assert "📧 Indeed alert · limited info" in notify.build_digest([row])["blocks"][1]["text"]["text"]


# ---- uncertain scores, qualifications, Slack bot, LinkedIn mode ----

def alert_row(**kw):
    row = {"key": "k", "title": "SOC Analyst", "company": "Acme", "location": "San Jose, CA", "remote": 0,
           "url": "https://x", "comp": None, "fit_score": 88, "why": "w", "source": "linkedin", "description": "full",
           "matched_skills": [], "missing_skills": [], "red_flags": [],
           "min_qualifications": ["1 year of SOC work"], "preferred_qualifications": ["Splunk <SPL>"]}
    return row | kw


def test_limited_score_shows_question_mark():
    row = alert_row(description=LIMITED_INFO, source="indeed")
    payload = notify.build_digest([row])
    assert "*88?*" in payload["blocks"][1]["text"]["text"] and payload["text"].startswith("88? ·")
    assert "limited info, score is a guess" in payload["blocks"][1]["text"]["text"]
    assert "[ 88?]" in notify.format_text([row])
    full = notify.build_digest([alert_row()])
    assert "*88*" in full["blocks"][1]["text"]["text"] and "guess" not in full["blocks"][1]["text"]["text"]
    # any source, not just the ones we know today
    assert "*70?*" in notify.build_digest([alert_row(fit_score=70, source="glassdoor", description=LIMITED_INFO)])["blocks"][1]["text"]["text"]


def test_qualifications_reply_and_inline():
    reply = notify.build_qualifications(alert_row())
    texts = [b["text"]["text"] for b in reply["blocks"]]
    assert texts == ["*Minimum qualifications*\n• 1 year of SOC work", "*Preferred qualifications*\n• Splunk &lt;SPL&gt;"]
    assert notify.build_qualifications(alert_row(min_qualifications=[], preferred_qualifications=[])) is None
    assert notify.build_qualifications(alert_row(min_qualifications=None, preferred_qualifications=None)) is None  # scored before v0.5
    inline = notify.build_digest([alert_row()], with_qualifications=True)["blocks"]
    assert any(b.get("text", {}).get("text", "").startswith("*Minimum qualifications*") for b in inline)
    assert not any("qualifications" in b.get("text", {}).get("text", "") for b in notify.build_digest([alert_row()])["blocks"])
    assert "      min: 1 year of SOC work" in notify.format_text([alert_row()])


def mock_slack_api(monkeypatch, handler):
    monkeypatch.setattr(notify.httpx, "post",
                        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).post(url, **kw))


def test_slack_bot_threads_qualifications(monkeypatch):
    from job_radar import __main__ as cli
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")
    posts = []

    def handler(req):
        posts.append(json.loads(req.content))
        assert req.headers["authorization"] == "Bearer xoxb-test"
        return httpx.Response(200, json={"ok": True, "ts": f"17.{len(posts)}"})

    mock_slack_api(monkeypatch, handler)
    cli.Slack().alert(alert_row())
    assert [p.get("thread_ts") for p in posts] == [None, "17.1"]
    assert posts[0]["channel"] == "C123" and "Minimum qualifications" in json.dumps(posts[1])
    assert "qualifications" not in json.dumps(posts[0])


def test_slack_bot_error_raises(monkeypatch):
    mock_slack_api(monkeypatch, lambda req: httpx.Response(200, json={"ok": False, "error": "not_in_channel"}))
    with pytest.raises(httpx.HTTPError, match="not_in_channel"):
        notify.post_bot("t", "C1", {"text": "x"})


class FakeAlerter:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def notice(self, text):
        self.sent.append(text)
        return self.ok


def test_linkedin_mode_notices(tmp_path, monkeypatch):
    from collections import Counter
    from job_radar import __main__ as cli
    from job_radar.sources import linkedin
    db = DB(tmp_path / "t.db")

    def run(stats, ok=True):
        monkeypatch.setattr(linkedin, "page_stats", Counter(stats))
        alerter = FakeAlerter(ok)
        cli.check_linkedin_mode(db, alerter)
        return alerter.sent

    assert run({"read": 3}) == []  # already reading full pages: nothing to say
    assert run({}) == []  # no LinkedIn jobs this run
    assert run({"read": 2, "blocked": 1}, ok=False) == [cli.LINKEDIN_NOTICES["email_only"]]  # post failed...
    assert run({"blocked": 1}) == [cli.LINKEDIN_NOTICES["email_only"]]  # ...so it's tried again
    assert run({"blocked": 1}) == []  # once only
    assert run({}) == []
    assert run({"read": 1}) == [cli.LINKEDIN_NOTICES["full"]]
    assert run({"read": 1}) == []
