# Fixing issues: the live backlog

This is the one list of known defects and their status. It replaces the narrative backlog of 10 October (F01-F22) and
maps every later review onto it. Measured results do not live here: live scores are in
[results/BASELINE.md](../results/BASELINE.md), the closure review of 11 October is in
[results/QUALITY_REVIEW.md](../results/QUALITY_REVIEW.md), and the submission procedure is in
[SUBMISSION.md](SUBMISSION.md).

Last updated: Sunday 11 October 2026, before the hidden briefs arrive (17:00 UTC). After 17:00 only generic fixes are
allowed; nothing brief-specific goes into `builder/` (check with `tests/unit/test_compliance.py`, see AUD-58).

## Legend

| Id | Source |
|---|---|
| F01-F22 | The first backlog (10 October), kept as ids so older reports stay readable |
| AUD-01 to AUD-60 | The Opus audit of 11 October (02:53 UTC); the fix round for it is done |
| GAP-1 to GAP-7 | [CRITICAL-GAPS.md](CRITICAL-GAPS.md), the external reviewer's report, numbered as in that file |
| N-1 to N-3 | New findings of [results/QUALITY_REVIEW.md](../results/QUALITY_REVIEW.md) |

Status values: **fixed** (the named test fails without the fix), **this round** (a fix is being made on 11 October
afternoon; the named test exists), **open**, **deferred** (known, not worth the risk before the deadline),
**by design** (the behaviour is intended; the reason is given), **user action** (only the person who commits can do it).
"Fixed" means offline evidence. Only a live build scored with three repetitions proves customer behaviour.

## What can still cost hidden-brief cases

1. **Live proof of the final source (F14).** The accepted live builds (Brief A 8 of 10 customers, A+B 3 of 3 B cases)
   predate the AUD fix round. A second live acceptance is in progress (`results/acceptance2/`); its result goes to
   [BASELINE.md](../results/BASELINE.md).
2. **Date correction binding (F21).** The guard checks that a date is legal, not that it is the correction the brief
   requires for the customer's own proposal (PUB-07 was 2 of 3 live).
3. **Persuasion after an early refusal (AUD-31).** Starting each product at attempt 1 and making every scripted attempt
   is a prompt rule, not tracked state.
4. **Exact consent (GAP-2, F16).** Read-back and a clear yes before `create_promise` are prompt rules.
5. **Post-promise rules in a single-product call (AUD-33).** The call closes after the recorded sentence.

## CRITICAL-GAPS (external review) disposition

| Gap | Finding | Status | Reason or evidence |
|---|---|---|---|
| GAP-1 | Parallel bank results are not tied to their own product | by design (platform limit) | Post-hooks see only the response, and the bank's response carries no product id, so a hook cannot know which request it answers. Forcing one call at a time would make the agent read blocked-call errors to customers. The batch staging (AUD-06) commits a batch only when every call in it is acknowledged; the one wrong path (a retry of an already successful product) also creates a duplicate ledger record, which fails the case anyway. |
| GAP-2 | Exact customer consent is not enforced by stored state | by design (prompt rule) | Hooks cannot read the transcript, and the phone agent may call only the bank API, so no code can see the customer's yes. The tool description and the negotiation prompt require a read-back of product, amount and date and a clear yes. The hard guard still blocks every term the rules forbid. |
| GAP-3 | A successful build summary can hide unresolved policy errors | fixed (this round) | `guard_policy` records `policy_unresolved`; `finalize` lists them as `policy_issues`, adds a failed `policy_validation` test and a WARN trace line per issue. `tests/graph/test_policy_acceptance.py::test_unrepaired_validator_errors_are_reported_not_hidden` |
| GAP-4 | Callback guard accepts invalid clock times (`11:99`) or skips missing slot rules | fixed (this round) for clock times; by design for custom rules | Hours 00-23 and minutes 00-59 are checked: `tests/unit/test_state_routes_and_dates.py::test_the_callback_guard_blocks_an_impossible_clock_time`, `::test_the_compiled_callback_guard_blocks_an_impossible_clock_time`. A custom callback rule has no computable slots, so its text goes to the prompt and the guard passes the request (`::test_a_custom_callback_rule_is_not_guarded`); blocking every booking would lose every callback case of such a brief. |
| GAP-5 | A floor taken from an account field can exceed the debt | fixed (this round) | One effective floor, never above the full amount, for facts, offers and the guard (`account_facts` caps the field floor as `policy.money.effective_floor` does). `tests/unit/test_state_routes_and_dates.py::test_a_floor_above_the_past_due_amount_is_capped_everywhere`, `tests/unit/test_money.py::test_effective_floor` |
| GAP-6 | An unbound required enum value is guessed (first enum member) | by design | Failing the compile would lose all 30 cases of a brief; the choice is written as a compile note in the trace so a person can see it. |
| GAP-7 | Minor units are assumed to be cents (scale 100) | this round | `roles.minor_scale` reads a stated scale (`thousandths`, `3 decimal places`, `multiply by 1000`); an unreadable one keeps cents with a compile note. `tests/unit/test_hidden_tool_contracts.py::test_a_promise_amount_in_thousandths_is_sent_and_guarded_with_that_scale`, `::test_account_amounts_in_thousandths_are_read_with_that_scale`, `::test_an_unreadable_minor_unit_scale_keeps_cents_and_is_noted` |

