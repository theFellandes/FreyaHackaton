"""Node ``load_inputs``: read the brief files and the tool API, and decide which track the run starts on.

Reads ``state["inputs"]`` (``{"briefs": [...], "tool_api": "..."}`` from :func:`builder.inputs.classify_inputs`), converts
the briefs to text (:func:`builder.ingest.brief_loader.load_briefs`; example conversations labelled) and builds the tool
API:

1. the OpenAPI file is parsed (:func:`builder.ingest.tool_api.load_tool_api`) - or, for a case folder with a prose
   service contract only, derived from it by one model call (:func:`builder.understand.contract.derive_tool_api`, saved
   as ``out/<slot>.openapi.json``);
2. collections operations get the canonical tool names the compiler needs
   (:func:`builder.understand.tool_roles.canonical_tool_api`); unrelated case-folder services keep every operation and
   set ``service_mode`` so they can be built without a collections policy IR;
3. with a tool base URL (the team's mock) every tool URL is moved onto it (:func:`builder.ingest.tool_api.with_base_url`).

Test customers, reference contracts and unreadable files are named in the trace and not read. The node also copies the
caps the routing needs into ``state["limits"]`` (so :mod:`builder.graph.routing` stays a set of pure functions) and
chooses the starting track:

* ``builder_track = "agentic"`` starts on the agentic track when the Claude Agent SDK and a credential are available
  (otherwise the run continues on the compiled track and says so in ``notes``);
* ``"compiled"`` and ``"auto"`` start compiled; ``"auto"`` may fall back to the agentic track later.

Unreadable inputs, a contract no tool API can be derived from and a collections API without the required roles end
the run (``failed``): there is nothing to build from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.runtime import Runtime

from builder import paths
from builder.agentic.runner import agentic_available, agentic_unavailable_reason
from builder.graph.context import BuilderContext
from builder.graph.nodes._common import failure, run_node, short
from builder.graph.state import BuilderState
from builder.ingest.brief_loader import EXAMPLE_LABEL, BriefBundle, load_brief, load_briefs
from builder.ingest.tool_api import ToolApi, load_tool_api, read_tool_api_document, with_base_url
from builder.inputs import InputError
from builder.llm.errors import LLMError
from builder.understand.contract import derive_tool_api
from builder.understand.tool_roles import REQUIRED_ROLES, canonical_tool_api, roles_from_document

NAME = "load_inputs"


def limits_from_settings(ctx: BuilderContext, *, allow_agentic: bool) -> dict[str, Any]:
    """The caps copied into the state for the pure routing functions."""
    s = ctx.settings
    return {
        "max_policy_repairs": int(s.max_policy_repairs),
        "max_compile_repairs": int(s.max_compile_repairs),
        "max_selftest_repairs": int(s.max_selftest_repairs),
        "allow_agentic": allow_agentic,
        "dry_run": bool(s.dry_run),
        "self_test": str(s.self_test),
        "builder_track": str(s.builder_track),
        "budget_total_s": float(ctx.budget.total_s),
    }


def _trace_case_files(inputs: dict[str, Any], ctx: BuilderContext) -> None:
    """Name the files that are not read as policy: test customers, reference contracts, unreadable files."""
    base_url = inputs.get("tool_base_url")
    ctx.trace.event(
        "input",
        f"tool base URL: {base_url}" if base_url else "tool base URL: none given, the contract's own server is used",
        tool_base_url=base_url or None,
    )
    for key, message in (
        ("customers", "test customer file(s) for the mock, kept out of the policy"),
        ("reference", "contract document(s) next to the OpenAPI file, reference only (the OpenAPI file is used)"),
        ("skipped", "file(s) that cannot be read as text, skipped"),
        ("examples", "example conversation(s), read as labelled illustrations (lower priority than the brief)"),
    ):
        files = inputs.get(key) or []
        if files:
            ctx.trace.event("input", f"{len(files)} {message}", files=[Path(p).name for p in files])


async def _tool_api(
    state: BuilderState, inputs: dict[str, Any], ctx: BuilderContext,
) -> tuple[ToolApi, str | None, bool]:
    """The API with the base URL applied, the derived OpenAPI file (if any), and whether it is a generic service."""
    derived_file: str | None = None
    require_collection_roles = not bool(inputs.get("case_folder"))
    if inputs.get("tool_api"):
        api = load_tool_api(inputs["tool_api"])
        document: dict[str, Any] | None = read_tool_api_document(inputs["tool_api"])
    else:
        contracts = [load_brief(Path(p)) for p in inputs.get("contracts") or []]
        out_path = paths.OUT_DIR / f"{state['slot']}.openapi.json"
        derived = await derive_tool_api(
            contracts, llm=ctx.llm, trace=ctx.trace, deadline=ctx.budget.deadline(), out_path=out_path,
            require_collection_roles=require_collection_roles,
        )
        api, document, derived_file = derived.api, derived.document, str(derived.path or "")
    # An explicit payment-promise operation identifies the existing collections compiler path. Keep its
    # requirements strict, even in a case folder; a damaged collections contract must not silently change tasks.
    role_hints = roles_from_document(api, document)
    collection_promise = "create_promise" in role_hints.values() or any(
        name == "create_promise" or contract.path.rsplit("/", 1)[-1] == "create_promise"
        for name, contract in api.tools.items()
    )
    require_collection_roles = require_collection_roles or collection_promise
    api = await canonical_tool_api(
        api, document, llm=ctx.llm, trace=ctx.trace, deadline=ctx.budget.deadline(),
        require_collection_roles=require_collection_roles,
    )
    service_mode = any(role not in api.tools for role in REQUIRED_ROLES)
    base_url = inputs.get("tool_base_url")
    if base_url:
        api = with_base_url(api, str(base_url))
        ctx.trace.decision(
            f"every tool request goes to {api.base_url} (paths and methods of the contract kept)",
            urls={name: f"{contract.method} {contract.url}" for name, contract in api.tools.items()},
        )
    return api, derived_file, service_mode


def _load_case_documents(briefs: list[Path], examples: list[Path], ctx: BuilderContext) -> BriefBundle:
    """The documents of a case folder: one that cannot be read (a damaged PDF) is skipped and reported, unless it is
    the first one (the brief) or nothing readable remains."""
    labelled = set(examples)
    documents = []
    for index, path in enumerate(briefs):
        try:
            document = load_brief(path)
        except InputError as exc:
            if index == 0:
                raise
            ctx.trace.error(f"document {path.name} could not be read and is skipped: {short(exc)}")
            continue
        documents.append(document.model_copy(update={"label": EXAMPLE_LABEL}) if path in labelled else document)
    if not documents:
        raise InputError("None of the case folder's documents could be read.")
    return BriefBundle.from_documents(documents)


async def _body(state: BuilderState, ctx: BuilderContext) -> dict[str, Any]:
    inputs = state.get("inputs") or {}
    case_files = any(inputs.get(key) for key in ("customers", "reference", "skipped", "examples", "contracts",
                                                  "tool_base_url"))
    try:
        if case_files:
            _trace_case_files(inputs, ctx)
        briefs = [Path(p) for p in inputs["briefs"]]
        examples = [Path(p) for p in inputs.get("examples") or []]
        bundle: BriefBundle = (
            _load_case_documents(briefs, examples, ctx) if inputs.get("case_folder")
            else load_briefs(briefs, examples=examples)
        )
        tool_api, derived_file, service_mode = await _tool_api(state, inputs, ctx)
    except (InputError, OSError, KeyError, ValueError, LLMError) as exc:
        return failure(NAME, short(exc, 600), status="failed")

    settings = ctx.settings
    wants_agentic = settings.builder_track == "agentic" or (
        settings.builder_track == "auto" and not settings.dry_run
    )
    can_agentic = wants_agentic and (ctx.agentic_runner is not None or agentic_available(settings))
    notes: list[str] = []
    if wants_agentic and not can_agentic:
        # A doomed session would burn minutes before it fails: say in the trace why the agentic track is skipped.
        ctx.trace.decision(
            f"agentic track skipped: {agentic_unavailable_reason(settings) or 'not available'}",
            requested=settings.builder_track,
        )
    if settings.builder_track == "agentic" and not can_agentic:
        notes.append(
            "track 'agentic' requested but the Claude Agent SDK or a credential is not available: using compiled"
        )
    track = "agentic" if settings.builder_track == "agentic" and can_agentic else "compiled"
    ctx.trace.decision(
        f"track {track} (requested {settings.builder_track}; agentic fallback {'on' if can_agentic else 'off'})",
        profile=settings.agent_profile,
        dry_run=settings.dry_run,
    )
    ctx.trace.event(
        "input",
        f"{len(bundle.documents)} brief file(s), tool API with {len(tool_api.tools)} tool(s)",
        briefs=[doc.name for doc in bundle.documents],
        chars=sum(len(doc.text) for doc in bundle.documents),
        brief_sha=bundle.sha256[:12],
        tools=sorted(tool_api.tools),
    )
    update_inputs = {"inputs": {**inputs, "generic_service": service_mode,
                                **({"tool_api": derived_file} if derived_file else {})}}
    return {
        **update_inputs,
        "bundle": bundle.model_dump(mode="json"),
        "tool_api": tool_api.model_dump(mode="json"),
        "service_mode": service_mode,
        "track": track,
        "profile": settings.agent_profile,
        "dry_run": bool(settings.dry_run),
        "limits": limits_from_settings(ctx, allow_agentic=can_agentic),
        "notes": notes,
    }


async def run(state: BuilderState, runtime: Runtime[BuilderContext]) -> dict[str, Any]:
    """LangGraph entry point of the node."""
    return await run_node(NAME, state, runtime.context, _body)
