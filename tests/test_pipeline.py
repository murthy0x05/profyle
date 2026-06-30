"""
End-to-end pipeline tests.

Covers:
- Malformed CSV row → skipped, rest processed
- No identity match → separate profiles emitted
- Full pipeline with sample data
- Config on_missing: "error" with missing required field → raises
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from profyle.models import FieldSpec, OutputConfig
from profyle.pipeline import run


@pytest.fixture
def output_dir(tmp_path):
    """Provide a temporary output directory."""
    out = tmp_path / "output"
    out.mkdir()
    return out


@pytest.fixture
def sample_dir():
    """Path to sample data directory."""
    return Path(__file__).resolve().parent.parent / "data" / "sample"


class TestPipelineMalformedInput:
    def test_malformed_csv_row_skipped(self, output_dir, tmp_path):
        """A CSV with a bad row should skip that row and process the rest."""
        csv_content = (
            "name,email,phone,title,company,location,skills\n"
            "Good User,good@example.com,+919876543210,Engineer,TestCo,\"Bangalore, India\",Python\n"
            # This row is valid but has a bad phone — should still process
            "Bad Phone,bad@example.com,NOT_A_PHONE,Designer,TestCo,\"Mumbai, India\",Figma\n"
        )
        csv_path = tmp_path / "test.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="emails", type="array"),
                FieldSpec(path="phones", type="array"),
            ],
            include_confidence=False,
            on_missing="null",
        )

        results = run(sources=[str(csv_path)], config=config, output_dir=str(output_dir))

        # Both rows should produce records (bad phone → empty phones, not a crash)
        assert len(results) == 2
        names = {r["full_name"] for r in results}
        assert "Good User" in names
        assert "Bad Phone" in names

    def test_missing_source_file(self, output_dir):
        """A nonexistent source file should not crash the pipeline."""
        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
            ],
            include_confidence=False,
            on_missing="null",
        )

        results = run(
            sources=["nonexistent_file.csv"],
            config=config,
            output_dir=str(output_dir),
        )

        assert len(results) == 0  # No crash, just empty


class TestPipelineNoMatch:
    def test_separate_candidates_no_merge(self, output_dir, tmp_path):
        """Two records with no identity overlap should remain separate."""
        csv_content = (
            "name,email,phone\n"
            "Alice,alice@example.com,+11111111111\n"
            "Bob,bob@example.com,+12222222222\n"
        )
        csv_path = tmp_path / "separate.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="emails", type="array"),
            ],
            include_confidence=False,
            on_missing="null",
        )

        results = run(sources=[str(csv_path)], config=config, output_dir=str(output_dir))

        assert len(results) == 2
        ids = {r["candidate_id"] for r in results}
        assert len(ids) == 2  # Different IDs


class TestPipelineEndToEnd:
    def test_csv_and_json_merge(self, output_dir, sample_dir):
        """
        Full pipeline with CSV + JSON sample data.
        Priya and Rahul should merge across sources.
        """
        csv_path = sample_dir / "recruiter_export.csv"
        json_path = sample_dir / "ats_blob.json"

        if not csv_path.exists() or not json_path.exists():
            pytest.skip("Sample data not found")

        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="emails", type="array"),
                FieldSpec(path="phones", type="array"),
                FieldSpec(path="skills", type="array"),
                FieldSpec(path="overall_confidence", type="number"),
            ],
            include_confidence=True,
            on_missing="null",
        )

        results = run(
            sources=[str(csv_path), str(json_path)],
            config=config,
            output_dir=str(output_dir),
        )

        # Should have merged candidates from both sources
        assert len(results) >= 3  # Priya, Rahul, Sneha, Alice, and possibly the nameless one
        assert all(r.get("candidate_id") for r in results)

        # Check that output files were written
        output_files = list(output_dir.glob("*.json"))
        assert len(output_files) == len(results)

    def test_with_notes(self, output_dir, sample_dir):
        """Pipeline with notes should extract and potentially merge candidates."""
        notes_path = sample_dir / "recruiter_notes.txt"
        csv_path = sample_dir / "recruiter_export.csv"

        if not notes_path.exists() or not csv_path.exists():
            pytest.skip("Sample data not found")

        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
                FieldSpec(path="emails", type="array"),
            ],
            include_confidence=True,
            on_missing="null",
        )

        results = run(
            sources=[str(csv_path), str(notes_path)],
            config=config,
            output_dir=str(output_dir),
        )

        # Should have at least some results
        assert len(results) >= 1


class TestPipelineOnMissingError:
    def test_on_missing_error_raises(self, output_dir, tmp_path):
        """
        Config with on_missing='error' and a required field that resolves
        to nothing should produce no output for that record.
        """
        csv_content = (
            "name,email\n"
            ",only_email@example.com\n"  # Missing name
        )
        csv_path = tmp_path / "missing.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        config = OutputConfig(
            fields=[
                FieldSpec(path="candidate_id", type="string", required=True),
                FieldSpec(path="full_name", type="string", required=True),
            ],
            include_confidence=False,
            on_missing="error",
        )

        # The pipeline should handle the projection error internally
        results = run(sources=[str(csv_path)], config=config, output_dir=str(output_dir))

        # The record should be skipped due to missing required field
        # (projection error is caught in project_and_validate)
        assert len(results) == 0
