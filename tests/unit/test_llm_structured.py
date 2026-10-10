"""Tests for builder.llm.structured: provider-friendly schemas and robust JSON extraction / validation."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from builder._compat import ensure_uuid_utils
from builder.llm.errors import ParseError
from builder.llm.structured import (
    extract_json,
    has_free_form_objects,
    json_instruction,
    parse_json_model,
    repair_instruction,
    schema_for_llm,
)

# ------------------------------------------------------------------------------------------------- models


class Address(BaseModel):
    """A postal address."""

    street: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 ]+$")
    number: int = Field(ge=1, le=9999)


class Person(BaseModel):
    """A person record."""

    name: str
    kind: Literal["customer"] = "customer"
    home: Address = Field(description="Where the person lives")
    others: list[Address] = Field(default_factory=list, min_length=2, max_length=5)
    tags: list[str] = Field(default_factory=list, min_length=1)
    score: float = Field(default=0.5, ge=0, le=1, multiple_of=0.5)
    nickname: str | None = None


class KeywordNames(BaseModel):
    """Field names that collide with JSON-schema keywords must survive cleaning."""

    pattern: str
    default: str = "x"
    minimum: int = 0
    title: str = ""


class WithMap(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    count: int = 0


class Node(BaseModel):
    """Recursive tree node."""

    value: int
    children: list[Node] = Field(default_factory=list)


class Bad_Name(BaseModel):
    x: int = 0


def _all_nodes(node: Any) -> list[dict[str, Any]]:
    """Every dict reachable in a schema (for whole-tree assertions)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found += _all_nodes(value)
    elif isinstance(node, list):
        for value in node:
            found += _all_nodes(value)
    return found


# ------------------------------------------------------------------------------------------------- schema


def test_schema_inlines_refs_and_closes_objects() -> None:
    schema = schema_for_llm(Person)
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped
    assert schema["additionalProperties"] is False
    home = schema["properties"]["home"]
    assert home["type"] == "object"
    assert home["additionalProperties"] is False
    assert set(home["properties"]) == {"street", "number"}
    assert home["description"] == "Where the person lives"  # the field's description wins over the docstring
    others_items = schema["properties"]["others"]["items"]
    assert others_items["additionalProperties"] is False


def test_schema_drops_unsupported_keywords_everywhere() -> None:
    schema = schema_for_llm(Person)
    forbidden = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "maxItems",
        "pattern",
        "default",
    }
    for node in _all_nodes(schema):
        keyword_keys = set(node) - ({"properties"} if "properties" in node else set())
        assert not (keyword_keys & forbidden), node
    assert "minItems" not in schema["properties"]["others"]  # 2 -> dropped
    assert schema["properties"]["tags"]["minItems"] == 1  # 0/1 are supported -> kept


def test_schema_const_becomes_enum_and_titles_only_at_root() -> None:
    schema = schema_for_llm(Person)
    assert schema["properties"]["kind"]["enum"] == ["customer"]
    assert "const" not in schema["properties"]["kind"]
    assert schema["title"] == "Person"
    assert all("title" not in node for node in _all_nodes(schema["properties"]))


def test_schema_keeps_properties_named_like_keywords() -> None:
    schema = schema_for_llm(KeywordNames)
    assert set(schema["properties"]) == {"pattern", "default", "minimum", "title"}
    assert schema["required"] == ["pattern"]


def test_schema_keeps_free_form_maps_open() -> None:
    schema = schema_for_llm(WithMap)
    assert schema["properties"]["labels"]["additionalProperties"] == {"type": "string"}
    assert schema["additionalProperties"] is False
    assert has_free_form_objects(schema)
    assert not has_free_form_objects(schema_for_llm(Person))


