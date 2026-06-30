"""
Tests for the projection layer.

Covers:
- Default config projects all fields
- Custom config with from path resolution (skills[].name)
- on_missing: "omit" excludes missing fields
- on_missing: "null" sets None for missing fields
- on_missing: "error" raises on missing required field
- Dot-notation path resolution
"""

import pytest

from profyle.models import (
    CanonicalRecord,
    FieldSpec,
    Links,
    Location,
    OutputConfig,
    Skill,
)
from profyle.projection import ProjectionError, project


def _make_full_record() -> CanonicalRecord:
    """Create a fully populated CanonicalRecord for testing."""
    return CanonicalRecord(
        candidate_id="abc123",
        full_name="Priya Sharma",
        emails=["priya@example.com"],
        phones=["+919876543210"],
        location=Location(city="Bangalore", region="Karnataka", country="IN"),
        links=Links(linkedin="priyasharma", github="priyasharma-dev"),
        headline="ML Engineer",
        years_experience=7.0,
        skills=[
            Skill(name="Python", confidence=0.95, sources=["csv", "github"]),
            Skill(name="Machine Learning", confidence=0.9, sources=["github"]),
        ],
        overall_confidence=0.92,
    )


class TestProjection:
    def test_default_config_all_fields(self):
        """Default config should project all canonical fields."""
        record = _make_full_record()
        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="emails", type="array"),
                FieldSpec(path="phones", type="array"),
                FieldSpec(path="headline", type="string"),
                FieldSpec(path="overall_confidence", type="number"),
            ],
            include_confidence=True,
            on_missing="null",
        )

        result = project(record, config)
        assert result["candidate_id"] == "abc123"
        assert result["full_name"] == "Priya Sharma"
        assert result["emails"] == ["priya@example.com"]
        assert result["overall_confidence"] == 0.92

    def test_custom_config_skills_name_projection(self):
        """Custom config with from='skills[].name' should extract skill names."""
        record = _make_full_record()
        config = OutputConfig(
            fields=[
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="skill_names", type="array", **{"from": "skills[].name"}),
            ],
            include_confidence=False,
            on_missing="omit",
        )

        result = project(record, config)
        assert result["full_name"] == "Priya Sharma"
        assert result["skill_names"] == ["Python", "Machine Learning"]

    def test_on_missing_null(self):
        """on_missing='null' should set None for missing fields."""
        record = CanonicalRecord(
            candidate_id="abc123",
            full_name="Test User",
        )
        config = OutputConfig(
            fields=[
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="headline", type="string"),
                FieldSpec(path="years_experience", type="number"),
            ],
            include_confidence=False,
            on_missing="null",
        )

        result = project(record, config)
        assert result["full_name"] == "Test User"
        assert result["headline"] is None
        assert result["years_experience"] is None

    def test_on_missing_omit(self):
        """on_missing='omit' should exclude missing fields from output."""
        record = CanonicalRecord(
            candidate_id="abc123",
            full_name="Test User",
        )
        config = OutputConfig(
            fields=[
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="headline", type="string"),
                FieldSpec(path="years_experience", type="number"),
            ],
            include_confidence=False,
            on_missing="omit",
        )

        result = project(record, config)
        assert result["full_name"] == "Test User"
        assert "headline" not in result
        assert "years_experience" not in result

    def test_on_missing_error_required_field(self):
        """on_missing='error' with missing required field should raise."""
        record = CanonicalRecord(candidate_id="abc123")
        config = OutputConfig(
            fields=[
                FieldSpec(path="full_name", type="string", required=True),
            ],
            include_confidence=False,
            on_missing="error",
        )

        with pytest.raises(ProjectionError, match="Required field"):
            project(record, config)

    def test_dot_notation_path(self):
        """Dot-notation paths like 'location.city' should resolve correctly."""
        record = _make_full_record()
        config = OutputConfig(
            fields=[
                FieldSpec(path="city", type="string", **{"from": "location.city"}),
            ],
            include_confidence=False,
            on_missing="null",
        )

        result = project(record, config)
        assert result["city"] == "Bangalore"

    def test_confidence_excluded_when_disabled(self):
        """When include_confidence=False, confidence fields should be stripped."""
        record = _make_full_record()
        config = OutputConfig(
            fields=[
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="skills", type="array"),
            ],
            include_confidence=False,
            on_missing="null",
        )

        result = project(record, config)
        # Skills should not have confidence/sources sub-fields
        if result.get("skills"):
            for skill in result["skills"]:
                if isinstance(skill, dict):
                    assert "confidence" not in skill
                    assert "sources" not in skill
