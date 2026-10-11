# Extra live cases

Thirteen extra scenarios for the official scorer `tools/run_cases.py`. They test customer behaviour that the 13 public
practice cases do not cover. They run on Freya (simulation plus the judge model) and spend credits.

| File | Brief | Cases |
|---|---|---|
| `brief_a_extra.json` | A | 10: dispute, wrong number, declined identity questions, request for a human, hardship accepting the support team, customer's own amount and date, "next Friday", rude customer who then pays, "is this a scam?", customer has died |
| `brief_b_extra.json` | B | 3: dispute on one product with promises on the others, one product already paid plus a restructuring request, "next Friday" for one product and a refusal for the other |

Run the A file against an agent built from Brief A, and the B file against an agent built from Briefs A and B.

## Why they reuse the PUB ids

The practice bank serves only the customers PUB-01 to PUB-13. `run_cases.py` opens a bank session per case id, and the
bank creates that id's customer and accounts. So every extra case keeps a public id and exactly the same `variables`,
`products` and `identity` as the public case with that id. Only `title`, `family`, `script` (what the customer does),
`expected` (the ledger the scorer compares) and `checks` (what the judge reads for) are new.

## Where the expectations come from

Each expectation was derived from the public briefs' rules, not from observed agent behaviour:

| Case | Rule |
|---|---|
| A PUB-01 dispute | Brief A rule 40: do not argue, no promise, `no_promise` / `dispute` |
| A PUB-02 wrong number | Brief A rule 8: `not_reached` / `wrong_number` |
| A PUB-03 declined identity questions | Brief A rule 15: explain once, then `not_verified` / `declined` |
| A PUB-04 request for a human | Brief A rule 42: ask once, then transfer to `general` |
| A PUB-05 hardship | Brief A rule 32: one sentence of sympathy, no persuasion, offer the support team |
| A PUB-06 own amount and date | Brief A rule 27: accept an amount from the floor up to the full amount |
| A PUB-07, PUB-08 weekdays | Brief A rule 22: "next Friday" and "this Friday" |
| A PUB-09 scam question | Brief A rule 43: point to the bank's customer service number, then continue |
| A PUB-10 customer has died | Brief A rule 9: the fixed condolence sentence, nothing else, `not_reached` / `deceased` |
| All A cases | Brief A rule 3: what counts as confirming the customer's name |
| B PUB-11 to PUB-13 | Brief B rules 4 (one product at a time), 8 (a note for each product without a promise), 9 (a dispute or "already paid" on one product does not end the call), 10 (a restructuring request transfers; no notes on any transfer) |

## How to run

`run_cases.py` reads `FREYA_API_KEY` and `FREYA_BANK_TEAM_KEY` from the environment; it does not read `.env`. The
`.venv` has python-dotenv, which loads `.env` for one command. In Git Bash, from the repository root:

```sh
.venv/Scripts/python.exe -m dotenv -f .env run -- "$PWD/.venv/Scripts/python.exe" tools/run_cases.py <agent_id> --cases evals/extra_cases/brief_a_extra.json --out results/extra_a.json
.venv/Scripts/python.exe -m dotenv -f .env run -- "$PWD/.venv/Scripts/python.exe" tools/run_cases.py <agent_ab_id> --cases evals/extra_cases/brief_b_extra.json --out results/extra_b.json
```

Add `--reps 3` for the strict score (a case counts only if every repetition passes). Only one simulation runs at a time
per workspace; the scorer waits if another is running. `<agent_id>` is in the builder's final JSON line.

These cases are for the public briefs only. Never run them against a hidden-brief agent: the practice bank serves only
the public customers, and the hidden briefs come with their own tool API.

## Results so far

On 11 October the first attempt against the final-code agents did not complete: Freya staging returned HTTP 504 during
the scoring runs (see `results/acceptance2/`). No complete score of these cases exists yet.