def test_schema_keeps_recursive_definitions_and_spells_out_the_root() -> None:
    schema = schema_for_llm(Node)
    assert "$ref" not in schema  # the root is expanded so providers see its properties
    assert set(schema["properties"]) == {"value", "children"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["children"]["items"] == {"$ref": "#/$defs/Node"}
    assert schema["$defs"]["Node"]["additionalProperties"] is False
    assert schema["title"] == "Node"


def test_recursive_schema_is_accepted_by_langchain_function_conversion() -> None:
    ensure_uuid_utils()  # a blocked native uuid_utils must not stop LangChain from importing
    function_calling = pytest.importorskip("langchain_core.utils.function_calling")
    function = function_calling.convert_to_openai_function(schema_for_llm(Node))
    assert set(function["parameters"]["properties"]) == {"value", "children"}


def test_schema_root_title_is_a_safe_name() -> None:
    assert schema_for_llm(Bad_Name)["title"] == "Bad_Name"


def test_schema_does_not_mutate_pydantic_cache() -> None:
    before = json.dumps(Person.model_json_schema(), sort_keys=True)
    schema_for_llm(Person)
    assert json.dumps(Person.model_json_schema(), sort_keys=True) == before


def test_schema_is_accepted_by_anthropic_transform() -> None:
    anthropic = pytest.importorskip("anthropic")
    transformed = anthropic.transform_schema(schema_for_llm(Person))
    assert transformed["type"] == "object"
    assert transformed["additionalProperties"] is False


def test_schema_is_accepted_by_langchain_openai_function_conversion() -> None:
    ensure_uuid_utils()  # a blocked native uuid_utils must not stop LangChain from importing
    function_calling = pytest.importorskip("langchain_core.utils.function_calling")
    function = function_calling.convert_to_openai_function(schema_for_llm(Person))
    assert function["name"] == "Person"
    assert "home" in function["parameters"]["properties"]


# ---------------------------------------------------------------------------------------------- extraction


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('  \n{"a": 1}\n ', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go:\n```\n{"a": 2}\n```\nThanks', {"a": 2}),
        ('Sure! The answer is {"a": {"b": [1, 2]}} - done.', {"a": {"b": [1, 2]}}),
        ('{"text": "braces } and { inside \\" strings"}', {"text": 'braces } and { inside " strings'}),
        ('```json\n{broken\n```\nfixed: {"a": 3}', {"a": 3}),
    ],
)
def test_extract_json_variants(text: str, expected: Any) -> None:
    assert json.loads(extract_json(text)) == expected


def test_extract_json_prefers_objects_when_asked() -> None:
    text = 'There are [3] items: {"items": 3}'
    assert json.loads(extract_json(text)) == [3]
    assert json.loads(extract_json(text, prefer="object")) == {"items": 3}


def test_extract_json_array() -> None:
    assert json.loads(extract_json("result: [1, 2, 3]", prefer="array")) == [1, 2, 3]


@pytest.mark.parametrize("text", ["", "   ", "no json here", "{not: json}", "[1, 2"])
def test_extract_json_raises_parse_error(text: str) -> None:
    with pytest.raises(ParseError):
        extract_json(text)


def test_parse_json_model_success() -> None:
    reply = 'Result:\n```json\n{"name": "N", "home": {"street": "Main", "number": 3}}\n```'
    person = parse_json_model(reply, Person)
    assert isinstance(person, Person)
    assert person.home.number == 3
    assert person.kind == "customer"


def test_parse_json_model_validation_failure_names_the_fields() -> None:
    reply = '{"name": "N", "home": {"street": "Main", "number": "many"}}'
    with pytest.raises(ParseError) as info:
        parse_json_model(reply, Person)
    assert "Person" in info.value.message
    assert "home.number" in info.value.message
    assert info.value.raw_text == reply


def test_parse_json_model_enforces_dropped_constraints() -> None:
    reply = '{"name": "N", "home": {"street": "Main", "number": 0}}'  # ge=1 not in the LLM schema
    with pytest.raises(ParseError):
        parse_json_model(reply, Person)


def test_parse_json_model_without_json() -> None:
    with pytest.raises(ParseError) as info:
        parse_json_model("I cannot help with that.", Person)
    assert info.value.raw_text == "I cannot help with that."


# --------------------------------------------------------------------------------------------- prompts


def test_json_instruction_embeds_schema() -> None:
    schema = schema_for_llm(Address)
    text = json_instruction(schema)
    assert text.startswith("Return ONLY JSON matching this JSON schema")
    assert json.loads(text.split("\n", 1)[1]) == schema


def test_repair_instruction_quotes_error_and_is_bounded() -> None:
    text = repair_instruction("home.number: Input should be a valid integer" + "x" * 2000)
    assert "home.number: Input should be a valid integer" in text
    assert "ONLY one JSON object" in text
    assert len(text) < 900
