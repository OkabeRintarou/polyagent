"""JSON Schema subset validator and SummaryTemplate (tasks 9.1, 9.2; summary-template spec)."""

from __future__ import annotations

import json

import pytest

from polyagent import DEFAULT_TEMPLATE, InvalidConfigError, SummaryTemplate
from polyagent.template import instance_errors, schema_problems


def obj(props, required=None):
    s = {"type": "object", "properties": props}
    if required is not None:
        s["required"] = required
    return s


S = {"type": "string"}

# ---- 9.1 schema checks ------------------------------------------------------------------


@pytest.mark.parametrize("t", ["string", "number", "integer", "boolean", "array", "object"])
def test_every_supported_type_is_accepted(t):
    node = {"type": t, "description": "d", **({"items": S} if t == "array" else {})}
    assert schema_problems(obj({"x": node}, ["x"])) == []


def test_array_needs_items():
    assert schema_problems(obj({"xs": {"type": "array"}})) == ["$.xs: type 'array' needs 'items'"]


@pytest.mark.parametrize("t", ["null", "any", "date", ["string", "null"], 3])
def test_unsupported_type_values_are_reported(t):
    problems = schema_problems(obj({"x": {"type": t}}))
    assert len(problems) == 1 and "unsupported type" in problems[0] and "$.x" in problems[0]


@pytest.mark.parametrize("kw", ["$ref", "additionalProperties", "oneOf", "pattern", "minLength", "default", "format"])
def test_keywords_outside_the_subset_are_reported(kw):
    problems = schema_problems(obj({"x": {"type": "string", kw: "v"}}))
    assert any(repr(kw) in p and "unsupported keyword" in p for p in problems)


def test_nested_unsupported_keyword_names_its_path():
    schema = obj({"d": {"type": "array", "items": obj({"r": {"type": "string", "$ref": "#/x"}})}})
    assert schema_problems(schema) == ["$.d[].r: unsupported keyword '$ref' (supported: type, properties, required, "
                                       "items, enum, description)"]


def test_top_level_must_be_object():
    assert any("top-level type must be 'object'" in p for p in schema_problems({"type": "array", "items": S}))
    assert schema_problems({"type": "array", "items": S}, top_object=False) == []


def test_type_is_required_on_every_node():
    assert schema_problems(obj({"x": {"description": "no type"}})) == ["$.x: 'type' is required"]


def test_required_must_name_defined_properties():
    problems = schema_problems(obj({"a": S}, ["a", "b"]))
    assert problems == ["$: 'required' names 'b', which is not defined in 'properties'"]


@pytest.mark.parametrize("schema,needle", [
    (obj({"a": S}, "a"), "'required' must be an array of strings"),
    (obj({"a": S}, ["a", "a"]), "lists a property twice"),
    ({"type": "string", "properties": {}}, "'properties' only applies"),
    ({"type": "object", "items": S}, "'items' only applies"),
    ({"type": "object", "properties": []}, "'properties' must be an object"),
    (obj({"e": {"type": "string", "enum": []}}), "non-empty array"),
    (obj({"e": {"type": "string", "enum": ["a", 1]}}), "is not of type 'string'"),
    (obj({"e": {"type": "string", "enum": ["a", "a"]}}), "twice"),
    (obj({"e": {"type": "string", "description": 3}}), "'description' must be a string"),
    (obj({"e": "string"}), "a schema must be an object"),
])
def test_malformed_keyword_values(schema, needle):
    problems = schema_problems(schema, top_object=False)
    assert any(needle in p for p in problems), problems


def test_deep_nesting_is_reported_not_crashing():
    node = S
    for _ in range(40):
        node = {"type": "array", "items": node}
    assert any("nested deeper" in p for p in schema_problems(obj({"x": node})))


# ---- 9.1 instance checks -----------------------------------------------------------------

SCHEMA = obj({
    "title": S,
    "score": {"type": "number"},
    "count": {"type": "integer"},
    "ok": {"type": "boolean"},
    "level": {"type": "string", "enum": ["low", "high"]},
    "decisions": {"type": "array", "items": obj({"decision": S, "why": S}, ["decision"])},
}, ["title", "decisions"])


def test_conforming_instance_has_no_errors():
    value = {"title": "t", "score": 1.5, "count": 2.0, "ok": False, "level": "high",
             "decisions": [{"decision": "d", "why": "w"}, {"decision": "e"}]}
    assert instance_errors(SCHEMA, value) == []


