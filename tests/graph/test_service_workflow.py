"""Generic inbound service builds use invented bank data, with no live services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from builder.checks.lint import lint_workflow
from builder.compiler.errors import CompileError
from builder.compiler.profiles.service import ServicePlan, compile_service_workflow, service_lint_api
from builder.graph import build_graph, initial_state
from builder.graph.nodes import self_test, service_workflow
from builder.graph.routing import (
    after_compile_workflow,
    after_guard_inputs,
    after_static_checks,
    fallback_target,
)
from builder.guardrails import check_workflow
from builder.ingest.brief_loader import load_briefs
from builder.ingest.tool_api import FieldSpec, load_tool_api

BRIEF = """Elm Bank inbound service. The caller is already verified by the authenticated app.
The platform supplies memberReference and todayISO as call variables.
Use memberReference as member_ref when looking up savings plans. Read the returned plans.
Only activate the caller's selected plan after explicit confirmation. Never claim a failed activation succeeded.
If no plans exist, explain that none are available. If a lookup fails, explain the error and offer a retry.
Finish by asking whether any help remains and saying goodbye before ending.
"""


def api_document() -> dict[str, Any]:
    def operation(name: str, properties: dict[str, Any]) -> dict[str, Any]:
        return {"operationId": name, "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": properties, "required": list(properties),
        }}}}, "responses": {"200": {"description": "Success", "content": {"application/json": {
            "schema": {"type": "object", "properties": {"status": {"type": "string"}}},
        }}}}}
    return {"openapi": "3.0.3", "info": {"title": "Elm service", "version": "1"},
            "servers": [{"url": "https://elm-mock.example"}], "paths": {
                "/plans": {"post": operation("plans", {"member_ref": {"type": "string"}})},
                "/activate": {"post": operation("activate", {
                    "member_ref": {"type": "string"}, "plan_id": {"type": "string"},
                    "channel": {"type": "string", "enum": ["voice", "app"]},
                    "confirmed": {"type": "boolean"},
                })},
            }}


def plan_data() -> dict[str, Any]:
    evidence = {"document": "BRIEF.md", "quote": "The platform supplies memberReference and todayISO as call variables."}
    return {"language": "en", "system_prompt": "You are Elm Bank's inbound service assistant. The caller is verified.",
            "call_variables": [{"name": name, "description": name, "evidence": evidence}
                               for name in ("memberReference", "todayISO")],
            "rules": [{"id": "S1", "instruction": "Obtain explicit confirmation before activation.",
                       "evidence": {"document": "BRIEF.md", "quote": "Only activate the caller's selected plan after explicit confirmation."}}],
            "initial_stage": "lookup", "closing_instruction": "Ask whether more help is needed; say goodbye.",
            "stages": [{"name": "lookup", "task_message": "List the member's plans; handle errors honestly.",
                        "tools": [{"tool": "plans", "when_to_call": "to list the member's plans, then read the result.",
                                   "bindings": [{"field": "member_ref", "call_variable": "memberReference"}]}],
                        "exits": [{"target": "activate", "instruction": "After a successful lookup and caller's selected plan."},
                                  {"target": "end_call", "instruction": "Caller wants to end, or no plans after explaining and saying goodbye."}]},
                       {"name": "activate", "task_message": "Read back selection, obtain consent, then activate. Remain on failure.",
                        "tools": [{"tool": "activate", "when_to_call": "only after explicit confirmation, with a returned plan ID and exact channel enum.",
                                   "bindings": [{"field": "member_ref", "call_variable": "memberReference"}]}],
                        "exits": [{"target": "lookup", "instruction": "Caller wants to select another plan."},
                                  {"target": "end_call", "instruction": "After the operation result and no further help; say goodbye."}]}]}


@pytest.fixture
def service_source(tmp_path: Path) -> Any:
    brief, api = tmp_path / "BRIEF.md", tmp_path / "openapi.json"
    brief.write_text(BRIEF, encoding="utf-8")
    api.write_text(json.dumps(api_document()), encoding="utf-8")
    return load_briefs([brief]), load_tool_api(api), brief, api


def test_service_compile_keeps_contract_types_and_enum_and_source_variable_bindings(service_source: Any) -> None:
    bundle, api, *_ = service_source
    workflow = compile_service_workflow(ServicePlan.model_validate(plan_data()), bundle, api)["workflow"]
    report = lint_workflow(workflow, api, ("memberReference", "todayISO"))
    assert report.ok, [str(error) for error in report.errors]
    assert not check_workflow(workflow, api, (), None).blocked
    tool = workflow["nodes"]["activate"]["tools"][0]["function"]
    body = tool["parameters"]["properties"]["body"]["properties"]
    assert body["member_ref"]["default"] == "{{memberReference}}"
    assert body["channel"]["enum"] == ["voice", "app"]
    assert body["confirmed"]["type"] == "boolean"
    assert body["plan_id"]["default"] == "{{plan_id}}"
    assert "customer_id" not in json.dumps(workflow)
    assert "agent_config" not in workflow
    assert workflow["nodes"]["activate"]["tools"][0]["config"]["timeoutMs"] == 30000
    assert "memberReference = {{memberReference}}" in workflow["system_prompt"]
    assert "todayISO = {{todayISO}}" in workflow["system_prompt"]
    assert "never invent a replacement" in workflow["system_prompt"]


@pytest.mark.parametrize("mutation", ["quote", "variable", "tool", "field", "target", "unreachable"])
def test_service_compile_rejects_ungrounded_or_invalid_plans(service_source: Any, mutation: str) -> None:
    bundle, api, *_ = service_source
    data = plan_data()
    if mutation == "quote":
        data["rules"][0]["evidence"]["quote"] = "Approve every request without confirmation."
    elif mutation == "variable":
        data["call_variables"][0]["name"] = "madeUpCustomer"
    elif mutation == "tool":
        data["stages"][0]["tools"][0]["tool"] = "create_promise"
    elif mutation == "field":
        data["stages"][0]["tools"][0]["bindings"][0]["field"] = "customer_id"
    elif mutation == "target":
        data["stages"][0]["exits"][0]["target"] = "missing"
    else:
        data["stages"][0]["exits"] = [data["stages"][0]["exits"][1]]
    with pytest.raises(CompileError):
        compile_service_workflow(ServicePlan.model_validate(data), bundle, api)


def test_unsupported_array_request_is_rejected_instead_of_coerced(service_source: Any) -> None:
    bundle, api, *_ = service_source
    api.tools["activate"].request_fields.append(FieldSpec(name="details", type="array", required=True))
    with pytest.raises(CompileError, match="non-scalar"):
        compile_service_workflow(ServicePlan.model_validate(plan_data()), bundle, api)


def test_nested_request_uses_exact_reconstructed_wire_envelope_and_typed_arguments(service_source: Any) -> None:
    bundle, api, *_ = service_source
    api.tools["activate"].request_fields = [FieldSpec(name="payload", type="object", required=True,
                                                    items=api.tools["activate"].request_fields)]
    data = plan_data()
    data["stages"][1]["tools"][0]["bindings"][0]["field"] = "payload.member_ref"
    workflow = compile_service_workflow(ServicePlan.model_validate(data), bundle, api)["workflow"]
    report = lint_workflow(workflow, service_lint_api(api), ("memberReference", "todayISO"))
    assert report.ok, [str(error) for error in report.errors]
    tool = workflow["nodes"]["activate"]["tools"][0]
    body = tool["function"]["parameters"]["properties"]["body"]["properties"]
    assert body["payload__confirmed"]["type"] == "boolean"
    assert body["payload__channel"]["enum"] == ["voice", "app"]
    assert body["payload__member_ref"]["default"] == "{{memberReference}}"
    config = {"body": {"payload__member_ref": "M42", "payload__plan_id": "P7",
                       "payload__channel": "voice", "payload__confirmed": True}}
    exec(tool["pre_process_code"], {"config": config, "vars": {}})
    assert config["body"] == {"payload": {"member_ref": "M42", "plan_id": "P7", "channel": "voice", "confirmed": True}}


def test_source_outcomes_remain_distinct_platform_end_reasons(service_source: Any) -> None:
    bundle, api, *_ = service_source
    data = plan_data()
    data["outcomes"] = [{"name": "service_complete", "reason": "Service is finished.",
                         "evidence": {"document": "BRIEF.md", "quote": "Finish by asking whether any help remains and saying goodbye before ending."}}]
    for stage in data["stages"]:
        for edge in stage["exits"]:
            if edge["target"] == "end_call":
                edge["target"] = "service_complete"
    workflow = compile_service_workflow(ServicePlan.model_validate(data), bundle, api)["workflow"]
    assert workflow["nodes"]["service_complete"]["terminal"]["function"]["name"] == "service_complete"
    assert "end_call" not in workflow["nodes"]


def test_form_request_encodes_exact_scalar_fields_and_reserved_utf8_values(service_source: Any) -> None:
    from urllib.parse import parse_qs

    bundle, api, *_ = service_source
    api.tools["activate"].request_content_type = "application/x-www-form-urlencoded"
    api.tools["activate"].headers["Content-Type"] = "application/x-www-form-urlencoded"
    workflow = compile_service_workflow(ServicePlan.model_validate(plan_data()), bundle, api)["workflow"]
    tool = workflow["nodes"]["activate"]["tools"][0]
    config = {"body": {"member_ref": "M&42", "plan_id": "P+7=one", "channel": "Ses Türkçe", "confirmed": True}}
    exec(tool["pre_process_code"], {"config": config, "vars": {}})
    assert isinstance(config["body"], str)
    assert parse_qs(config["body"]) == {"member_ref": ["M&42"], "plan_id": ["P+7=one"],
                                       "channel": ["Ses Türkçe"], "confirmed": ["true"]}
    assert config["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_numeric_request_enums_remain_numbers_on_platform_schema(service_source: Any) -> None:
    bundle, api, *_ = service_source
    api.tools["activate"].request_fields.append(FieldSpec(name="period", type="integer", enum=["2", "6"], required=True))
    workflow = compile_service_workflow(ServicePlan.model_validate(plan_data()), bundle, api)["workflow"]
    body = workflow["nodes"]["activate"]["tools"][0]["function"]["parameters"]["properties"]["body"]["properties"]
    assert body["period"]["enum"] == [2, 6]
    assert lint_workflow(workflow, service_lint_api(api), ("memberReference", "todayISO")).ok


def test_duplicate_readme_filenames_do_not_overwrite_grounded_variable_evidence(service_source: Any, tmp_path: Path) -> None:
    _, api, brief, _ = service_source
    first = tmp_path / "README.md"
    first.write_text("The platform supplies memberReference and todayISO as call variables.", encoding="utf-8")
    mock = tmp_path / "mock"
    mock.mkdir()
    second = mock / "README.md"
    second.write_text("Run the mock service with the deployment CLI.", encoding="utf-8")
    bundle = load_briefs([brief, first, second])
    data = plan_data()
    for variable in data["call_variables"]:
        variable["evidence"]["document"] = "README.md"
    compiled = compile_service_workflow(ServicePlan.model_validate(data), bundle, api)
    assert compiled["profile"] == "service"


@pytest.mark.parametrize("changed_word", [False, True])
def test_forwarded_email_and_inline_emphasis_preserve_exact_evidence_words(
    service_source: Any, tmp_path: Path, changed_word: bool,
) -> None:
    _, api, brief, _ = service_source
    mail = tmp_path / "policy-mail.txt"
    mail.write_text(
        ">> Approved policy: the assistant must **not**\n"
        "> > reveal more than the final *four* digits.\n"
        "> Numeric rule remains amount > 0 and amount * 2 < 10.\n", encoding="utf-8")
    bundle = load_briefs([brief, mail])
    data = plan_data()
    data["rules"][0]["evidence"] = {
        "document": mail.name,
        "quote": "the assistant must not reveal more than the final " + ("six" if changed_word else "four") + " digits.",
    }
    if changed_word:
        with pytest.raises(CompileError, match="evidence is absent"):
            compile_service_workflow(ServicePlan.model_validate(data), bundle, api)
    else:
        assert compile_service_workflow(ServicePlan.model_validate(data), bundle, api)["profile"] == "service"


def test_evidence_normalization_retains_math_operators() -> None:
    from builder.compiler.profiles.service import _normal

    assert _normal("amount > 0 and amount * 2 < 10; power=2**3") == "amount > 0 and amount * 2 < 10; power=2**3"


def test_evidence_paraphrase_ellipses_and_multiple_graph_errors_are_reported_together(service_source: Any) -> None:
    bundle, api, *_ = service_source
    data = plan_data()
    data["rules"][0]["evidence"]["quote"] = "Only activate ... after explicit confirmation."
    data["call_variables"][0]["evidence"]["quote"] = "The platform gives memberReference."
    data["stages"][0]["tools"][0]["bindings"][0]["field"] = "unknown_field"
    data["stages"][1]["exits"].append(data["stages"][1]["exits"][1])
    with pytest.raises(CompileError) as rejected:
        compile_service_workflow(ServicePlan.model_validate(data), bundle, api)
    feedback = str(rejected.value)
    assert "rule S1: service evidence is absent" in feedback
    assert "call variable memberReference: service evidence is absent" in feedback
    assert "unknown service request field" in feedback
    assert "needs distinct exits" in feedback


def test_service_routes_are_bounded_and_never_fall_back_to_collections_or_agentic() -> None:
    assert after_guard_inputs({"service_mode": True, "track": "agentic"}) == "service_workflow"
    assert after_static_checks({"service_mode": True, "lint": {"ok": False}}) == "service_workflow"
    assert after_compile_workflow({"service_mode": True, "failed": "service_workflow", "compile_repairs": 2}) == "finalize"
    assert fallback_target({"service_mode": True, "limits": {"allow_agentic": True}}) == "finalize"


@pytest.mark.parametrize("fetch_route", ["optional_branch", "every_path", "current_stage"])
def test_response_dependency_checks_every_incoming_path_and_local_fetch(
    service_source: Any, fetch_route: str,
) -> None:
    bundle, api, *_ = service_source
    api.tools["contact_lookup"] = api.tools["plans"].model_copy(update={"name": "contact_lookup"})
    data = plan_data()
    data["stages"][0]["exits"].append({"target": "offer", "instruction": "Fetch contact details before offering."})
    data["stages"].append({"name": "offer", "task_message": "Fetch and inspect the registered contact details.",
                           "tools": [{"tool": "contact_lookup", "when_to_call": "Before the consent question.",
                                      "bindings": [{"field": "member_ref", "call_variable": "memberReference"}]}],
                           "exits": [{"target": "activate", "instruction": "After contact details were fetched."},
                                     {"target": "end_call", "instruction": "The caller wants to end."}]})
    data["stages"][1]["task_message"] += " Read contact_lookup_response.data.email before asking for consent."
    if fetch_route == "every_path":
        data["stages"][0]["exits"] = [edge for edge in data["stages"][0]["exits"] if edge["target"] != "activate"]
    elif fetch_route == "current_stage":
        data["stages"][1]["tools"].append(data["stages"][2]["tools"][0])
    if fetch_route == "optional_branch":
        with pytest.raises(CompileError, match=r"needs contact_lookup_response.*every incoming path"):
            compile_service_workflow(ServicePlan.model_validate(data), bundle, api)
    else:
        assert compile_service_workflow(ServicePlan.model_validate(data), bundle, api)["profile"] == "service"


async def test_generic_service_graph_dry_run_preserves_original_model_prompt_and_provenance(
    service_source: Any, make_ctx: Any, make_gateway: Any, tmp_dirs: Any,
) -> None:
    bundle, api, brief, api_path = service_source
    gateway = make_gateway(responders={"ServicePlan": lambda call: plan_data(),
                                      "ServicePolicyReview": lambda call: {"issues": []}})
    ctx = make_ctx(llm=gateway)
    start = initial_state(run_id="service-test", slot="elm", started_at="2026-10-11T17:00:00Z", dry_run=True,
                          inputs={"briefs": [str(brief)], "tool_api": str(api_path), "case_folder": str(brief.parent)})
    state = await build_graph().ainvoke(start, context=ctx)
    assert state["final"]["status"] == "dry_run", state["final"].get("errors")
    assert state["profile"] == "service" and state["service_mode"] is True
    assert state.get("ir") is None and state.get("ledger") is None
    assert [call.schema for call in gateway.calls] == ["ServicePlan", "ServicePolicyReview"]
    assert BRIEF.strip() in gateway.calls[0].user
    assert BRIEF.strip() in gateway.calls[1].user and "CANDIDATE SERVICE PLAN" in gateway.calls[1].user
    receipt = json.loads((tmp_dirs.out / "elm.service-plan.json").read_text(encoding="utf-8"))
    assert receipt["bundle_sha256"] == bundle.sha256 and receipt["tool_api_sha256"] == api.sha256
    assert receipt["policy_review"] == {"issues": []}


async def test_service_planner_failure_counts_attempt_without_retaining_stale_workflow(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    bundle, api, *_ = service_source
    gateway = make_gateway(fail_schemas={"ServicePlan"})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "service_mode": True, "service_attempts": 1, "compiled": {"workflow": {"stale": True}}}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert update["failed"] == "service_workflow" and update["service_attempts"] == 2
    assert update["compile_repairs"] == 1 and update["compiled"] == {}


async def test_source_policy_review_can_repair_after_deterministic_repair_budget_was_spent(
    service_source: Any, make_ctx: Any, make_gateway: Any, tmp_dirs: Any,
) -> None:
    _, _, brief, api_path = service_source
    incomplete = plan_data()
    incomplete["rules"][0]["instruction"] = "Activate immediately."
    incomplete["stages"][1]["task_message"] = "Activate immediately; report the result."
    incomplete["stages"][1]["tools"][0]["when_to_call"] = "Activate immediately with the returned plan ID."
    invalid_target = plan_data()
    invalid_target["stages"][0]["exits"][0]["target"] = "nonexistent_stage"
    invalid_tool = plan_data()
    invalid_tool["stages"][0]["tools"][0]["tool"] = "invented_lookup"
    plans = iter([invalid_target, invalid_tool, incomplete, plan_data()])
    issue = {"location": "activate task and tool prerequisites",
             "problem": "The plan activates without the caller's explicit confirmation.",
             "correction": "Ask for confirmation of the selected returned plan before activating it.",
             "evidence": {"document": "BRIEF.md",
                          "quote": "Only activate the caller's selected plan after explicit confirmation."}}
    reviews = iter([{"issues": [issue]}, {"issues": []}])
    gateway = make_gateway(responders={"ServicePlan": lambda call: next(plans),
                                      "ServicePolicyReview": lambda call: next(reviews)})
    start = initial_state(run_id="review-test", slot="elm-reviewed", started_at="2026-10-11T17:00:00Z",
                          dry_run=True, inputs={"briefs": [str(brief)], "tool_api": str(api_path),
                                                "case_folder": str(brief.parent)})
    state = await build_graph().ainvoke(start, context=make_ctx(llm=gateway))
    assert state["final"]["status"] == "dry_run", state["final"].get("errors")
    assert state["service_attempts"] == 4 and state["compile_repairs"] == 2
    assert state["service_policy_repairs"] == 1
    assert [call.schema for call in gateway.calls] == ["ServicePlan", "ServicePlan", "ServicePlan",
                                                     "ServicePolicyReview", "ServicePlan", "ServicePolicyReview"]
    assert issue["problem"] in gateway.calls[4].user and issue["correction"] in gateway.calls[4].user
    assert "PRIOR CANDIDATE TO REPAIR" in gateway.calls[4].user and "Activate immediately" in gateway.calls[4].user
    assert BRIEF.strip() in gateway.calls[3].user
    assert "Activate immediately" not in json.dumps(state["compiled"]["workflow"])
    receipt = json.loads((tmp_dirs.out / "elm-reviewed.service-plan.json").read_text(encoding="utf-8"))
    assert receipt["attempt"] == 4 and receipt["policy_review"] == {"issues": []}
    assert receipt["policy_repairs"] == 1 and receipt["compile_repairs"] == 2


async def test_policy_reviewer_reads_actual_compiler_fixed_transport_defaults(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    bundle, api, *_ = service_source
    api.tools["activate"].request_fields.append(
        FieldSpec(name="service_kind", type="string", required=True, enum=["Savings"], fixed="Savings")
    )

    def review(call: Any) -> dict[str, Any]:
        transport = json.loads(call.user.split("ACTUAL COMPILED TRANSPORT INPUTS (defaults supplied by compiler):\n", 1)[1])
        body = transport["activate"]["activate"]["properties"]["body"]["properties"]
        assert body["service_kind"]["default"] == "Savings"
        assert body["member_ref"]["default"] == "{{memberReference}}"
        assert body["channel"]["enum"] == ["voice", "app"]
        assert body["channel"]["default"] == "{{channel}}"
        return {"issues": []}

    gateway = make_gateway(responders={"ServicePlan": lambda call: plan_data(), "ServicePolicyReview": review})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "service_mode": True}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert not update.get("failed"), update.get("errors")
    assert update["compiled"]["workflow"]["nodes"]["activate"]["tools"][0]["function"]["parameters"][
        "properties"]["body"]["properties"]["service_kind"]["default"] == "Savings"


async def test_grounded_policy_review_stops_at_configured_policy_repair_cap(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    _, _, brief, api_path = service_source
    incomplete = plan_data()
    incomplete["rules"][0]["instruction"] = "Activate immediately."
    incomplete["stages"][1]["task_message"] = "Activate immediately; report the result."
    incomplete["stages"][1]["tools"][0]["when_to_call"] = "Activate immediately with the returned plan ID."
    review = {"issues": [{"location": "activate", "problem": "Explicit consent is missing.",
                           "correction": "Ask for explicit confirmation before activation.", "evidence": {
                               "document": "BRIEF.md",
                               "quote": "Only activate the caller's selected plan after explicit confirmation."}}]}
    gateway = make_gateway(responders={"ServicePlan": lambda call: incomplete,
                                      "ServicePolicyReview": lambda call: review})
    start = initial_state(run_id="review-cap-test", slot="elm-review-cap", started_at="2026-10-11T17:00:00Z",
                          dry_run=True, inputs={"briefs": [str(brief)], "tool_api": str(api_path),
                                                "case_folder": str(brief.parent)})
    state = await build_graph().ainvoke(start, context=make_ctx(llm=gateway, max_policy_repairs=1))
    assert state["final"]["status"] == "failed"
    assert state["service_attempts"] == 2 and state["service_policy_repairs"] == 1
    assert state["compile_repairs"] == 0 and state["compiled"] == {}
    assert [call.schema for call in gateway.calls] == ["ServicePlan", "ServicePolicyReview"] * 2


async def test_source_policy_review_rejects_fabricated_evidence_and_retains_no_workflow(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    bundle, api, *_ = service_source
    review = {"issues": [{"location": "activate", "problem": "A fabricated source demands no consent.",
                           "correction": "Activate without asking.", "evidence": {
                               "document": "BRIEF.md", "quote": "The bank requires immediate activation without consent."}}]}
    gateway = make_gateway(responders={"ServicePlan": lambda call: plan_data(),
                                      "ServicePolicyReview": lambda call: review,
                                      "EvidenceSelections": lambda call: {"selections": []}})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "service_mode": True, "compiled": {"workflow": {"stale": True}}}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert update["failed"] == "service_workflow" and update["compiled"] == {}
    assert "Evidence selections must cover every invalid location" in update["compile_feedback"]
    assert "Activate without asking" not in update["compile_feedback"]
    assert update["service_attempts"] == 1
    assert update["service_policy_repair_pending"] is False


async def test_node_repairs_plan_and_review_citations_without_changing_runtime_instructions(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    bundle, api, *_ = service_source
    plan = plan_data()
    plan["rules"][0]["evidence"]["quote"] = "Only activate ... after explicit confirmation."
    plan["stages"][1]["task_message"] = "Activate immediately; report the result."
    review = {"issues": [{"location": "activate.task_message",
                           "problem": "The stage's immediate activation instruction conflicts with required consent.",
                           "correction": "Ask for explicit confirmation before activation.",
                           "evidence": {"document": "BRIEF.md",
                                        "quote": "Only activate ... after explicit confirmation."}}]}

    def select(call: Any) -> dict[str, Any]:
        data = json.loads(call.user)
        span = next(span for span in data["indexed_original_source_spans"]
                    if "Only activate the caller's selected plan after explicit confirmation." in span["text"])
        return {"selections": [{"location": item["location"], "span_id": span["span_id"]}
                               for item in data["invalid_items"]]}

    gateway = make_gateway(responders={"ServicePlan": lambda call: plan, "ServicePolicyReview": lambda call: review,
                                      "EvidenceSelections": select,
                                      "EvidenceSupportChecks": lambda call: {"checks": [{
                                          "location": "issues[0].evidence", "supported": True}]}})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "service_mode": True, "inputs": {"tool_base_url": "https://elm-mock.example"}}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert update["failed"] == "service_workflow" and update["service_policy_repair_pending"] is True
    assert update["compiled"] == {} and update["compile_repairs"] == 0
    assert [call.schema for call in gateway.calls] == ["ServicePlan", "EvidenceSelections",
                                                     "ServicePolicyReview", "EvidenceSelections", "EvidenceSupportChecks"]
    assert update["service_plan"]["stages"] == plan["stages"]
    assert update["service_plan"]["rules"][0]["instruction"] == plan["rules"][0]["instruction"]
    assert update["service_plan"]["rules"][0]["evidence"]["quote"] in BRIEF
    assert review["issues"][0]["problem"] in update["compile_feedback"]
    assert '"target": "supplied mock endpoint"' in gateway.calls[2].user


async def test_policy_reviewer_sees_implicit_and_source_grounded_terminal_topology(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    bundle, api, *_ = service_source
    data = plan_data()
    data["outcomes"] = [{"name": "request_complete", "reason": "The request is complete.", "evidence": {
        "document": "BRIEF.md", "quote": "Finish by asking whether any help remains and saying goodbye before ending."}}]
    data["stages"][1]["exits"][1]["target"] = "request_complete"

    def review(call: Any) -> dict[str, Any]:
        raw = call.user.split("ACTUAL COMPILED TERMINAL TOPOLOGY (including implicit reserved end_call):\n", 1)[1]
        topology = json.loads(raw.split("\n\nACTUAL COMPILED TRANSPORT INPUTS", 1)[0])
        assert topology["end_call"]["node_type"] == "end_call"
        assert topology["end_call"]["terminal"]["function"]["name"] == "end_call"
        assert topology["request_complete"]["terminal"]["function"]["name"] == "request_complete"
        assert topology["end_call"]["ended_reason"] == "end_call"
        assert topology["request_complete"]["ended_reason"] == "request_complete"
        assert topology["lookup"]["ended_reason"] is None
        assert topology["request_complete"]["terminal"]["function"]["parameters"][
            "properties"]["message_mode"]["default"] == "none"
        assert topology["lookup"]["transitions"][1]["target_node"] == "end_call"
        assert topology["activate"]["transitions"][1]["target_node"] == "request_complete"
        assert "must not be declared" in call.system
        return {"issues": []}

    gateway = make_gateway(responders={"ServicePlan": lambda call: data, "ServicePolicyReview": review})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "service_mode": True}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert not update.get("failed"), update.get("errors")
    assert set(update["compiled"]["workflow"]["nodes"]) == {"lookup", "activate", "end_call", "request_complete"}


async def test_policy_reviewer_focuses_on_unimplemented_original_response_fields(
    service_source: Any, make_ctx: Any, make_gateway: Any,
) -> None:
    _, api, brief, _ = service_source
    source_rule = "When conversionDelta is positive, announce that the amount was converted."
    brief.write_text(BRIEF + "\n" + source_rule, encoding="utf-8")
    bundle = load_briefs([brief])
    api.tools["plans"].response_fields.append(FieldSpec(name="conversionDelta", type="number"))
    data = plan_data()
    data["rules"].append({"id": "S2", "instruction": "Use the returned values.",
                          "evidence": {"document": "BRIEF.md", "quote": source_rule}})

    def review(call: Any) -> dict[str, Any]:
        raw = call.user.split("POTENTIAL SOURCE RESPONSE-FIELD COVERAGE:\n", 1)[1]
        focus = json.loads(raw.split("\n\nACTUAL COMPILED TERMINAL TOPOLOGY", 1)[0])
        assert "conversionDelta" in focus["absent_response_field_names"]
        gap = next(item for item in focus["potential_uncovered_response_fields"] if item["field"] == "conversionDelta")
        assert gap["sources"][0]["text"] == source_rule
        assert "potential coverage gaps" in focus["instruction"]
        return {"issues": [{"location": "lookup.task_message", "problem": "The conversion notice is absent.",
                            "correction": "Implement the positive-value conversion notice.",
                            "evidence": {"document": "BRIEF.md", "quote": source_rule}}]}

    gateway = make_gateway(responders={"ServicePlan": lambda call: data, "ServicePolicyReview": review})
    update = await service_workflow._body({"slot": "elm", "bundle": bundle.model_dump(mode="json"),
                                         "tool_api": api.model_dump(mode="json"), "service_mode": True},
                                        make_ctx(llm=gateway))
    assert update["failed"] == "service_workflow" and update["service_policy_repair_pending"] is True
    assert update["compiled"] == {} and "conversion notice is absent" in update["compile_feedback"]


async def test_planner_reads_original_contract_and_reference_without_duplicates(
    service_source: Any, make_ctx: Any, make_gateway: Any, tmp_path: Path,
) -> None:
    bundle, api, brief, _ = service_source
    contract = tmp_path / "service-contract.md"
    text = "Elm Bank service contract. POST /activate requires a selected plan ID and explicit consent.\n"
    contract.write_text(text, encoding="utf-8")
    data = plan_data()
    data["rules"].append({"id": "S2", "instruction": "Use a selected plan ID and consent for activation.",
                          "evidence": {"document": contract.name, "quote": "POST /activate requires a selected plan ID and explicit consent."}})
    gateway = make_gateway(responders={"ServicePlan": lambda call: data,
                                      "ServicePolicyReview": lambda call: {"issues": []}})
    state = {"slot": "elm", "bundle": bundle.model_dump(mode="json"), "tool_api": api.model_dump(mode="json"),
             "inputs": {"contracts": [str(contract), str(brief)], "reference": [str(contract)]}}
    update = await service_workflow._body(state, make_ctx(llm=gateway))
    assert not update.get("failed"), update.get("errors")
    assert text.strip() in gateway.calls[0].user
    assert [doc["name"] for doc in update["bundle"]["documents"]] == [brief.name, contract.name]
    assert len(update["service_plan"]["rules"]) == 2
    repeated = service_workflow.source_bundle({**state, "bundle": update["bundle"]})
    assert len(repeated.documents) == 2


async def test_service_does_not_report_legacy_collection_smoke_as_passed(make_ctx: Any, make_gateway: Any) -> None:
    result = await self_test._run_one("smoke", {"service_mode": True}, make_ctx(llm=make_gateway()))
    assert result["details"]["skipped"] and result["passed"] is False
