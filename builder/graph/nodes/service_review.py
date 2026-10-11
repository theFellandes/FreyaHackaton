"""Source-policy completeness gate for a proposed generic service plan."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from builder.compiler.errors import CompileError
from builder.compiler.profiles.service import ServiceEvidence, _normal
from builder.ingest.brief_loader import BriefBundle


class ServicePolicyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    correction: str = Field(min_length=1)
    evidence: ServiceEvidence


class ServicePolicyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[ServicePolicyIssue]


class ServicePolicyRepairError(CompileError):
    """All reported policy defects are source-grounded and can use the policy repair budget."""


SERVICE_POLICY_REVIEW_SYSTEM = """Review a proposed phone-agent ServicePlan against the COMPLETE original bank sources and actual API.
Return only substantive blocking policy defects, or issues=[] when all operative
requirements are implemented. This is an independent source-completeness gate,
not a summary of the plan's claimed rules or a style review. Do not rewrite the plan.

Resolve authority, dates, amendment scope and explicit supersession FIRST. Review
only the resulting authoritative operative policy. Older examples, FAQs, recorded
prompts and implementation samples must not override later authoritative updates.
Do not turn test data, mock setup, submission/admin instructions or unresolved
historical suggestions into new phone-agent obligations.
Judge the provided EXECUTION CONTEXT, not an imagined production integration.
Explicit original-source waivers for that test/mock environment are binding:
do not demand production authentication, credentials or metadata when those are
explicitly waived for the supplied endpoint. A waiver is not permission to claim
that production authentication was performed. Use contract requirements for any
behavior that the source does not waive.
When sources explicitly declare a field, eligibility signal or integration
unavailable or unresolved, do not require inventing it as a plan correction.
Do not create new eligibility predicates, caller attestations, credential inputs,
API operations or hidden facts. Check the source's documented feasible behavior
and truthful handling of the limitation instead; do not unilaterally suppress a
documented customer flow to compensate for an explicitly unavailable signal.

Read each operative source branch and locate its IMPLEMENTATION in the candidate
system prompt, rule instructions, stage messages, tool descriptions and exits.
First build an internal coverage inventory of EVERY authoritative obligation in
EVERY original source, including conditions embedded in prose, tables, amendments
and exception paragraphs. Do not review only the most prominent flow or the last
repair's defects. For each policy-referenced response field, enumerate every
source-defined predicate and its consequence: positive/zero/negative thresholds,
strict versus inclusive boundaries, equality cases, flags, absent or empty values,
partial data, and date/time boundaries. Check the required figures, notice or
acknowledgment, spoken wording, ordering, next question/action and terminal route
for each predicate separately. Do not invent obligations for fields the policy
does not use. A source-defined conditional notice remains mandatory even when
all visible examples or fixtures leave its triggering field at zero or never
exercise that branch. Examples and successful samples cannot prove completeness.
The deployed agent cannot access the original sources. A source quote in evidence,
a branch name, or 'use the source wording/conditions' does NOT implement a branch.
Its actual field conditions, complete spoken utterance/template and next action
must appear as standalone runtime instructions. Flag missing or wrong conditions,
required amounts, partial-payment distinctions, notices, acknowledgments, currency
ordering, date/time cases, zero/negative/missing data, and product-specific details.
Check all required fields separately for every applicable returned product and
currency, rather than accepting a vague 'read the balance/details correctly'.

Check caller/customer actions, existing verification and trusted supplied values,
privacy, product selection, write prerequisites and explicit consent, preview and
confirmation order, success/error evidence, failure truthfulness, no-data endings,
source-required offers and what follows acceptance, refusal or interruption.
Check source-wide interruption routes and counted outcomes in EVERY applicable
stage: each must have an explicit usable exit with the source's threshold, scope,
prerequisites and mandatory utterance. Global prose without an available stage
exit is insufficient. Ensure required information is fetched on every applicable
path before it is used, including shortcuts that combine requests. Never assume
an optional branch has run. Distinguish the platform's response wrapper from all
bank response-envelope layers and use only actual documented fields.
Trace each exceptional branch through its actual exits before accepting a generic
completion/offer path. A narrow requested-information shortcut, no-data/empty
result, zero-value result, refusal or combined request may require a different
next action; the usual route must not silently replace that source-specific
ending. Check date/time-specific notices and figures before an ordinary reading
can contradict them, and check that the next action preserves the branch's scope.

