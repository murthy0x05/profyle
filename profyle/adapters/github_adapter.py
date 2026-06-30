"""
GitHub profile adapter via the public REST API.

Fetches user profile + repos/languages and extracts candidate data.
Supports optional GITHUB_TOKEN env var for higher rate limits.

Error handling:
- 404 → user not found, returns []
- 403 → primary rate limit exceeded, logs warning, returns []
- 429 → secondary rate limit (abuse detection), logs different warning, returns []
- Proactively checks X-RateLimit-Remaining header
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urlparse

import requests

from profyle.adapters.notes_adapter import (
    _EMAIL_RE,
    _LINKEDIN_RE,
    _PHONE_RE,
    _SKILL_KEYWORDS_RE,
)
from profyle.models import RawRecord, SourceType
from profyle.utils import get_github_token

logger = logging.getLogger("profyle.adapters.github")

API_BASE = "https://api.github.com"
REQUEST_TIMEOUT = 15  # seconds


def adapt(source: str) -> list[RawRecord]:
    """
    Fetch a GitHub user profile, their repo languages, and Profile README.

    *source* can be:
    - A GitHub username (e.g. "octocat")
    - A GitHub URL (e.g. "https://github.com/octocat")

    Returns a single-element list on success, empty list on error.
    """
    username = _extract_username(source)
    if not username:
        logger.warning("Could not extract GitHub username from '%s' — skipping", source)
        return []

    headers = _build_headers()

    # ----- Fetch user profile -----
    profile = _api_get(f"{API_BASE}/users/{username}", headers)
    if profile is None:
        return []

    # ----- Fetch repos for language data -----
    repos = _api_get(f"{API_BASE}/users/{username}/repos?per_page=100&sort=updated", headers)
    languages: list[str] = []
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, dict):
                lang = repo.get("language")
                if lang and isinstance(lang, str):
                    languages.append(lang)

    # ----- Fetch Profile README -----
    readme_text = ""
    readme_resp = _api_get(f"{API_BASE}/repos/{username}/{username}/readme", headers)
    if readme_resp and isinstance(readme_resp, dict):
        content_b64 = readme_resp.get("content", "")
        try:
            readme_text = base64.b64decode(content_b64).decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            logger.warning("Could not decode GitHub README for %s: %s", username, exc)

    # ----- Parse README data -----
    emails = []
    phones = []
    linkedin = None
    if readme_text:
        emails = _EMAIL_RE.findall(readme_text)
        phones = [p.strip() for p in _PHONE_RE.findall(readme_text) if len(p.strip()) >= 7]
        
        linkedin_matches = _LINKEDIN_RE.findall(readme_text)
        if linkedin_matches:
            linkedin = linkedin_matches[0]
            
        # Add skills from README to languages
        skill_matches = _SKILL_KEYWORDS_RE.findall(readme_text)
        languages.extend(skill_matches)

    # Deduplicate skills while preserving order
    seen: set[str] = set()
    unique_skills: list[str] = []
    for lang in languages:
        if lang.lower() not in seen:
            seen.add(lang.lower())
            unique_skills.append(lang)

    # Check if the public email is set, add to emails list
    if profile.get("email"):
        emails.append(profile.get("email"))

    # ----- Build raw record -----
    data: dict = {
        "full_name": profile.get("name"),
        "emails": list(set(emails)),
        "phones": list(set(phones)),
        "linkedin": linkedin,
        "headline": profile.get("bio"),
        "github": username,
        "portfolio": profile.get("blog") if profile.get("blog") else None,
        "skills": unique_skills,
        "location_raw": profile.get("location"),
        "html_url": profile.get("html_url"),
        "company": _clean_company(profile.get("company")),
    }

    # Parse location string (e.g. "San Francisco, CA" or "Bangalore, India")
    location = {}
    loc_str = profile.get("location")
    if loc_str and isinstance(loc_str, str):
        parts = [p.strip() for p in loc_str.split(",")]
        if len(parts) >= 2:
            location = {"city": parts[0], "country": parts[-1]}
        else:
            location = {"city": parts[0]}
    data["location"] = location

    record = RawRecord(
        source_name=f"github:{username}",
        source_type=SourceType.GITHUB,
        data=data,
    )
    logger.info("GitHub adapter: fetched profile for '%s'", username)
    return [record]


def _extract_username(source: str) -> str | None:
    """Extract a GitHub username from a URL or bare string."""
    source = source.strip()
    if not source:
        return None

    # Try to parse as URL
    if "github.com" in source.lower():
        m = re.search(r"github\.com/([A-Za-z0-9_-]+)", source)
        if m:
            return m.group(1)

    # Bare username — no spaces, no slashes (except leading)
    cleaned = source.strip("/")
    if re.match(r"^[A-Za-z0-9_-]+$", cleaned):
        return cleaned

    return None


def _build_headers() -> dict[str, str]:
    """Build request headers, optionally with auth token."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_get(url: str, headers: dict[str, str]):
    """
    Make a GET request to the GitHub API with error handling.

    Returns parsed JSON on success, None on error.
    """
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("GitHub API request failed for %s: %s — skipping", url, exc)
        return None

    # Check rate limit headers proactively
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        try:
            remaining_int = int(remaining)
            if remaining_int <= 5:
                logger.warning(
                    "GitHub rate limit almost exhausted (%d remaining). "
                    "Set GITHUB_TOKEN for higher limits.",
                    remaining_int,
                )
        except ValueError:
            pass

    if resp.status_code == 200:
        return resp.json()

    if resp.status_code == 404:
        logger.warning("GitHub user not found (404) for %s — skipping", url)
        return None

    if resp.status_code == 403:
        logger.warning(
            "GitHub primary rate limit exceeded (403) for %s — "
            "hourly quota used up. Set GITHUB_TOKEN for higher limits. Skipping.",
            url,
        )
        return None

    if resp.status_code == 429:
        logger.warning(
            "GitHub secondary rate limit hit (429) for %s — "
            "abuse detection triggered, too many concurrent requests. Skipping.",
            url,
        )
        return None

    logger.warning(
        "GitHub API returned %d for %s — skipping", resp.status_code, url,
    )
    return None


def _clean_company(company: str | None) -> str | None:
    """Remove leading '@' from GitHub company field."""
    if company and isinstance(company, str):
        return company.lstrip("@").strip() or None
    return None
