"""
Tests for the normalisation functions.

Covers:
- Phone normalisation (E.164, with/without country code, invalid)
- Date normalisation (full date, year-only, month+year, garbage)
- Email lowercasing/trimming
- Skill alias resolution (mapped and unmapped)
- Country to ISO-3166 alpha-2
- Link/handle extraction
"""

import pytest

from profyle.normalizers import (
    extract_handle,
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_phone,
    normalize_skill,
)


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    def test_e164_with_country_code(self):
        result, conf = normalize_phone("+919876543210")
        assert result == "+919876543210"
        assert conf == 1.0

    def test_e164_us_number(self):
        result, conf = normalize_phone("+14155551234")
        assert result == "+14155551234"
        assert conf == 1.0

    def test_without_country_code_defaults_to_in(self):
        """Number without '+' should be parsed with IN region (confidence penalty)."""
        result, conf = normalize_phone("9876543210")
        assert result is not None
        assert result.startswith("+91")
        assert conf == 0.7

    def test_invalid_phone(self):
        result, conf = normalize_phone("INVALID_PHONE")
        assert result is None
        assert conf == 0.0

    def test_empty_phone(self):
        result, conf = normalize_phone("")
        assert result is None
        assert conf == 0.0

    def test_none_phone(self):
        result, conf = normalize_phone(None)
        assert result is None
        assert conf == 0.0

    def test_phone_with_spaces(self):
        result, conf = normalize_phone("+91 98765 43210")
        assert result == "+919876543210"
        assert conf == 1.0


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

class TestNormalizeDate:
    def test_full_date_yyyy_mm_dd(self):
        result, conf = normalize_date("2022-03-15")
        assert result == "2022-03"
        assert conf == 1.0

    def test_yyyy_mm(self):
        result, conf = normalize_date("2022-03")
        assert result == "2022-03"
        assert conf == 1.0

    def test_mm_yyyy(self):
        result, conf = normalize_date("03/2022")
        assert result == "2022-03"
        assert conf == 1.0

    def test_month_name_year(self):
        result, conf = normalize_date("January 2020")
        assert result == "2020-01"
        assert conf == 1.0

    def test_month_abbrev_year(self):
        result, conf = normalize_date("Mar 2022")
        assert result == "2022-03"
        assert conf == 1.0

    def test_year_only(self):
        """Year-only dates become YYYY-01 with confidence penalty."""
        result, conf = normalize_date("2018")
        assert result == "2018-01"
        assert conf == 0.7

    def test_garbage_date(self):
        result, conf = normalize_date("not-a-date")
        assert result is None
        assert conf == 0.0

    def test_empty_date(self):
        result, conf = normalize_date("")
        assert result is None
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Email normalisation
# ---------------------------------------------------------------------------

class TestNormalizeEmail:
    def test_basic_email(self):
        result, conf = normalize_email("User@Example.COM")
        assert result == "user@example.com"
        assert conf == 1.0

    def test_email_with_whitespace(self):
        result, conf = normalize_email("  alice@test.com  ")
        assert result == "alice@test.com"
        assert conf == 1.0

    def test_invalid_email(self):
        result, conf = normalize_email("not-an-email")
        assert result is None
        assert conf == 0.0

    def test_empty_email(self):
        result, conf = normalize_email("")
        assert result is None
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Skill normalisation
# ---------------------------------------------------------------------------

class TestNormalizeSkill:
    def test_mapped_skill(self):
        aliases = {"js": "JavaScript", "javascript": "JavaScript"}
        result, conf = normalize_skill("JS", aliases)
        assert result == "JavaScript"
        assert conf == 1.0

    def test_unmapped_skill(self):
        aliases = {"js": "JavaScript"}
        result, conf = normalize_skill("Elixir", aliases)
        assert result == "Elixir"
        assert conf == 0.7

    def test_case_insensitive_lookup(self):
        aliases = {"python": "Python"}
        result, conf = normalize_skill("PYTHON", aliases)
        assert result == "Python"
        assert conf == 1.0

    def test_empty_skill(self):
        result, conf = normalize_skill("", {})
        assert result is None
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Country normalisation
# ---------------------------------------------------------------------------

class TestNormalizeCountry:
    def test_full_name(self):
        result, conf = normalize_country("India")
        assert result == "IN"
        assert conf == 1.0

    def test_abbreviation(self):
        result, conf = normalize_country("US")
        assert result == "US"
        assert conf == 1.0

    def test_full_name_usa(self):
        result, conf = normalize_country("United States")
        assert result == "US"
        assert conf == 1.0

    def test_unknown_country(self):
        """Unknown countries pass through with lower confidence."""
        result, conf = normalize_country("Atlantis")
        assert result is not None
        assert conf == 0.7


# ---------------------------------------------------------------------------
# Link/handle extraction
# ---------------------------------------------------------------------------

class TestExtractHandle:
    def test_linkedin_url(self):
        result, conf = extract_handle("https://www.linkedin.com/in/priyasharma", "linkedin")
        assert result == "priyasharma"
        assert conf == 1.0

    def test_github_url(self):
        result, conf = extract_handle("https://github.com/priyasharma-dev", "github")
        assert result == "priyasharma-dev"
        assert conf == 1.0

    def test_bare_handle(self):
        result, conf = extract_handle("priyasharma-dev", "github")
        assert result == "priyasharma-dev"
        assert conf == 0.7

    def test_empty_url(self):
        result, conf = extract_handle("", "github")
        assert result is None
        assert conf == 0.0