Flag imaginary executable actions, tokens, credentials, retry mechanisms, APIs,
customer identifiers or eligibility facts. An action must have an exposed tool
and its required inputs available; generic claims about background integrations
do not make it executable. Do not require unavailable integrations yourself.
The review includes ACTUAL COMPILED TRANSPORT INPUTS. Inspect them before reporting
a missing input: the compiler supplies contract-fixed/default/singleton values
and source-bound call variables automatically, even if plan prose omits them.
A literal compiled body default is already supplied; do not demand another plan
binding for it. A field's own unresolved {{field}} placeholder is model-selected,
not a fixed value: it still needs actionable instructions when policy restricts
its choice among several enum values. Do not mistake compiler transport/context
mechanisms for undocumented background integration capabilities.
The review also includes ACTUAL COMPILED TERMINAL TOPOLOGY. Judge its real nodes
and transition targets: the reserved end_call exit is compiled automatically to
a silent end_call terminal with endedReason=end_call. It must not be declared
as an additional outcomes entry. Source-grounded additional outcomes become
their own terminal nodes and endedReason values. Do not report a missing terminal
solely because end_call is absent from the plan's outcomes list; verify the
compiled topology. Still check that its incoming exits and required prior spoken
utterances implement the authoritative source conditions.

For EVERY issue give a precise candidate location, the concrete defect, and an
actionable correction grounded in the actual sources/API. Include ONE short,
continuous VERBATIM source span and its exact document name. Preserve words and
punctuation; line-start mail forwarding prefixes and Markdown emphasis may be
omitted. No ellipses, paraphrased evidence, merged spans or wrong-file attribution.
Aggregate all substantive defects in one review. Do not flag matters already
implemented elsewhere in the standalone runtime instructions. Do not propose
case-specific test values, undocumented behavior, or cosmetic changes.
There is no artificial issue-count limit: do not stop after the first few defects.
Report all substantive defects supported by the sources within the output budget;
consolidate related defects only when their concrete conditions and corrections
remain explicit. Corrections must be surgical: retain all unaffected implemented
branches, wording, prerequisites and routes rather than replacing a complete
section with a narrower description of only the latest reported defect.
"""


def service_execution_context(inputs: dict[str, object]) -> dict[str, object]:
    """Make the existing mock-server override explicit without inventing policy waivers."""
    override = inputs.get("tool_base_url")
    return {
        "target": "supplied mock endpoint" if override else "original contract servers",
        "tool_server_override_supplied": bool(override),
        "tool_base_url": override or "",
        "waivers": "Apply only explicit original-source waivers relevant to this target; invent none.",
        "unavailable_inputs": "Never invent credentials, eligibility fields, customer facts or API operations.",
    }


def require_complete_service_policy(review: ServicePolicyReview, bundle: BriefBundle) -> None:
    """Block on all grounded reviewer defects; fabricated evidence cannot drive repairs."""
    documents: dict[str, list[str]] = {}
    for document in bundle.documents:
        documents.setdefault(document.name, []).append(_normal(document.text))
    defects: list[str] = []
    ungrounded = False
    for issue in review.issues:
        quote = _normal(issue.evidence.quote)
        if not quote or not any(quote in text for text in documents.get(issue.evidence.document, [])):
            ungrounded = True
            defects.append(f"review issue {issue.location!r} is not source-grounded in {issue.evidence.document!r}: "
                           f"{quote[:150]!r}")
            continue
        defects.append(f"{issue.location}: {issue.problem}\nCorrection: {issue.correction}\n"
                       f"Source {issue.evidence.document}: {issue.evidence.quote}")
    if defects:
        error_type = CompileError if ungrounded else ServicePolicyRepairError
        raise error_type("Source-policy review requires repair:\n" + "\n\n".join(defects))
