"""
Profyle CLI — Candidate data ingestion and merge tool.

Usage:
    python main.py --sources <paths/urls> --config <config.json> --out <output_dir>

Examples:
    python main.py --sources data/sample/recruiter_export.csv data/sample/ats_blob.json --config config/default_config.json --out output/
    python main.py --sources data/sample/recruiter_export.csv https://github.com/octocat --config config/custom_config.json --out output/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from profyle.models import OutputConfig
from profyle.pipeline import run
from profyle.utils import setup_logging

logger = logging.getLogger("profyle")


def main() -> int:
    """CLI entry point. Returns 0 on success, 1 on fatal error."""
    parser = argparse.ArgumentParser(
        prog="profyle",
        description=(
            "Ingest candidate data from multiple sources, normalise, "
            "resolve identity, merge, and project into a configurable output shape."
        ),
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        required=True,
        help=(
            "One or more source paths or URLs. "
            "Auto-detected by extension (.csv, .json, .pdf, .docx, .txt) "
            "or URL pattern (github.com/)."
        ),
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(Path(__file__).parent / "config" / "default_config.json"),
        help="Path to the output config JSON (default: config/default_config.json).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="output",
        help="Output directory for per-candidate JSON files (default: output/).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("Config file not found: %s", args.config)
        return 1

    try:
        with open(config_path, encoding="utf-8") as f:
            config_data = json.load(f)
        config = OutputConfig(**config_data)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Invalid config file %s: %s", args.config, exc)
        return 1

    # Run pipeline
    try:
        results = run(
            sources=args.sources,
            config=config,
            output_dir=args.out,
        )
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        return 1

    if not results:
        logger.warning("Pipeline produced no output.")
        return 0

    print(f"\n[OK] {len(results)} candidate profile(s) written to {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
