"""
Resume adapter for PDF and DOCX files.

Uses pdfplumber for PDF extraction and python-docx for DOCX.
Section detection is regex/heuristic-based with case-insensitive matching
and common variant aliases per section type.

Design decisions (per user feedback):
- Case-insensitive section headers with alias lists
- method_score = 0.8 for regex, 0.6 for heuristic/inferred
- Missing sections recorded in provenance as "section-not-detected"
- Structureless text → log warning, return nulls, continue run
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from profyle.models import RawRecord, SourceType

logger = logging.getLogger("profyle.adapters.resume")

# ---------------------------------------------------------------------------
# Section header aliases (case-insensitive)
# ---------------------------------------------------------------------------

_SECTION_ALIASES: dict[str, list[str]] = {
    "experience": [
        "experience", "work experience", "professional experience",
        "employment history", "work history", "career history",
        "professional background", "employment",
    ],
    "education": [
        "education", "academic background", "academic history",
        "qualifications", "academic qualifications", "educational background",
    ],
    "skills": [
        "skills", "technical skills", "core competencies",
        "competencies", "technologies", "tech stack",
        "areas of expertise", "proficiencies", "key skills",
    ],
    "summary": [
        "summary", "professional summary", "objective",
        "career objective", "profile", "about", "about me",
    ],
}

# Build a combined regex for detecting any section header
_ALL_HEADERS = []
for section, aliases in _SECTION_ALIASES.items():
    for alias in aliases:
        _ALL_HEADERS.append((alias, section))
# Sort by length descending so longer matches take priority
_ALL_HEADERS.sort(key=lambda x: len(x[0]), reverse=True)

# Regex to find section headers (line starts with header text, possibly followed by colon)
_HEADER_PATTERN = re.compile(
    r"^\s*(?:" +
    "|".join(re.escape(alias) for alias, _ in _ALL_HEADERS) +
    r")\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Common patterns
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?"  # optional country code
    r"(?:\(?\d{2,5}\)?[\s.-]?)?"  # optional area code
    r"\d{3,5}[\s.-]?\d{3,5}"  # core number
)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_-]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_-]+", re.IGNORECASE)


def adapt(source_path: str) -> list[RawRecord]:
    """
    Extract candidate data from a resume PDF or DOCX file.

    Returns a single-element list on success, empty list on error.
    """
    path = Path(source_path)
    if not path.exists():
        logger.warning("Resume file not found: %s — skipping", source_path)
        return []

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf_text(path)
    elif suffix in (".docx", ".doc"):
        text = _extract_docx_text(path)
    else:
        logger.warning("Unsupported resume format '%s' for %s — skipping", suffix, source_path)
        return []

    if not text or not text.strip():
        logger.warning("No text extracted from resume %s — skipping", source_path)
        return []

    data = _parse_resume_text(text, path.name)

    record = RawRecord(
        source_name=path.name,
        source_type=SourceType.RESUME,
        data=data,
    )
    logger.info("Resume adapter: extracted data from %s", source_path)
    return [record]


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(path: Path) -> str | None:
    """Extract text from a PDF using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            pages = []
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
            return "\n".join(pages) if pages else None
    except Exception as exc:
        logger.warning("Could not extract text from PDF %s: %s — skipping", path, exc)
        return None


def _extract_docx_text(path: Path) -> str | None:
    """Extract text from a DOCX using python-docx."""
    try:
        from docx import Document
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs) if paragraphs else None
    except Exception as exc:
        logger.warning("Could not extract text from DOCX %s: %s — skipping", path, exc)
        return None


# ---------------------------------------------------------------------------
# Resume text parsing
# ---------------------------------------------------------------------------

