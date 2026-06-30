"""
Tests for identity resolution.

Covers:
- Email match → same candidate
- 2-of-3 match (phone + GitHub) → same candidate
- Only 1-of-3 match → separate candidates
- No overlap at all → separate candidates
- Deterministic candidate_id — same inputs → same ID
"""

import pytest

from profyle.identity import resolve
from profyle.models import CanonicalRecord, Links


def _make_record(
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    github: str | None = None,
    linkedin: str | None = None,
    full_name: str | None = None,
) -> CanonicalRecord:
    """Helper to create a minimal CanonicalRecord for testing."""
    links = None
    if github or linkedin:
        links = Links(github=github, linkedin=linkedin)
    return CanonicalRecord(
        full_name=full_name or "Test User",
        emails=emails or [],
        phones=phones or [],
        links=links,
    )


class TestIdentityResolution:
    def test_email_match_same_candidate(self):
        """Two records with the same email should be grouped together."""
        r1 = _make_record(emails=["alice@example.com"], full_name="Alice")
        r2 = _make_record(emails=["alice@example.com"], full_name="Alice Johnson")

        groups = resolve([r1, r2])
        assert len(groups) == 1
        group_records, cid, method = groups[0]
        assert len(group_records) == 2
        assert method == "email"
        assert cid  # Should have a non-empty ID

    def test_two_of_three_match_phone_github(self):
        """Records matching on phone + GitHub (2 of 3) should merge."""
        r1 = _make_record(
            emails=["r1@example.com"],
            phones=["+919876543210"],
            github="devuser",
        )
        r2 = _make_record(
            emails=["r2@different.com"],
            phones=["+919876543210"],
            github="devuser",
        )

        groups = resolve([r1, r2])
        assert len(groups) == 1
        group_records, cid, method = groups[0]
        assert len(group_records) == 2
        assert method == "secondary_2of3"

    def test_two_of_three_match_phone_linkedin(self):
        """Records matching on phone + LinkedIn (2 of 3) should merge."""
        r1 = _make_record(
            phones=["+14155551234"],
            linkedin="johndoe",
        )
        r2 = _make_record(
            phones=["+14155551234"],
            linkedin="johndoe",
        )

        groups = resolve([r1, r2])
        assert len(groups) == 1
        _, _, method = groups[0]
        assert method == "secondary_2of3"

    def test_only_one_of_three_no_merge(self):
        """Records matching on only 1 of 3 signals should NOT merge."""
        r1 = _make_record(
            emails=["r1@example.com"],
            phones=["+919876543210"],
            github="user-a",
        )
        r2 = _make_record(
            emails=["r2@different.com"],
            phones=["+919876543210"],
            github="user-b",  # different github
            linkedin=None,
        )

        groups = resolve([r1, r2])
        # Only phone matches (1 of 3) — should be separate
        assert len(groups) == 2

    def test_no_overlap_separate_candidates(self):
        """Records with zero overlap should remain separate."""
        r1 = _make_record(
            emails=["alice@example.com"],
            phones=["+11111111111"],
            github="alice",
        )
        r2 = _make_record(
            emails=["bob@example.com"],
            phones=["+12222222222"],
            github="bob",
        )

        groups = resolve([r1, r2])
        assert len(groups) == 2

    def test_deterministic_candidate_id(self):
        """Same inputs must always produce the same candidate_id."""
        r1 = _make_record(emails=["test@example.com"])
        r2 = _make_record(emails=["test@example.com"])

        groups_run1 = resolve([r1, r2])
        groups_run2 = resolve([r1, r2])

        assert groups_run1[0][1] == groups_run2[0][1]  # Same ID

    def test_absent_field_not_counted(self):
        """
        A field that is absent on one side should not count toward matching.
        If only one record has a GitHub handle, that's neither a match nor mismatch.
        """
        r1 = _make_record(
            emails=["r1@example.com"],
            phones=["+919876543210"],
            github="devuser",
            linkedin=None,
        )
        r2 = _make_record(
            emails=["r2@different.com"],
            phones=["+919876543210"],
            github=None,  # absent — should not count
            linkedin=None,
        )

        # Only phone matches, GitHub absent on one side → 1 of 3 → separate
        groups = resolve([r1, r2])
        assert len(groups) == 2

    def test_single_record(self):
        """A single record should be its own group."""
        r1 = _make_record(emails=["solo@example.com"])
        groups = resolve([r1])
        assert len(groups) == 1
        assert len(groups[0][0]) == 1
        assert groups[0][2] == "single"

    def test_empty_input(self):
        """Empty input should return empty output."""
        groups = resolve([])
        assert len(groups) == 0