## AUD-01 to AUD-60

Test paths are relative to `tests/`. "unit/x.py::name" is one test; a file name alone means the AUD section of that file.

| Id | Finding (short) | Status | Evidence |
|---|---|---|---|
| AUD-01 | Bare command ran `track=auto`, `self_test=auto` | fixed | Defaults are now `compiled` and `smoke`. unit/test_build_cli.py::test_the_bare_command_reproduces_the_accepted_live_configuration, unit/test_settings.py::test_defaults_match_the_contract, graph/test_graph_runs.py::test_the_default_self_test_never_redeploys_a_tested_version |
| AUD-02 | `python3` is 3.14 on the build machine | fixed + user action | `builder/_startup.py` hands the run to a supported interpreter or exits 2 with install commands. unit/test_startup.py::test_an_unsupported_python_hands_the_run_over_to_a_supported_interpreter. Run with Python 3.11-3.13. |
| AUD-03 | Untracked runtime modules break a clean checkout | fixed + user action | unit/test_startup.py::test_an_incomplete_checkout_ends_with_a_json_line_and_exit_2, unit/test_submission.py::test_the_commit_check_covers_the_install_files_and_the_startup_module. Commit every runtime file. |
| AUD-04 | Reworded success flags raise CompileError | fixed | unit/test_hidden_tool_contracts.py::test_a_reworded_success_flag_compiles_and_is_read_by_the_hooks; string status fields: test_a_string_status_is_the_success_signal (same file), variants/test_structural_variants.py::test_a_string_status_rejection_is_never_acknowledged |
| AUD-05 | Same-named briefs share one slot | fixed | A colliding derived slot gets an inputs fingerprint. graph/test_deploy.py::test_the_deploy_records_the_inputs_fingerprint_with_the_slot, unit/test_inputs.py |
| AUD-06 | Parallel calls lose acknowledgements | fixed | Batch staging. unit/test_parallel_acknowledgement.py::test_parallel_promises_are_all_acknowledged_and_each_repeat_is_blocked. Residual: GAP-1 |
| AUD-07 | Routes used HTTP success counters | fixed | `promise_acked` / `callback_acked` flags written by post-hooks only on a bank acknowledgement. unit/test_compile_staged.py::test_third_party_books_callbacks_with_a_deterministic_farewell |
| AUD-08 | Intent actions other than transfer were dropped | fixed | unit/test_compile_flow_exits.py::test_an_after_verification_callback_intent_books_and_closes_on_its_own_row, ::test_a_single_product_note_action_is_written_by_the_engine_before_the_end |
| AUD-09 | Prescribed sentences dropped | fixed | unit/test_compile_text_round3.py::test_a_sentence_under_an_unknown_extra_key_is_placed_in_its_step, ::test_the_paid_sentence_reaches_the_already_paid_bullet |
| AUD-10 | Rules the IR cannot express counted as covered | fixed (floors); prompt-only for the rest | Computed floors: unit/test_floor_rules.py::test_a_percent_floor_reaches_the_facts_and_the_promise_guard. Other cited residual rules are printed verbatim: unit/test_cited_rule_text.py |
| AUD-11 | Stages without an exit for a closing situation | fixed | unit/test_compile_flow_exits.py::test_multi_product_refuse_to_talk_notes_every_open_product_and_ends, ::test_third_party_scoped_intent_gets_a_third_party_exit |
| AUD-12 | Single-brace placeholders spoken | fixed | unit/test_compile_text_round3.py::test_a_declared_variable_in_any_sentence_becomes_a_reference, ::test_an_undeclared_name_in_a_spoken_line_is_left_out_with_a_note |
| AUD-13 | Clock variable fixed to `now` | fixed | variants/test_structural_variants.py::test_the_date_code_reads_the_renamed_clock |
| AUD-14 | Guard false positives end the build `blocked` | fixed | Grounding findings left after the repairs deploy best effort, listed as policy issues. graph/test_graph_runs.py::test_grounding_findings_left_after_the_repairs_deploy_best_effort |
| AUD-15 | Identity answer normalisation | fixed | unit/test_hooks.py::test_identity_answers_use_the_expected_length, variants/test_structural_variants.py::test_a_postcode_is_sent_in_the_format_the_contract_states |
| AUD-16 | Renamed transfer queue field | fixed | unit/test_hidden_tool_contracts.py::test_an_ir_keyed_by_the_renamed_queue_field_keeps_every_handoff |
| AUD-17 | Date table contradicts the guard | fixed | unit/test_compiler_lessons.py::test_the_date_table_marks_every_day_after_the_deadline_for_one_product. Correction binding stays open (F21). |
| AUD-18 | Persuasion date kinds not checked against step text | fixed | unit/test_persuasion_date_cues.py. Run-to-run variance of one extraction call remains. |
| AUD-19 | No hidden-day runbook | fixed (docs) | [FINAL_RUNBOOK.md](FINAL_RUNBOOK.md) and [SUBMISSION.md](SUBMISSION.md); IMPLEMENTATION_PLAN section 10 is marked historical. |
| AUD-20 | Validator read the kit's field names | fixed | unit/test_validate_policy.py::test_vocabulary_follows_renamed_api_fields |
| AUD-21 | Minor-unit amounts converted to floats | fixed (cents) | unit/test_hidden_tool_contracts.py::test_a_minor_unit_amount_is_sent_as_integer_cents, ::test_account_amounts_in_cents_are_read_as_cents. Other scales: GAP-7 |
| AUD-22 | Fixed required request field breaks the compile | fixed | unit/test_hidden_tool_contracts.py::test_an_engine_call_sends_a_fixed_required_field_and_keeps_the_staged_profile |
| AUD-23 | No "all verified" flag reaches `get_account` by intent | fixed | unit/test_compile_api_variants.py::test_unrelated_booleans_never_route_a_per_answer_contract_to_account, unit/test_state_routes_and_dates.py |
| AUD-24 | Empty account makes the agent improvise; guard fails open | fixed | `account_unavailable` node. unit/test_state_routes_and_dates.py::test_an_account_that_could_not_be_loaded_closes_as_a_recording_failure, unit/test_compiler_lessons.py::test_guard_blocks_when_no_account_facts_are_loaded |
| AUD-25 | Transfer not retried after a 500 | fixed | unit/test_hidden_tool_contracts.py::test_every_handoff_call_is_retried_once_and_writes_are_not |
| AUD-26 | `add_note` without once-per-product guard | fixed | unit/test_parallel_acknowledgement.py::test_a_second_note_for_a_product_is_never_sent, unit/test_compile_consistency.py::test_several_product_notes_are_written_at_the_end_never_before_a_transfer |
| AUD-27 | No instruction after a 5xx (post-hook does not run) | fixed (prompt rule; platform limit) | unit/test_hidden_tool_contracts.py::test_the_system_prompt_says_how_to_handle_a_tool_error |
| AUD-28 | A transition sent with bank tools is dropped | fixed | `rescue_<stage>` nodes. unit/test_compile_flow_exits.py::test_every_tool_exit_of_a_node_with_bank_tools_has_a_rescue_route |
| AUD-29 | Callback slots not checked in code | fixed | `pre_hook_callback_guard`, `callback_fix_<stage>`. unit/test_state_routes_and_dates.py. Invalid clock times: GAP-4 |
| AUD-30 | Short ladder silently lowers attempts | fixed | Validation error. unit/test_validate_policy.py::test_a_band_with_more_attempts_than_steps_is_an_error_that_names_both_remedies |
| AUD-31 | Attempt 1 skipped after an early refusal (PUB-12) | open (prompt rule only) | `negotiate.v1.j2` says to start each product at attempt 1 and make every scripted attempt; no tracked offer state. |
| AUD-32 | Joint read-back and one confirmation not enforced | fixed (prompt wording) | unit/test_compile_text_round3.py::test_the_joint_confirmation_defers_every_per_product_read_back |
| AUD-33 | Single-product promise hangs up without a customer turn | deferred | The measured live agent behaves this way and passed; post-promise rules are reachable only before the promise. |
| AUD-34 | Multi-product IR switches ignored | fixed | unit/test_compile_flow_exits.py::test_a_custom_product_order_keeps_the_brief_rule, ::test_attempts_counted_for_the_whole_call |
| AUD-35 | Identity decline limit hard-coded to two | fixed | unit/test_compile_flow_exits.py::test_the_identity_decline_limit_follows_decline_max |
| AUD-36 | Amended rules printed without their amendment | fixed | unit/test_compile_text_round3.py::test_a_narrowed_rule_carries_its_amendment |
| AUD-37 | Offer wording counted as a second offer | fixed | unit/test_compile_offer_branches.py::test_a_question_is_answered_and_the_decision_is_asked_for_once |
| AUD-38 | Guard correction asks "Which amount can you pay?" | fixed | unit/test_compile_text_round3.py::test_the_amount_correction_never_asks_how_much |
| AUD-39 | Bands cannot depend on product type or amount | fixed (product type); deferred (amount) | unit/test_band_product_types.py. A gap in a type's table is a warning. |
| AUD-40 | Only one provider has run live | user action | Pin `LLM_PROVIDERS` in `.env` to the provider used in the accepted run. |
| AUD-41 | Tests use hand-written IRs only | fixed | unit/test_real_ir_compile.py (model-made IRs of the accepted runs in `tests/fixtures/real_ir/`) |
| AUD-42 | Committed `.freya-agents.json` can update a foreign workflow | fixed + user action | Entries of another workspace are never updated. unit/test_freya_client.py::test_a_403_on_the_recorded_workflow_recreates_the_slot. See N-3. |
| AUD-43 | Status docs contradict the code | fixed (docs, 11 October) | Status claims now link to BASELINE.md and QUALITY_REVIEW.md; planning documents carry a historical banner. |
| AUD-44 | Docs depend on machine-local evidence | fixed (docs, 11 October) | Links into the git-ignored `.cache/` were removed or marked machine-local. |
| AUD-45 | Third party hands the phone over: no route back | fixed | unit/test_compile_flow_exits.py::test_third_party_can_hand_back_to_identity_when_the_customer_comes_to_the_phone |
| AUD-46 | Outcome table cannot depend on promise terms | deferred | Shadowed rows are a validation warning: unit/test_validate_policy.py::test_a_shadowed_outcome_row_is_a_warning |
| AUD-47 | Hard-coded "no discount or waiver" line | fixed | unit/test_compile_text_round3.py::test_a_brief_with_a_waiver_queue_does_not_refuse_waivers |
| AUD-48 | More contract shapes break the compile | fixed | unit/test_hidden_tool_contracts.py::test_integer_product_ids_compile_and_are_sent_as_integers, ::test_a_constant_header_is_sent_and_an_open_credential_is_reported |
| AUD-49 | Floor sentence repeats its note, lacks an article | fixed | unit/test_compile_text_round3.py::test_a_floor_sentence_that_already_holds_the_note_says_it_once |
| AUD-50 | Non-ISO dates and `now` values | fixed | unit/test_state_routes_and_dates.py::test_a_now_with_an_offset_is_read_as_wall_clock_time, variants/test_structural_variants.py::test_a_non_iso_day_in_a_date_field_without_a_format_is_rebuilt |
| AUD-51 | Opening mentions the account to a relative | fixed | unit/test_compile_text_round3.py::test_the_opening_gives_a_relative_nothing_about_the_account |
| AUD-52 | Form of address for non-consumer briefs | fixed | unit/test_compile_text_round3.py::test_only_name_like_placeholders_form_the_address |
| AUD-53 | Model calls get the whole-budget deadline | fixed | graph/test_graph_runs.py::test_a_policy_repair_leaves_time_for_compile_validation_and_deploy |
| AUD-54 | Two identical tool-API files abort the run | fixed | unit/test_inputs.py::test_identical_tool_api_copies_are_collapsed |
| AUD-55 | Missing Freya key turns a build into a dry run | fixed | Exit 2 with the reason. unit/test_build_cli.py::test_a_missing_freya_key_without_dry_run_exits_2_and_says_why |
| AUD-56 | Workflow-cap rejection sent to the repair loop | fixed | graph/test_deploy.py::test_a_workspace_limit_on_create_ends_the_run_without_a_repair |
| AUD-57 | Trace lacks git state; absolute paths | fixed | The START record names the commit and dirty count. unit/test_trace.py::test_repository_paths_are_written_relative_to_the_repository |
| AUD-58 | No literal scan for post-17:00 fixes | fixed | `COMPLIANCE_BRIEFS=<hidden brief files> pytest tests/unit/test_compliance.py` (`test_builder_holds_no_brief_literals`) |
| AUD-59 | Tests write into `results/`; tracked temp files | fixed + user action | unit/test_test_isolation.py::test_unit_tests_never_point_builder_output_dirs_at_the_repo. 165 old files under `results/test-runs/` are still tracked. |
| AUD-60 | Small doc inaccuracies | fixed (docs, 11 October) | Lint range L001-L029, defaults, module map, links. |

