"""
Pipeline orchestrator — the full Ingest→Adapt→Normalize→Resolve→Merge→Project→Validate→Emit flow.

Each stage is a pure function with no knowledge of adjacent stages beyond its
defined input/output type.  Each adapter is wrapped in isolated error handling.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from profyle.adapters import csv_adapter, github_adapter, json_adapter, notes_adapter, resume_adapter
from profyle.identity import resolve
from profyle.merger import merge_group
from profyle.models import (
    CanonicalRecord,
    Education,
    Experience,
    ExtractionMethod,
    Links,
    Location,
    OutputConfig,
    Provenance,
    RawRecord,
    Skill,
    SourceType,
)
from profyle.normalizers import (
    extract_handle,
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_phone,
    normalize_skill,
)
from profyle.projection import ProjectionError, project
from profyle.utils import load_skill_aliases

logger = logging.getLogger("profyle.pipeline")


# ---------------------------------------------------------------------------
# Stage 1: Ingest — detect source types and dispatch to adapters
# ---------------------------------------------------------------------------

def ingest(sources: list[str]) -> list[RawRecord]:
    """
    Ingest all sources, dispatching each to the appropriate adapter.

    Source type is auto-detected by file extension or URL pattern.
    Each adapter is called in isolation — one failure never crashes the run.
    """
    all_records: list[RawRecord] = []

    for source in sources:
        source = source.strip()
        if not source:
            continue

        try:
            records = _dispatch_adapter(source)
            all_records.extend(records)
        except Exception as exc:
            logger.error(
                "Unexpected error ingesting source '%s': %s — skipping",
                source, exc,
            )

    logger.info("Ingestion complete: %d raw records from %d sources", len(all_records), len(sources))
    return all_records


def _dispatch_adapter(source: str) -> list[RawRecord]:
    """Detect source type and call the appropriate adapter."""
    source_lower = source.lower().strip()

    # GitHub URL or username
    if "github.com/" in source_lower or (
        not Path(source).suffix and not Path(source).exists()
        and source_lower.replace("-", "").replace("_", "").isalnum()
    ):
        logger.info("Detected GitHub source: %s", source)
        return github_adapter.adapt(source)

    path = Path(source)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        logger.info("Detected CSV source: %s", source)
        return csv_adapter.adapt(source)

    if suffix == ".json":
        logger.info("Detected JSON source: %s", source)
        return json_adapter.adapt(source)

    if suffix in (".pdf", ".docx", ".doc"):
        logger.info("Detected resume source: %s", source)
        return resume_adapter.adapt(source)

    if suffix == ".txt":
        logger.info("Detected notes source: %s", source)
        return notes_adapter.adapt(source)

    logger.warning("Unknown source type for '%s' — skipping", source)
    return []


# ---------------------------------------------------------------------------
# Stage 2 & 3: Adapt + Normalize — convert RawRecords to CanonicalRecords
# ---------------------------------------------------------------------------

def adapt_and_normalize(raw_records: list[RawRecord]) -> list[CanonicalRecord]:
    """
    Convert raw records to normalised canonical records.

    Each raw record is independently processed — errors in one don't
    affect others.
    """
    skill_aliases = load_skill_aliases()
    canonical: list[CanonicalRecord] = []

    for raw in raw_records:
        try:
            rec = _raw_to_canonical(raw, skill_aliases)
            canonical.append(rec)
        except Exception as exc:
            logger.warning(
                "Error normalising record from %s: %s — skipping",
                raw.source_name, exc,
            )

    logger.info("Normalisation complete: %d canonical records", len(canonical))
    return canonical


def _raw_to_canonical(raw: RawRecord, skill_aliases: dict[str, str]) -> CanonicalRecord:
    """Convert one RawRecord to a normalised CanonicalRecord."""
    data = raw.data
    provenance: list[Provenance] = []
    field_confidences: dict[str, float] = {}

    # Determine extraction method based on source type
    default_method = _default_method(raw.source_type)

    # ----- Name -----
    full_name = data.get("full_name")
    if full_name and isinstance(full_name, str):
        full_name = full_name.strip()
        method = _get_extraction_method(data, "full_name", default_method)
        method_score = _method_score(method)
        provenance.append(Provenance(
            field="full_name", source=raw.source_name,
            method=method, value=full_name, confidence=method_score,
        ))
        field_confidences["full_name"] = method_score
    else:
        full_name = None

    # ----- Emails -----
    raw_emails = data.get("emails", [])
    if isinstance(data.get("email"), str):
        raw_emails = [data["email"]]
    elif isinstance(raw_emails, str):
        raw_emails = [raw_emails]

    emails: list[str] = []
    for raw_email in raw_emails:
        normed, conf = normalize_email(raw_email)
        if normed:
            emails.append(normed)
    if emails:
        provenance.append(Provenance(
            field="emails", source=raw.source_name,
            method="regex" if raw.source_type in (SourceType.RESUME, SourceType.NOTES) else "direct_read",
            value=emails, confidence=1.0,
        ))
        field_confidences["emails"] = 1.0

    # ----- Phones -----
    raw_phones = data.get("phones", [])
    if isinstance(data.get("phone"), str):
        raw_phones = [data["phone"]]
    elif isinstance(raw_phones, str):
        raw_phones = [raw_phones]

    phones: list[str] = []
    phone_confidences: list[float] = []
    for raw_phone in raw_phones:
        normed, conf = normalize_phone(raw_phone)
        if normed:
            phones.append(normed)
            phone_confidences.append(conf)
    if phones:
        avg_phone_conf = sum(phone_confidences) / len(phone_confidences)
        method = "regex" if raw.source_type in (SourceType.RESUME, SourceType.NOTES) else "direct_read"
        method_score = _method_score(method)
        provenance.append(Provenance(
            field="phones", source=raw.source_name,
            method=method, value=phones,
            confidence=method_score * avg_phone_conf,
        ))
        field_confidences["phones"] = method_score * avg_phone_conf

    # ----- Location -----
    loc_data = data.get("location", {})
    location = None
    if isinstance(loc_data, dict) and any(loc_data.values()):
        country_raw = loc_data.get("country")
        country_norm, country_conf = normalize_country(country_raw) if country_raw else (None, 0.0)
        location = Location(
            city=loc_data.get("city"),
            region=loc_data.get("region"),
            country=country_norm,
        )
        provenance.append(Provenance(
            field="location", source=raw.source_name,
            method=default_method, confidence=country_conf if country_raw else 1.0,
        ))
        field_confidences["location"] = 1.0 * (country_conf if country_raw else 1.0)

    # ----- Links -----
    linkedin_raw = data.get("linkedin")
    github_raw = data.get("github")
    portfolio_raw = data.get("portfolio")

    linkedin_handle, _ = extract_handle(linkedin_raw, "linkedin") if linkedin_raw else (None, 0.0)
    github_handle, _ = extract_handle(github_raw, "github") if github_raw else (None, 0.0)

    links = None
    if linkedin_handle or github_handle or portfolio_raw:
        links = Links(
            linkedin=linkedin_handle,
            github=github_handle,
            portfolio=portfolio_raw,
            other=[],
        )
        field_confidences["links"] = 1.0

    # ----- Headline -----
    headline = data.get("headline")
    if headline and isinstance(headline, str):
        headline = headline.strip()
        method = _get_extraction_method(data, "headline", default_method)
        method_score = _method_score(method)
        provenance.append(Provenance(
            field="headline", source=raw.source_name,
            method=method, value=headline, confidence=method_score,
        ))
        field_confidences["headline"] = method_score
    else:
        headline = None

    # ----- Years of experience -----
    yoe = data.get("years_experience")
    if yoe is not None:
        try:
            yoe = float(yoe)
            provenance.append(Provenance(
                field="years_experience", source=raw.source_name,
                method=default_method, value=yoe, confidence=_method_score(default_method),
            ))
            field_confidences["years_experience"] = _method_score(default_method)
        except (ValueError, TypeError):
            yoe = None

    # ----- Skills -----
    raw_skills = data.get("skills", [])
    skills: list[Skill] = []
    for raw_skill in raw_skills:
        if isinstance(raw_skill, str) and raw_skill.strip():
            normed, norm_conf = normalize_skill(raw_skill, skill_aliases)
            if normed:
                method = "regex" if raw.source_type == SourceType.NOTES else default_method
                method_score = _method_score(method)
                skills.append(Skill(
                    name=normed,
                    confidence=method_score * norm_conf,
                    sources=[raw.source_name],
                ))
    if skills:
        field_confidences["skills"] = sum(s.confidence for s in skills) / len(skills)

    # ----- Experience -----
    raw_experience = data.get("experience", [])
    experience: list[Experience] = []
    for exp in raw_experience:
        if isinstance(exp, dict):
            start_norm, start_conf = normalize_date(exp.get("start"))
            end_norm, end_conf = normalize_date(exp.get("end"))

            experience.append(Experience(
                company=exp.get("company"),
                title=exp.get("title"),
                start=start_norm,
                end=end_norm,
                summary=exp.get("summary"),
            ))

            # Provenance for dates with confidence penalties
            if start_norm and start_conf < 1.0:
                provenance.append(Provenance(
                    field="experience.start", source=raw.source_name,
                    method=default_method, value=start_norm, confidence=start_conf,
                ))
            if end_norm and end_conf < 1.0:
                provenance.append(Provenance(
                    field="experience.end", source=raw.source_name,
                    method=default_method, value=end_norm, confidence=end_conf,
                ))
    if experience:
        field_confidences["experience"] = _method_score(default_method)

    # ----- Education -----
    raw_education = data.get("education", [])
    education: list[Education] = []
    for edu in raw_education:
        if isinstance(edu, dict):
            end_year = edu.get("end_year")
            if end_year is not None:
                try:
                    end_year = int(end_year)
                except (ValueError, TypeError):
                    end_year = None
            education.append(Education(
                institution=edu.get("institution"),
                degree=edu.get("degree"),
                field=edu.get("field"),
                end_year=end_year,
            ))
    if education:
        field_confidences["education"] = _method_score(default_method)

    # ----- Handle section-not-detected provenance from resume adapter -----
    prov_notes = data.get("_provenance_notes", [])
    for note in prov_notes:
        if note.startswith("section-not-detected:"):
            field = note.split(":", 1)[1]
            provenance.append(Provenance(
                field=field, source=raw.source_name,
                method="section-not-detected", confidence=0.0,
            ))
        elif note == "no-section-headers-detected":
            provenance.append(Provenance(
                field="all", source=raw.source_name,
                method="section-not-detected",
                value="no section headers detected in resume",
                confidence=0.0,
            ))

    rec = CanonicalRecord(
        full_name=full_name,
        emails=emails,
        phones=phones,
        location=location,
        links=links,
        headline=headline,
        years_experience=yoe,
        skills=skills,
        experience=experience,
        education=education,
        provenance=provenance,
    )
    rec._field_confidences = field_confidences
    return rec


def _default_method(source_type: SourceType) -> str:
    """Default extraction method based on source type."""
    if source_type in (SourceType.CSV, SourceType.JSON_ATS):
        return ExtractionMethod.DIRECT_READ.value
    if source_type == SourceType.GITHUB:
        return ExtractionMethod.DIRECT_READ.value  # API structured data
    if source_type == SourceType.RESUME:
        return ExtractionMethod.REGEX.value
    if source_type == SourceType.NOTES:
        return ExtractionMethod.HEURISTIC.value
    return ExtractionMethod.HEURISTIC.value


def _method_score(method: str) -> float:
    """Get the method score for confidence calculation."""
    scores = {
        "direct_read": 1.0,
        "regex": 0.8,
        "heuristic": 0.6,
        "section-not-detected": 0.0,
    }
    return scores.get(method, 0.8)


def _get_extraction_method(data: dict, field: str, default: str) -> str:
    """Get extraction method from adapter metadata, if available."""
    methods = data.get("_extraction_methods", {})
    return methods.get(field, default)


# ---------------------------------------------------------------------------
# Stage 4-7: Resolve → Merge → Project → Validate
# ---------------------------------------------------------------------------

def resolve_and_merge(canonical: list[CanonicalRecord]) -> list[CanonicalRecord]:
    """Identity resolution followed by merge."""
    groups = resolve(canonical)
    merged: list[CanonicalRecord] = []
    for group_records, cid, method in groups:
        merged_record = merge_group(group_records, cid, method)
        merged.append(merged_record)
    logger.info("Merge complete: %d merged profiles", len(merged))
    return merged


def project_and_validate(
    merged: list[CanonicalRecord],
    config: OutputConfig,
) -> list[tuple[str, dict[str, Any]]]:
    """
    Project each merged record through the config and validate.

    Returns a list of (candidate_id, projected_dict) tuples so the emit
    stage always has the canonical ID for filenames, even when the config
    doesn't include candidate_id.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    for rec in merged:
        try:
            projected = project(rec, config)
            results.append((rec.candidate_id or "unknown", projected))
        except ProjectionError as exc:
            logger.error(
                "Projection error for candidate %s: %s — skipping",
                rec.candidate_id, exc,
            )
    logger.info("Projection complete: %d valid outputs", len(results))
    return results


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run(
    sources: list[str],
    config: OutputConfig,
    output_dir: str | Path,
) -> list[dict[str, Any]]:
    """
    Run the full pipeline: Ingest → Adapt → Normalize → Resolve → Merge → Project → Validate → Emit.

    Parameters
    ----------
    sources : list[str]
        Paths/URLs to source data.
    config : OutputConfig
        Runtime output configuration.
    output_dir : str | Path
        Directory to write output JSON files.

    Returns
    -------
    list[dict]
        The projected outputs (also written to disk).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: Ingest
    raw_records = ingest(sources)
    if not raw_records:
        logger.warning("No records ingested from any source — nothing to do")
        return []

    # Stage 2-3: Adapt + Normalize
    canonical = adapt_and_normalize(raw_records)
    if not canonical:
        logger.warning("No records survived normalisation — nothing to merge")
        return []

    # Stage 4-5: Resolve Identity + Merge
    merged = resolve_and_merge(canonical)

    # Stage 6-7: Project + Validate
    projected_results = project_and_validate(merged, config)

    # Stage 8: Emit — always use canonical candidate_id for filenames
    results: list[dict[str, Any]] = []
    for cid, projected in projected_results:
        out_path = output_dir / f"{cid}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(projected, f, indent=2, ensure_ascii=False, default=str)
        logger.info("Written: %s", out_path)
        results.append(projected)

    logger.info(
        "Pipeline complete: %d candidates written to %s",
        len(results), output_dir,
    )
    return results