def _parse_resume_text(text: str, filename: str) -> dict:
    """Parse unstructured resume text into a structured dict."""
    data: dict = {
        "full_name": None,
        "emails": [],
        "phones": [],
        "linkedin": None,
        "github": None,
        "skills": [],
        "experience": [],
        "education": [],
        "headline": None,
        "_provenance_notes": [],  # internal: tracks what was/wasn't detected
    }

    # ----- Extract contact info from full text (regex — reliable) -----
    emails = _EMAIL_RE.findall(text)
    data["emails"] = list(set(emails))

    phones = _PHONE_RE.findall(text)
    # Filter out overly short matches
    data["phones"] = list(set(p.strip() for p in phones if len(p.strip()) >= 7))

    linkedin_matches = _LINKEDIN_RE.findall(text)
    if linkedin_matches:
        data["linkedin"] = linkedin_matches[0]

    github_matches = _GITHUB_RE.findall(text)
    if github_matches:
        data["github"] = github_matches[0]

    # ----- Name extraction (heuristic: first non-empty line) -----
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        first_line = lines[0]
        # If the first line is short and doesn't look like a section header, use as name
        if len(first_line) < 60 and not _is_section_header(first_line):
            # Strip any emails/phones from the name line
            candidate_name = _EMAIL_RE.sub("", first_line)
            candidate_name = _PHONE_RE.sub("", candidate_name)
            candidate_name = candidate_name.strip(" |·•–—-,")
            if candidate_name and len(candidate_name.split()) <= 5:
                data["full_name"] = candidate_name

    # ----- Section-based parsing -----
    sections = _split_into_sections(text)

    if not sections:
        logger.warning(
            "Resume %s has no detectable section headers — "
            "returning contact info only", filename,
        )
        data["_provenance_notes"].append("no-section-headers-detected")
        return data

    # Parse each section
    if "experience" in sections:
        data["experience"] = _parse_experience_section(sections["experience"])
    else:
        data["_provenance_notes"].append("section-not-detected:experience")

    if "education" in sections:
        data["education"] = _parse_education_section(sections["education"])
    else:
        data["_provenance_notes"].append("section-not-detected:education")

    if "skills" in sections:
        data["skills"] = _parse_skills_section(sections["skills"])
    else:
        data["_provenance_notes"].append("section-not-detected:skills")

    if "summary" in sections:
        data["headline"] = sections["summary"].strip()[:500]  # Cap length

    return data


def _is_section_header(line: str) -> bool:
    """Check if a line matches a known section header."""
    cleaned = line.strip().rstrip(":").strip().lower()
    for alias, _ in _ALL_HEADERS:
        if cleaned == alias:
            return True
    return False


def _identify_section(line: str) -> str | None:
    """Identify which section a header line belongs to."""
    cleaned = line.strip().rstrip(":").strip().lower()
    for alias, section in _ALL_HEADERS:
        if cleaned == alias:
            return section
    return None


def _split_into_sections(text: str) -> dict[str, str]:
    """
    Split resume text into sections based on detected headers.

    Returns a dict mapping section type → section content text.
    """
    lines = text.split("\n")
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines:
        section = _identify_section(line)
        if section:
            # Save previous section
            if current_section and current_lines:
                sections[current_section] = "\n".join(current_lines)
            current_section = section
            current_lines = []
        elif current_section:
            current_lines.append(line)

    # Save last section
    if current_section and current_lines:
        sections[current_section] = "\n".join(current_lines)

    return sections


def _parse_experience_section(text: str) -> list[dict]:
    """Parse an experience section into structured entries."""
    entries: list[dict] = []
    # Heuristic: look for company/title patterns
    # Common patterns: "Title at Company" or "Company — Title" or just bold/uppercase lines
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    current_entry: dict | None = None
    summary_lines: list[str] = []

    # Date patterns to detect experience entries
    date_pattern = re.compile(
        r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?\d{4}"
        r"\s*[-–—to]+\s*"
        r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?"
        r"(?:\d{4}|[Pp]resent|[Cc]urrent)",
        re.IGNORECASE,
    )
    date_single = re.compile(
        r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+)?\d{4}",
        re.IGNORECASE,
    )

    for line in lines:
        # Check if this line contains a date range → likely a new entry
        date_match = date_pattern.search(line)
        if date_match or (len(line) < 80 and not line.startswith(("•", "-", "·", "*"))):
            # Try to extract title/company
            if date_match:
                # Save previous entry
                if current_entry:
                    if summary_lines:
                        current_entry["summary"] = " ".join(summary_lines)
                    entries.append(current_entry)

                # Parse the date range
                date_text = date_match.group()
                dates = date_single.findall(date_text)
                start_date = dates[0] if dates else None
                end_raw = dates[1] if len(dates) > 1 else None
                if end_raw and end_raw.lower() in ("present", "current"):
                    end_raw = None

                # What's left after removing the date is title/company
                remainder = date_pattern.sub("", line).strip(" |·•–—-,")

                title, company = _split_title_company(remainder)
                current_entry = {
                    "company": company,
                    "title": title,
                    "start": start_date,
                    "end": end_raw,
                    "summary": None,
                }
                summary_lines = []
            else:
                # Could be a title/company line without a date
                if current_entry is None and not line.startswith(("•", "-", "·", "*")):
                    title, company = _split_title_company(line)
                    if title or company:
                        current_entry = {
                            "company": company,
                            "title": title,
                            "start": None,
                            "end": None,
                            "summary": None,
                        }
                        summary_lines = []
                    else:
                        if current_entry:
                            summary_lines.append(line)
                else:
                    if current_entry:
                        summary_lines.append(line)
        else:
            # Description/bullet line
            if current_entry:
                summary_lines.append(line.lstrip("•-·* "))

    # Save the last entry
    if current_entry:
        if summary_lines:
            current_entry["summary"] = " ".join(summary_lines)
        entries.append(current_entry)

    return entries


