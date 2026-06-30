"""
Recruiter CSV export adapter.

Reads a CSV file where each row is a candidate.  Skips malformed rows
rather than crashing the entire run.

Expected columns (case-insensitive, flexible naming):
    name, email, phone, title, company, location, linkedin, github, skills

Any missing column → the corresponding field is None.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from profyle.models import RawRecord, SourceType

logger = logging.getLogger("profyle.adapters.csv")


def adapt(source_path: str) -> list[RawRecord]:
    """
    Ingest a recruiter CSV export and return a list of RawRecords.

    Errors on individual rows are logged and skipped; file-level errors
    return an empty list.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning("CSV file not found: %s — skipping", source_path)
        return []

    records: list[RawRecord] = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                logger.warning("CSV file %s has no header row — skipping", source_path)
                return []

            # Normalise header names to lowercase for flexible matching
            field_map = {name.strip().lower(): name for name in reader.fieldnames}

            for row_num, row in enumerate(reader, start=2):  # row 1 is header
                try:
                    data = _parse_row(row, field_map)
                    records.append(
                        RawRecord(
                            source_name=path.name,
                            source_type=SourceType.CSV,
                            data=data,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "CSV %s row %d skipped: %s", source_path, row_num, exc,
                    )
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read CSV file %s: %s — skipping", source_path, exc)
    except csv.Error as exc:
        logger.warning("CSV parsing error in %s: %s — skipping", source_path, exc)

    logger.info("CSV adapter: %d records from %s", len(records), source_path)
    return records


def _get(row: dict, field_map: dict[str, str], *keys: str) -> str | None:
    """Look up the first matching key (case-insensitive) in the row."""
    for key in keys:
        header = field_map.get(key.lower())
        if header and row.get(header, "").strip():
            return row[header].strip()
    return None


def _parse_row(row: dict, field_map: dict[str, str]) -> dict:
    """Extract structured fields from one CSV row."""
    skills_raw = _get(row, field_map, "skills", "skill", "technologies")
    skills = [s.strip() for s in skills_raw.split(",") if s.strip()] if skills_raw else []

    location_raw = _get(row, field_map, "location", "city", "address")

    # Try to split "City, Region, Country" style locations
    location = {}
    if location_raw:
        parts = [p.strip() for p in location_raw.split(",")]
        if len(parts) >= 3:
            location = {"city": parts[0], "region": parts[1], "country": parts[2]}
        elif len(parts) == 2:
            location = {"city": parts[0], "country": parts[1]}
        else:
            location = {"city": parts[0]}

    return {
        "full_name": _get(row, field_map, "name", "full_name", "candidate_name", "full name"),
        "email": _get(row, field_map, "email", "email_address", "e-mail", "email address"),
        "phone": _get(row, field_map, "phone", "phone_number", "mobile", "telephone", "phone number"),
        "title": _get(row, field_map, "title", "job_title", "position", "current_title", "job title", "current title"),
        "company": _get(row, field_map, "company", "current_company", "employer", "organization", "current company"),
        "location": location,
        "linkedin": _get(row, field_map, "linkedin", "linkedin_url", "linkedin url"),
        "github": _get(row, field_map, "github", "github_url", "github url"),
        "skills": skills,
        "years_experience": _get(row, field_map, "years_experience", "experience_years", "yoe", "years experience", "experience years"),
        "headline": _get(row, field_map, "headline", "summary", "bio"),
    }
