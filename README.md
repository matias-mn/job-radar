# job-radar

[![tests](https://github.com/matias-mn/job-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/matias-mn/job-radar/actions/workflows/tests.yml)

I built job-radar to run my own job search. Every 15 minutes it pulls new security postings from about 130 company job boards (Greenhouse, Lever, Ashby, Workday), [freehire](https://github.com/strelov1/freehire)'s open index of postings from 120+ applicant tracking systems, Activision Blizzard's career site, GameJobs.co, and LinkedIn and Indeed job-alert emails. It filters them and has an LLM (OpenAI by default, Claude optional) score each one against my resume. Each good match goes to Slack as soon as it's scored, along with the posting's minimum and preferred qualifications.

It's the same pattern as the detection tooling I build at work: collect noisy data from many sources, normalize it, filter cheaply first, use an LLM only where judgment is needed, and alert with enough context to act.

```
~130 job boards ──▶ filters ──▶ LLM scores fit ───▶ SQLite ──▶ Slack, one message per match
 + alert emails      (title, level,   (new jobs only,                (score ≥ 70, or ≥ 60 at
 (changed boards      Bay Area/remote  0-100 + reasons)               gaming companies 🎮)
  only, via ETag)     US, ≤15 days)
```

## Highlights

- **Cheap polling:** every board returns an ETag. job-radar sends it back on the next check, and boards with no changes answer `304 Not Modified` with an empty body. Workday has no ETags, so each board remembers the job IDs it has seen and reads newest-first until it hits known ones. A check where nothing changed downloads almost nothing.
- **Async fetching:** `httpx` with one fetcher per source, each normalizing to a shared `Job` model. A board that fails is reported and skipped instead of stopping the run.
- **Structured LLM output:** the scorer returns a typed `FitScore` (score, category, seniority fit, matched and missing skills, red flags, shift/on-call flag, qualifications). Both providers are supported. A fixed rubric and resume prefix is prompt-cached, and there's a per-run cost cap.
- **Dedupe across sources:** the same job shows up on a company board, freehire, LinkedIn, Indeed and GameJobs. Matching by link or by company + title means each job is scored and alerted once.
- **Graceful degradation:** alert emails carry only a title. job-radar looks for the full posting on the company's own board, then on LinkedIn's public job page (paced, with backoff on 429s). If neither works, it scores from the title and marks the score as a guess (`72?`).
- **Privacy by default:** contact info is stripped from the resume before it's sent to the API, and tracking tokens are stripped from email links. Personal config stays local (see [What stays local](#what-stays-local)).
- **Tested:** 100+ pytest tests with mocked HTTP and IMAP for every source. No network or API keys needed, and they run in GitHub Actions.

## Setup

```bash
git clone https://github.com/matias-mn/job-radar.git && cd job-radar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml    # point resume_path at your .docx resume, pick your boards
cp profile.example.md profile.md      # who you are and what you want, in plain English
cp .env.example .env                  # API key + Slack (see below)
.venv/bin/python -m pytest            # should pass
```

### 1. LLM API key
`provider` in `config.toml` picks the scorer:

- **OpenAI (default):** put your key in `.env` as `OPENAI_API_KEY=...`. The model is `gpt-5-mini`; change `model` to use another one.
- **Claude:** set `provider = "anthropic"` and `model = "claude-opus-5-5"`, then add `ANTHROPIC_API_KEY=...` from https://platform.claude.com → API Keys.

Use a **personal** key, not a work one. Each call sends your resume and shows you're job hunting.

### 2. Slack webhook
1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**. Pick a workspace you own; a free personal one is fine.
2. **Incoming Webhooks** → turn it on → **Add New Webhook to Workspace** → pick a channel (e.g. `#job-radar`).
3. Copy the URL into `.env` as `SLACK_WEBHOOK_URL=...`.
4. `.venv/bin/python -m job_radar test-slack` should post a sample message.
5. Optional, to get each posting's minimum/preferred qualifications as a **thread reply** under its alert (a webhook can't reply in a thread, so without this they're appended to the alert):
   **OAuth & Permissions** → Bot Token Scopes → add `chat:write` → **Reinstall to Workspace** → copy the Bot User OAuth Token into `.env` as `SLACK_BOT_TOKEN=xoxb-...`. In Slack, run `/invite @<app name>` in the channel, and copy the channel ID (channel name → **About**, at the bottom) into `.env` as `SLACK_CHANNEL=C...`. When both are set, job-radar posts through the bot instead of the webhook. Run `test-slack` again to check the thread.

### 3. Job-alert emails (optional)
LinkedIn and Indeed job alerts cover companies whose career sites can't be polled (Google, Meta, TikTok, …).
1. Make a dedicated Gmail, turn on 2-Step Verification, and create an app password at https://myaccount.google.com/apppasswords.
2. Put both in `.env` as `ALERT_EMAIL=...` and `ALERT_EMAIL_APP_PASSWORD=...`.
3. In the Gmail that gets your alerts, add filters on `from:jobalerts-noreply@linkedin.com` and `from:donotreply@jobalert.indeed.com` that forward to the dedicated address.

job-radar reads that inbox over IMAP (read-only; emails stay unread) and only opens emails from the senders in `email_alerts.SENDERS`.

## Usage

```bash
.venv/bin/python -m job_radar run --dry-run     # score and print matches, no Slack
.venv/bin/python -m job_radar run               # score and post to Slack
.venv/bin/python -m job_radar run --full        # ignore ETags, re-check every board
.venv/bin/python -m job_radar check-companies   # verify every board in config.toml
.venv/bin/python -m job_radar stats             # totals, estimated API spend, what freehire adds
.venv/bin/python -m job_radar rehydrate         # re-read LinkedIn jobs stored without a description, re-score them
```

- Each job is scored **once**. Re-running only scores postings it hasn't seen.
- Each job is alerted **once**, as its own Slack message, right after it's scored (max 10 per run). The posting's minimum and preferred qualifications (pulled out by the scorer) go in a thread reply, or at the bottom of the alert without a bot token.
- A score with a `?` (e.g. `72?`) was a guess: the job had no description (an alert email whose posting couldn't be found), so it was scored on title, company and location only, and capped at `limited_info_max_score`.
- If LinkedIn starts refusing its job pages, Slack gets a one-time ⚠️ notice that LinkedIn alerts are being scored from the email alone, and a ✅ notice once full postings are back.
- Changing `[filters]`, `[companies]` or `max_age_days` automatically triggers a full re-check on the next run.
- A job that fails to score (API or network error) is retried on the next run.

## Tuning (`config.toml`)

- **Sources:**
  - `greenhouse`, `lever`, `ashby`: board slugs from the careers URL, e.g. `job-boards.greenhouse.io/<slug>`. Greenhouse lists are fetched without descriptions; a description is downloaded only for a new job that passes the filters. Run `check-companies` after editing.
  - `workday`: entries are `tenant.wdN/site`, taken from the careers URL `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`. Workday sends no ETags, so each board remembers the job IDs it has shown. A check reads newest-first until two pages in a row have nothing new, which is 2 small requests when nothing changed. Adding or changing a board triggers a one-time scan of its last 14 days.
  - Boards bigger than `big_board_mb` (OpenAI's Ashby board is ~14 MB) are checked at most every `big_board_interval_minutes`.
  - `phenom`: Activision Blizzard's career site. Full descriptions come from its Workday postings.
  - `gamejobs`: GameJobs.co searches. Entries are queries, not companies. Each job's page gives its location, description and application link. Jobs it re-lists from boards above are skipped, matched by that link or by company + title. Neither `phenom` nor `gamejobs` sends ETags, so each check re-downloads about 0.4 MB.
  - `freehire`: searches of [freehire.me](https://freehire.me/docs/api)'s public API, which indexes postings from 120+ applicant tracking systems (Oracle, iCIMS, SmartRecruiters, Eightfold, …) that job-radar has no fetcher for. Entries are filter strings for its `/jobs/search` endpoint, e.g. `category=security&requires_clearance=false`, limited to US jobs unless an entry sets `countries`. No key is needed for personal, non-commercial use (600 requests a minute). Results are read newest-first until the jobs the last check saw, so a quiet check is about 0.15 MB; the first check reads the last 15 days (~12 MB). A new job that passes the filters gets its full posting from freehire. A filter freehire doesn't recognize fails `check-companies` instead of quietly widening the search. freehire re-lists jobs from boards job-radar polls (dropped by link: Greenhouse, Lever, Ashby, Workday and Activision postings) and from LinkedIn, Indeed and GameJobs (dropped by company + title: freehire's copy wins, unless the alert email sent it on an earlier run). `stats` shows how many alerts only freehire found and how many of the other alerts it also listed, per source.
  - `email`: job-alert emails in `ALERT_EMAIL`. Entries are senders: `"linkedin"`, `"indeed"`. Each board remembers the last email it read; the first check reads the last 15 days. Emails don't include descriptions, so job-radar looks for the same title on the company's Greenhouse, Lever or Ashby board (guessing the slug from the company name). Found: full description, and the alert links to the company's posting. Not found (most big companies): a LinkedIn job takes its description from LinkedIn's public job page (no login), headed by LinkedIn's seniority level, which is what catches "Security Engineer" postings that want 8+ years. Those pages are read one at a time, 3 seconds apart. LinkedIn sometimes answers 429 anyway, so a refusal is retried after 15 s, then 30 s, and after three in a row LinkedIn's pages are skipped for the rest of the run. An Indeed job that LinkedIn also sent (same title and company) is dropped as a duplicate, since Indeed's pages block scripts. Anything still without a description is scored on title, company, location and (Indeed only) the one-line snippet, capped at `limited_info_max_score`, and the alert is tagged `📧 LinkedIn alert · limited info` or `📧 Indeed alert · limited info`. When a job turns up on a board config.toml doesn't poll, the log suggests adding it.
- **Filters:** `title_include`, `title_exclude`, `bay_area`, `us_markers`, `non_us_markers`. Each part of a multi-location posting (`;`, `|`, `/`, ` or `) is checked on its own, so "San Jose, Costa Rica" isn't San Jose, CA. `filters.NON_US_PLACES` adds countries and big tech hubs abroad to `non_us_markers`, so a remote posting from a place nobody listed doesn't pass as US.
- **Thresholds:** `alert_threshold`, `max_age_days`, `max_llm_calls_per_run` (cost cap).
- **Gaming:** `[gaming] any_us_location = true` lets gaming jobs anywhere in the US through the location filter. Board slugs under `[gaming] companies` (and every job from `[gaming] sources`) alert at `[gaming] alert_threshold` (default 60) instead of 70, get a 🎮 tag, and are sent first. The LLM is told about the preference but still scores fit honestly; the boost is applied by the threshold, not by inflating scores.
- **What "a good fit" means:** `profile.md` describes the candidate, and the rubric in `job_radar/scorer.py` (`RUBRIC`) says how to score: how well the resume matches and how realistic the job is. Shift work, on-call and role type (SOC, GRC, physical security) don't lower the score; they're flagged so I can decide. The scorer also judges `location_fit` from the posting's own words about where you'd live or work: a job tied to somewhere else (offices outside your area, or remote only in states that leave yours out) isn't alerted, and a remote job that names an out-of-area city without saying whether you must live there is tagged 📍.
- **Each alert shows** a job category (SOC / Security Operations, Detection & Response, GRC, Physical Security, …; the list is `CATEGORIES` in `job_radar/models.py`), the posted date, and a shift/on-call tag.
- **Resume:** read live from `resume_path`, so editing the .docx takes effect on the next run. Everything above the EDUCATION header (name, phone, email) is removed before it's sent to the API.

## Schedule (launchd, macOS)

```bash
# install: runs every 15 minutes (and once right away). Run from the project folder.
sed "s|__PROJECT_DIR__|$PWD|g" launchd/com.matias.jobradar.plist > ~/Library/LaunchAgents/com.matias.jobradar.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matias.jobradar.plist

# run it right now
launchctl kickstart -k gui/$(id -u)/com.matias.jobradar
tail -f logs/job-radar.log

# uninstall
launchctl bootout gui/$(id -u)/com.matias.jobradar
rm ~/Library/LaunchAgents/com.matias.jobradar.plist
```

## Cost

Usually 5–30 new jobs a day get past the filters, and each one is a single LLM call at low reasoning effort. LLM cost doesn't depend on how often it checks, because each job is scored once.
- `gpt-5-mini` is a small model: well under a cent per job.
- Claude Opus 5.5 costs roughly 2–3¢ per job.

`stats` shows the token totals. To get a dollar estimate, add your model's prices under `[prices."<model>"]` in `config.toml`.

## What stays local

These files are gitignored, so nothing personal is in the repo:

| File | Holds | Template |
|---|---|---|
| `config.toml` | resume path, the boards I actually poll | `config.example.toml` |
| `profile.md` | who I am and what I'm looking for | `profile.example.md` |
| `.env` | API keys, Slack and Gmail credentials | `.env.example` |
| `job-radar.db` | every job seen, scores, alerts | created on first run |
| `logs/` | run logs | |

## Layout

| Path | What it does |
|---|---|
| `job_radar/sources/` | One fetcher per source (Greenhouse, Lever, Ashby, Workday, Phenom, GameJobs, freehire, LinkedIn/Indeed email), each normalizing to a `Job`; `resolve.py` finds full postings for email jobs |
| `job_radar/filters.py` | Title, seniority, location, age filters |
| `job_radar/scorer.py` | Rubric + OpenAI or Claude call → `FitScore` |
| `job_radar/db.py` | SQLite: `jobs`, `scores`, `alerts`, board state |
| `job_radar/notify.py` | Slack Block Kit alerts |
| `job_radar/resume.py` | .docx → text, contact info removed |
| `tests/` | pytest suite; fixtures are hand-made, not real emails or postings |

## Next

Web UI over `job-radar.db` (ranked feed, saved/applied/dismissed, which `jobs.status` already supports), Slack Save/Dismiss buttons, skill-gap analytics across the top matches, more sources (Adzuna, USAJobs, HN "Who's Hiring").