@pytest.mark.parametrize("value,error", [
    ({"decisions": []}, "$: missing required property 'title'"),
    ({"title": "t", "decisions": [], "extra": 1}, "$: undeclared property 'extra'"),
    ({"title": 3, "decisions": []}, "$.title: expected string, got integer"),
    ({"title": "t", "decisions": [], "count": 1.5}, "$.count: expected integer, got number"),
    ({"title": "t", "decisions": [], "count": True}, "$.count: expected integer, got boolean"),
    ({"title": "t", "decisions": [], "score": True}, "$.score: expected number, got boolean"),
    ({"title": "t", "decisions": [], "ok": 1}, "$.ok: expected boolean, got integer"),
    ({"title": "t", "decisions": [], "level": "mid"}, '$.level: "mid" is not one of "low", "high"'),
    ({"title": "t", "decisions": {}}, "$.decisions: expected array, got object"),
    ({"title": "t", "decisions": [{"why": "w"}]}, "$.decisions[0]: missing required property 'decision'"),
    ({"title": "t", "decisions": [{"decision": "d", "x": 1}]}, "$.decisions[0]: undeclared property 'x'"),
    ({"title": "t", "decisions": [{"decision": 1}]}, "$.decisions[0].decision: expected string, got integer"),
    ([], "$: expected object, got array"),
])
def test_nonconforming_instances(value, error):
    assert error in instance_errors(SCHEMA, value)


def test_enum_uses_json_equality_not_python_equality():
    schema = obj({"v": {"type": "integer", "enum": [1, 0]}})
    assert instance_errors(schema, {"v": 1.0}) == []
    schema = obj({"v": {"type": "boolean", "enum": [True]}})
    assert instance_errors(schema, {"v": True}) == []


def test_object_without_properties_is_closed():
    assert instance_errors({"type": "object"}, {"a": 1}) == ["$: undeclared property 'a'"]


# ---- 9.2 SummaryTemplate --------------------------------------------------------------------


def tech_review(version=1, **schema_override):
    schema = obj({"title": S, "conclusion": S, "decisions": {"type": "array", "items": S}},
                 ["title", "conclusion", "decisions"])
    schema.update(schema_override)
    return SummaryTemplate("tech-review", version, "Technical review", "Be brief.", schema)


def test_custom_template_is_valid_and_checks_summaries():
    tpl = tech_review()
    assert tpl.validate() == []
    assert instance_errors(tpl.schema, {"title": "t", "conclusion": "c", "decisions": ["a"]}) == []


def test_default_template_has_the_required_fields_and_is_valid():
    assert DEFAULT_TEMPLATE.validate() == []
    props = DEFAULT_TEMPLATE.schema["properties"]
    for f in ("title", "conclusion", "requirements", "assumptions", "decisions", "disagreements", "risks",
              "action_items", "open_questions"):
        assert f in props and f in DEFAULT_TEMPLATE.schema["required"]


@pytest.mark.parametrize("kwargs,needle", [
    ({"name": ""}, "name must be a non-empty string"),
    ({"name": "  "}, "name must be a non-empty string"),
    ({"version": 0}, "version must be a positive integer"),
    ({"version": -1}, "version must be a positive integer"),
    ({"version": True}, "version must be a positive integer"),
    ({"version": "1"}, "version must be a positive integer"),
    ({"description": 3}, "description must be a string"),
    ({"instructions": None}, "instructions must be a string"),
    ({"instructions": "a\x1bb"}, "control characters"),
    ({"schema": {"type": "array", "items": S}}, "top-level type must be 'object'"),
    ({"schema": obj({"a": {"type": "string", "$ref": "#"}})}, "unsupported keyword '$ref'"),
    ({"schema": obj({"a": S}, ["b"])}, "'required' names 'b'"),
    ({"schema": None}, "schema must be an object"),
    ({"instructions": "x" * 20000}, "exceed"),
])
def test_validate_reports_problems(kwargs, needle):
    base = dict(name="t", version=1, description=None, instructions="i", schema=obj({"a": S}))
    base.update(kwargs)
    problems = SummaryTemplate(**base).validate()
    assert any(needle in p for p in problems), problems


def test_validate_reports_every_problem_at_once():
    problems = SummaryTemplate("", 0, None, "i", obj({"a": {"type": "x", "$ref": "#"}}, ["b"])).validate()
    assert len(problems) == 5


