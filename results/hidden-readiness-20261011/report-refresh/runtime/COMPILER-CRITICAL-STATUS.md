# Current compiler gap status

Focused offline compiler critical-gap refresh; no official hidden brief, remote/LLM calls, application edits or pytest suites.

Captured 2026-10-11T10:31:11.099129+00:00 to 2026-10-11T10:31:11.969703+00:00, 0.875 seconds. Source stable: True; scoped source SHA-256 `c980396fdfaf76a4a53fc4014ab07fdd38ce2814b36fb1dd1b2be975705c740a` before, `c980396fdfaf76a4a53fc4014ab07fdd38ce2814b36fb1dd1b2be975705c740a` after.

Snapshot covers 140 builder Python/Jinja, policy fixture, API, relevant test and helper files. No pytest suite ran.

URL retained only means a pre-hook permitted the request; no HTTP request was made. Mock results are inputs, not bank observations.

| Finding | Status |
|---|---|
| callback_invalid_minutes | still_present |
| callback_missing_facts | still_present |
| callback_staged_spoken_recovery | fixed |
| exact_customer_consent | still_present |
| parallel_response_attribution | still_present |
| parallel_supported_retry_and_duplicate | fixed |
| live_consent_and_engine_behavior | unproven |

## callback_invalid_minutes — still_present

Callback guard lacks numeric hour/minute validation.

In both compiled profiles, 11:99 and 12:60 retain the URL in the full chain and standalone guard while the helper labels them unreadable. Ordinary equal-to-now/weekend requests are blocked. URL retained means the pre-hook permits a request; this runner sends none.

Actual selected outputs:

```json
[
  {
    "profile": "staged",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:00"
    },
    "complete_chain": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:00"
      },
      "guard": "blocked: the time 11:00 on 2026-10-14 is outside the allowed callback times",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "guard_alone": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:00"
      },
      "guard": "blocked: the time 11:00 on 2026-10-14 is outside the allowed callback times",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "helper_check": "not_after_now"
  },
  {
    "profile": "staged",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:01"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:01"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:01"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "ok"
  },
  {
    "profile": "staged",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-17",
      "time": "12:00"
    },
    "complete_chain": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-17",
        "time": "12:00"
      },
      "guard": "blocked: a callback cannot be booked on 2026-10-17",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "guard_alone": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-17",
        "time": "12:00"
      },
      "guard": "blocked: a callback cannot be booked on 2026-10-17",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "helper_check": "not_allowed_day"
  },
  {
    "profile": "staged",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:99"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:99"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:99"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "unreadable"
  },
  {
    "profile": "staged",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "12:60"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "12:60"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "12:60"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "unreadable"
  },
  {
    "profile": "lean",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:00"
    },
    "complete_chain": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:00"
      },
      "guard": "blocked: the time 11:00 on 2026-10-14 is outside the allowed callback times",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "guard_alone": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:00"
      },
      "guard": "blocked: the time 11:00 on 2026-10-14 is outside the allowed callback times",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "helper_check": "not_after_now"
  },
  {
    "profile": "lean",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:01"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:01"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:01"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "ok"
  },
  {
    "profile": "lean",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-17",
      "time": "12:00"
    },
    "complete_chain": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-17",
        "time": "12:00"
      },
      "guard": "blocked: a callback cannot be booked on 2026-10-17",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "guard_alone": {
      "url_retained": false,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-17",
        "time": "12:00"
      },
      "guard": "blocked: a callback cannot be booked on 2026-10-17",
      "say": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?"
    },
    "helper_check": "not_allowed_day"
  },
  {
    "profile": "lean",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "11:99"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:99"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "11:99"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "unreadable"
  },
  {
    "profile": "lean",
    "input": {
      "customer_id": "c1",
      "date": "2026-10-14",
      "time": "12:60"
    },
    "complete_chain": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "12:60"
      },
      "guard": "ok",
      "say": null
    },
    "guard_alone": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "date": "2026-10-14",
        "time": "12:60"
      },
      "guard": "ok",
      "say": null
    },
    "helper_check": "unreadable"
  }
]
```

Source evidence:

