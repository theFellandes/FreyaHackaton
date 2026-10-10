"""Tests for builder.ingest.tool_api against the kit's tools/openapi.json and hand-written variants."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from builder.ingest.tool_api import (
    EXPECTED_TOOL_NAMES,
    FieldSpec,
    ToolApi,
    ToolApiError,
    load_tool_api,
    parse_tool_api,
)
from builder.inputs import InputError

KIT_BASE = "https://freya-bank-mock.freyavoice.workers.dev/v1"


# --- the kit's tool API -------------------------------------------------------------------------------------------------


def test_kit_api_has_the_seven_tools_in_order(kit_tool_api: ToolApi, kit_openapi_path: Path) -> None:
    assert EXPECTED_TOOL_NAMES == (
        "verify_identity", "get_account", "create_promise", "add_note", "schedule_callback",
        "transfer_to_agent", "close_call",
    )  # fmt: skip
    assert kit_tool_api.tool_names == list(EXPECTED_TOOL_NAMES)
    assert kit_tool_api.missing_tools() == []
    assert kit_tool_api.extra_tools() == []
    assert kit_tool_api.title == "Freya Bank collections tools"
    assert kit_tool_api.version == "0.3-draft"
    assert kit_tool_api.base_url == KIT_BASE
    assert kit_tool_api.sha256 == hashlib.sha256(kit_openapi_path.read_bytes()).hexdigest()


def test_kit_urls_methods_and_errors(kit_tool_api: ToolApi) -> None:
    for name in EXPECTED_TOOL_NAMES:
        contract = kit_tool_api.require(name)
        assert contract.method == "POST"
        assert contract.url == f"{KIT_BASE}/{name}"
        assert contract.path == f"/{name}"
        assert contract.errors == {
            "500": "The service failed. Nothing was recorded. The call may be repeated."
        }
        assert contract.response_description == "`ok` is false when the action was not carried out."
        assert contract.field("customer_id").required
        assert contract.field("ok", where="response").type == "boolean"
        assert contract.field("error", where="response").type == "string"


def test_kit_enums(kit_tool_api: ToolApi) -> None:
    assert kit_tool_api.require("close_call").enum("outcome") == [
        "promise", "paid", "transferred", "no_promise", "not_verified", "not_reached",
    ]  # fmt: skip
    assert kit_tool_api.require("verify_identity").enum("field") == ["date_of_birth", "phone_last4"]
    assert kit_tool_api.require("verify_identity").enum("hint") == ["day_month_swapped"]
    assert kit_tool_api.require("add_note").enum("reason") == [
        "refused",
        "hardship",
        "dispute",
        "paid",
        "service_error",
    ]
    assert kit_tool_api.require("transfer_to_agent").enum("queue") == ["restructuring", "general", "support"]
    assert kit_tool_api.require("get_account").enum("products.type") == [
        "credit_card",
        "personal_loan",
        "car_loan",
    ]
    assert kit_tool_api.require("close_call").enum("customer_id") == []  # exists, unconstrained


def test_kit_request_fields(kit_tool_api: ToolApi) -> None:
    verify = kit_tool_api.require("verify_identity")
    assert [(f.name, f.type, f.required) for f in verify.request_fields] == [
        ("customer_id", "string", True), ("field", "string", True), ("answer", "string", True),
    ]  # fmt: skip
    assert verify.field("answer").description.startswith("For date_of_birth: the date as YYYY-MM-DD.")

    promise = kit_tool_api.require("create_promise")
    assert promise.required_fields == ["customer_id", "product_id", "amount", "date"]
    assert promise.field("amount").type == "number"
    assert promise.field("date").format == "date"

    close = kit_tool_api.require("close_call")
    assert close.required_fields == ["customer_id", "outcome"]
    assert not close.field("reason").required
    assert "Required for every outcome except promise and paid" in close.field("reason").description

    note = kit_tool_api.require("add_note")
    assert note.required_fields == ["customer_id", "product_id", "reason"]
    assert not note.field("text").required

    callback = kit_tool_api.require("schedule_callback")
    assert callback.field("time").description == "HH:MM"
    assert callback.field("date").format == "date"


def test_kit_nested_products(kit_tool_api: ToolApi) -> None:
    account = kit_tool_api.require("get_account")
    products = account.field("products")
    assert products.type == "array"
    assert products.items is not None
    assert [(f.name, f.type) for f in products.items] == [
        ("product_id", "string"), ("type", "string"), ("card_last4", "string"), ("past_due", "number"),
        ("days_late", "integer"), ("minimum_payment", "number"), ("instalment_amount", "number"),
    ]  # fmt: skip
    assert account.field("products.minimum_payment").description == "credit cards only"
    assert account.field("products.instalment_amount").description == "loans only"
    assert account.get_field("products.unknown") is None
    assert account.get_field("products", where="request") is None
    verify = kit_tool_api.require("verify_identity")
    assert [f.name for f in verify.response_fields] == [
        "ok", "error", "verified", "wrong_answers_left", "all_verified", "hint",
    ]  # fmt: skip
    assert verify.field("wrong_answers_left").type == "integer"


def test_missing_tools_and_fields_raise_helpful_errors(kit_tool_api: ToolApi) -> None:
    with pytest.raises(ToolApiError, match=r"no tool 'send_sms'.*close_call"):
        kit_tool_api.require("send_sms")
    assert issubclass(ToolApiError, InputError)
    with pytest.raises(KeyError, match=r"no field 'amount'.*customer_id"):
        kit_tool_api.require("close_call").field("amount")
    with pytest.raises(KeyError):
        kit_tool_api.require("close_call").enum("amount")


def test_describe_lists_fields_and_enums(kit_tool_api: ToolApi) -> None:
    text = kit_tool_api.describe()
    assert text.startswith("Tool API: Freya Bank collections tools (version 0.3-draft)")
    assert f"- close_call  POST {KIT_BASE}/close_call" in text
    assert (
        "* outcome (string, required) one of: promise | paid | transferred | no_promise | not_verified | not_reached"
        in text
    )
    assert "* products (array of objects)" in text
    assert "        * type (string) one of: credit_card | personal_loan | car_loan" in text
    assert "* date (string, format date, required)" in text
    assert "error 500: The service failed." in text


def test_dump_round_trip(kit_tool_api: ToolApi) -> None:
    restored = ToolApi.model_validate(json.loads(json.dumps(kit_tool_api.model_dump(mode="json"))))
    assert restored == kit_tool_api


# --- variants -----------------------------------------------------------------------------------------------------------


def write_json(tmp_path: Path, document: dict[str, Any], name: str = "api.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_refs_allof_nullable_and_server_variables(tmp_path: Path) -> None:
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Variant", "version": "2"},
        "servers": [
            {
                "url": "https://{env}.bank.test/{base}/",
                "variables": {"env": {"default": "api"}, "base": {"default": "v2"}},
            }
        ],
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
                "Outcome": {"type": "string", "enum": ["done", "not_done"], "description": "How it ended"},
                "Request": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {
                            "type": "object",
                            "properties": {
                                "outcome": {"$ref": "#/components/schemas/Outcome"},
                                "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                "flags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
                                "on": {"type": "boolean", "enum": [True, False]},
                            },
                            "required": ["outcome"],
                        },
                    ]
                },
                "Wrapper": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/Data"}}},
                "Data": {"type": "object", "properties": {"all_verified": {"type": ["boolean", "null"]}}},
            },
            "requestBodies": {
                "Close": {
                    "content": {
                        "application/json; charset=utf-8": {
                            "schema": {"$ref": "#/components/schemas/Request"}
                        }
                    }
                }
            },
            "responses": {
                "Ok": {
                    "description": "fine",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Wrapper"}}},
                }
            },
        },
        "paths": {
            "/tools/close_call/": {
                "post": {
                    "summary": "Close.",
                    "requestBody": {"$ref": "#/components/requestBodies/Close"},
                    "responses": {
                        "201": {"$ref": "#/components/responses/Ok"},
                        "422": {"description": "bad input"},
                    },
                },
            }
        },
    }
    api = load_tool_api(write_json(tmp_path, document))
    close = api.require("close_call")
    assert api.base_url == "https://api.bank.test/v2"
    assert close.url == "https://api.bank.test/v2/tools/close_call/"
    assert [f.name for f in close.request_fields] == ["customer_id", "outcome", "note", "flags", "on"]
    assert close.required_fields == ["customer_id", "outcome"]
    assert close.field("outcome").enum == ["done", "not_done"]
    assert close.field("outcome").description == "How it ended"
    assert close.field("note").type == "string"
    assert (close.field("flags").type, close.field("flags").enum) == ("array", ["a", "b"])
    assert close.field("on").enum == ["true", "false"]
    assert close.field("data.all_verified").type == "boolean"
    assert close.response_description == "fine"
    assert close.errors == {"422": "bad input"}


def test_recursive_schema_terminates(tmp_path: Path) -> None:
    document = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "Node": {
                    "description": "A tree node",
                    "properties": {
                        "name": {"type": "string"},
                        "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                        "parent": {"$ref": "#/components/schemas/Node"},
                    },
                }
            }
        },
        "paths": {
            "/get_account": {
                "post": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"root": {"$ref": "#/components/schemas/Node"}},
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    contract = load_tool_api(write_json(tmp_path, document)).require("get_account")
    root = contract.field("root")
    assert root.type == "object"
    assert [f.name for f in root.items or []] == ["name", "children", "parent"]
    # The second occurrence of Node on the same path is not expanded again, but keeps its shape.
    parent = contract.field("root.parent")
    assert (parent.type, parent.description, parent.items) == ("object", "A tree node", None)
    children = contract.field("root.children")
    assert (children.type, children.items) == ("array", None)


def test_yaml_keeps_yes_no_strings_and_int_status_codes(tmp_path: Path) -> None:
    path = tmp_path / "api.yaml"
    path.write_text(
        """
