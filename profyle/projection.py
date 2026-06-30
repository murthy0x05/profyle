"""
Config-driven projection layer.

Takes a CanonicalRecord and an OutputConfig, and projects the canonical
record into the output shape defined by the config.

Features:
- Resolves dot-notation paths (e.g., "location.city")
- Resolves array paths (e.g., "skills[].name" → list of skill names)
- Applies per-field normalize functions
- Applies on_missing policy: null | omit | error
- Builds a dynamic Pydantic model from config and validates against it
- Output schema is derived from config — separate from canonical validation
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

from profyle.models import CanonicalRecord, OutputConfig

logger = logging.getLogger("profyle.projection")


class ProjectionError(Exception):
    """Raised when projection fails due to config or validation issues."""


def project(record: CanonicalRecord, config: OutputConfig) -> dict[str, Any]:
    """
    Project a CanonicalRecord into the shape defined by the OutputConfig.

    Parameters
    ----------
    record : CanonicalRecord
        The merged canonical profile.
    config : OutputConfig
        The runtime output configuration.

    Returns
    -------
    dict[str, Any]
        The projected and validated output dictionary.

    Raises
    ------
    ProjectionError
        If on_missing="error" and a required field is missing, or if
        validation fails.
    """
    record_dict = record.model_dump(exclude={"provenance"} if not config.include_confidence else set())

    # If not including confidence, strip confidence-related fields
    if not config.include_confidence:
        record_dict.pop("overall_confidence", None)
        record_dict.pop("provenance", None)
        # Strip confidence from skills
        if "skills" in record_dict and isinstance(record_dict["skills"], list):
            for skill in record_dict["skills"]:
                if isinstance(skill, dict):
                    skill.pop("confidence", None)
                    skill.pop("sources", None)

    projected: dict[str, Any] = {}

    for field_spec in config.fields:
        # Determine the source path in the canonical record
        source_path = field_spec.from_path or field_spec.path

        # Resolve the value from the canonical dict
        value = _resolve_path(record_dict, source_path)

        # Apply per-field normalize if specified
        if field_spec.normalize and value is not None:
            value = _apply_normalize(value, field_spec.normalize)

        # Apply on_missing policy
        if value is None or (isinstance(value, (list, dict)) and not value):
            if field_spec.required and config.on_missing == "error":
                raise ProjectionError(
                    f"Required field '{field_spec.path}' is missing "
                    f"(source path: '{source_path}') and on_missing='error'"
                )
            if config.on_missing == "omit":
                continue  # Don't include in output
            # on_missing == "null" → include as None
            value = None

        projected[field_spec.path] = value

    # Validate against derived schema
    try:
        _validate_output(projected, config)
    except ValidationError as exc:
        raise ProjectionError(f"Output validation failed: {exc}") from exc

    return projected


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    """
    Resolve a dot-notation path, supporting array element access.

    Examples:
        "full_name" → data["full_name"]
        "location.city" → data["location"]["city"]
        "skills[].name" → [skill["name"] for skill in data["skills"]]
        "skills" → data["skills"]
    """
    if not path:
        return None

    # Handle array projection: "skills[].name" → extract 'name' from each element
    if "[]." in path:
        array_part, rest = path.split("[].", 1)
        array_val = _resolve_simple_path(data, array_part)
        if not isinstance(array_val, list):
            return None
        result = []
        for item in array_val:
            if isinstance(item, dict):
                val = _resolve_simple_path(item, rest)
                if val is not None:
                    result.append(val)
        return result if result else None

    return _resolve_simple_path(data, path)


def _resolve_simple_path(data: dict | Any, path: str) -> Any:
    """Resolve a simple dot-notation path (no array brackets)."""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
        if current is None:
            return None
    return current


def _apply_normalize(value: Any, normalize: str) -> Any:
    """Apply a named normalize function to a value."""
    if normalize == "lowercase":
        if isinstance(value, str):
            return value.lower()
        if isinstance(value, list):
            return [v.lower() if isinstance(v, str) else v for v in value]
    elif normalize == "uppercase":
        if isinstance(value, str):
            return value.upper()
        if isinstance(value, list):
            return [v.upper() if isinstance(v, str) else v for v in value]
    elif normalize == "strip":
        if isinstance(value, str):
            return value.strip()
    # Unknown normalize — pass through
    return value


def _validate_output(output: dict[str, Any], config: OutputConfig) -> None:
    """
    Build a dynamic Pydantic model from the config's field specs and
    validate the output against it.
    """
    field_definitions: dict[str, Any] = {}

    for field_spec in config.fields:
        field_name = field_spec.path
        if field_name not in output and config.on_missing == "omit":
            continue  # Field was omitted, don't require it

        py_type = _config_type_to_python(field_spec.type)

        if field_spec.required:
            field_definitions[field_name] = (py_type, ...)
        else:
            field_definitions[field_name] = (py_type | None, Field(default=None))

    if not field_definitions:
        return  # Nothing to validate

    DynamicModel = create_model("ProjectedOutput", **field_definitions)
    DynamicModel.model_validate(output)


def _config_type_to_python(type_str: str) -> type:
    """Map config type strings to Python types."""
    mapping = {
        "string": str,
        "number": float,
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return mapping.get(type_str, Any)
