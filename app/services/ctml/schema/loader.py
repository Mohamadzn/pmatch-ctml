"""The CTML output schema: loading, enumerated values and validators.

The enumerated values of the agent contracts are read from this schema at import time,
never typed by hand, so an invalid value fails at the tool call and the error lists the
allowed values.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from jsonschema import Draft202012Validator

SCHEMA_FILE = "ctml.schema.json"


@lru_cache(maxsize=1)
def ctml_schema() -> dict:
    text = resources.files("app.services.ctml.schema").joinpath(SCHEMA_FILE).read_text(encoding="utf-8")
    return json.loads(text)


def schema_id() -> str:
    return str(ctml_schema().get("$id", ""))


def enum_values(definition: str, field: str) -> tuple[str, ...]:
    """Allowed values of an enumerated field, for example ("CTMLGenomicLeaf", "variant_category")."""
    values = ctml_schema()["$defs"][definition]["properties"][field]["enum"]
    return tuple(str(value) for value in values)


def literal_of(definition: str, field: str) -> Any:
    """A typing.Literal of the field's enumerated values, for Pydantic contracts."""
    return Literal[enum_values(definition, field)]  # type: ignore[valid-type]


@lru_cache(maxsize=1)
def document_validator() -> Draft202012Validator:
    return Draft202012Validator(ctml_schema())


@lru_cache(maxsize=8)
def definition_validator(definition: str) -> Draft202012Validator:
    """Validator for one definition, for example "CTMLMatchLeaf", with its references."""
    schema = ctml_schema()
    fragment = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(fragment)


def validation_errors(instance: Any, definition: str | None = None, limit: int = 20) -> list[dict]:
    """Schema errors as {path, message}, for the whole document or one definition."""
    validator = definition_validator(definition) if definition else document_validator()
    errors = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(part) for part in error.absolute_path)
        errors.append({"path": path, "message": error.message[:300]})
        if len(errors) >= limit:
            break
    return errors
