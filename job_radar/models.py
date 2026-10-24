from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class Job:
    source: str  # greenhouse | lever | ashby | workday | phenom | gamejobs | linkedin
    company: str  # board slug (company name for aggregators)
    ext_id: str
    title: str
    location: str | None  # None = unknown until the job is hydrated
    remote: bool
    url: str
    posted_at: datetime | None
    description: str
    comp: str | None = None
    gaming: bool = False  # set from config [gaming]; not stored
    apply_url: str = ""  # original application link, when the source is an aggregator; not stored
    teaser: str = ""  # short description fallback; not stored
    board: str = ""  # 'source:slug' this job was fetched from (for aggregators, slug = the query); not stored

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.ext_id}"


# Description stored for an email-alert job whose full posting couldn't be found.
LIMITED_INFO = (
    "No job description available. This job came from a job-alert email, which only gives the title, "
    "company, location and sometimes pay, and the posting wasn't found on a public job board. "
    "Score it from those details and keep skill lists to what the title clearly implies."
)

CATEGORIES = (
    "SOC / Security Operations", "Detection & Response", "Incident Response", "Security Engineering",
    "Security Automation", "Application / Product Security", "Cloud / Infrastructure Security",
    "Threat Intelligence", "Vulnerability Management", "Offensive Security / Red Team", "GRC / Risk & Compliance",
    "Trust & Safety / Fraud", "Game Security / Anti-Cheat", "Physical Security", "Other",
)
Category = Literal[CATEGORIES]


class FitScore(BaseModel):
    """What Claude returns for one job. Field descriptions go into the JSON schema."""

    fit_score: int = Field(description="0-100. How well the candidate's background matches and how realistic the job is for them.")
    category: Category = Field(description="The kind of security job this is, judged from the duties, not just the title.")
    seniority_fit: Literal["good", "stretch", "too_senior"] = Field(
        description="good = new grad / entry level; stretch = asks for ~2-3 years; too_senior = 4+ years or senior scope."
    )
    matched_skills: list[str] = Field(description="Requirements the resume clearly covers, short phrases.")
    missing_skills: list[str] = Field(description="Requirements the resume does not show, short phrases.")
    red_flags: list[str] = Field(
        description="Dealbreakers or concerns: years required, clearance, cert required, shift work, on-call, non-US, etc."
    )
    shift_or_oncall: bool = Field(description="True if the role mentions shift work, 24/7 coverage, or on-call rotation.")
    why: str = Field(description="1-2 sentences explaining the score, written to the candidate.")
    min_qualifications: list[str] = Field(
        description="The posting's minimum / required / basic qualifications, one per item, in the posting's own words. "
        "Empty if the posting doesn't list them."
    )
    preferred_qualifications: list[str] = Field(
        description="The posting's preferred / nice-to-have qualifications, one per item, in the posting's own words. "
        "Empty if the posting doesn't list them."
    )
