# job-radar

Pulls new security job postings from public job boards (Greenhouse, Lever, Ashby, Workday), Activision Blizzard's career site, and GameJobs.co, filters them, scores each one against my resume with an LLM (OpenAI by default, Claude optional), and posts the best matches to Slack. It checks every 15 minutes on my Mac and alerts on each match as soon as it's scored.

```
70 job boards ──▶ filters ──▶ LLM scores fit ───▶ SQLite ──▶ Slack, one message per match
 (changed boards   (title, level,   (new jobs only,                (score ≥ 70, or ≥ 60 at
  only, via ETag)   Bay Area/remote  0-100 + reasons)               gaming companies 🎮)
                    US, ≤14 days)
```

**Why polling is cheap:** every board returns an ETag. job-radar sends it back on the next check, and boards with no changes answer `304 Not Modified` with an empty body. Greenhouse lists are fetched without descriptions; a description is downloaded only for a new job that passes the filters. A full check of all 70 boards is ~42 MB, and a check where nothing changed is ~0 MB. LLM cost doesn't depend on how often it checks, because each job is scored once.

## Setup

```bash
cd job-radar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in (see below)
.venv/bin/python -m pytest  # should pass
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

## Usage

```bash
.venv/bin/python -m job_radar run --dry-run     # score and print matches, no Slack
.venv/bin/python -m job_radar run               # score and post to Slack
.venv/bin/python -m job_radar run --full        # ignore ETags, re-check every board
.venv/bin/python -m job_radar check-companies   # verify every board in config.toml
.venv/bin/python -m job_radar stats             # totals + estimated API spend
```

- Each job is scored **once**. Re-running only scores postings it hasn't seen.
- Each job is alerted **once**, as its own Slack message, right after it's scored (max 10 per run).
- Changing `[filters]`, `[companies]` or `max_age_days` automatically triggers a full re-check on the next run.
- A job that fails to score (API or network error) is retried on the next run.

## Tuning (`config.toml`)

- **Sources:**
  - `workday`: entries are `tenant.wdN/site`, taken from the careers URL `https://<tenant>.<wdN>.myworkdayjobs.com/<site>`. Workday sends no ETags, so each board remembers the job IDs it has shown. A check reads newest-first until two pages in a row have nothing new, which is 2 small requests when nothing changed. Adding or changing a board triggers a one-time scan of its last 14 days.
  - Boards bigger than `big_board_mb` (OpenAI's Ashby board is ~14 MB) are checked at most every `big_board_interval_minutes`.
  - `phenom`: Activision Blizzard's career site. Full descriptions come from its Workday postings.
  - `gamejobs`: GameJobs.co searches. Entries are queries, not companies. Each job's page gives its location, description and application link. Jobs it re-lists from boards above are skipped, matched by that link or by company + title.
  - Neither sends ETags, so each check re-downloads about 0.4 MB.
- **Companies:** add a board slug under `greenhouse`, `lever` or `ashby`. The slug is in the careers URL, e.g. `job-boards.greenhouse.io/<slug>`. Run `check-companies` after editing.
- **Filters:** `title_include`, `title_exclude`, `bay_area`, `us_markers`, `non_us_markers`.
- **Thresholds:** `alert_threshold`, `max_age_days`, `max_llm_calls_per_run` (cost cap).
- **Gaming:** `[gaming] any_us_location = true` lets gaming jobs anywhere in the US through the location filter. Board slugs under `[gaming] companies` (and every job from `[gaming] sources`) alert at `[gaming] alert_threshold` (default 60) instead of 70, get a 🎮 tag, and are sent first. The LLM is told about the preference but still scores fit honestly; the boost is applied by the threshold, not by inflating scores.
- **What "a good fit" means:** the rubric in `job_radar/scorer.py` (`RUBRIC`).
- **Resume:** read live from `resume_path`, so editing the .docx takes effect on the next run. Everything above the EDUCATION header (name, phone, email) is removed before it's sent to the API.

## Schedule (launchd)

```bash
# install: runs every 15 minutes (and once right away)
cp launchd/com.matias.jobradar.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matias.jobradar.plist

# run it right now
launchctl kickstart -k gui/$(id -u)/com.matias.jobradar
tail -f logs/job-radar.log

# uninstall
launchctl bootout gui/$(id -u)/com.matias.jobradar
rm ~/Library/LaunchAgents/com.matias.jobradar.plist
```

## Cost

Usually 5–30 new jobs a day get past the filters, and each one is a single LLM call at low reasoning effort. The rubric and resume form a fixed prefix, so both providers cache it within a run.
- `gpt-5-mini` is a small model: well under a cent per job.
- Claude Opus 5.5 costs roughly 2–3¢ per job.

`stats` shows the token totals. To get a dollar estimate, add your model's prices under `[prices."<model>"]` in `config.toml`.

## Layout

| Path | What it does |
|---|---|
| `job_radar/sources/` | One fetcher per source (Greenhouse, Lever, Ashby, Workday, Phenom, GameJobs), each normalizing to a `Job` |
| `job_radar/filters.py` | Title, seniority, location, age filters |
| `job_radar/scorer.py` | Rubric + OpenAI or Claude call → `FitScore` |
| `job_radar/db.py` | SQLite: `jobs`, `scores`, `alerts` |
| `job_radar/notify.py` | Slack Block Kit digest |
| `job_radar/resume.py` | .docx → text, contact info removed |

## Next (v1)

Web UI over `job-radar.db` (ranked feed, saved/applied/dismissed, which `jobs.status` already supports), Slack Save/Dismiss buttons, skill-gap analytics across the top matches, more sources (Adzuna, USAJobs, HN "Who's Hiring").
