"""The model-called tools of an agent, built from the tool contracts and the IR, with their hooks.

Both profiles use the same library: ``verify_identity`` (answer normalising pre-hook, ``what_to_do_next`` post-hook),
``create_promise`` (argument-cleaning pre-hook, promise guard, retry post-hook), ``add_note`` (notes-log pre-hook),
``schedule_callback`` (post-hook) and, for the lean profile only, ``transfer_to_agent`` and ``close_call`` as tools the
model calls itself.

Every field and flag is found by its **role** in the contract (:mod:`builder.compiler.roles`): the success boolean of a
response, the customer-id field, the outcome / reason fields of ``close_call``, the product / amount / date fields of
``create_promise``, the counter of ``verify_identity``. A hidden brief that renames a field therefore still gets the right
cleaning, the right flag in every post-hook and the same promise guard - or a ``CompileError`` when a role the agent
cannot do without is missing; it never gets an agent that quietly drops a field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from builder.compiler import hooks
from builder.compiler.context import (
    deferred_notes,
    failure_note_reason,
    recording_failure_texts,
    words,
)
from builder.compiler.errors import CompileError
from builder.compiler.hooks import CoerceKind
from builder.compiler.roles import (
    AccountRoles,
    CloseRoles,
    CustomerBinding,
    NoteRoles,
    PromiseRoles,
    VerifyShape,
    account_roles,
    close_roles,
    customer_binding,
    note_roles,
    promise_roles,
    queue_field,
    success_flag,
    verify_roles,
    verify_shape,
)
from builder.compiler.tools import model_tool
from builder.ingest.tool_api import FieldSpec, ToolApi, ToolContract
from builder.policy.ir import PolicyIR

__all__ = ["ToolSet", "VerifyShape", "build_tools", "field_kinds", "product_types", "success_rule", "verify_shape"]

NEUTRAL_VERIFY_DEFAULT = (
    "The result does not say whether this answer was right. Do not say whether it was right or wrong. If a security "
    "question has not been answered yet, ask it; otherwise ask the last question again."
)


def success_rule(contract: ToolContract) -> str:
    """The post-hook condition for "the tool did its job": its success flag, else the status of the result envelope."""
    flag = success_flag(contract)
    return hooks.flag_true(flag) if flag else hooks.envelope_ok()


def product_types(ir: PolicyIR) -> set[str]:
    """Product types the policy knows (the keys of the naming and floor tables)."""
    return set(ir.disclosure.product_naming) | set(ir.amounts.floor_field_by_type)


def field_kinds(
    contract: ToolContract, *, skip: tuple[str, ...] = ("customer_id",), product_field: str | None = None
) -> dict[str, CoerceKind]:
    """Cleaning kind per request field of ``contract`` (the customer id, ``skip`` and unknown shapes are left out).

    ``product_field`` is the field that carries the product id (a role found with :mod:`builder.compiler.roles`); it is
    cleaned against the ids of the account. Only number-typed fields (and fields named like an amount) are money.
    """
    kinds: dict[str, CoerceKind] = {}
    for spec in contract.request_fields:
        if spec.name in skip:
            continue
        kind = _kind_of(spec, product_field)
        if kind is not None:
            kinds[spec.name] = kind
    return kinds


def _kind_of(spec: FieldSpec, product_field: str | None) -> CoerceKind | None:
    lowered = spec.name.lower()
    if product_field is not None and spec.name == product_field:
        return "product_id"
    if spec.format == "date" or lowered.endswith("date") or lowered == "day":
        return "date_iso"
    if "time" in lowered and spec.type == "string":
        return "time_hhmm"
    if spec.type == "number" or ("amount" in lowered and spec.type != "boolean" and not spec.enum):
        return "money"
    if spec.enum:
        return "token"
    return None


def _enums(contract: ToolContract) -> dict[str, list[str]]:
    return {spec.name: list(spec.enum) for spec in contract.request_fields if spec.enum}


@dataclass
class ToolSet:
    """The built tools. ``None`` means the contract list did not need / provide the tool.

    The role fields tell the profiles which contract fields to bake into the engine's own calls (``customer``,
    ``account``, ``close`` roles, the transfer ``queue`` field).
    """

    verify: dict[str, Any] | None = None
    promise: dict[str, Any] | None = None
    note: dict[str, Any] | None = None
    callback: dict[str, Any] | None = None
    transfer: dict[str, Any] | None = None
    close: dict[str, Any] | None = None
    shape: VerifyShape | None = None
    customer: CustomerBinding = field(default_factory=CustomerBinding)
    account: AccountRoles = field(default_factory=AccountRoles)
    close_fields: CloseRoles | None = None
    note_fields: NoteRoles | None = None
    promise_fields: PromiseRoles | None = None
    queue: str | None = None
    notes: list[str] = field(default_factory=list)


def _identity_answer_note(ir: PolicyIR) -> str:
    parts = ["Never send a partial answer: send it only when it is complete."]
    for question in ir.identity.questions:
        how = question.normalisation.strip() or {
            "date_iso": "Send as YYYY-MM-DD.",
            "digits": f"Send exactly {question.digits or 'the'} digits.",
            "year": "Send the four-digit year.",
            "postcode": "Send the postcode as said.",
            "text": "Send the answer as said.",
        }[question.answer_format]
        parts.append(f"{question.field}: {how}")
    return " ".join(parts)


def _identity_rules(
    ir: PolicyIR, shape: VerifyShape, finish: str | None = None, done: str | None = None
) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    if shape.all_verified:
        rules.append((hooks.flag_true(shape.all_verified),
                      "All security answers are verified. Say nothing now; the call continues automatically."))
    if shape.ok:
        unreadable = hooks.flag_false(shape.ok)
        if shape.hint:
            unreadable = hooks.all_of(unreadable, hooks.is_empty(shape.hint))
        rules.append((unreadable,
                      "The answer could not be read, so it was not checked and does not count as a wrong answer. "
                      "Ask for the same answer again, complete and said clearly in the same format."))
    if shape.hint:
        for hint in ir.identity.hints:
            suffix = "" if hint.counts_as_wrong else " This does not count as a wrong answer."
            rules.append((hooks.is_value(shape.hint, hint.value), hint.instruction.strip() + suffix))
    if shape.verified:
        if shape.all_verified is None and done:  # the response has no overall flag: the model has to move on itself
            correct = (f"This answer is correct. If every security question is now answered correctly, {done} (no text "
                       "needed); otherwise ask the next security question that has not been answered yet.")
        else:
            correct = "This answer is correct. Now ask the next security question that has not been answered yet."
        rules.append((hooks.flag_true(shape.verified), correct))
    counter = shape.left or shape.wrong
    if counter:
        failed = ir.identity.failed_instruction.strip()
        sentence = ir.phrases.identity_failed_sentence.strip()
        ending = ((f'Say "{sentence}", then say goodbye and {finish}.' if sentence else f"Say goodbye and {finish}.")
                  if finish else "Say nothing more; the call continues automatically.")
        ended = (hooks.is_zero(counter) if shape.left else hooks.is_count(counter, ir.identity.max_wrong_total))
        rules.append((ended,
                      f"{words(ir.identity.max_wrong_total).capitalize()} wrong answers: the identity check has ended. "
                      f"{failed} {ending}".replace("  ", " ")))
    return rules


def build_tools(
    ir: PolicyIR,
    api: ToolApi,
    *,
    promise_failed_phrase: str,
    lean: bool = False,
    identity_failed_phrase: str | None = None,
    identity_done_phrase: str | None = None,
) -> ToolSet:
    """Build every model-called tool the IR needs.

    Args:
        ir: The policy (identity questions, recording sentences, multi-product settings, callback rule).
        api: The tool contracts.
        promise_failed_phrase: How the post-hook tells the model to end the call after the last failure
            (``call transition_to_end_recording_failed`` / ``call close_call with ...``).
        lean: Also build ``transfer_to_agent`` and ``close_call`` as model-called tools.
        identity_failed_phrase: Lean design only: how the model finishes the call after the last wrong answer
            (the staged design routes there by itself).
        identity_done_phrase: How the model moves on when the identity response has no overall "all verified" flag.

    Raises:
        CompileError: When a tool the policy cannot do without is missing from the tool API, or the contract of a tool has
            no field for a role the agent needs (the product id of ``create_promise``, the outcome of ``close_call``,
            a boolean that says whether an identity answer was right ...).
    """
    result = ToolSet()
    for needed in ("verify_identity", "get_account", "create_promise", "close_call"):
        if needed not in api.tools:
            raise CompileError(f"the tool API has no {needed!r} tool; a collections agent cannot be compiled without it")
    customer = result.customer = customer_binding(api, ir.call_variable_names())
    account = result.account = account_roles(api, product_types(ir), customer.field)
    base = {"customer_field": customer.field, "customer_ref": customer.ref}

    # ---- verify_identity -------------------------------------------------------------------------------------------
    verify = api.tools["verify_identity"]
    result.shape = shape = verify_shape(verify, [h.value for h in ir.identity.hints])
    vroles = verify_roles(verify, customer.field)
    formats = {q.field: q.answer_format for q in ir.identity.questions}
    selector_spec = verify.get_field(vroles.selector, "request")
    fields = [f for f in (selector_spec.enum or []) if f] if selector_spec is not None else []
    question_fields = ", ".join(q.field for q in ir.identity.questions) or ", ".join(fields)
    result.verify = model_tool(
        verify,
        description=("Check ONE security answer. CALL it every time the customer gives an answer to a security "
                     "question, with that single answer."),
        field_notes={vroles.selector: f"Which question this answer belongs to: {question_fields}.",
                     vroles.answer: _identity_answer_note(ir)},
        pre_process_code=hooks.pre_hook_identity_answers(
            vroles.selector, vroles.answer, formats, ir.calendar.date_input_order),
        post_process_code=hooks.post_hook(
            _identity_rules(ir, shape, identity_failed_phrase, identity_done_phrase),
            default=("This answer is wrong. Do not say what the right answer is. Ask the same question again."
                     if shape.verified else NEUTRAL_VERIFY_DEFAULT),
            error_message="The check service failed. Ask the same question again.",
        ),
        **base,
    )

    # ---- create_promise --------------------------------------------------------------------------------------------
    promise = api.tools["create_promise"]
    roles = result.promise_fields = promise_roles(promise, customer.field, account.id_key)
    missing = [name for name, value in (("product id", roles.product), ("amount", roles.amount), ("date", roles.date))
               if value is None]
    if missing:
        raise CompileError(
            f"create_promise: the compiler cannot find the {', '.join(missing)} field(s) (request fields: "
            f"{', '.join(s.name for s in promise.request_fields)}); the promise guard cannot be generated, and without it "
            "a promise the rules forbid could be recorded")
    assert roles.product and roles.amount and roles.date
    kinds = field_kinds(promise, skip=(customer.field,), product_field=roles.product)
    success = ir.recording.success_sentence.strip() or ir.phrases.recorded_sentence.strip()
    note_name = ir.multi_product.note_tool or "add_note"
    notes_for_failure = bool(ir.multi_product.enabled and failure_note_reason(ir))
    _prompt_rule, fail_text = recording_failure_texts(
        ir, promise.name, failed_phrase=promise_failed_phrase, note_tool=note_name, with_note=notes_for_failure)
    ok_message = "The promise IS recorded. If this was the only product, say nothing: the system confirms it."
    ok_message += (f' Otherwise tell the customer it is recorded, say exactly: "{success}"' if success
                   else " Otherwise tell the customer it is recorded")
    ok_message += " Then continue with the next product, or close the call if every product is handled."
    fail_message = f"The promise was NOT recorded. Never say it is recorded. {fail_text}"
    # The platform does not run a post-hook when the pre-hook emptied the URL (experiment E-9), so in the staged graph the
    # spoken correction comes from the ``promise_fix`` node. This line is a safety net for any path that does reach it.
    blocked_message = (
        "The promise was NOT recorded: the date or amount is not allowed for this product. Do not say it is recorded. "
        "Offer an allowed date (for a date after the deadline, offer the deadline for the same amount) or an amount from "
        "the floor to the full amount, read back and ask again."
    )
    notes = {
        roles.product: "The product id exactly as listed in LATE PRODUCTS.",
        roles.amount: "Promised amount as a number with 2 decimals.",
        roles.date: "Promise date YYYY-MM-DD, one of the product's allowed promise dates.",
    }
    result.promise = model_tool(
        promise,
        description=(f"Record ONE payment promise for ONE product. CALL it only after you read back "
                     f"{', '.join(ir.confirmation.readback_fields)} and the customer clearly said yes."),
        field_notes=notes,
        pre_process_code=hooks.join_hooks(
            hooks.pre_hook_coerce(kinds, enums=_enums(promise)),
            hooks.pre_hook_promise_guard(roles.product, roles.amount, roles.date,
                                         accept_between=ir.amounts.accept_between_floor_and_full,
                                         currency=ir.currency_word, after_deadline=ir.promise_dates.after_deadline,
                                         shift=ir.promise_dates.non_business_day_shift),
        ),
        post_process_code=hooks.post_hook([(success_rule(promise), ok_message)], default=fail_message,
                                          error_message=fail_message, blocked_message=blocked_message),
        **base,
    )

    # ---- add_note (multi-product) ----------------------------------------------------------------------------------
    if ir.multi_product.enabled:
        if note_name not in api.tools:
            raise CompileError(f"the policy uses {note_name!r} for several products but the tool API has no such tool")
        note = api.tools[note_name]
        nroles = result.note_fields = note_roles(note, customer.field, account.id_key)
        if nroles.product is None or nroles.reason is None:
            raise CompileError(
                f"{note_name}: the compiler cannot find the {'product id' if nroles.product is None else 'reason'} field "
                f"(request fields: {', '.join(s.name for s in note.request_fields)})")
        reason_spec = note.field(nroles.reason, "request")
        reasons = ", ".join(reason_spec.enum or [])
        later = deferred_notes(ir)
        result.note = model_tool(
            note,
            description=(
                "Record why ONE product ended without a promise. CALL it only at the very end of the call, once every "
                "product is handled and the call is not being transferred - never before a transfer - once for every "
                "product that gets no promise, with that product's id and reason." if later else
                "Record why ONE product ended without a promise. CALL it once for every product that gets no promise, "
                "with that product's id and reason."),
            field_notes={nroles.reason: f"Why this product has no promise: {reasons}." if reasons else "",
                         nroles.product: "The product id exactly as listed in LATE PRODUCTS."},
            pre_process_code=hooks.pre_hook_notes_log(nroles.product, nroles.reason, reasons=reason_spec.enum or ()),
            post_process_code=hooks.post_hook(
                [(success_rule(note),
                  "The note is saved. Continue with the next product, or close the call if every product is handled.")],
                default=f"The note was NOT saved. Call {note_name} once more with the same values.",
                error_message=f"The note was NOT saved. Call {note_name} once more with the same values.",
            ),
            **base,
        )

    # ---- schedule_callback -----------------------------------------------------------------------------------------
    if ir.third_party.callback is not None:
        if "schedule_callback" not in api.tools:
            raise CompileError("the policy books callbacks but the tool API has no 'schedule_callback' tool")
        callback = api.tools["schedule_callback"]
        booked = ("The callback is booked. " + ("Say goodbye and finish the call as instructed."
                                                 if lean else "Say nothing; the system confirms it and closes the call."))
        result.callback = model_tool(
            callback,
            description=("Book a callback for the customer. CALL it only when the person gave a day and time inside "
                         "the allowed callback times."),
            pre_process_code=hooks.pre_hook_coerce(field_kinds(callback, skip=(customer.field,)),
                                                   enums=_enums(callback)),
            post_process_code=hooks.post_hook(
                [(success_rule(callback), booked)],
                default=("The callback was NOT booked. Do not say it is booked. Check the date and time against the "
                         "allowed callback times and ask again."),
                error_message="The callback was NOT booked (service error). Try once more with the same values.",
            ),
            **base,
        )

    # ---- lean only: transfer and close as model-called tools -------------------------------------------------------
    close = api.tools["close_call"]
    result.close_fields = croles = close_roles(close, customer.field)
    if lean:
        if "transfer_to_agent" in api.tools:
            transfer = api.tools["transfer_to_agent"]
            result.queue = queue = queue_field(transfer, customer.field)
            queues = ", ".join(transfer.enum(queue)) if queue else ""
            result.transfer = model_tool(
                transfer,
                description="Transfer the call to a queue. CALL it only when the instructions say to transfer, with the named queue.",
                field_notes={queue: f"Team that takes over. One of: {queues}." if queues else ""} if queue else {},
                post_process_code=hooks.post_hook(
                    [(success_rule(transfer), "The transfer is done. Now close the call as instructed.")],
                    default="The transfer failed. Say so briefly and close the call as instructed.",
                    error_message="The transfer failed. Say so briefly and close the call as instructed.",
                ),
                **base,
            )
        outcomes = close.enum(croles.outcome)
        close_notes = {croles.outcome: f"One of: {', '.join(outcomes)}."} if outcomes else {}
        reason_optional = [croles.reason] if croles.reason else []
        result.close = model_tool(
            close,
            description=("Close the call. CALL it exactly once, as your LAST tool call, after you said goodbye, with the "
                         "outcome and (where the table lists one) the reason."),
            include_optional=reason_optional,
            field_notes=close_notes,
            max_retries=1,
            post_process_code=hooks.post_hook(
                [(success_rule(close), "The call is closed. Say nothing more.")],
                default="Closing failed. Call close_call once more with the same values.",
                error_message="Closing failed. Call close_call once more with the same values.",
            ),
            **base,
        )
    elif "transfer_to_agent" in api.tools:
        result.queue = queue_field(api.tools["transfer_to_agent"], customer.field)
    return result
