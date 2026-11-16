"""JSON Schema subset validator, ``SummaryTemplate`` and ``DEFAULT_TEMPLATE`` (design D15).

The subset has the keywords ``type``, ``properties``, ``required``, ``items``, ``enum`` and
``description``; ``type`` is one of ``string``, ``number``, ``integer``, ``boolean``, ``array``,
``object`` and is required on every node. Objects are closed: an instance property that is not
declared in ``properties`` is an error. The same validator checks templates, summaries and the
coordinator's actions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .errors import InvalidConfigError

__all__ = [
    "SUPPORTED_KEYWORDS", "SUPPORTED_TYPES", "schema_problems", "instance_errors", "SummaryTemplate",
    "DEFAULT_TEMPLATE",
]

SUPPORTED_KEYWORDS = ("type", "properties", "required", "items", "enum", "description")
SUPPORTED_TYPES = ("string", "number", "integer", "boolean", "array", "object")
MAX_DEPTH = 32
# The template travels in coordinator prompts (bounded by the coordinator input budget, D13).
MAX_TEMPLATE_BYTES = 16 * 1024
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _child(path: str, key: Any) -> str:
    if not isinstance(key, str):
        return f"{path}[{key!r}]"
    return f"{path}.{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _is_json(value: Any, depth: int = 0) -> bool:
    if depth > MAX_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v, depth + 1) for k, v in value.items())
    return False


def _json_equal(a: Any, b: Any) -> bool:
    """JSON equality: ``true`` is not ``1`` (unlike Python), ``1`` equals ``1.0``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _has_type(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if t == "integer":
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if t == "number":
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict) and all(isinstance(k, str) for k in value)
    return False


def schema_problems(schema: Any, *, top_object: bool = True) -> list[str]:
    """Every problem of ``schema`` as a schema of the subset (empty when it is usable)."""
    problems: list[str] = []
    if top_object and isinstance(schema, dict) and schema.get("type") != "object":
        problems.append("$: the top-level type must be 'object'")
    _check_node(schema, "$", 0, problems)
    return problems


def _check_node(node: Any, path: str, depth: int, problems: list[str]) -> None:
    if depth > MAX_DEPTH:
        problems.append(f"{path}: nested deeper than {MAX_DEPTH} levels")
        return
    if not isinstance(node, dict):
        problems.append(f"{path}: a schema must be an object, got {_type_name(node)}")
        return
    for key in node:
        if key not in SUPPORTED_KEYWORDS:
            problems.append(f"{path}: unsupported keyword {key!r} (supported: {', '.join(SUPPORTED_KEYWORDS)})")
    t = node.get("type")
    if "type" not in node:
        problems.append(f"{path}: 'type' is required")
    elif not isinstance(t, str) or t not in SUPPORTED_TYPES:
        problems.append(f"{path}: unsupported type {t!r} (supported: {', '.join(SUPPORTED_TYPES)})")
        t = None
    if "description" in node and not isinstance(node["description"], str):
        problems.append(f"{path}: 'description' must be a string")
    if "properties" in node:
        props = node["properties"]
        if t is not None and t != "object":
            problems.append(f"{path}: 'properties' only applies to type 'object'")
        if not isinstance(props, dict):
            problems.append(f"{path}: 'properties' must be an object")
        else:
            for name, sub in props.items():
                if not isinstance(name, str):
                    problems.append(f"{_child(path, name)}: property names must be strings")
                    continue
                _check_node(sub, _child(path, name), depth + 1, problems)
    if "required" in node:
        req = node["required"]
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        if t is not None and t != "object":
            problems.append(f"{path}: 'required' only applies to type 'object'")
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            problems.append(f"{path}: 'required' must be an array of strings")
        else:
            if len(set(req)) != len(req):
                problems.append(f"{path}: 'required' lists a property twice")
            for r in req:
                if r not in props:
                    problems.append(f"{path}: 'required' names {r!r}, which is not defined in 'properties'")
    if "items" in node:
        if t is not None and t != "array":
            problems.append(f"{path}: 'items' only applies to type 'array'")
        _check_node(node["items"], f"{path}[]", depth + 1, problems)
    elif t == "array":
        problems.append(f"{path}: type 'array' needs 'items'")
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not values:
            problems.append(f"{path}: 'enum' must be a non-empty array")
        elif not all(_is_json(v) for v in values):
            problems.append(f"{path}: 'enum' values must be JSON values")
        else:
            for i, v in enumerate(values):
                if any(_json_equal(v, w) for w in values[:i]):
                    problems.append(f"{path}: 'enum' lists {v!r} twice")
                if t is not None and not _has_type(v, t):
                    problems.append(f"{path}: 'enum' value {v!r} is not of type {t!r}")