openapi: 3.0.3
info:
  title: YAML tools
  version: 2026-10-01
paths:
  /transfer_to_agent:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [customer_id, consent]
              properties:
                customer_id: {type: string}
                consent: {type: string, enum: [yes, no, on, off]}
      responses:
        200:
          description: done
        500:
          description: failed
""",
        encoding="utf-8",
    )
    api = load_tool_api(path)
    contract = api.require("transfer_to_agent")
    assert api.version == "2026-10-01"  # not turned into a date object
    assert contract.enum("consent") == ["yes", "no", "on", "off"]
    assert contract.required_fields == ["customer_id", "consent"]
    assert contract.response_description == "done"
    assert contract.errors == {"500": "failed"}
    assert contract.url == "/transfer_to_agent"  # no servers -> path only


def test_swagger2_body_and_query_parameters(tmp_path: Path) -> None:
    document = {
        "swagger": "2.0",
        "host": "bank.test",
        "basePath": "/v0",
        "schemes": ["http"],
        "definitions": {
            "Promise": {
                "type": "object",
                "required": ["amount"],
                "properties": {"amount": {"type": "number"}},
            }
        },
        "paths": {
            "/create_promise": {
                "post": {
                    "parameters": [
                        {"in": "body", "name": "body", "schema": {"$ref": "#/definitions/Promise"}},
                        {"in": "query", "name": "dry", "type": "boolean"},
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "schema": {"type": "object", "properties": {"promise_id": {"type": "string"}}},
                        }
                    },
                }
            },
            "/get_account/{customer_id}": {
                "get": {
                    "parameters": [{"in": "path", "name": "customer_id", "type": "string"}],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    api = load_tool_api(write_json(tmp_path, document))
    promise = api.require("create_promise")
    assert promise.url == "http://bank.test/v0/create_promise"
    assert [(f.name, f.type, f.required) for f in promise.request_fields] == [
        ("amount", "number", True), ("dry", "boolean", False),
    ]  # fmt: skip
    assert promise.field("promise_id").type == "string"
    account = api.require("get_account")  # name taken from the last non-parameter segment
    assert account.method == "GET"
    assert account.field("customer_id").required


def test_missing_and_extra_tools_warn_but_load(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    document = {
        "openapi": "3.0.0",
        "paths": {
            "/close_call": {"post": {"responses": {"200": {"description": "ok"}}}},
            "/send_letter": {"post": {"responses": {"200": {"description": "ok"}}}},
        },
    }
    with caplog.at_level(logging.WARNING, logger="builder.ingest.tool_api"):
        api = load_tool_api(write_json(tmp_path, document))
    assert api.tool_names == ["close_call", "send_letter"]
    assert api.extra_tools() == ["send_letter"]
    assert "verify_identity" in api.missing_tools()
    assert "lacks expected tools" in caplog.text and "send_letter" in caplog.text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# just markdown\n\n- a list\n- b", "not an OpenAPI/Swagger document"),
        ('{"openapi": "3.0.0", "paths": {}}', "no paths"),
        (
            '{"openapi": "3.0.0", "paths": {"/x": {"post": {"requestBody": {"$ref": "#/nope"}}}}}',
            "Unresolvable",
        ),
        (
            '{"openapi": "3.0.0", "paths": {"/x": {"post": {"requestBody": {"$ref": "other.json#/a"}}}}}',
            "External",
        ),
        ('{"openapi": "3.0.0", "paths": {"/x": {"parameters": []}}}', "no usable operations"),
        (": : :\n\t- [", "neither valid JSON nor valid YAML"),
    ],
)
def test_invalid_documents_raise_tool_api_error(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "api.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ToolApiError, match=message):
        load_tool_api(path)


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ToolApiError, match="Cannot read"):
        load_tool_api(tmp_path / "missing.json")
    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ToolApiError, match="not UTF-8"):
        load_tool_api(binary)


def test_parse_tool_api_accepts_parsed_documents() -> None:
    api = parse_tool_api({"openapi": "3.0.0", "paths": {"/add_note": {"post": {}}}}, sha256="abc")
    assert api.sha256 == "abc"
    assert api.require("add_note").request_fields == []


def test_field_spec_child_lookup() -> None:
    spec = FieldSpec(name="products", type="array", items=[FieldSpec(name="type", enum=["x"])])
    assert spec.child("type") == FieldSpec(name="type", enum=["x"])
    assert spec.child("missing") is None
    assert FieldSpec(name="leaf").child("anything") is None