## F01-F22 and N-1 to N-3

| Id | Finding (short) | Status | Evidence |
|---|---|---|---|
| F01 | Tool field roles from the contract | fixed | unit/test_compile_api_variants.py, AUD-04, AUD-16, AUD-20 |
| F02 | Empty allowed promise dates fail open | fixed | unit/test_codegen_promise_dates.py, unit/test_codegen_calendar_parity.py |
| F03 | Product ids changed or merged | fixed | unit/test_hidden_tool_contracts.py::test_ids_that_differ_only_by_case_are_never_merged (also N-2) |
| F04 | State updated before acknowledgement | fixed | unit/test_compiled_note_acknowledgment.py, AUD-07 |
| F05 | Published version and matching export | fixed | graph/test_deploy.py |
| F06 | Identity mismatch vs unreadable input vs service failure | fixed in its main part | AUD-15, AUD-23. A service error still gets the generic error rule (AUD-27). |
| F07 | Retry and refusal limits from policy | fixed | Retries: unit/test_compile_consistency.py; refusals: AUD-35 |
| F08 | Wall-clock limit on active operations | fixed; residual deferred | graph/test_wall_clock_guard.py. The opt-in practice self-test waits for the simulation slot without its own timeout; it runs only on the kit briefs. |
| F09 | Walkthrough evidence | deferred | The walkthrough is opt-in (`--self-test walkthrough`); the default is `smoke`. |
| F10 | Failures hidden by the last repetition | fixed | unit/test_eval_triage.py |
| F11 | Secrets in error texts | fixed | graph/test_graph_runs.py::test_a_secret_inside_an_error_text_never_reaches_the_result_file, unit/test_build_cli.py::test_log_output_of_the_cli_masks_secrets |
| F12 | Agentic credential forwarding | fixed | QUALITY_REVIEW row F12 |
| F13 | Reproducible environment | fixed | The project `.venv` runs Python 3.13 and the suite passes there; AUD-02 |
| F14 | Live acceptance of the automatic builder on the final source | open | See "What can still cost hidden-brief cases" |
| F15 | Packaging | user action | Commit all runtime files (AUD-03) |
| F16 | Confirmation bound to the pending proposal | by design | Same as GAP-2 |
| F17 | One acknowledged promise per product | fixed | unit/test_compiled_promise_once.py |
| F18 | Clarification vs declining support | fixed | unit/test_compile_offer_branches.py |
| F19 | Short persuasion ladders padded | fixed | unit/test_codegen_persuasion_ladder.py, AUD-30 |
| F20 | Real audio interruption | deferred | The evaluation uses text chat; audio behaviour is not scored. |
| F21 | Date correction bound to the customer's proposal | open | Partly: AUD-17 (date table and per-product holiday list). |
| F22 | Model-fallback regressions and the lone-provider cooldown | fixed | unit/test_llm_gateway.py::test_an_open_circuit_with_one_provider_still_retries_with_backoff |
| N-1 | A test writes into `results/` | fixed | AUD-59 |
| N-2 | Product id matching without exact-match precedence | fixed | unit/test_hidden_tool_contracts.py::test_the_staged_promise_check_and_decide_keep_exact_twin_ids |
| N-3 | `.freya-agents.json` is tracked | fixed in code + user action | AUD-42; decide before submitting whether the file stays in the commit. |

## Closing an item

1. Write a small failing test with synthetic inputs (no public-brief values in `builder/`).
2. Fix the generic mechanism; run the affected tests, then the full suite and `ruff check builder tests evals`.
3. Run an offline dry run on both kit briefs (`--dry-run --fake-llm tests/fixtures/policy`).
4. Set the status here with the test name. A changed prompt alone closes a prompt-rule item, never a behaviour defect.