def instance_errors(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Every way ``value`` does not conform to ``schema`` (a schema without problems)."""
    errors: list[str] = []
    _check_instance(schema, value, path, errors)
    return errors


def _check_instance(schema: dict[str, Any], value: Any, path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if not _has_type(value, t):
        errors.append(f"{path}: expected {t}, got {_type_name(value)}")
        return
    if "enum" in schema and not any(_json_equal(value, v) for v in schema["enum"]):
        allowed = ", ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])
        errors.append(f"{path}: {json.dumps(value, ensure_ascii=False)[:80]} is not one of {allowed}")
    if t == "object":
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        for name, sub in value.items():
            if name not in props:
                errors.append(f"{path}: undeclared property {name!r}")
            else:
                _check_instance(props[name], sub, _child(path, name), errors)
    elif t == "array" and "items" in schema:
        for i, item in enumerate(value):
            _check_instance(schema["items"], item, f"{path}[{i}]", errors)


# ---- templates --------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryTemplate:
    """Structure and writing instructions of a discussion summary (summary-template spec).

    ``schema`` is a schema of the subset with a top-level ``object``; the summary is a JSON object
    conforming to it. :meth:`validate` lists every problem; a discussion refuses a template that has any.
    """

    name: str
    version: int
    description: str | None = None
    instructions: str = ""
    schema: Any = None

    __hash__ = None  # type: ignore[assignment]  # the schema is a mutable dict

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "schema", copy.deepcopy(self.schema))
        except RecursionError:  # kept as given; validate() reports the depth (review C9)
            pass

    def validate(self) -> list[str]:
        """Every problem of this template (an empty list when it can be used); never raises."""
        problems = []
        if not isinstance(self.name, str) or not self.name.strip():
            problems.append("name must be a non-empty string")
        elif _CONTROL_RE.search(self.name):
            problems.append("name contains control characters")
        elif _SURROGATE_RE.search(self.name):
            problems.append("name is not valid Unicode text (lone surrogate)")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            problems.append(f"version must be a positive integer, got {self.version!r}")
        if self.description is not None and not isinstance(self.description, str):
            problems.append("description must be a string or None")
        elif isinstance(self.description, str) and _SURROGATE_RE.search(self.description):
            problems.append("description is not valid Unicode text (lone surrogate)")  # review N4
        if not isinstance(self.instructions, str):
            problems.append("instructions must be a string")
        elif _CONTROL_RE.search(self.instructions.replace("\r\n", "\n")):
            problems.append("instructions contain control characters")
        elif _SURROGATE_RE.search(self.instructions):
            problems.append("instructions are not valid Unicode text (lone surrogate)")
        if not isinstance(self.schema, dict):
            problems.append(f"schema must be an object, got {_type_name(self.schema)}")
        else:
            problems += [f"schema {p}" for p in schema_problems(self.schema)]
        if not problems:
            try:
                size = len(self._content().encode("utf-8"))
            except (InvalidConfigError, UnicodeEncodeError):
                problems.append("schema is not valid Unicode JSON (lone surrogate or non-JSON value)")
            else:
                if size > MAX_TEMPLATE_BYTES:
                    problems.append(f"instructions and schema together exceed {MAX_TEMPLATE_BYTES} bytes")
        return problems

    def _content(self) -> str:
        try:
            return json.dumps({"instructions": self.instructions, "schema": self.schema},
                              sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise InvalidConfigError(f"template content is not JSON: {e}") from None

    @property
    def fingerprint(self) -> str:
        """sha256 of the instructions and schema; independent of object key order (D15)."""
        try:
            data = self._content().encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidConfigError("template content is not valid Unicode text (lone surrogate)") from None
        return hashlib.sha256(data).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """JSON-native form, with the ``fingerprint`` (checked again by :meth:`from_dict`)."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "instructions": self.instructions,
            "schema": copy.deepcopy(self.schema),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SummaryTemplate":
        """Rebuild a template from :meth:`to_dict`; a stored ``fingerprint`` must match the content."""
        if not isinstance(data, dict):
            raise InvalidConfigError("a template must be a dict")
        known = {"name", "version", "description", "instructions", "schema", "fingerprint"}
        extra = sorted(set(data) - known)
        if extra:
            raise InvalidConfigError(f"unknown template keys {extra}")
        missing = sorted({"name", "version", "instructions", "schema"} - set(data))
        if missing:
            raise InvalidConfigError(f"template keys missing: {missing}")
        tpl = cls(name=data["name"], version=data["version"], description=data.get("description"),
                  instructions=data["instructions"], schema=data["schema"])
        if "fingerprint" in data and data["fingerprint"] != tpl.fingerprint:
            raise InvalidConfigError("the template content does not match its stored fingerprint")
        return tpl


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _strings(description: str) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "string"}}


def _objects(description: str, required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "description": description,
            "items": {"type": "object", "properties": props, "required": required}}


DEFAULT_TEMPLATE = SummaryTemplate(
    name="default",
    version=1,
    description="General-purpose summary of a coordinator-driven discussion.",
    instructions=(
        "Summarise the discussion for an engineer who did not follow it. Base every point on what the "
        "participants said; attribute positions to participants by name where it matters. Keep "
        "disagreements that were not resolved, with each side's position. Write the requirement and "
        "assumptions as they were finally understood. Be concise and concrete; use empty arrays for "
        "sections that have nothing to report."
    ),
    schema={
        "type": "object",
        "properties": {
            "title": _str("Short title of the discussion"),
            "conclusion": _str("The overall conclusion in a few sentences"),
            "requirements": _strings("The requirement as finally understood, one point per item"),
            "assumptions": _strings("Assumptions made where the requirement was ambiguous"),
            "decisions": _objects("Decisions reached", ["decision", "rationale"],
                                  decision=_str("What was decided"), rationale=_str("Why")),
            "disagreements": _objects(
                "Points without agreement", ["topic", "positions"], topic=_str("What was disputed"),
                positions=_objects("Each side's position", ["participant", "position"],
                                   participant=_str("Participant name"), position=_str("Their position"))),
            "risks": _objects("Risks identified", ["risk"], risk=_str("The risk"),
                              mitigation=_str("Proposed mitigation, if any")),
            "action_items": _objects("Follow-up work", ["item"], item=_str("What to do"),
                                     owner=_str("Suggested owner or role, if any")),
            "open_questions": _strings("Questions left open"),
        },
        "required": ["title", "conclusion", "requirements", "assumptions", "decisions", "disagreements", "risks",
                     "action_items", "open_questions"],
    },
)