def test_round_trip_through_json_and_storage():
    tpl = tech_review()
    restored = SummaryTemplate.from_dict(json.loads(json.dumps(tpl.to_dict())))
    assert restored == tpl and restored.fingerprint == tpl.fingerprint and restored.validate() == []
    assert set(tpl.to_dict()) == {"name", "version", "description", "instructions", "schema", "fingerprint"}


def test_from_dict_rejects_a_tampered_fingerprint_and_unknown_keys():
    d = tech_review().to_dict()
    d["instructions"] = "changed"
    with pytest.raises(InvalidConfigError, match="fingerprint"):
        SummaryTemplate.from_dict(d)
    with pytest.raises(InvalidConfigError, match="unknown"):
        SummaryTemplate.from_dict({**tech_review().to_dict(), "x": 1})
    with pytest.raises(InvalidConfigError, match="missing"):
        SummaryTemplate.from_dict({"name": "n"})


def test_template_is_isolated_from_the_callers_dict():
    schema = obj({"a": S})
    tpl = SummaryTemplate("t", 1, None, "i", schema)
    fp = tpl.fingerprint
    schema["properties"]["b"] = S
    assert tpl.fingerprint == fp and "b" not in tpl.schema
    tpl.to_dict()["schema"]["properties"]["c"] = S
    assert tpl.fingerprint == fp


def test_fingerprint_ignores_object_key_order():
    a = SummaryTemplate("t", 1, None, "i", {"type": "object", "properties": {"x": S, "y": S}, "required": ["x"]})
    b = SummaryTemplate("t", 1, None, "i", {"required": ["x"], "properties": {"y": S, "x": S}, "type": "object"})
    assert a.fingerprint == b.fingerprint


def test_fingerprint_depends_on_array_order():
    a = SummaryTemplate("t", 1, None, "i", obj({"x": S, "y": S}, ["x", "y"]))
    b = SummaryTemplate("t", 1, None, "i", obj({"x": S, "y": S}, ["y", "x"]))
    assert a.fingerprint != b.fingerprint


def test_fingerprint_changes_with_any_content_change_but_not_name_or_version():
    base = tech_review()
    assert SummaryTemplate("tech-review", 1, None, "Be brief!", base.schema).fingerprint != base.fingerprint
    changed = dict(base.schema, required=["title"])
    assert SummaryTemplate("tech-review", 1, None, "Be brief.", changed).fingerprint != base.fingerprint
    assert SummaryTemplate("other", 7, "x", "Be brief.", base.schema).fingerprint == base.fingerprint


def test_fingerprint_is_sha256_of_the_canonical_content():
    import hashlib
    tpl = tech_review()
    canon = json.dumps({"instructions": tpl.instructions, "schema": tpl.schema}, sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    assert tpl.fingerprint == hashlib.sha256(canon.encode()).hexdigest()


def test_a_field_named_action_is_an_ordinary_template_field():
    tpl = SummaryTemplate("t", 1, None, "i", obj({"action": S}, ["action"]))
    assert tpl.validate() == []
    assert instance_errors(tpl.schema, {"action": "ship it"}) == []


def _deep_schema(levels):
    node = {"type": "string"}
    for _ in range(levels):
        node = {"type": "array", "items": node}
    return {"type": "object", "properties": {"x": node}}


@pytest.mark.parametrize("kwargs,needle", [
    ({"schema": {"type": "object", "properties": {1: {"type": "string"}}}}, "property names must be strings"),
    ({"instructions": "a\ud800b"}, "lone surrogate"),
    ({"name": "n\udc00"}, "lone surrogate"),
    ({"schema": {"type": "object", "properties": {"k\ud800": {"type": "string"}}}}, "lone surrogate"),
    ({"schema": _deep_schema(5000)}, "nested deeper"),
])
def test_c9_validate_reports_instead_of_raising(kwargs, needle):
    base = dict(name="t", version=1, description=None, instructions="i", schema={"type": "object", "properties": {}})
    base.update(kwargs)
    problems = SummaryTemplate(**base).validate()
    assert any(needle in p for p in problems), problems


def test_c9_fingerprint_of_invalid_unicode_is_a_config_error():
    with pytest.raises(InvalidConfigError):
        SummaryTemplate("t", 1, None, "a\ud800", {"type": "object"}).fingerprint
