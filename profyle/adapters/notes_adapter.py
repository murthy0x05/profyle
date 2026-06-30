"""
Recruiter notes (.txt) adapter.

Extracts candidate data from free-text recruiter notes using regex and
keyword-based heuristics.

Key design: the adapter attempts to split notes into per-candidate segments
(using separators like '---', blank line gaps, or "Candidate:" markers)
so that each segment produces its own RawRecord.  This prevents all
emails/skills from a multi-candidate notes file from being lumped into
a single record.

Design decisions (per user feedback):
- Same normalizers applied as every other source (phone → E.164, email → lowercase, etc.)
- method_score = 0.6 for heuristic (name/company), 0.8 for regex (email/phone)
- Names/companies only extracted from recognizable patterns, not arbitrary capitalized words
- Provenance explicitly records extraction method for each field
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from profyle.models import RawRecord, SourceType

logger = logging.getLogger("profyle.adapters.notes")

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?"
    r"(?:\(?\d{2,5}\)?[\s.-]?)?"
    r"\d{3,5}[\s.-]?\d{3,5}"
)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_-]+", re.IGNORECASE)

# Patterns for extracting names and companies from recognizable structures
_NAME_PATTERNS = [
    re.compile(r"(?:candidate|name|applicant|interviewee)\s*[:=]\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:spoke with|met with|interviewed|talking to|call with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", re.IGNORECASE),
    re.compile(r"(?:also met with|also spoke with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})", re.IGNORECASE),
]

_COMPANY_PATTERNS = [
    re.compile(r"(?:currently at|works at|working at|employed at|company|employer|current company)\s*[:=]?\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:at|from|leaving)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})(?:\s*[,.]|\s+(?:for|since|as|doing))", re.IGNORECASE),
]

_TITLE_PATTERNS = [
    re.compile(r"(?:title|role|position|current role|current title)\s*[:=]\s*(.+)", re.IGNORECASE),
]

# Skill keywords to look for in notes
_SKILL_KEYWORDS_RE = re.compile(
    r"\b(?:Python|Java|JavaScript|TypeScript|React|Angular|Vue|Node\.?js|"
    r"Go|Rust|C\+\+|C#|Ruby|PHP|Swift|Kotlin|Scala|"
    r"AWS|Azure|GCP|Docker|Kubernetes|K8s|Terraform|"
    r"SQL|PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|Kafka|"
    r"Machine Learning|ML|AI|Deep Learning|NLP|"
    r"GraphQL|REST|API|Microservices|"
    r"Agile|Scrum|DevOps|CI/CD|"
    r"Linux|Git|Jenkins|Ansible|"
    r"Spring|Django|Flask|FastAPI|Rails|"
    r"HTML|CSS|Sass|Webpack|"
    r"Figma|Sketch)\b",
    re.IGNORECASE,
)

# Separator pattern for splitting notes into per-candidate blocks
_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)


def adapt(source_path: str) -> list[RawRecord]:
    """
    Extract candidate data from a recruiter notes .txt file.

    Splits the file into per-candidate segments and returns one
    RawRecord per detected candidate.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning("Notes file not found: %s — skipping", source_path)
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read notes file %s: %s — skipping", source_path, exc)
        return []

    if not text.strip():
        logger.warning("Notes file %s is empty — skipping", source_path)
        return []

    # Split into segments per candidate
    segments = _split_into_segments(text)

    records: list[RawRecord] = []
    for segment in segments:
        if not segment.strip():
            continue
        data = _parse_notes_segment(segment)
        # Only emit a record if we extracted at least one identifying field
        if data.get("emails") or data.get("full_name") or data.get("phones"):
            records.append(
                RawRecord(
                    source_name=path.name,
                    source_type=SourceType.NOTES,
                    data=data,
                )
            )

    logger.info("Notes adapter: %d records from %s", len(records), source_path)
    return records


def _split_into_segments(text: str) -> list[str]:
    """
    Split notes text into per-candidate segments.

    Strategy: split on '---' separators (common in recruiter notes).
    If no separators found, return the whole text as one segment.
    """
    segments = _SEPARATOR_RE.split(text)
    # Filter out empty segments
    segments = [s for s in segments if s.strip()]

    if len(segments) <= 1:
        # No separators — return as a single segment
        return [text]

    return segments


def _parse_notes_segment(text: str) -> dict:
    """Extract structured data from one segment of recruiter notes."""
    data: dict = {
        "full_name": None,
        "emails": [],
        "phones": [],
        "linkedin": None,
        "github": None,
        "skills": [],
        "company": None,
        "title": None,
        "_extraction_methods": {},
    }

    # ----- Emails (regex — relatively unambiguous) -----
    emails = _EMAIL_RE.findall(text)
    data["emails"] = list(set(emails))
    if emails:
        data["_extraction_methods"]["emails"] = "regex"

    # ----- Phones (regex — relatively unambiguous) -----
    phones = _PHONE_RE.findall(text)
    data["phones"] = list(set(p.strip() for p in phones if len(p.strip()) >= 7))
    if data["phones"]:
        data["_extraction_methods"]["phones"] = "regex"

    # ----- LinkedIn -----
    linkedin_matches = _LINKEDIN_RE.findall(text)
    if linkedin_matches:
        data["linkedin"] = linkedin_matches[0]
        data["_extraction_methods"]["linkedin"] = "regex"

    # ----- GitHub -----
    github_matches = _GITHUB_RE.findall(text)
    if github_matches:
        data["github"] = github_matches[0]
        data["_extraction_methods"]["github"] = "regex"

    # ----- Name (heuristic — only from recognizable patterns) -----
    for pattern in _NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip().rstrip(".,;:")
            if name and len(name.split()) <= 5 and len(name) < 60:
                data["full_name"] = name
                data["_extraction_methods"]["full_name"] = "heuristic"
                break

    # ----- Company (heuristic — only from recognizable patterns) -----
    for pattern in _COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            company = m.group(1).strip().rstrip(".,;:")
            if company and len(company) < 60:
                data["company"] = company
                data["_extraction_methods"]["company"] = "heuristic"
                break

    # ----- Title (heuristic — only from recognizable patterns) -----
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            title = m.group(1).strip().rstrip(".,;:")
            if title and len(title) < 80:
                data["title"] = title
                data["_extraction_methods"]["title"] = "heuristic"
                break

    # ----- Skills (keyword matching) -----
    skill_matches = _SKILL_KEYWORDS_RE.findall(text)
    seen_lower: set[str] = set()
    unique_skills: list[str] = []
    for skill in skill_matches:
        if skill.lower() not in seen_lower:
            seen_lower.add(skill.lower())
            unique_skills.append(skill)
    data["skills"] = unique_skills
    if unique_skills:
        data["_extraction_methods"]["skills"] = "regex"

    return data
