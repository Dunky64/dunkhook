"""Tiny builders for strict JSON schemas (structured outputs + strict tools).

Strict mode requires every object to list all properties in ``required`` and set
``additionalProperties: false``. Optional values are expressed as nullable types.
"""

from __future__ import annotations


def obj(**props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def string(description: str | None = None) -> dict:
    s: dict = {"type": "string"}
    if description:
        s["description"] = description
    return s


def integer(description: str | None = None) -> dict:
    s: dict = {"type": "integer"}
    if description:
        s["description"] = description
    return s


def boolean(description: str | None = None) -> dict:
    s: dict = {"type": "boolean"}
    if description:
        s["description"] = description
    return s


def enum(*values: str, description: str | None = None) -> dict:
    s: dict = {"type": "string", "enum": list(values)}
    if description:
        s["description"] = description
    return s


def array(items: dict, description: str | None = None) -> dict:
    s: dict = {"type": "array", "items": items}
    if description:
        s["description"] = description
    return s


def nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}
