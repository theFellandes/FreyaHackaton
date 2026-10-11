"""Map the operations of a bank's tool API to the canonical tool roles the compiler works with.

The compiler needs tools called ``verify_identity``, ``get_account``, ``create_promise`` and ``close_call`` and uses
``add_note``, ``schedule_callback`` and ``transfer_to_agent`` when they exist (:data:`CANONICAL_ROLES`). A bank's own
contract names its operations as it likes, in any language (``/musteri/dogrula``, ``createPaymentArrangement``).
:func:`canonical_tool_api` finds the role of every operation and renames the tools of the :class:`ToolApi` accordingly;
the HTTP method, the path (so the URL) and every field name stay the contract's, so the agent still calls the bank's
real endpoints with the bank's own field names.

How a role is found, cheapest first:

1. an API that already has every required canonical name and no unassigned operation (the practice API) is returned
   untouched - no model call;
2. ``x-freya-role`` on the operation (written by :mod:`builder.understand.contract` when the API is derived from a
   prose contract, or by hand in an OpenAPI file);
3. an operation already named like a canonical role keeps it;
4. otherwise ONE small model call maps the remaining operations (operationId, method, path, descriptions, field names)
   to roles; the answer is validated (known operations, each canonical role at most once, roles already taken stay
   taken) and repaired once with the problems quoted.

When the required roles cannot all be found, :class:`ToolRoleError` names the missing roles: the build fails clearly
instead of compiling tools that call endpoints the bank does not have.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from builder.ingest.tool_api import EXPECTED_TOOL_NAMES, ToolApi, ToolContract, parse_tool_api
from builder.inputs import InputError
from builder.llm.providers import LLMRole
from builder.prompts import prompt_id, render_prompt_parts
from builder.understand._common import LLMLike, call_structured, trace_decision, trace_event

#: Every role an operation can have, the canonical tool names first.
CANONICAL_ROLES: tuple[str, ...] = EXPECTED_TOOL_NAMES
#: The roles a collections agent cannot be compiled without (see ``builder.compiler.toolset.build_tools``).
REQUIRED_ROLES: tuple[str, ...] = ("verify_identity", "get_account", "create_promise", "close_call")
ROLE_EXTENSION = "x-freya-role"
OTHER = "other"
ROLE_PROMPT = "contract/tool_roles.v1.j2"

ToolRole = Literal[
    "verify_identity", "get_account", "create_promise", "add_note", "schedule_callback", "transfer_to_agent",
    "close_call", "other",
]


class ToolRoleError(InputError):
    """The tool API lacks an operation for a required role, or its roles cannot be told apart."""


class RoleChoice(BaseModel):
    """The role of one operation."""

    operation: str = Field(description="The operation's name exactly as listed (the 'name' column).")
    role: ToolRole = Field(description="What the operation does for the call; 'other' when none of the roles fits.")


class ToolRoleMap(BaseModel):
    """Answer of the role-mapping call: one entry per listed operation."""

    roles: list[RoleChoice] = Field(default_factory=list)


def roles_from_document(api: ToolApi, document: Mapping[str, Any] | None) -> dict[str, str]:
    """``tool name -> role`` from the ``x-freya-role`` extension of each operation (unknown values ignored)."""
    if not isinstance(document, Mapping):
        return {}
    paths = document.get("paths")
    found: dict[str, str] = {}
    if not isinstance(paths, Mapping):
        return found
    for name, contract in api.tools.items():
        item = paths.get(contract.path)
        operation = item.get(contract.method.lower()) if isinstance(item, Mapping) else None
        role = operation.get(ROLE_EXTENSION) if isinstance(operation, Mapping) else None
        if isinstance(role, str) and role.strip() in (*CANONICAL_ROLES, OTHER):
            found[name] = role.strip()
    return found


def _settled(api: ToolApi) -> bool:
    """True when the API already speaks the canonical names: every required one present, no other operation."""
    return all(role in api.tools for role in REQUIRED_ROLES) and all(name in CANONICAL_ROLES for name in api.tools)


async def canonical_tool_api(
    api: ToolApi,
    document: Mapping[str, Any] | None,
    *,
    llm: LLMLike | None = None,
    trace: Any = None,
    deadline: float | None = None,
) -> ToolApi:
    """``api`` with its operations renamed to the canonical roles (see the module docstring).

    Args:
        api: The loaded tool API.
        document: The raw OpenAPI document ``api`` was read from (for ``x-freya-role``); ``None`` when unknown.
        llm: Gateway for the role-mapping call; ``None`` = no call (missing roles then fail).
        trace: The build trace.
        deadline: ``time.monotonic()`` value after which no model call starts.

    Raises:
        ToolRoleError: When a required role has no operation, or the model's map stays invalid after one repair.
    """
    if _settled(api):
        return api
    if isinstance(document, Mapping):  # every operation of every path, safely named, so that each can take a role
        with contextlib.suppress(InputError):  # an unparsable document keeps the API as loaded
            api = parse_tool_api(document, sha256=api.sha256, every_operation=True)
    roles = roles_from_document(api, document)
    taken = set(roles.values()) - {OTHER}
    for name, contract in api.tools.items():
        hint = _canonical_name(name, contract)
        if name not in roles and hint is not None and hint not in taken:
            roles[name] = hint
            taken.add(hint)
    duplicates = _duplicate_roles(roles)
    if duplicates:
        raise ToolRoleError(
            f"The tool API gives the same {ROLE_EXTENSION} to several operations: {duplicates}. "
            "Each canonical role belongs to one operation; mark the others 'other'."
        )
    open_names = [name for name in api.tools if name not in roles]
    open_roles = [role for role in CANONICAL_ROLES if role not in taken]
    if open_names and open_roles and llm is not None:
        roles.update(await _map_with_model(api, roles, open_names, llm=llm, trace=trace, deadline=deadline))
    missing = [role for role in REQUIRED_ROLES if role not in roles.values()]
    if missing:
        raise ToolRoleError(
            f"The tool API has no operation for the required role(s) {', '.join(missing)} (operations: "
            f"{_operation_list(api)}). A collections agent needs an operation for each of {', '.join(REQUIRED_ROLES)}; "
            f"if the contract has one under another name, mark it with \"{ROLE_EXTENSION}: <role>\" in the OpenAPI "
            "file. Nothing was compiled: the agent would call endpoints the bank does not have."
        )
    renamed = rename_to_roles(api, roles)
    mapping = {name: roles.get(name, OTHER) for name in api.tools}
    trace_decision(
        trace,
        "tool roles: " + ", ".join(f"{name} -> {role}" for name, role in mapping.items()),
        roles=mapping,
        urls={name: contract.url for name, contract in renamed.tools.items()},
    )
    return renamed


def rename_to_roles(api: ToolApi, roles: Mapping[str, str]) -> ToolApi:
    """A copy of ``api`` whose tools are keyed and named by role (canonical roles first, in canonical order); an
    operation of role ``other`` keeps its own name (suffixed when it collides with a canonical one)."""
    by_role = {role: name for name, role in roles.items() if role != OTHER}
    tools: dict[str, ToolContract] = {}
    for role in CANONICAL_ROLES:
        name = by_role.get(role)
        if name is not None:
            tools[role] = api.tools[name].model_copy(update={"name": role})
    for name, contract in api.tools.items():
        if roles.get(name, OTHER) != OTHER:
            continue
        own = name
        while own in tools or own in CANONICAL_ROLES:
            own = f"{own}_op"
        tools[own] = contract.model_copy(update={"name": own})
    return api.model_copy(update={"tools": tools})


def _canonical_name(name: str, contract: ToolContract) -> str | None:
    """The canonical role an operation is already named after (its tool name or its last path segment), if any."""
    segments = [part for part in contract.path.strip("/").split("/") if part and not part.startswith("{")]
    for candidate in (name, segments[-1] if segments else ""):
        if candidate in CANONICAL_ROLES:
            return candidate
    return None


def _duplicate_roles(roles: Mapping[str, str]) -> str:
    seen: dict[str, list[str]] = {}
    for name, role in roles.items():
        if role != OTHER:
            seen.setdefault(role, []).append(name)
    return "; ".join(f"{role}: {', '.join(names)}" for role, names in seen.items() if len(names) > 1)


def _operation_list(api: ToolApi) -> str:
    return ", ".join(f"{name} ({contract.method} {contract.path})" for name, contract in api.tools.items()) or "none"


def check_role_map(answer: ToolRoleMap, open_names: Sequence[str], taken: Mapping[str, str]) -> list[str]:
    """Problems of a model's role map: unknown operations, a role given twice, a role another operation already has."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    already = {role: name for name, role in taken.items() if role != OTHER}
    for choice in answer.roles:
        if choice.operation not in open_names:
            problems.append(f"'{choice.operation}' is not one of the listed operations ({', '.join(open_names)})")
            continue
        if choice.role == OTHER:
            continue
        if choice.role in already:
            problems.append(f"role {choice.role} already belongs to {already[choice.role]}; "
                            f"give {choice.operation} another role or 'other'")
        elif choice.role in seen and seen[choice.role] != choice.operation:
            problems.append(f"role {choice.role} is given to both {seen[choice.role]} and {choice.operation}; "
                            "each role belongs to at most one operation")
        else:
            seen[choice.role] = choice.operation
    return problems