- `builder/compiler/hooks.py:480` ("        if not (len(tm) == 5)
- `builder/compiler/hooks.py:486` ("            low_ok = tm > s[1])
- `builder/compiler/toolset.py:411` (hooks.pre_hook_callback_guard(*slot_fields)
- `builder/compiler/codegen.py:1077` (def check_callback_snippet()

## callback_missing_facts — still_present

Missing callback slots and custom day rules fail open.

The Saturday 08:00 request retains its URL when setup slots are absent, and when allowed_days='custom' generates an empty slot table. The numeric/time/day slot guard only runs with slots.

Actual selected outputs:

```json
[
  {
    "profile": "staged",
    "request": {
      "date": "2026-10-17",
      "time": "08:00"
    },
    "missing_setup_slots": {
      "url_retained": true,
      "body": {
        "date": "2026-10-17",
        "time": "08:00"
      },
      "guard": null,
      "say": null
    },
    "custom_rule_slots": "",
    "custom_rule": {
      "url_retained": true,
      "body": {
        "date": "2026-10-17",
        "time": "08:00"
      },
      "guard": null,
      "say": null
    }
  },
  {
    "profile": "lean",
    "request": {
      "date": "2026-10-17",
      "time": "08:00"
    },
    "missing_setup_slots": {
      "url_retained": true,
      "body": {
        "date": "2026-10-17",
        "time": "08:00"
      },
      "guard": null,
      "say": null
    },
    "custom_rule_slots": "",
    "custom_rule": {
      "url_retained": true,
      "body": {
        "date": "2026-10-17",
        "time": "08:00"
      },
      "guard": null,
      "say": null
    }
  }
]
```

Source evidence:

- `builder/compiler/hooks.py:475` ("    if slots and isinstance(b, dict):)
- `builder/compiler/codegen.py:448` (vars['callback_slots'] = ';'.join(slots))

## callback_staged_spoken_recovery — fixed

The staged workflow formerly lacked a dedicated guard correction route.

The current compiled graph has callback_fix_third_party and callback_fix_say_third_party; the local fix snippet writes the allowed-times correction and marks the block handled. This verifies generated wiring and text, not a live spoken turn or engine execution.

Actual selected outputs:

```json
[
  {
    "compiled_node_ids": [
      "callback_stop_third_party",
      "callback_fix_third_party",
      "callback_fix_say_third_party"
    ],
    "third_party_routes": [
      {
        "target_node": "callback_fix_third_party",
        "description": "The callback guard blocked a schedule_callback call",
        "condition": {
          "logic": "or",
          "conditions": [
            {
              "variable": "callback_guard",
              "operator": "contains",
              "operand": "blocked"
            }
          ]
        }
      },
      {
        "target_node": "callback_done",
        "description": "The callback was booked",
        "condition": {
          "logic": "or",
          "conditions": [
            {
              "variable": "callback_acked",
              "operator": "equals",
              "operand": "yes"
            }
          ]
        }
      },
      {
        "target_node": "rescue_third_party",
        "description": "A move was sent together with other tool calls and did not happen",
        "condition": {
          "logic": "or",
          "conditions": [
            {
              "variable": "transition_to_end_callback_booked_response",
              "operator": "contains",
              "operand": "Transitioning"
            },
            {
              "variable": "transition_to_end_callback_not_booked_response",
              "operator": "contains",
              "operand": "Transitioning"
            },
            {
              "variable": "transition_to_identity_response",
              "operator": "contains",
              "operand": "Transitioning"
            },
            {
              "variable": "transition_to_end_wrong_number_response",
              "operator": "contains",
              "operand": "Transitioning"
            },
            {
              "variable": "transition_to_end_deceased_response",
              "operator": "contains",
              "operand": "Transitioning"
            }
          ]
        }
      },
      {
        "target_node": "end_callback_booked",
        "description": "The callback is booked; you said goodbye.",
        "condition": {
          "operator": "matches_intent",
          "description": "The callback is booked; you said goodbye."
        },
        "is_tool_call": true
      },
      {
        "target_node": "end_callback_not_booked",
        "description": "No callback inside the allowed times, or nothing to book; you said goodbye.",
        "condition": {
          "operator": "matches_intent",
          "description": "No callback inside the allowed times, or nothing to book; you said goodbye."
        },
        "is_tool_call": true
      }
    ],
    "fixed_callback_text": "I am sorry, I cannot book a callback at that time. I can book one today Wednesday 14 October after 11:00 to 19:00 or Thursday 15 October from 10:00 to 19:00. Which time would suit?",
    "callback_guard_after_fix": "handled"
  }
]
```

Source evidence:

- `builder/compiler/profiles/staged.py:889` (def callback_fix_routes()
- `builder/compiler/codegen.py:308` (def callback_fix_snippet()

## exact_customer_consent — still_present

No deterministic confirmation ledger binds a clear yes to exact promise terms.

A legal p1/100.00/2026-10-16 request is permitted without any stored customer confirmation in both profiles. Tool descriptions instruct clear yes after read-back; the hard pre-hook does not require evidence of that yes. This is an enforcement gap, not evidence that a real customer dialogue lacked consent.

Actual selected outputs:

```json
[
  {
    "profile": "staged",
    "request": {
      "customer_id": "c1",
      "product_id": "p1",
      "amount": 100,
      "date": "2026-10-16"
    },
    "consent_named_state_fields": [],
    "pre_hook_output": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "product_id": "p1",
        "amount": 100.0,
        "date": "2026-10-16"
      },
      "guard": "ok",
      "say": null
    },
    "tool_description": "Record ONE payment promise for ONE product. CALL it only after you read back product, amount, date and the customer clearly said yes."
  },
  {
    "profile": "lean",
    "request": {
      "customer_id": "c1",
      "product_id": "p1",
      "amount": 100,
      "date": "2026-10-16"
    },
    "consent_named_state_fields": [],
    "pre_hook_output": {
      "url_retained": true,
      "body": {
        "customer_id": "c1",
        "product_id": "p1",
        "amount": 100.0,
        "date": "2026-10-16"
      },
      "guard": "ok",
      "say": null
    },
    "tool_description": "Record ONE payment promise for ONE product. CALL it only after you read back product, amount, date and the customer clearly said yes."
  }
]
```

Source evidence:

- `builder/compiler/toolset.py:308` (description=(("Record ONE payment promise)
- `builder/compiler/toolset.py:316` (hooks.pre_hook_promise_guard(roles.product)
- `builder/compiler/hooks.py:521` (def pre_hook_promise_guard()
- `builder/compiler/hooks.py:809` ("            vars['promise_guard'] = 'ok'")

## parallel_response_attribution — still_present

A retry of an already successful member can falsely settle a held batch.

After p1 mock5xx and p2/p3 positive results, retrying p2 also marks p1 as promised despite no positive p1 result in this probe. The shared settlement rule accepts enough matching retried keys, without per-request response correlation. The note probe similarly logs p1:hardship after its negative result and an unrelated p2 retry. Both profiles reproduce this.

Actual selected outputs:

```json
{
  "promise_batches": [
    {
      "profile": "staged",
      "initial_calls": [
        "p1 mock5xx/no post",
        "p2 acknowledged",
        "p3 acknowledged"
      ],
      "initial_sent": [
        true,
        true,
        true
      ],
      "initial_state": {
        "promise_acks": "2",
        "promised_products": null,
        "promise_pending_product": "p1,p2,p3"
      },
      "correct_retry_sent": [
        true
      ],
      "correct_retry_state": {
        "promise_acks": "3",
        "promised_products": "p1,p2,p3"
      },
      "wrong_retry_sent": [
        true
      ],
      "wrong_retry_state": {
        "promise_acks": "3",
        "promised_products": "p2,p1,p3"
      },
      "duplicate_after_correct_retry_sent": [
        false
      ],
      "duplicate_guard": "blocked: a promise for p2 is already recorded and cannot be changed or recorded again"
    },
    {
      "profile": "lean",
      "initial_calls": [
        "p1 mock5xx/no post",
        "p2 acknowledged",
        "p3 acknowledged"
      ],
      "initial_sent": [
        true,
        true,
        true
      ],
      "initial_state": {
        "promise_acks": "2",
        "promised_products": null,
        "promise_pending_product": "p1,p2,p3"
      },
      "correct_retry_sent": [
        true
      ],
      "correct_retry_state": {
        "promise_acks": "3",
        "promised_products": "p1,p2,p3"
      },
      "wrong_retry_sent": [
        true
      ],
      "wrong_retry_state": {
        "promise_acks": "3",
        "promised_products": "p2,p1,p3"
      },
      "duplicate_after_correct_retry_sent": [
        false
      ],
      "duplicate_guard": "blocked: a promise for p2 is already recorded and cannot be changed or recorded again"
    }
  ],
  "note_batches": [
    {
      "profile": "staged",
      "initial_sent": [
        true,
        true
      ],
      "initial_notes_log": null,
      "wrong_retry_sent": [
        true
      ],
      "after_wrong_retry_notes_log": "p2:refused;p1:hardship;"
    },
    {
      "profile": "lean",
      "initial_sent": [
        true,
        true
      ],
      "initial_notes_log": null,
      "wrong_retry_sent": [
        true
      ],
      "after_wrong_retry_notes_log": "p2:refused;p1:hardship;"
    }
  ]
}
```

Source evidence:

- `builder/compiler/hooks.py:393` (def _batch_settle()
- `builder/compiler/hooks.py:423` ("                if kept and len([k for k in kept if)
- `builder/compiler/hooks.py:904` (def post_hook_promise_log()
- `builder/compiler/hooks.py:886` (def post_hook_notes_log()

## parallel_supported_retry_and_duplicate — fixed

A successful supported retry and committed-product duplicate block are implemented.

The correct p1 retry logs all three products; a later p2 promise is blocked. This establishes that these local patterns work while the wrong-member attribution case above remains open.

Actual selected outputs:

```json
[
  {
    "profile": "staged",
    "initial_calls": [
      "p1 mock5xx/no post",
      "p2 acknowledged",
      "p3 acknowledged"
    ],
    "initial_sent": [
      true,
      true,
      true
    ],
    "initial_state": {
      "promise_acks": "2",
      "promised_products": null,
      "promise_pending_product": "p1,p2,p3"
    },
    "correct_retry_sent": [
      true
    ],
    "correct_retry_state": {
      "promise_acks": "3",
      "promised_products": "p1,p2,p3"
    },
    "wrong_retry_sent": [
      true
    ],
    "wrong_retry_state": {
      "promise_acks": "3",
      "promised_products": "p2,p1,p3"
    },
    "duplicate_after_correct_retry_sent": [
      false
    ],
    "duplicate_guard": "blocked: a promise for p2 is already recorded and cannot be changed or recorded again"
  },
  {
    "profile": "lean",
    "initial_calls": [
      "p1 mock5xx/no post",
      "p2 acknowledged",
      "p3 acknowledged"
    ],
    "initial_sent": [
      true,
      true,
      true
    ],
    "initial_state": {
      "promise_acks": "2",
      "promised_products": null,
      "promise_pending_product": "p1,p2,p3"
    },
    "correct_retry_sent": [
      true
    ],
    "correct_retry_state": {
      "promise_acks": "3",
      "promised_products": "p1,p2,p3"
    },
    "wrong_retry_sent": [
      true
    ],
    "wrong_retry_state": {
      "promise_acks": "3",
      "promised_products": "p2,p1,p3"
    },
    "duplicate_after_correct_retry_sent": [
      false
    ],
    "duplicate_guard": "blocked: a promise for p2 is already recorded and cannot be changed or recorded again"
  }
]
```

Source evidence:

- `builder/compiler/hooks.py:354` (def _batch_open()
- `builder/compiler/hooks.py:521` (def pre_hook_promise_guard()

## live_consent_and_engine_behavior — unproven

Actual dialogue compliance and current live response correlation were not checked.

These are deterministic local counterexamples under the saved regression test double. No actual customer conversation, bank persistence, or current remote engine execution was observed.

Actual selected outputs:

```json
{
  "network_requests": 0,
  "llm_calls": 0,
  "test_model": "all pre-hooks before mock post-hooks; counters supplied by saved helper"
}
```

Source evidence:

- `docs/codebase-guide/examples/compiler_examples.py:93` (def batch()
- `tests/unit/test_parallel_acknowledgement.py:62` (def batch()

## Capture limits

When source_stable=false, results are provisional because concurrent edits occurred during capture.

The mock batch helper supplies counters and assumes the pre/post order recorded in existing tests. Per-request bank results are deliberately not injected into a post-hook's configuration. This reproduces the local algorithm, not an unobserved live deployment.

Original worked-example JSON and all prior readiness reports remain untouched. [Machine-readable current report](compiler-critical-status.json).
