"""
Normalization functions for the Profyle pipeline.

Each normalizer is a pure function returning (normalized_value, confidence_factor).
The confidence_factor is the normalization_factor component of the per-field
confidence formula:  field_confidence = method_score × normalization_factor × agreement_factor

Rules
-----
- Phones → E.164 via phonenumbers; country-guessed → 0.7 confidence
- Dates → YYYY-MM; year-only → YYYY-01 with 0.7 confidence
- Country → ISO-3166 alpha-2
- Email → lowercased + trimmed
- Skills → canonical name via alias dict; unmapped → 0.7 confidence
- Links → extract bare handle/username
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import phonenumbers
from phonenumbers import PhoneNumberFormat

logger = logging.getLogger("profyle.normalizers")

# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

def normalize_phone(raw: str | None, default_region: str = "IN") -> tuple[str | None, float]:
    """
    Parse a phone string into E.164 format.

    Returns (e164_string, normalization_factor).
    - 1.0 if the number parsed cleanly with an explicit country code
    - 0.7 if the country code was guessed via *default_region*
    - (None, 0.0) on failure
    """
    if not raw or not raw.strip():
        return None, 0.0
    raw = raw.strip()

    # Try parsing with no default region first (works if number has '+' prefix)
    try:
        parsed = phonenumbers.parse(raw, None)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, PhoneNumberFormat.E164), 1.0
    except phonenumbers.NumberParseException:
        pass

    # Fall back to default region — apply confidence penalty
    try:
        parsed = phonenumbers.parse(raw, default_region)
        if phonenumbers.is_valid_number(parsed):
            e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
            logger.debug(
                "Phone '%s' parsed with assumed region %s → %s (confidence penalty)",
                raw, default_region, e164,
            )
            return e164, 0.7
    except phonenumbers.NumberParseException:
        pass

    # Could not parse at all
    logger.debug("Phone '%s' could not be parsed — dropping", raw)
    return None, 0.0


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------

_DATE_PATTERNS: list[tuple[re.Pattern, str, float]] = [
    # YYYY-MM-DD or YYYY/MM/DD
    (re.compile(r"^(\d{4})[-/](\d{1,2})[-/]\d{1,2}$"), "{}-{:02d}", 1.0),
    # YYYY-MM or YYYY/MM
    (re.compile(r"^(\d{4})[-/](\d{1,2})$"), "{}-{:02d}", 1.0),
    # MM/YYYY or MM-YYYY
    (re.compile(r"^(\d{1,2})[-/](\d{4})$"), "{}-{:02d}", 1.0),
    # Month YYYY (e.g. "January 2020", "Jan 2020")
    (re.compile(r"^([A-Za-z]+)\s+(\d{4})$"), None, 1.0),
    # YYYY only
    (re.compile(r"^(\d{4})$"), "{}-01", 0.7),
]

_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def normalize_date(raw: str | None) -> tuple[str | None, float]:
    """
    Normalize a date string to YYYY-MM format.

    Returns (normalized, normalization_factor).
    - 1.0 for a full month+year parse
    - 0.7 for year-only (becomes YYYY-01)
    - (None, 0.0) on failure
    """
    if not raw or not raw.strip():
        return None, 0.0
    raw = raw.strip()

    # YYYY-MM-DD or YYYY/MM/DD
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/]\d{1,2}$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}", 1.0

    # YYYY-MM or YYYY/MM
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}", 1.0

    # MM/YYYY or MM-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", raw)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}", 1.0

    # Month YYYY (e.g. "January 2020", "Jan 2020")
    m = re.match(r"^([A-Za-z]+)\s+(\d{4})$", raw)
    if m:
        month_str = m.group(1).lower()
        month_num = _MONTH_MAP.get(month_str)
        if month_num:
            return f"{m.group(2)}-{month_num:02d}", 1.0

    # YYYY only → YYYY-01 with penalty
    m = re.match(r"^(\d{4})$", raw)
    if m:
        logger.debug("Date '%s' is year-only → %s-01 (confidence penalty)", raw, m.group(1))
        return f"{m.group(1)}-01", 0.7

    logger.debug("Date '%s' could not be parsed — dropping", raw)
    return None, 0.0


# ---------------------------------------------------------------------------
# Country → ISO-3166 alpha-2
# ---------------------------------------------------------------------------

# Common country name → alpha-2 mapping (extend as needed)
_COUNTRY_MAP: dict[str, str] = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "u.s.a.": "US",
    "united states of america": "US",
    "india": "IN", "in": "IN",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "canada": "CA", "ca": "CA",
    "australia": "AU", "au": "AU",
    "germany": "DE", "de": "DE", "deutschland": "DE",
    "france": "FR", "fr": "FR",
    "japan": "JP", "jp": "JP",
    "china": "CN", "cn": "CN",
    "brazil": "BR", "br": "BR",
    "singapore": "SG", "sg": "SG",
    "netherlands": "NL", "nl": "NL", "holland": "NL",
    "ireland": "IE", "ie": "IE",
    "israel": "IL", "il": "IL",
    "south korea": "KR", "korea": "KR", "kr": "KR",
    "sweden": "SE", "se": "SE",
    "switzerland": "CH", "ch": "CH",
    "spain": "ES", "es": "ES",
    "italy": "IT", "it": "IT",
    "mexico": "MX", "mx": "MX",
    "new zealand": "NZ", "nz": "NZ",
    "portugal": "PT", "pt": "PT",
    "poland": "PL", "pl": "PL",
    "russia": "RU", "ru": "RU",
    "south africa": "ZA", "za": "ZA",
    "uae": "AE", "united arab emirates": "AE", "ae": "AE",
}


def normalize_country(raw: str | None) -> tuple[str | None, float]:
    """
    Map a country name/code to ISO-3166 alpha-2.

    Returns (alpha2, normalization_factor).
    - 1.0 for exact match
    - 0.7 for no match (passes through as-is, uppercased)
    """
    if not raw or not raw.strip():
        return None, 0.0
    cleaned = raw.strip().lower()

    # Check if already a valid 2-letter code
    if len(cleaned) == 2 and cleaned.upper() in {v for v in _COUNTRY_MAP.values()}:
        return cleaned.upper(), 1.0

    mapped = _COUNTRY_MAP.get(cleaned)
    if mapped:
        return mapped, 1.0

    # Pass through uppercased — unknown country
    logger.debug("Country '%s' not in lookup table — passing through", raw)
    return raw.strip().upper()[:2] if len(raw.strip()) >= 2 else raw.strip().upper(), 0.7


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def normalize_email(raw: str | None) -> tuple[str | None, float]:
    """Lowercase and trim an email address."""
    if not raw or not raw.strip():
        return None, 0.0
    cleaned = raw.strip().lower()
    # Basic email sanity check
    if "@" in cleaned and "." in cleaned.split("@")[-1]:
        return cleaned, 1.0
    logger.debug("Email '%s' failed basic validation — dropping", raw)
    return None, 0.0


# ---------------------------------------------------------------------------
# Skill normalization
# ---------------------------------------------------------------------------

def normalize_skill(raw: str | None, alias_dict: dict[str, str] | None = None) -> tuple[str | None, float]:
    """
    Map a skill name to its canonical form via the alias dictionary.

    Returns (canonical_name, normalization_factor).
    - 1.0 if the skill mapped to a known canonical name
    - 0.7 if unmapped (passes through as-is with title casing)
    """
    if not raw or not raw.strip():
        return None, 0.0
    cleaned = raw.strip()
    lookup_key = cleaned.lower()

    if alias_dict and lookup_key in alias_dict:
        return alias_dict[lookup_key], 1.0

    # Pass through as-is (keep original casing if it looks intentional)
    return cleaned, 0.7


# ---------------------------------------------------------------------------
# Link / handle extraction
# ---------------------------------------------------------------------------

_LINKEDIN_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/([^/?#]+)", re.IGNORECASE),
]

_GITHUB_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/?#]+)", re.IGNORECASE),
]


def extract_handle(url: str | None, platform: str = "generic") -> tuple[str | None, float]:
    """
    Extract a bare handle/username from a URL.

    Parameters
    ----------
    url : str | None
        The URL or handle string.
    platform : str
        One of 'linkedin', 'github', or 'generic'.

    Returns (handle, normalization_factor).
    """
    if not url or not url.strip():
        return None, 0.0
    url = url.strip()

    if platform == "linkedin":
        for pat in _LINKEDIN_PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1).lower(), 1.0
        # Might already be a bare handle
        if "/" not in url and "." not in url:
            return url.lower(), 0.7
        return url, 0.7

    if platform == "github":
        for pat in _GITHUB_PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1).lower(), 1.0
        # Might already be a bare handle
        if "/" not in url and "." not in url:
            return url.lower(), 0.7
        return url, 0.7

    # Generic — return as-is
    return url.strip(), 1.0
