"""
Merge & confidence scoring for resolved candidate groups.

Confidence formula (per user specification):
    field_confidence = method_score × normalization_factor × agreement_factor

    method_score:
        direct_read (CSV/ATS field)  = 1.0
        regex (resume, notes pattern) = 0.8
        heuristic (inferred)          = 0.6

    normalization_factor:
        clean conversion = 1.0
        assumption-based fallback = 0.7

    agreement_factor:
        single source or agreement = 1.0
        conflict (priority winner picked) = 0.7

    overall_confidence = avg(populated field_confidences) × identity_match_factor
        identity_match_factor = 1.0 if email match, 0.85 if 2-of-3 rule

Per-field source priority (not one source winning every field):
    full_name:   CSV > ATS > Resume > GitHub > Notes
    headline:    CSV > ATS > GitHub > Resume > Notes
    title/company: CSV > ATS > Resume > Notes > GitHub
    skills:      GitHub > Resume > CSV > ATS > Notes
    education:   Resume > ATS > CSV > Notes
    location:    CSV > ATS > GitHub > Resume > Notes
    links:       All merged; GitHub preferred for GitHub handle
    emails/phones: Union across all sources (deduped on normalised value)
"""

from __future__ import annotations

import logging
from collections import defaultdict

from profyle.models import (
    CanonicalRecord,
    Education,
    Experience,
    ExtractionMethod,
    Links,
    Location,
    Provenance,
    Skill,
    SourceType,
)

logger = logging.getLogger("profyle.merger")

# ---------------------------------------------------------------------------
# Method scores
# ---------------------------------------------------------------------------

METHOD_SCORES: dict[str, float] = {
    ExtractionMethod.DIRECT_READ.value: 1.0,
    ExtractionMethod.REGEX.value: 0.8,
    ExtractionMethod.HEURISTIC.value: 0.6,
    ExtractionMethod.SECTION_NOT_DETECTED.value: 0.0,
    "direct_read": 1.0,
    "regex": 0.8,
    "heuristic": 0.6,
    "section-not-detected": 0.0,
}

# Source priority by field group (lower index = higher priority)
SOURCE_PRIORITY: dict[str, list[SourceType]] = {
    "full_name": [SourceType.CSV, SourceType.JSON_ATS, SourceType.RESUME, SourceType.GITHUB, SourceType.NOTES],
    "headline": [SourceType.CSV, SourceType.JSON_ATS, SourceType.GITHUB, SourceType.RESUME, SourceType.NOTES],
    "title": [SourceType.CSV, SourceType.JSON_ATS, SourceType.RESUME, SourceType.NOTES, SourceType.GITHUB],
    "company": [SourceType.CSV, SourceType.JSON_ATS, SourceType.RESUME, SourceType.NOTES, SourceType.GITHUB],
    "skills": [SourceType.GITHUB, SourceType.RESUME, SourceType.CSV, SourceType.JSON_ATS, SourceType.NOTES],
    "education": [SourceType.RESUME, SourceType.JSON_ATS, SourceType.CSV, SourceType.NOTES],
    "location": [SourceType.CSV, SourceType.JSON_ATS, SourceType.GITHUB, SourceType.RESUME, SourceType.NOTES],
    "years_experience": [SourceType.CSV, SourceType.JSON_ATS, SourceType.RESUME, SourceType.NOTES, SourceType.GITHUB],
}


