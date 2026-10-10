# Banking conversations: persuasion, repetition and interruption

**The project has useful policy instructions, but several important conversation safeguards still depend on the model following them.** The most urgent work is to enforce confirmation and recorded-action state, prevent duplicated offers, and distinguish clarification from refusal. Real audio interruption needs separate verification.

This review combines current source inspection, synthetic offline probes and primary-source research checked on **10 October 2026**. It reviews the generated collections agent and its bank-write path. Proposed acceptance scenarios below have **not** been executed as live calls. No implementation was changed or committed for this review.

## Which rules apply

The [official kit](../KIT_README.md), supplied brief and supplied tool API govern the hackathon. An external design recommendation must not introduce a new repayment plan, change an attempt budget, replace identity questions, alter required sentences or add an unauthorized tool. Bank-specific behavior must still be extracted into the policy model.

The regulatory references below provide real-world design context with defined scope. UK [CONC 7.1](https://handbook.fca.org.uk/handbook/conc7/conc7s1) applies to specified credit and collection activities. US [Regulation F definitions](https://www.consumerfinance.gov/rules-policy/regulations/1006/2/) concern covered FDCPA debt collectors; a bank collecting its own debt is not automatically one. These sources do not establish the fictional bank's jurisdiction or legal obligations. A real deployment needs its applicable product, jurisdiction and organizational policy established separately.

The distinction also matters for numbers: cross-call contact-frequency provisions in US [§1006.14](https://www.consumerfinance.gov/rules-policy/regulations/1006/14/) use rebuttable presumptions with qualifications. They are not a per-conversation persuasion budget or permission to harass below a numerical threshold. Generic voice-design retry counts are likewise not replacements for the brief.

## What the current project actually establishes

The local [conversation probe script](../.cache/project-audit/conversation-practices/probe_source_capabilities.py) and [results](../.cache/project-audit/conversation-practices/capability_probe_results.json) use fixture policy and synthetic requests. A nonempty request URL means the hook **leaves the request enabled**; no real request was sent. Source hashes are included. These ignored cache artifacts should become portable regression tests during repairs.

| Area | Current evidence | Assessment |
|---|---|---|
| Persuasion boundaries | [Negotiation prompt](../builder/prompts/agent/negotiate.v1.j2) distinguishes date/amount proposals from refusal, specifies per-product attempts and forbids repeating rejected offers. | Good instructions; no explicit per-product offer history or refusal counter was found in the inspected compiled profiles. |
| Entry repetition | [Staged profile](../builder/compiler/profiles/staged.py) sets `on_enter_first_entry_only=true` for negotiation. | Prevents the entry debt statement replaying on ordinary reentry. Does not enforce the model's later questions or offers. |
| Missing persuasion steps | [Validation](../builder/understand/validate.py) warns when too few steps exist; [code generation](../builder/compiler/codegen.py) pads by repeating the final step. | Synthetic compilation produces identical first and second attempts. See F19 in the [backlog](FIXING_ISSUES.md). |
| Readback and clear agreement | Tool description and negotiation prompt require readback, confirmation and fresh readback after changed terms. | A valid synthetic request remains enabled with no confirmation state. The hook checks values, not whether those values were confirmed. |
| One immutable promise per product | Negotiation prompt forbids a second promise or an in-call change after recording. | Same-product repeats and changed values remain enabled by the pre-hook even with an injected success counter. No deterministic protection was demonstrated. |
| Support offer clarification | [Context generator](../builder/compiler/context.py) treats an answer other than “yes” as declining an offered handoff. | Conflicts with clarification or a request for thinking time; no live misclassification was measured. |
| Acknowledged notes | [Note pre-hook](../builder/compiler/hooks.py) appends to `notes_log` before receiving a response. | A failed response leaves the entry present; a retry appends it again. Confirmed synthetic state defect, already F04. |
| Success and handoff routing | [Staged routes](../builder/compiler/profiles/staged.py) and [decision code](../builder/compiler/codegen.py) use tool counters. | Review execution success versus bank acknowledgment. A synthetic failure-only transfer counter produces a transferred outcome, but actual transfer completion and the intended failure outcome remain unverified. |
| Audio configuration | Inspected A staged, A+B staged and A+B lean outputs have no explicit speech/idle/interruption settings or global interrupt nodes. | Does not prove platform barge-in is disabled: defaults and real playback behavior were not measured. |
| Practice evidence | [Practice client](../freya.py) uses `mode="chat"`; [platform API](api.md) describes text simulation. | Can evaluate conversational responses and bank records. Cannot establish acoustic interruption performance. |

The broader implementation and submission defects remain in [Fixing issues](FIXING_ISSUES.md). The [identity guide](EDGE_CASES.md) explains birthday and phone verification in detail.

## Persuasion should respond to the customer's actual barrier

Appropriate persuasion presents an authorized, relevant option and leaves room for a decision. For in-scope UK firms, [CONC 7.3](https://handbook.fca.org.uk/handbook/conc7/conc7s3) emphasizes forbearance and sustainable arrangements, and prohibits specified pressure toward harmful repayments or raising funds through borrowing or asset sales. This supports treating affordability as a support need rather than an excuse to repeat a demand. It does not authorize this agent to create new plans.

Keep at least these conversational categories separate:

| What the customer means | Appropriate project response |
|---|---|
| “I can pay Friday.” | Validate the proposed date and amount. Do not count a proposal as refusal. |
| “That amount is too much.” | Apply the extracted amount policy and any authorized next option. Do not invent a discount. |
| “I don't understand which account this is.” | Explain the bank-provided product facts after verification; preserve the current negotiation position. |
| “No, I will not pay.” | Use only the remaining policy-defined persuasion steps, then the prescribed outcome. |
| “I lost my job / I am seriously ill.” | Apply the brief's hardship override. Public Brief A stops persuasion and permits the specified support route; a volunteered date or amount can still lead to a valid confirmed promise. |
| “That debt is not mine.” | Apply the dispute branch. Do not argue or try to obtain a promise for the disputed product. |
| “I already paid.” | Apply the paid-already policy; do not equate a missing current ledger update with dishonesty. |
| “Can support explain my options?” | Answer within known facts or clarify the offered transfer. This is not a refusal. |
| “I need a person.” | Follow the extracted human-request rule and invoke the actual supported transfer operation. |
| “I'm at work; this is a bad time.” | Apply the contact/timing policy and available callback behavior. Do not improvise a contact commitment. |

For comparison, UK [CONC 7.14](https://handbook.fca.org.uk/handbook/conc7/conc7s14) addresses reasonable repayment proposals, explanations for rejection, intimidation and investigation of disputes. [CONC 7.5](https://handbook.fca.org.uk/handbook/conc7/conc7s5) addresses pursuing the wrong borrower and disregarding paid or disputed claims. Neither is a justification for substituting a general script for the supplied brief.

An additional offer should follow the authorized ladder and respond to the actual barrier. Do not invent another attempt by paraphrasing a rejected offer; equally, do not suppress a step the brief explicitly requires merely because some terms repeat. Avoid shame, exaggerated urgency, unsupported legal consequences, invented credit-score effects and promises that an offer will solve the customer's financial position. US [§1006.18](https://www.consumerfinance.gov/rules-policy/regulations/1006/18/) prohibits specified false or misleading collection representations for covered collectors. In this project, accurate status language also follows directly from the brief and API: agreement, submission, recording and actual payment are different events.

Preserve choice without asking a dozen broad questions. Ask the one question needed for the current rule, explain a constraint when necessary, and allow a correction. UK [PRIN 2A.5](https://handbook.fca.org.uk/handbook/prin2a/prin2as5), within Consumer Duty scope, supports understandable, tailored communications and appropriate checks of understanding; it does not prescribe one universal confirmation script.

## Prevent unwanted repetition while preserving necessary confirmation

There are three useful kinds of repetition: repairing an unheard or ambiguous answer, repeating information at the customer's request, and confirming a material commitment. Clarification or requested repetition is not automatically refusal; apply the brief's explicit counting rules. Public Brief A counts purpose questions during identity as declining and counts negotiation non-answers toward persuasion. Repeating information must not itself trigger another bank write.

Google's [voice-agent design guidance](https://docs.cloud.google.com/dialogflow/cx/docs/concept/voice-agent-design) supports short, contextual exchanges and repair focused on the missing information. Adapt that principle to the brief: ask for a missing birthday year, repeat a requested date, or retry the failing phone question. Do not restart every identity question or negotiation step.

Proposed builder state should retain verified components, resolved questions, current product, offers already presented and the reason for a repair. For each offer, compare its **meaning**: product, amount, date and policy step. Rewording the same terms does not create a fresh substantive offer. The same amount can legitimately appear in a different prescribed step: [Brief A rule 26](../briefs/brief-a.md) permits the floor to be named again on the deadline during attempt two. Different prescribed steps can also resolve to the same terms; follow their source rules and precedence rather than automatically inventing novelty or deleting a step. Do not ban every repeated number or mandatory phrase. An interrupted offer also needs delivery state: an unheard tail is different from a fully delivered and rejected proposal.

| Situation | Useful response | Unwanted repetition |
|---|---|---|
| “14 March.” | Ask for the year if required. | Ask for the entire birthday again without explaining what is missing. |
| “Please repeat the date.” | Repeat the date associated with the current proposal. | Restart the debt summary and both persuasion attempts. |
| “What does that amount include?” | Explain known account facts or admit the detail is unavailable. | Repeat the demand as though the question were refusal. |
| Same offer rejected twice | Apply remaining authorized steps and the no-repeat rule with policy precedence. | Invent another attempt by rewording rejected terms. |
| Customer changes amount before recording | Validate and read back the new complete proposal. | Submit using the old confirmation or ask irrelevant identity questions. |
| Recorded promise mentioned again | Report its acknowledged status. | Execute the promise tool again to reproduce a confirmation. |

The CFPB's [2023 consumer-finance chatbot report](https://www.consumerfinance.gov/data-research/research-reports/chatbots-in-consumer-finance/chatbots-in-consumer-finance/) describes repetitive loops, difficulties reaching human help and failure to recognize disputes without exact wording. This is research evidence, not a new universal chatbot rule. For this builder, add progress checks: repeated unresolved questions should lead to the brief's recovery path, never an indefinite cycle.

## Barge-in requires audio behavior and conversation state

**Barge-in means the caller speaks while the agent's audio is playing.** It is distinct from deciding that the caller has finished speaking, handling silence, or routing a typed “wait” message. Amazon's [Nova barge-in documentation](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-barge-in.html) explains that the client must stop current playback and discard queued audio from the interrupted response. Stopping generation alone can leave buffered speech playing.

A proposed real-call acceptance sequence is:

1. Detect caller speech and identify the interrupted output turn.
2. Stop its audible playback and flush stale queued audio; prevent late chunks from restarting it.
3. Preserve the caller's new speech and determine when the complete utterance is available.
4. Retain the portion of the agent's message actually delivered. Do not assume the caller heard the generated remainder.
5. Classify the interruption: correction, question, hardship, dispute, transfer, timing request or backchannel.
6. Invalidate pending confirmation if material terms changed or the necessary readback was not delivered.
7. Continue according to the bank-action state; speech cancellation does not undo a submitted action.

Twilio's [ConversationRelay message reference](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages) exposes an interruption event with spoken-prefix and duration information, and distinguishes caller interruption from application preemption. That illustrates useful observability; it is not evidence that Freya exposes identical fields or needs a Twilio integration. Track output and action identifiers where the actual platform supports them.

Do not write on a provisional “yes.” Google's [streaming recognition reference](https://docs.cloud.google.com/dialogflow/cx/docs/reference/rpc/google.cloud.dialogflow.cx.v3#streamingrecognitionresult) explains that interim transcripts can change and a turn-ending signal can precede the final transcript portion. Our proposed safeguard is to wait for the complete actionable answer before accepting “yes, but change the date.” This is an engineering inference, not a reproduced acoustic defect.

Pauses during dates and phone digits deserve different treatment from silence after a finished question. [Amazon Connect guidance](https://docs.aws.amazon.com/connect/latest/adminguide/agentic-voice-best-practices.html) distinguishes premature turn ending from interruption and recommends slot-specific timing for dictated input. [Google speech settings](https://docs.cloud.google.com/dialogflow/cx/docs/concept/advanced-speech) separately configure end-of-speech detection, no-speech timeout and barge-in. Tune available Freya settings with actual audio, respecting language support; do not copy another vendor's milliseconds or thresholds.

Include coughs, echo, television, another person speaking and acknowledgments such as “mm-hmm” in the tests. Noise, devices, accents and integration behavior affect speech systems, as described in the [AWS Nova Sonic service card](https://docs.aws.amazon.com/ai/responsible-ai/nova-sonic/overview.html). Neither a prompt nor an unmeasured confidence threshold proves these conditions work.

Freya provides [voice timing settings and farewell behavior](workflow-format.md). Its timed farewell interruption window explicitly cannot be measured by chat simulation. Also, global interrupt nodes are checked after local transitions fail to match; merely adding a global hardship handler would not prove priority over a matching local success route. Test the actual transition order.

## Bind every bank action to the correct state

The [OWASP transaction-authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html) recommends acknowledgment of significant data, ordered authorization, invalidating authorization when data changes, and a final execution gate. A collections promise is not a funds transfer, but the underlying principle is useful: consent must apply to the exact action being executed.

For this builder, a **proposed** generic design is a per-product proposal containing canonical product ID, currency, amount in minor units, canonical date and a revision number. Record the revision read back and the revision clearly confirmed. The write hook should require matching revisions and allowed values, then reject any product with an acknowledged immutable promise. Do not create an approval flag solely because the model says it is approved; bind it to the relevant customer turn and completed readback evidence available from the platform.

Keep separate states for draft, awaiting confirmation, confirmed, submitted, acknowledged, known failed and unknown outcome. A changed amount, date or product invalidates the old confirmation. One product's agreement does not authorize another product; a combined confirmation requires the combined-readback policy. Currency and money normalization must never silently reinterpret the amount. Preserve canonical identifiers and use the brief's calendar and call date.

| When interruption occurs | Proposed handling consistent with the public contract |
|---|---|
| Before submission | Apply a permitted correction; validate and obtain fresh confirmation. |
| During a submitted request | Preserve the pending action. Do not announce cancellation or issue a replacement write merely because speech stopped. |
| After `ok:true` | State the recorded facts accurately. Public Brief A disallows changing or cancelling that promise during the call. |
| After a documented mock-bank `500` | The supplied API says nothing was recorded. Apply only the extracted retry rule. |
| After a transport timeout | Outcome may be unknown. Do not extend the `500` guarantee to it or invent an unsupported reconciliation endpoint. |

Freya [background tools](tools-and-code.md) with `config.interruptible=false` can survive a user interruption and return later. Gate result-dependent transitions on result availability and associate a late result with its original operation. Review the exact counter semantics rather than equating tool execution success with the bank's business acknowledgment.

Google's [webhook guidance](https://docs.cloud.google.com/dialogflow/cx/docs/concept/webhook) recommends deduplication or idempotency for retries. The hackathon API does not thereby acquire an idempotency-key field: use only supported fields, and add local protection against duplicate acknowledged actions. Local state alone cannot establish exactly-once behavior across a lost response or restarted call.

## Identity, privacy, accessibility and handoff

Birthday format validation and bank truth checking are different. A valid but wrong birthday must remain unverified. An impossible calendar date, incomplete answer or correction should receive the appropriate clarification without being mislabeled as a verified mismatch. Preserve leading zeros in phone answers. The existing [identity matrix](EDGE_CASES.md#identity-scenarios-to-verify) supplies these tests and documents current gaps.

The mock identity procedure is prescribed by the brief. It is not evidence of production-grade digital authentication: [NIST SP 800-63-4](https://pages.nist.gov/800-63-4/sp800-63/model/) rejects knowledge-based authentication as an acceptable secret for digital authentication under those guidelines. A real bank needs authentication appropriate to its channel, risks and required assurance. Do not add unsupported authentication tools to the hackathon agent.

Protect debt details before verification and avoid disclosure to a wrong party or an unverified representative. For in-scope firms, UK [CONC 7.9](https://handbook.fca.org.uk/handbook/conc7/conc7s9) addresses reasonable contact preferences and third-party disclosure. US [§1006.6](https://www.consumerfinance.gov/rules-policy/regulations/1006/6/) has specific contact restrictions and exceptions. Neither source licenses guessing that a spouse, caller or background speaker is authorized.

Accessibility should change the available interaction, not merely the apology. UK [FG21/1, Chapter 4](https://www.fca.org.uk/publication/finalised-guidance/fg21-1.pdf) supports flexible service, extra time, understandable communication and avoiding repeated disclosure of support needs. Proposed tests should cover slow speech, hearing or language needs and an unusable suggested channel. Use available routes; do not invent a text service, promise translation, infer a diagnosis or retain unnecessary medical detail.

A handoff needs the correct supported queue and truthful outcome. Where permitted, preserve verification status, the customer's request, acknowledged promises and unresolved matters so they need not start over. Google's [handoff reference](https://docs.cloud.google.com/dialogflow/cx/docs/reference/rpc/google.cloud.dialogflow.cx.v3#liveagenthandoff) distinguishes signaling handoff from implementing it. UK [PRIN 2A.6](https://handbook.fca.org.uk/handbook/prin2a/prin2as6) addresses unreasonable support barriers within scope. Saying a transfer will happen is not proof it succeeded; test failed transfer behavior against the supplied contract.

Record useful audit events without exposing identity answers or secrets in ordinary diagnostic output. [OWASP logging guidance](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) recommends protecting sensitive data and correlating events for an interaction. Proposed safe fields include pseudonymous call reference, product reference where appropriate, policy step, proposal revision, interruption event, action status and reason code. Control transcript access and retention separately. Existing redaction gaps are F11; this review did not establish production retention or access controls.

## Additional acceptance scenarios

All rows below are **proposed tests**, not passing results. **Text** means a full conversation with transcript and bank-state assertions. **Hook** means deterministic synthetic enforcement. **Audio** requires real playback and captured timing. Combine modes where indicated; a hook test cannot prove the caller heard a readback. Real-call testing validates voice use; the supplied kit's documented practice evaluation uses text, and this review establishes no additional hackathon requirement to run audio tests.

| ID | Scenario | Expected observation | Mode |
|---|---|---|---|
| C01 | Customer proposes a date instead of saying yes/no to today's question. | Validate the proposal; no refusal count spent. | Text |
| C02 | Customer asks what the product is after an offer. | Explain known facts; no repeated persuasion step or assumed refusal. | Text |
| C03 | One extracted persuasion step, two allowed attempts. | Repair or reject inconsistent policy; do not duplicate the final step. | Hook |
| C04 | Full amount equals the floor, or two steps resolve to the same date/amount. | Resolve according to the source rules and precedence; preserve explicitly authorized steps and invent no replacement offer. | Hook + Text |
| C05 | Caller returns to a product after another topic. | Preserve its attempt and rejected-offer history. | Text |
| C06 | Caller says “no,” then volunteers valid terms. | Evaluate the new proposal under current policy; do not treat it as another flat refusal. | Text |
| C07 | Hardship disclosed halfway through an offer. | Stop persuasion and apply the hardship override; validate and confirm a volunteered proposal where the brief permits it. | Text + Audio |
| C08 | “Would support let me change my payment date?” | Clarification of the support offer; no automatic declined-support note. | Text |
| C09 | “Give me a moment before I decide.” | Preserve waiting/undecided state under applicable limits. | Text + Audio |
| C10 | “I need a person” repeated after the allowed help offer. | Actual prescribed transfer; no loop of renewed help offers. | Text |
| C11 | Dispute expressed indirectly: “That balance cannot be mine.” | Recognize the dispute without a required magic phrase. | Text |
| C12 | Paid claim after current account retrieval. | Apply the paid-already rule without inventing proof or another promise. | Text |
| C13 | Wrong party requests debt details or offers to answer for the customer. | No unauthorized disclosure or assumed representation. | Text |
| C14 | Caller cannot use the app suggested after verification failure. | Use only an authorized alternative; avoid repeating an unusable route indefinitely. | Text |
| C15 | “Repeat just the last four digits question.” | Repeat that question; preserve verified birthday and prior attempts. | Text |
| C16 | Date or digits arrive over paused fragments. | Preserve components and leading zeros; avoid premature submission. | Text + Audio |
| C17 | Amount corrected within one utterance. | Select the final unambiguous amount or clarify; no concatenated numbers. | Text + Hook |
| C18 | “Yes, but change Thursday to Friday.” | No write for Thursday; validate and confirm Friday. | Text + Audio + Hook |
| C19 | A valid promise request appears without confirmed proposal state. | Block the write despite valid money and date. | Hook |
| C20 | Product changes after readback. | Old confirmation invalid; correct product must be read back. | Text + Hook |
| C21 | One readback covers two products; caller agrees to only one. | No authorization inferred for the other product. | Text |
| C22 | Caller interrupts before hearing the date in a readback. | No assumption of complete readback; restore required information before confirmation. | Audio |
| C23 | Caller says “yes” to “Can you hear me?” | No payment confirmation inferred. | Text + Audio |
| C24 | Same-product request repeated after acknowledgment. | No second write; report existing immutable record. | Hook + Text |
| C25 | Caller requests changing an already recorded promise. | Follow the prescribed change policy; no overwrite or invented cancellation. | Text + Hook |
| C26 | Response is lost after the bank may have recorded the action. | Accurate unknown/pending state; no unsupported success, failure or retry. | Hook + integration |
| C27 | Failed note is retried. | Only an acknowledged note affects outcome; one logical note in state. | Hook |
| C28 | Transfer fails after a promise was recorded. | Preserve the promise; closing and transfer claims match the actual response and policy. | Hook + Text |
| C29 | Late write result arrives after another product or terminal intent. | Correlate with original action; no cross-product mutation or contradictory farewell. | Hook + integration |
| C30 | Interrupt a long explanation. | Stop audible speech, flush its queued tail, capture and address caller input. | Audio |
| C31 | Cough, echo, television and another speaker. | Measure false interruptions, missed input and inappropriate confirmations separately. | Audio |
| C32 | Backchannel acknowledgment while the agent speaks. | No unintended commitment; preserve appropriate turn-taking. | Audio |
| C33 | Slow backend during an otherwise silent turn. | Appropriate waiting behavior; no fabricated result or extra write. | Text + Audio |
| C34 | “Goodbye” versus “Wait, one more thing” after farewell. | Correct actual hang-up-window behavior without polite replies causing loops. | Audio |
| C35 | Customer repeats or changes instructions to bypass bank policy. | No disclosure, unauthorized terms or skipped confirmation; tool data cannot override policy. | Text + Hook |
| C36 | Different language, accent or requested slower delivery. | Verify understanding or use an authorized recovery route; classify clarification and non-answers according to the brief. | Text + Audio |

## Repair order and evidence to collect

Prioritize **F16 confirmation, F17 immutable/duplicate action protection, F18 clarification classification and F19 repeated-step generation**, alongside existing F01–F09 blockers. These are generic builder changes; their field names, values and limits must come from the contract. Treat **F20 real audio acceptance** separately from text-score improvement. Empty WhatsApp, knowledge-base or integration menus are not a reason to add those features unless the supplied contract requires them.

For each repair, preserve a minimal failing synthetic case, implement the mechanism, and run focused regressions. Then run current-source automatic builds and official text practice with retained transcripts, bank records and all required repetitions. Review serious failures separately from average scores: unsupported disclosure, wrong product/terms, false success, duplicate acknowledged actions and policy-override errors cannot be justified by a good aggregate rate.

For real voice acceptance, measure interruption-to-playback-stop latency, residual stale speech, lost caller words, premature turn endings, false interruptions and backend waiting time. Track repeated questions without new need, repeated rejected offers, confirmation revisions, duplicate requests and transfer completion. Select timing targets after baselining the actual platform; this review establishes no universal millisecond threshold.

Evaluate appropriate resolution and policy correctness alongside promise amount. A correct dispute, hardship, identity-failure or transfer outcome can be a successful customer case. The [kit's scoring contract](../KIT_README.md) and the exact current-source acceptance evidence determine hackathon readiness; these research findings and proposed scenarios do not predict a win.
