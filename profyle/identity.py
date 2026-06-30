"""
Identity resolution — determine which raw records refer to the same candidate.

Rules (as specified):
1. Normalised email match → same candidate
2. Else, at least 2 of {E.164 phone, GitHub handle, LinkedIn handle} match
   → same candidate.  A field only counts if it is populated on BOTH sides.
3. If neither rule applies → separate candidates.

candidate_id is generated deterministically:
- Email match: hash of matching email(s)
- 2-of-3 match: hash of the matching fields, sorted alphabetically
"""

from __future__ import annotations

import logging
from collections import defaultdict

from profyle.models import CanonicalRecord
from profyle.utils import deterministic_id

logger = logging.getLogger("profyle.identity")


def resolve(records: list[CanonicalRecord]) -> list[tuple[list[CanonicalRecord], str, str]]:
    """
    Group records that refer to the same candidate.

    Parameters
    ----------
    records : list[CanonicalRecord]
        Post-normalisation, pre-merge canonical records.

    Returns
    -------
    list of (group, candidate_id, match_method)
        Each group is a list of CanonicalRecords to be merged, with a
        deterministic candidate_id and the method used for matching
        ("email" | "secondary_2of3" | "single").
    """
    if not records:
        return []

    n = len(records)
    # Union-Find for grouping
    parent = list(range(n))
    match_methods: dict[int, str] = {}     # root → match method
    match_ids: dict[int, list[str]] = {}   # root → identifiers used for hashing

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int, method: str, identifiers: list[str]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            # Prefer email match method over secondary
            existing = match_methods.get(ra, "single")
            if method == "email" or existing != "email":
                match_methods[ra] = method
            match_ids.setdefault(ra, []).extend(identifiers)

    # ----- Rule 1: email match -----
    email_index: dict[str, list[int]] = defaultdict(list)
    for idx, rec in enumerate(records):
        for email in rec.emails:
            email_index[email].append(idx)

    for email, indices in email_index.items():
        if len(indices) > 1:
            for i in range(1, len(indices)):
                union(indices[0], indices[i], "email", [email])

    # ----- Rule 2: 2-of-3 secondary match -----
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue  # Already matched via email

            matches = _count_secondary_matches(records[i], records[j])
            if len(matches) >= 2:
                union(i, j, "secondary_2of3", matches)

    # ----- Build groups -----
    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    result: list[tuple[list[CanonicalRecord], str, str]] = []
    for root, member_indices in groups.items():
        group_records = [records[i] for i in member_indices]
        method = match_methods.get(root, "single")

        # Generate deterministic candidate ID
        ids = match_ids.get(root, [])
        if method == "email":
            # Use matching emails for the hash
            cid = deterministic_id(*ids)
        elif method == "secondary_2of3":
            cid = deterministic_id(*ids)
        else:
            # Single record — hash whatever unique identifier is available
            rec = group_records[0]
            id_parts = list(rec.emails)
            if rec.links and rec.links.github:
                id_parts.append(f"github:{rec.links.github}")
            if rec.links and rec.links.linkedin:
                id_parts.append(f"linkedin:{rec.links.linkedin}")
            for phone in rec.phones:
                id_parts.append(f"phone:{phone}")
            if id_parts:
                cid = deterministic_id(*id_parts)
            else:
                # Fallback: hash the full name + source
                cid = deterministic_id(
                    rec.full_name or "",
                    rec.provenance[0].source if rec.provenance else "unknown",
                )

        result.append((group_records, cid, method))

    logger.info(
        "Identity resolution: %d records → %d candidates "
        "(%d groups via email, %d via 2-of-3, %d singles)",
        n,
        len(result),
        sum(1 for _, _, m in result if m == "email"),
        sum(1 for _, _, m in result if m == "secondary_2of3"),
        sum(1 for _, _, m in result if m == "single"),
    )
    return result


def _count_secondary_matches(a: CanonicalRecord, b: CanonicalRecord) -> list[str]:
    """
    Check how many of {phone, github, linkedin} match between two records.

    A field only counts if it is populated on BOTH sides.
    Returns the list of matching identifier strings (for hashing).
    """
    matches: list[str] = []

    # Phone match — any phone in common
    if a.phones and b.phones:
        common_phones = set(a.phones) & set(b.phones)
        if common_phones:
            matches.append(f"phone:{sorted(common_phones)[0]}")

    # GitHub handle match
    a_gh = a.links.github if a.links else None
    b_gh = b.links.github if b.links else None
    if a_gh and b_gh and a_gh.lower() == b_gh.lower():
        matches.append(f"github:{a_gh.lower()}")

    # LinkedIn handle match
    a_li = a.links.linkedin if a.links else None
    b_li = b.links.linkedin if b.links else None
    if a_li and b_li and a_li.lower() == b_li.lower():
        matches.append(f"linkedin:{a_li.lower()}")

    return matches