def merge_group(
    records: list[CanonicalRecord],
    candidate_id: str,
    match_method: str,
) -> CanonicalRecord:
    """
    Merge a group of CanonicalRecords into one, applying per-field source
    priority, union on lists, and the confidence formula.

    Parameters
    ----------
    records : list[CanonicalRecord]
        Records to merge (already resolved as same candidate).
    candidate_id : str
        Deterministic ID for this candidate.
    match_method : str
        How records were matched ("email" | "secondary_2of3" | "single").

    Returns
    -------
    CanonicalRecord
        The merged canonical profile.
    """
    if len(records) == 1:
        rec = records[0].model_copy(deep=True)
        rec.candidate_id = candidate_id
        identity_factor = _identity_factor(match_method)
        _compute_overall_confidence(rec, identity_factor)
        return rec

    merged = CanonicalRecord(candidate_id=candidate_id)
    provenance: list[Provenance] = []
    field_confidences: dict[str, float] = {}

    # Tag each record with its source type for priority lookups
    source_records: list[tuple[SourceType, CanonicalRecord]] = []
    for rec in records:
        # Determine source type from provenance
        st = _infer_source_type(rec)
        source_records.append((st, rec))

    # Sort by priority for the default ordering
    def _priority(field: str, st: SourceType) -> int:
        prio_list = SOURCE_PRIORITY.get(field, list(SourceType))
        try:
            return prio_list.index(st)
        except ValueError:
            return len(prio_list)

    # ----- Scalar fields with source priority -----
    for field_name in ("full_name", "headline", "years_experience"):
        values: list[tuple[int, str | float | None, SourceType, str]] = []
        for st, rec in source_records:
            val = getattr(rec, field_name)
            if val is not None:
                method = _get_method_for_field(rec, field_name)
                values.append((_priority(field_name, st), val, st, method))

        if values:
            values.sort(key=lambda x: x[0])
            winner_prio, winner_val, winner_st, winner_method = values[0]
            setattr(merged, field_name, winner_val)

            # Determine agreement/conflict
            agreement_factor = _agreement_factor(values)
            norm_factor = _get_norm_factor(records, field_name)
            method_score = METHOD_SCORES.get(winner_method, 0.8)
            field_conf = method_score * norm_factor * agreement_factor
            field_confidences[field_name] = field_conf

            # Record provenance for winner
            provenance.append(Provenance(
                field=field_name, source=winner_st.value,
                method=winner_method, value=winner_val,
                confidence=field_conf,
            ))
            # Record losing values too
            for p, v, s, m in values[1:]:
                if v != winner_val:
                    provenance.append(Provenance(
                        field=field_name, source=s.value,
                        method=m, value=v,
                        confidence=METHOD_SCORES.get(m, 0.8) * norm_factor * 0.7,
                    ))

    # ----- Location (source priority) -----
    loc_values: list[tuple[int, Location | None, SourceType]] = []
    for st, rec in source_records:
        if rec.location and (rec.location.city or rec.location.region or rec.location.country):
            loc_values.append((_priority("location", st), rec.location, st))
    if loc_values:
        loc_values.sort(key=lambda x: x[0])
        merged.location = loc_values[0][1]
        provenance.append(Provenance(
            field="location", source=loc_values[0][2].value,
            method="direct_read", confidence=1.0,
        ))
        field_confidences["location"] = 1.0 if len(loc_values) == 1 or _locations_agree(loc_values) else 0.7

    # ----- Links (merged, GitHub preferred for github handle) -----
    merged_links = Links()
    for st, rec in sorted(source_records, key=lambda x: _priority("skills", x[0])):
        if rec.links:
            if rec.links.github and not merged_links.github:
                merged_links.github = rec.links.github
            if rec.links.linkedin and not merged_links.linkedin:
                merged_links.linkedin = rec.links.linkedin
            if rec.links.portfolio and not merged_links.portfolio:
                merged_links.portfolio = rec.links.portfolio
            for other_link in rec.links.other:
                if other_link not in merged_links.other:
                    merged_links.other.append(other_link)
    if merged_links.github or merged_links.linkedin or merged_links.portfolio or merged_links.other:
        merged.links = merged_links
        field_confidences["links"] = 1.0

    # ----- Emails (union, deduped) -----
    all_emails: list[str] = []
    seen_emails: set[str] = set()
    for _, rec in source_records:
        for email in rec.emails:
            if email.lower() not in seen_emails:
                seen_emails.add(email.lower())
                all_emails.append(email)
    merged.emails = all_emails
    if all_emails:
        field_confidences["emails"] = 1.0

    # ----- Phones (union, deduped) -----
    all_phones: list[str] = []
    seen_phones: set[str] = set()
    for _, rec in source_records:
        for phone in rec.phones:
            if phone not in seen_phones:
                seen_phones.add(phone)
                all_phones.append(phone)
    merged.phones = all_phones
    if all_phones:
        # Average phone norm factors
        phone_norms = [
            p.confidence for p in _gather_provenance(records, "phones")
        ]
        field_confidences["phones"] = sum(phone_norms) / len(phone_norms) if phone_norms else 1.0

    # ----- Skills (union with confidence) -----
    skill_map: dict[str, Skill] = {}  # canonical name → merged skill
    for st, rec in source_records:
        for skill in rec.skills:
            key = skill.name.lower()
            if key in skill_map:
                existing = skill_map[key]
                existing.sources = list(set(existing.sources + skill.sources + [st.value]))
                # Agreement boost
                existing.confidence = min(1.0, existing.confidence + 0.05)
            else:
                skill_map[key] = Skill(
                    name=skill.name,
                    confidence=skill.confidence,
                    sources=list(set(skill.sources + [st.value])),
                )
    merged.skills = list(skill_map.values())
    if merged.skills:
        field_confidences["skills"] = sum(s.confidence for s in merged.skills) / len(merged.skills)

    # ----- Experience (merge by company+title, fill gaps) -----
    merged.experience = _merge_experience(source_records)
    if merged.experience:
        field_confidences["experience"] = 0.85  # Heuristic baseline for merged experience

    # ----- Education (source priority + merge) -----
    merged.education = _merge_education(source_records)
    if merged.education:
        field_confidences["education"] = 0.85

    # ----- Provenance from all sources -----
    for rec in records:
        provenance.extend(rec.provenance)
    merged.provenance = provenance

    # ----- Overall confidence -----
    identity_factor = _identity_factor(match_method)
    merged._field_confidences = field_confidences
    merged._identity_match_factor = identity_factor
    _compute_overall_confidence(merged, identity_factor)

    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identity_factor(match_method: str) -> float:
    """Identity match factor per user spec."""
    if match_method == "email":
        return 1.0
    if match_method == "secondary_2of3":
        return 0.85
    return 1.0  # single record, no merge ambiguity


