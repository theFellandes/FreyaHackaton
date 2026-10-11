# Synthetic compiler and tool-contract readiness

SYNTHETIC pre-release compiler/tool-contract preparedness; not official hidden briefs.

Observed 2026-10-11T09:52:29.914220+00:00 through 2026-10-11T09:52:49.252197+00:00. Official release is 2026-10-11T17:00:00+00:00.

**Result:** 261/261 passed; 0 failures, 0 errors, 0 skipped. Pytest exit 0. Pytest reported 16.37 seconds; process wall time 17.797 seconds.

Source fingerprint before/after: `38ae58b7fb874d8972e1f17d1a0e3d973ece22d0febde6c5fca372c685f9e6e6` / `38ae58b7fb874d8972e1f17d1a0e3d973ece22d0febde6c5fca372c685f9e6e6`; 143 hashed files; stable: True.

The manifest covers builder Python/Jinja files, selected tests, their conftest, policy JSON fixtures, tools/openapi.json and pyproject.toml. It does not hash the whole workspace.

## Command

```powershell
C:\Users\Felladien\Documents\Projects\Python\FreyaHackaton\.venv-playground\Scripts\python.exe -m pytest tests/unit/test_hidden_tool_contracts.py tests/unit/test_compile_api_variants.py tests/unit/test_tool_api.py tests/unit/test_hooks.py -m "not live" -q -o "addopts=--strict-markers -p no:langsmith" --basetemp C:\Users\Felladien\Documents\Projects\Python\FreyaHackaton\.cache\hidden-readiness-20261011\compiler\tmp -o cache_dir=C:\Users\Felladien\Documents\Projects\Python\FreyaHackaton\.cache\hidden-readiness-20261011\compiler\pytest-cache --junitxml C:\Users\Felladien\Documents\Projects\Python\FreyaHackaton\.cache\hidden-readiness-20261011\compiler\junit.xml
```

The non-live test fixtures scrub credentials, reject the real .env, and block non-loopback socket connections. The additional probes block every socket connect.

## Per-module results

| Module | Tests | Passed | Failures | Errors | Skipped |
|---|---:|---:|---:|---:|---:|
| `tests/unit/test_hidden_tool_contracts.py` | 88 | 88 | 0 | 0 | 0 |
| `tests/unit/test_compile_api_variants.py` | 67 | 67 | 0 | 0 | 0 |
| `tests/unit/test_tool_api.py` | 22 | 22 | 0 | 0 | 0 |
| `tests/unit/test_hooks.py` | 84 | 84 | 0 | 0 | 0 |

## Actual pure local examples

These are generated code/schema/hook outputs, not dialogue transcripts. The exact selected inputs and observed fields are recorded in compiler-readiness.json; hook states are reduced to those fields.

### explicit_success_vs_audit

Input:

```json
{
  "success_field": "success",
  "transport_status": "success"
}
```

Actual output:

```json
{
  "selected_success_field": "success",
  "runs": [
    {
      "response": {
        "audit_logged": true,
        "success": false
      },
      "promise_acks": null,
      "promised_products": "",
      "instruction": "The promise was NOT recorded. Never say it is recorded. If this is the first failure for this product, call create_promise once more with exactly the same values now. If it already failed once before, say \"I'm sorry, I could not record your promise.\", then remember the reason \"service_error\" for that product (do not call add_note yet) and continue with the next product (rule B7)."
    },
    {
      "response": {
        "audit_logged": false,
        "success": true
      },
      "promise_acks": "1",
      "promised_products": "p1",
      "instruction": "The promise IS recorded. If this was the only product, say nothing: the system confirms it. If the customer confirmed other products in the same read-back that are not recorded yet, call create_promise for the next one now, without asking again. Once all of them are recorded, tell the customer they are recorded, say exactly: \"You will receive a text message confirming this promise.\" once, then continue with any product not handled yet, or close the call if every product is handled."
    }
  ]
}
```

Transport success and a true audit flag do not acknowledge a rejected promise.

Evidence: `tests/unit/test_compile_api_variants.py::test_the_acknowledged_promise_follows_the_success_role_not_an_audit_boolean`.

### minor_units_and_floor

Input:

```json
{
  "field": "amount_cents",
  "field_type": "integer",
  "floor_major_units": 10
}
```

Actual output:

```json
[
  {
    "model_amount_major_units": "20.00",
    "body": {
      "product_id": "p1",
      "date": "2026-10-15",
      "amount_cents": 2000
    },
    "url": "u",
    "guard": "ok"
  },
  {
    "model_amount_major_units": "5",
    "body": {
      "product_id": "p1",
      "date": "2026-10-15",
      "amount_cents": 500
    },
    "url": "",
    "guard": "blocked: the amount 5.00 is not allowed for p1"
  }
]
```

20.00 major units becomes integer 2000; a 5.00 proposal is blocked by the floor.

Evidence: `tests/unit/test_hidden_tool_contracts.py::test_a_minor_unit_amount_is_sent_as_integer_cents`.

### integer_is_not_automatically_cents

Input:

```json
{
  "field": "amount",
  "type": "integer",
  "description": "",
  "said": "20"
}
```

Actual output:

```json
{
  "body": {
    "product_id": "p1",
    "date": "2026-10-15",
    "amount": 20.0
  },
  "amount_python_type": "float",
  "guard": "ok"
}
```

Without a minor-unit hint the current hook keeps major units as 20.0; it emits a float even for this integer schema.

Evidence: `tests/unit/test_hidden_tool_contracts.py::test_an_integer_amount_without_a_minor_hint_stays_in_major_units`.

### case_sensitive_product_ids

Input:

