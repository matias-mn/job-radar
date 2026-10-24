"""Score one job against the resume with an LLM (Claude or OpenAI). Returns a FitScore or a refusal."""

import logging
from dataclasses import dataclass

import anthropic
import openai

from .models import FitScore, Job

log = logging.getLogger(__name__)

RUBRIC = """\
You screen job postings for one candidate and score how good a fit each one is. \
You see the candidate's resume and preferences once, then one job posting per request.

## The candidate
- Finishing a B.S. in a computing or networking field, graduating within the next few months. Wants a full-time role starting after graduation.
- About a year as a security intern doing detection engineering and security automation (Python services, alert triage, Slack/Jira integrations), plus some IT or lab work.
- Open to on-site/hybrid in their metro area or remote within the US.

## What they want
Best fit is detection engineering / security automation: understand an attack, then build tooling that catches it. They like an even mix of investigating threats and building tools.
They'd especially like to work in the video game industry (studios, publishers, game platforms, anti-cheat). When a posting is from a gaming company, say so in `why` if it's a real fit, but score the fit honestly: the app applies the gaming preference separately.
Any entry-level security role is in scope, including SOC analyst, incident response, threat intel, GRC and trust & safety / fraud.

## How to score (fit_score, 0-100)
Score how well their background matches and how realistic it is for them to get the job. \
Do NOT lower the score for shift work, 24/7 coverage, weekends, on-call, or the type of security role: \
set shift_or_oncall and list it in red_flags so they can decide.
- 85-100: entry-level or new-grad role; most requirements are on their resume.
- 70-84: good fit with a gap or two they could close (a specific SIEM, a cert), or asks for ~2 years.
- 50-69: plausible but a stretch (a few missing skills, or ~3 years asked for).
- 0-49: too senior, wrong field (sales, legal, pure SWE with no security), or requires a clearance \
or a cert they lack.
Years of experience asked for matter most: treat internship + lab work as roughly 1-2 years. \
Explicit "new grad" or "early career" is a strong positive. Be skeptical: a high score should mean \
"they're competitive for this today".

Base matched_skills only on what the resume actually shows. Keep every item in matched_skills, \
missing_skills and red_flags short (2-5 words), at most 6 items per list.

Copy min_qualifications and preferred_qualifications from the posting itself (headings like "Minimum", \
"Required", "Basic", "Preferred", "Nice to have"), lightly trimmed, at most 12 items each. Never invent \
them: if the posting has no such list, or there's no description, leave the list empty.

## Resume
<resume>
{resume}
</resume>
"""


@dataclass
class Usage:
    """Token counts, normalized across providers. `input` excludes cached tokens."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class ScoreResult:
    fit: FitScore | None
    refused: bool
    model: str
    usage: Usage


class RateLimited(Exception):
    """Raised to stop scoring for the rest of this run."""


def job_prompt(job: Job) -> str:
    posted = job.posted_at.date().isoformat() if job.posted_at else "unknown"
    return (
        "<job>\n"
        f"Company: {job.company}{' (video game industry)' if job.gaming else ''}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location or 'unspecified'}{' (remote)' if job.remote else ''}\n"
        f"Posted: {posted}\n"
        f"Compensation: {job.comp or 'not listed'}\n\n"
        f"{job.description}\n"
        "</job>\n\n"
        "Score this job for the candidate."
    )


def _clamp(fit: FitScore) -> FitScore:
    fit.fit_score = max(0, min(100, fit.fit_score))
    return fit


class AnthropicScorer:
    def __init__(self, resume_text: str, model: str, effort: str, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        # Frozen per run so every call after the first reads it from the prompt cache.
        self.system = [
            {"type": "text", "text": RUBRIC.format(resume=resume_text), "cache_control": {"type": "ephemeral"}}
        ]

    def score(self, job: Job) -> ScoreResult | None:
        """Returns None on a transient error (the job is retried next run)."""
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                messages=[{"role": "user", "content": job_prompt(job)}],
                output_format=FitScore,
                output_config={"effort": self.effort},
                # Security postings can trip the cyber classifier; on a decline the API
                # re-runs the request on a fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise RateLimited(str(e)) from e
        except anthropic.APIStatusError as e:
            log.error("API error scoring %s (%s): %s", job.key, e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("connection error scoring %s: %s", job.key, e)
            return None

        u = response.usage
        usage = Usage(u.input_tokens, u.output_tokens, u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0)
        if response.stop_reason == "refusal":
            log.warning("refused: %s %r", job.key, job.title)
            return ScoreResult(None, True, response.model, usage)
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            log.error("no parsed output for %s (stop_reason=%s)", job.key, response.stop_reason)
            return None
        return ScoreResult(_clamp(response.parsed_output), False, response.model, usage)


class OpenAIScorer:
    def __init__(self, resume_text: str, model: str, effort: str, client: openai.OpenAI | None = None):
        self.client = client or openai.OpenAI()
        self.model = model
        self.effort = effort
        # OpenAI caches identical prompt prefixes (1024+ tokens) automatically.
        self.instructions = RUBRIC.format(resume=resume_text)

    def score(self, job: Job) -> ScoreResult | None:
        """Returns None on a transient error (the job is retried next run)."""
        kwargs = {"reasoning": {"effort": self.effort}} if self.effort else {}
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=self.instructions,
                input=job_prompt(job),
                text_format=FitScore,
                max_output_tokens=16000,
                store=False,  # don't keep the resume in OpenAI's stored responses
                prompt_cache_key="job-radar",
                **kwargs,
            )
        except openai.RateLimitError as e:
            raise RateLimited(str(e)) from e
        except openai.APIStatusError as e:
            log.error("API error scoring %s (%s): %s", job.key, e.status_code, e.message)
            return None
        except openai.APIConnectionError as e:
            log.error("connection error scoring %s: %s", job.key, e)
            return None

        u = response.usage
        cached = u.input_tokens_details.cached_tokens if u and u.input_tokens_details else 0
        usage = Usage(u.input_tokens - cached, u.output_tokens, cached, 0) if u else Usage()
        refused = any(
            c.type == "refusal" for item in response.output if item.type == "message" for c in item.content
        )
        if refused:
            log.warning("refused: %s %r", job.key, job.title)
            return ScoreResult(None, True, response.model, usage)
        if response.status == "incomplete" or response.output_parsed is None:
            log.error("no parsed output for %s (status=%s, %s)", job.key, response.status, response.incomplete_details)
            return None
        return ScoreResult(_clamp(response.output_parsed), False, response.model, usage)


SCORERS = {"anthropic": AnthropicScorer, "openai": OpenAIScorer}


def make_scorer(provider: str, resume_text: str, model: str, effort: str):
    if provider not in SCORERS:
        raise ValueError(f"unknown provider {provider!r}; use one of {sorted(SCORERS)}")
    return SCORERS[provider](resume_text, model, effort)