def _agreement_factor(values: list[tuple[int, object, SourceType, str]]) -> float:
    """
    1.0 if only one source or all agree, 0.7 if there's a conflict.
    """
    if len(values) <= 1:
        return 1.0
    # Check if all values are the same
    first_val = values[0][1]
    if all(_values_match(v[1], first_val) for v in values[1:]):
        return 1.0
    return 0.7


def _values_match(a: object, b: object) -> bool:
    """Compare two field values for agreement."""
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _get_method_for_field(rec: CanonicalRecord, field: str) -> str:
    """Look up the extraction method for a field from its provenance."""
    for p in rec.provenance:
        if p.field == field:
            return p.method
    # Default based on source type
    return "direct_read"


def _get_norm_factor(records: list[CanonicalRecord], field: str) -> float:
    """Get the normalization factor for a field from provenance."""
    for rec in records:
        for p in rec.provenance:
            if p.field == field and p.confidence < 1.0:
                return p.confidence
    return 1.0


def _gather_provenance(records: list[CanonicalRecord], field: str) -> list[Provenance]:
    """Gather all provenance entries for a specific field across records."""
    result: list[Provenance] = []
    for rec in records:
        for p in rec.provenance:
            if p.field == field:
                result.append(p)
    return result


def _infer_source_type(rec: CanonicalRecord) -> SourceType:
    """Infer the source type from a record's provenance."""
    if rec.provenance:
        source = rec.provenance[0].source
        for st in SourceType:
            if st.value == source:
                return st
        # Try matching source names
        source_lower = source.lower()
        if "csv" in source_lower:
            return SourceType.CSV
        if "json" in source_lower or "ats" in source_lower:
            return SourceType.JSON_ATS
        if "github" in source_lower:
            return SourceType.GITHUB
        if "resume" in source_lower or "pdf" in source_lower or "docx" in source_lower:
            return SourceType.RESUME
        if "note" in source_lower:
            return SourceType.NOTES
    return SourceType.CSV  # Default fallback


def _locations_agree(loc_values: list[tuple[int, Location, SourceType]]) -> bool:
    """Check if all location values agree on city and country."""
    first = loc_values[0][1]
    for _, loc, _ in loc_values[1:]:
        if first.city and loc.city and first.city.lower() != loc.city.lower():
            return False
        if first.country and loc.country and first.country.lower() != loc.country.lower():
            return False
    return True


def _merge_experience(
    source_records: list[tuple[SourceType, CanonicalRecord]],
) -> list[Experience]:
    """Merge experience entries, deduplicating by company+title."""
    seen: dict[str, Experience] = {}
    for _, rec in source_records:
        for exp in rec.experience:
            key = f"{(exp.company or '').lower()}|{(exp.title or '').lower()}"
            if key not in seen:
                seen[key] = exp.model_copy(deep=True)
            else:
                existing = seen[key]
                # Fill gaps
                if not existing.start and exp.start:
                    existing.start = exp.start
                if not existing.end and exp.end:
                    existing.end = exp.end
                if not existing.summary and exp.summary:
                    existing.summary = exp.summary
    return list(seen.values())


def _merge_education(
    source_records: list[tuple[SourceType, CanonicalRecord]],
) -> list[Education]:
    """Merge education entries, deduplicating by institution+degree."""
    seen: dict[str, Education] = {}
    for _, rec in source_records:
        for edu in rec.education:
            key = f"{(edu.institution or '').lower()}|{(edu.degree or '').lower()}"
            if key not in seen:
                seen[key] = edu.model_copy(deep=True)
            else:
                existing = seen[key]
                if not existing.field and edu.field:
                    existing.field = edu.field
                if not existing.end_year and edu.end_year:
                    existing.end_year = edu.end_year
    return list(seen.values())


def _compute_overall_confidence(rec: CanonicalRecord, identity_factor: float) -> None:
    """
    Compute overall_confidence as:
        avg(populated field_confidences) × identity_match_factor

    Only populated fields count — null fields are NOT counted as zero.
    """
    confidences: list[float] = []

    fc = getattr(rec, "_field_confidences", {})
    if fc:
        confidences.extend(fc.values())
    else:
        # Fallback: estimate from provenance
        for p in rec.provenance:
            if p.confidence > 0:
                confidences.append(p.confidence)

    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        rec.overall_confidence = round(avg_conf * identity_factor, 4)
    else:
        rec.overall_confidence = None
