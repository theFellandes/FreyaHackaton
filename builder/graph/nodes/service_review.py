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

Read each operative source branch and locate its IMPLEMENTATION in the candidate
system prompt, rule instructions, stage messages, tool descriptions and exits.
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

For EVERY issue give a precise candidate location, the concrete defect, and an
actionable correction grounded in the actual sources/API. Include ONE short,
continuous VERBATIM source span and its exact document name. Preserve words and
punctuation; line-start mail forwarding prefixes and Markdown emphasis may be
omitted. No ellipses, paraphrased evidence, merged spans or wrong-file attribution.
Aggregate all substantive defects in one review. Do not flag matters already
implemented elsewhere in the standalone runtime instructions. Do not propose
case-specific test values, undocumented behavior, or cosmetic changes.
"""


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
