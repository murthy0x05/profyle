"""
Pydantic v2 models for the Profyle canonical schema, raw records, and output config.

The CanonicalRecord is the single internal representation — config-agnostic, never
reshaped directly.  Projection is handled separately in projection.py.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ExtractionMethod(str, Enum):
    """How a value was obtained from a source."""
    DIRECT_READ = "direct_read"
    REGEX = "regex"
    HEURISTIC = "heuristic"
    SECTION_NOT_DETECTED = "section_not_detected"


class SourceType(str, Enum):
    """Category of data source."""
    CSV = "csv"
    JSON_ATS = "json_ats"
    GITHUB = "github"
    RESUME = "resume"
    NOTES = "notes"


# ---------------------------------------------------------------------------
# Raw record (adapter output, loosely typed)
# ---------------------------------------------------------------------------

class RawRecord(BaseModel):
    """Loosely typed container emitted by each source adapter."""
    source_name: str  # e.g. "recruiter_export.csv"
    source_type: SourceType
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical sub-models
# ---------------------------------------------------------------------------

class Location(BaseModel):
    city: str | None = None
    region: str | None = None
    country: str | None = None  # ISO-3166 alpha-2


class Links(BaseModel):
    linkedin: str | None = None   # handle only, not full URL
    github: str | None = None     # handle only
    portfolio: str | None = None
    other: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    name: str
    confidence: float = 0.0
    sources: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    company: str | None = None
    title: str | None = None
    start: str | None = None   # YYYY-MM
    end: str | None = None     # YYYY-MM or None (current)
    summary: str | None = None


class Education(BaseModel):
    institution: str | None = None
    degree: str | None = None
    field: str | None = None
    end_year: int | None = None


class Provenance(BaseModel):
    """Tracks the origin and extraction method of every field value."""
    field: str
    source: str
    method: str  # ExtractionMethod value or custom string
    value: Any = None           # the value from this source (useful on conflicts)
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Canonical record (the single internal representation)
# ---------------------------------------------------------------------------

class CanonicalRecord(BaseModel):
    """
    The one-and-only internal profile model.  Config-agnostic — never
    reshaped directly.  Output shape is handled by the projection layer.
    """
    candidate_id: str | None = None
    full_name: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    location: Location | None = None
    links: Links | None = None
    headline: str | None = None
    years_experience: float | None = None
    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    overall_confidence: float | None = None

    # Internal bookkeeping — not part of the output schema
    _field_confidences: dict[str, float] = {}
    _identity_match_factor: float = 1.0


# ---------------------------------------------------------------------------
# Output config models (runtime projection)
# ---------------------------------------------------------------------------

class FieldSpec(BaseModel):
    """One field in the projection config."""
    path: str                                        # e.g. "skills[].name"
    type: str = "string"                              # string | number | array | object | boolean
    required: bool = False
    from_path: str | None = Field(default=None, alias="from")  # source path in canonical record
    normalize: str | None = None                      # optional per-field normalizer name

    model_config = {"populate_by_name": True}


class OutputConfig(BaseModel):
    """Runtime configuration for the projection layer."""
    fields: list[FieldSpec]
    include_confidence: bool = True
    on_missing: Literal["null", "omit", "error"] = "null"