async def _map_with_model(
    api: ToolApi,
    taken: Mapping[str, str],
    open_names: Sequence[str],
    *,
    llm: LLMLike,
    trace: Any,
    deadline: float | None,
) -> dict[str, str]:
    operations = [_operation_row(name, api.tools[name]) for name in api.tools]
    fixed = {name: role for name, role in taken.items()}
    problems: list[str] = []
    previous: ToolRoleMap | None = None
    for attempt in (1, 2):
        prompt = render_prompt_parts(
            ROLE_PROMPT, operations=operations, open_names=list(open_names), fixed=fixed,
            roles=list(CANONICAL_ROLES), required=list(REQUIRED_ROLES), problems=problems,
            previous=previous.model_dump() if previous is not None else None,
        )
        purpose = (f"map the contract's operations to the agent's tool roles ({prompt_id(ROLE_PROMPT)})"
                   + (" - repair" if attempt == 2 else ""))
        answer = await call_structured(
            llm, purpose=purpose, schema=ToolRoleMap, system=prompt.system, user=prompt.user, role=LLMRole.FAST,
            max_tokens=2000, deadline=deadline,
        )
        problems = check_role_map(answer, open_names, taken)
        if not problems:
            mapped = {choice.operation: choice.role for choice in answer.roles}
            trace_event(trace, "contract", f"tool roles mapped by the model ({len(mapped)} operation(s))",
                        roles=mapped, attempt=attempt)
            return mapped
        previous = answer
    raise ToolRoleError("The model's map of operations to tool roles stays invalid: " + "; ".join(problems))


def _operation_row(name: str, contract: ToolContract) -> dict[str, Any]:
    return {
        "name": name,
        "method": contract.method,
        "path": contract.path,
        "summary": " ".join(f"{contract.summary} {contract.description}".split())[:400],
        "request": [spec.name for spec in contract.request_fields],
        "response": [spec.name for spec in contract.response_fields],
    }


__all__ = [
    "CANONICAL_ROLES",
    "REQUIRED_ROLES",
    "ROLE_EXTENSION",
    "RoleChoice",
    "ToolRoleError",
    "ToolRoleMap",
    "canonical_tool_api",
    "check_role_map",
    "rename_to_roles",
    "roles_from_document",
]
