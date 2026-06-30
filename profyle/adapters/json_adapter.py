"""
ATS JSON blob adapter.

Reads a JSON file that contains either a single candidate object or a list
of candidate objects.  Gracefully handles malformed JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from profyle.models import RawRecord, SourceType

logger = logging.getLogger("profyle.adapters.json_ats")


def adapt(source_path: str) -> list[RawRecord]:
    """
    Ingest an ATS JSON blob and return a list of RawRecords.

    Returns an empty list on any file-level error.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning("JSON file not found: %s — skipping", source_path)
        return []

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not parse JSON file %s: %s — skipping", source_path, exc)
        return []

    # Accept both a single object and a list of objects
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        logger.warning("JSON file %s has unexpected top-level type: %s — skipping", source_path, type(raw).__name__)
        return []

    records: list[RawRecord] = []
    for idx, entry in enumerate(candidates):
        if not isinstance(entry, dict):
            logger.warning("JSON %s entry %d is not an object — skipping", source_path, idx)
            continue
        try:
            data = _parse_entry(entry)
            records.append(
                RawRecord(
                    source_name=path.name,
                    source_type=SourceType.JSON_ATS,
                    data=data,
                )
            )
        except Exception as exc:
            logger.warning("JSON %s entry %d skipped: %s", source_path, idx, exc)

    logger.info("JSON adapter: %d records from %s", len(records), source_path)
    return records


def _safe_get(entry: dict, *keys: str):
    """Walk nested keys, returning None if any key is missing."""
    current = entry
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _parse_entry(entry: dict) -> dict:
    """Extract structured fields from one ATS JSON object."""
    # Skills can be a list of strings or a list of dicts with 'name' key
    raw_skills = entry.get("skills", [])
    skills: list[str] = []
    if isinstance(raw_skills, list):
        for s in raw_skills:
            if isinstance(s, str) and s.strip():
                skills.append(s.strip())
            elif isinstance(s, dict) and s.get("name"):
                skills.append(str(s["name"]).strip())

    # Experience
    raw_experience = entry.get("experience", entry.get("work_history", []))
    experience: list[dict] = []
    if isinstance(raw_experience, list):
        for exp in raw_experience:
            if isinstance(exp, dict):
                experience.append({
                    "company": exp.get("company") or exp.get("organization"),
                    "title": exp.get("title") or exp.get("position") or exp.get("role"),
                    "start": exp.get("start") or exp.get("start_date"),
                    "end": exp.get("end") or exp.get("end_date"),
                    "summary": exp.get("summary") or exp.get("description"),
                })

    # Education
    raw_education = entry.get("education", [])
    education: list[dict] = []
    if isinstance(raw_education, list):
        for edu in raw_education:
            if isinstance(edu, dict):
                end_year = edu.get("end_year") or edu.get("graduation_year") or edu.get("year")
                if end_year is not None:
                    try:
                        end_year = int(end_year)
                    except (ValueError, TypeError):
                        end_year = None
                education.append({
                    "institution": edu.get("institution") or edu.get("school") or edu.get("university"),
                    "degree": edu.get("degree"),
                    "field": edu.get("field") or edu.get("major") or edu.get("field_of_study"),
                    "end_year": end_year,
                })

    # Location
    location = {}
    loc = entry.get("location")
    if isinstance(loc, dict):
        location = {
            "city": loc.get("city"),
            "region": loc.get("region") or loc.get("state"),
            "country": loc.get("country") or loc.get("country_code"),
        }
    elif isinstance(loc, str) and loc.strip():
        parts = [p.strip() for p in loc.split(",")]
        if len(parts) >= 3:
            location = {"city": parts[0], "region": parts[1], "country": parts[2]}
        elif len(parts) == 2:
            location = {"city": parts[0], "country": parts[1]}
        else:
            location = {"city": parts[0]}

    # Links
    linkedin = entry.get("linkedin") or entry.get("linkedin_url") or _safe_get(entry, "links", "linkedin")
    github = entry.get("github") or entry.get("github_url") or _safe_get(entry, "links", "github")
    portfolio = entry.get("portfolio") or entry.get("website") or _safe_get(entry, "links", "portfolio")

    # Emails — can be string or list
    raw_email = entry.get("email") or entry.get("emails")
    emails: list[str] = []
    if isinstance(raw_email, str) and raw_email.strip():
        emails = [raw_email.strip()]
    elif isinstance(raw_email, list):
        emails = [e.strip() for e in raw_email if isinstance(e, str) and e.strip()]

    # Phones — can be string or list
    raw_phone = entry.get("phone") or entry.get("phones") or entry.get("phone_number")
    phones: list[str] = []
    if isinstance(raw_phone, str) and raw_phone.strip():
        phones = [raw_phone.strip()]
    elif isinstance(raw_phone, list):
        phones = [p.strip() for p in raw_phone if isinstance(p, str) and p.strip()]

    # Years of experience
    yoe = entry.get("years_experience") or entry.get("experience_years") or entry.get("yoe")
    if yoe is not None:
        try:
            yoe = float(yoe)
        except (ValueError, TypeError):
            yoe = None

    return {
        "full_name": entry.get("name") or entry.get("full_name") or entry.get("candidate_name"),
        "emails": emails,
        "phones": phones,
        "title": entry.get("title") or entry.get("current_title") or entry.get("job_title"),
        "company": entry.get("company") or entry.get("current_company"),
        "location": location,
        "linkedin": linkedin,
        "github": github,
        "portfolio": portfolio,
        "skills": skills,
        "experience": experience,
        "education": education,
        "years_experience": yoe,
        "headline": entry.get("headline") or entry.get("summary") or entry.get("bio"),
    }