def _split_title_company(text: str) -> tuple[str | None, str | None]:
    """
    Split a line like 'Software Engineer at Google' or 'Google — Software Engineer'
    into (title, company).
    """
    if not text:
        return None, None

    # "Title at Company"
    m = re.match(r"^(.+?)\s+(?:at|@)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip() or None, m.group(2).strip() or None

    # "Company — Title" or "Company | Title" or "Company - Title"
    m = re.match(r"^(.+?)\s*[—–|]\s*(.+)$", text)
    if m:
        return m.group(2).strip() or None, m.group(1).strip() or None

    # "Company, Title" (less common, but possible)
    m = re.match(r"^(.+?)\s*,\s*(.+)$", text)
    if m:
        # Heuristic: if the first part is shorter, it's likely the company
        if len(m.group(1)) < len(m.group(2)):
            return m.group(2).strip() or None, m.group(1).strip() or None
        return m.group(1).strip() or None, m.group(2).strip() or None

    # Can't split — return as title
    return text.strip() or None, None


def _parse_education_section(text: str) -> list[dict]:
    """Parse an education section into structured entries."""
    entries: list[dict] = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Year pattern
    year_re = re.compile(r"\b(19|20)\d{2}\b")
    # Degree patterns
    degree_re = re.compile(
        r"\b(?:B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?A\.?|Ph\.?D\.?|MBA|"
        r"Bachelor|Master|Doctor|Associate|Diploma)\b",
        re.IGNORECASE,
    )

    current_entry: dict | None = None

    for line in lines:
        has_year = year_re.search(line)
        has_degree = degree_re.search(line)

        if has_degree or has_year:
            # Save previous
            if current_entry:
                entries.append(current_entry)

            end_year = None
            if has_year:
                # Take the last year mentioned
                years = year_re.findall(line)
                all_years = [int(f"{century}{y}") if len(y) == 2 else int(y) for century, y in [(yr[:2], yr[2:]) for yr in years]]
                # Actually the years returned are full strings
                all_years_int = [int(y) for y in year_re.findall(line)]
                end_year = max(all_years_int) if all_years_int else None

            degree_match = degree_re.search(line)
            degree = degree_match.group() if degree_match else None

            # Try to extract institution and field
            # Remove year and degree to find institution/field
            remainder = year_re.sub("", line)
            if degree:
                remainder = remainder.replace(degree, "", 1)
            remainder = remainder.strip(" ,|·•–—-")

            institution = None
            field = None
            if remainder:
                # If there's a comma or dash, split into institution and field
                parts = re.split(r"[,|–—-]", remainder, maxsplit=1)
                institution = parts[0].strip() or None
                if len(parts) > 1:
                    field = parts[1].strip() or None

            current_entry = {
                "institution": institution,
                "degree": degree,
                "field": field,
                "end_year": end_year,
            }
        elif current_entry and line:
            # Additional info for current entry
            if not current_entry.get("field") and len(line) < 60:
                current_entry["field"] = line

    if current_entry:
        entries.append(current_entry)

    return entries


def _parse_skills_section(text: str) -> list[str]:
    """Parse a skills section into a list of skill strings."""
    skills: list[str] = []

    # Split on commas, pipes, bullets, newlines
    raw_items = re.split(r"[,|•·\n]", text)
    for item in raw_items:
        cleaned = item.strip(" \t-*:")
        # Filter out obviously non-skill text (too long, too short)
        if cleaned and 1 < len(cleaned) < 50:
            skills.append(cleaned)

    return list(dict.fromkeys(skills))  # Deduplicate, preserve order