```json
{
  "known_ids": [
    "Ab-1",
    "aB-1"
  ],
  "amount": 200,
  "ranges": {
    "Ab-1": [
      10,
      50
    ],
    "aB-1": [
      100,
      500
    ]
  }
}
```

Actual output:

```json
[
  {
    "sent_id": "aB-1",
    "body": {
      "product_id": "aB-1",
      "amount": 200.0,
      "date": "2026-10-15"
    },
    "url": "offline",
    "guard": "ok"
  },
  {
    "sent_id": "Ab-1",
    "body": {
      "product_id": "Ab-1",
      "amount": 200.0,
      "date": "2026-10-15"
    },
    "url": "",
    "guard": "blocked: the amount 200.00 is not allowed for Ab-1"
  },
  {
    "sent_id": "AB-1",
    "body": {
      "product_id": "AB-1",
      "amount": 200.0,
      "date": "2026-10-15"
    },
    "url": "",
    "guard": "blocked: unknown product_id AB-1; use one of Ab-1,aB-1"
  }
]
```

Exact spelling selects its own product facts. An ambiguous normalized spelling is blocked.

Evidence: `tests/unit/test_hidden_tool_contracts.py::test_ids_that_differ_only_by_case_are_never_merged`.

### fixed_currency

Input:

```json
{
  "required_currency_schema": {
    "type": "string",
    "const": "EUR"
  }
}
```

Actual output:

```json
{
  "currency_property": {
    "type": "string",
    "default": "EUR"
  },
  "required_fields": [
    "product_id",
    "amount",
    "date"
  ]
}
```

The contract supplies EUR as a default and removes currency from the fields the model must provide.

Evidence: `tests/unit/test_hidden_tool_contracts.py::test_a_model_tool_never_asks_the_model_for_a_fixed_value`.

### required_multivalue_engine_enum

Input:

```json
{
  "priority": {
    "required": true,
    "enum": [
      "normal",
      "urgent"
    ]
  }
}
```

Actual output:

```json
{
  "handoff_support_body_defaults": {
    "customer_id": "{{customer_id}}",
    "queue": "support",
    "priority": "normal"
  },
  "compiler_notes": [
    "transfer_to_agent.priority is required and the contract fixes no value; the engine call sends its first allowed value 'normal'"
  ]
}
```

For this unbound engine field the compiler chooses the first enum and reports the guess; valid syntax is not proof of bank intent.

Evidence: `tests/unit/test_hidden_tool_contracts.py::test_an_engine_call_sends_a_fixed_required_field_and_keeps_the_staged_profile`.

### identity_scope_from_contract

Input:

```json
{
  "answer_flag": "answer_result",
  "overall_flag": "check_result"
}
```

Actual output:

```json
{
  "account_routes": [
    "verify_identity_response.data.check_result=true",
    "verify_identity_response.data.check_result=True"
  ],
  "runs": [
    {
      "response": {
        "ok": true,
        "answer_result": true,
        "check_result": false
      },
      "instruction": "This answer is correct. Now ask the next security question that has not been answered yet."
    },
    {
      "response": {
        "ok": true,
        "answer_result": true,
        "check_result": true
      },
      "instruction": "All security answers are verified. Say nothing now; the call continues automatically."
    }
  ]
}
```

A correct individual answer does not unlock account access until the overall check is true.

Evidence: `tests/unit/test_compile_api_variants.py::test_opaque_identity_names_follow_separate_documented_scopes`.

### unsupported_contracts_rejected

Input:

```json
{
  "synthetic_contracts": [
    "inverted_success",
    "unfillable_required_engine_field",
    "ambiguous_identity_verdict"
  ]
}
```

Actual output:

```json
[
  {
    "contract": "inverted_success",
    "result": "CompileError",
    "message": "verify_identity describes an inverted action-success flag; the compiler requires true to mean a positive result"
  },
  {
    "contract": "unfillable_required_engine_field",
    "result": "CompileError",
    "message": "close_call has required fields the compiler cannot fill: ['agent_ref']; every call would fail to close"
  },
  {
    "contract": "ambiguous_identity_verdict",
    "result": "CompileError",
    "message": "verify_identity has ambiguous overall verification fields: all_verified, check_result; the contract must distinguish their meanings"
  }
]
```

These unsupported or ambiguous contracts stop compilation instead of producing a usable workflow.

Evidence: `tests/unit/test_hidden_tool_contracts.py; tests/unit/test_compile_api_variants.py`.

## Limits

- All API variants are synthetic copies of the public seven-operation tool contract and known Brief A/A+B fixture policy IRs.
- No unreleased official brief, brief-to-IR LLM extraction, dialogue, remote client, Freya engine, or bank endpoint was tested.
- Passing tests establish these named examples only; they do not establish arbitrary-schema or arbitrary-brief generalization.
- Role matching uses supported names/types/English prose/enum heuristics; ambiguous or unsupported contracts may reject compilation.
- Minor-unit conversion assumes 100 units per major unit; currencies with other precision require separate support and tests.
- An unbound required engine enum may choose its first value with a compiler note; this can be semantically wrong for a new bank.
- An integer major-unit amount currently remains float 20.0 in the hook output; no real server type validation was exercised.
- These probes do not verify customer consent, bank persistence, remote concurrency, or spoken prompt compliance.

## Saved evidence

- [Machine-readable report](compiler-readiness.json)
- [Pytest stdout](stdout.log)
- [Pytest stderr](stderr.log)
- [JUnit XML](junit.xml)

These portable copies match the original cache files byte for byte. Their SHA-256 values and original cache paths are recorded in the JSON report. Capture times, source fingerprints, test counts and the executed command are unchanged.
