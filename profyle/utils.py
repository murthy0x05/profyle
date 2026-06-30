"""
Shared utility functions for hashing, logging, and file I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("profyle")


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the pipeline run."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def deterministic_id(*identifiers: str) -> str:
    """
    Generate a deterministic candidate ID from a set of matching identifiers.

    Sorts the identifiers, hashes them with SHA-256, and returns the first
    16 hex characters.  Same inputs → same ID, always.
    """
    sorted_ids = sorted(str(i).strip().lower() for i in identifiers if i)
    combined = "|".join(sorted_ids)
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def load_skill_aliases(path: str | Path | None = None) -> dict[str, str]:
    """
    Load the skill alias dictionary.  Falls back to the bundled default
    if no path is given.
    """
    if path is None:
        # Default location relative to the project root
        path = Path(__file__).resolve().parent.parent / "data" / "skill_aliases.json"
    path = Path(path)
    if not path.exists():
        logger.warning("Skill alias file not found at %s — using empty map", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw: dict = json.load(f)
        # Normalise keys to lowercase for case-insensitive lookup
        return {k.lower(): v for k, v in raw.items()}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load skill aliases from %s: %s", path, exc)
        return {}


def safe_read_file(path: str | Path) -> str | None:
    """Read a text file, returning None on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read file %s: %s", path, exc)
        return None


def get_github_token() -> str | None:
    """Read GITHUB_TOKEN from the environment, if set."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        logger.debug("GITHUB_TOKEN found in environment")
    return token
