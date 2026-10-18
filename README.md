# account-margin

Contribution profit by customer account, rolled forward monthly, with controls.

One command a month turns a usage-priced AI company's books into contribution profit by customer account, tied to the P&L to the cent, with a memo that makes the judgement calls. Open the published memo for any closed month, click any number, and land on a chain that ends at hashed source rows.

Everything is synthetic. The books belong to "Acme AI", a generated usage-priced AI API business (about 300 accounts, 12 months), written by a seeded generator. The only real numbers will be cited, published list prices; the file that holds them (`data/reference/model_prices.csv`) lists each model's vendor price page and the date it was read.

## Status

Work in progress. Built and tested now: the seeded generator and answer key; the close (dbt models, allocation of shared cloud cost under three rules, firm and soft margin flags with reason codes); the tie-out, allocation, price-pin and sign-off controls; `close --check` against the answer key; a provenance record for every number with `explain`; and the injected-error tests. Roll-forward and month locking (`rollforward`, `close --recompute` and `status`) are built too. The token-price scenario is built too (`scenario --check` and `scenario --month`, described below). Still to come: the full memo site, `agreement` and `doctor`.

The production allocation and policy configs are not signed yet, so run it today on the committed `mini` fixture (12 accounts, February to April 2026), which carries its own signed configs:

```bash
uv sync --frozen
export MARGIN_SIGNOFF_DIR=tests/fixtures/signoff
uv run margin close --fixture mini --config tests/fixtures/config --month 2026-04 --check
uv run margin close --fixture mini --config tests/fixtures/config --month 2026-03 --warehouse warehouse/roll.duckdb
uv run margin rollforward --fixture mini --config tests/fixtures/config --month 2026-04 --warehouse warehouse/roll.duckdb
uv run margin close --fixture mini --config tests/fixtures/config --month 2026-04 --check --warehouse warehouse/roll.duckdb
uv run margin status --fixture mini --warehouse warehouse/roll.duckdb
uv run margin explain cp/2026-03/acct_0001/contribution_profit --fixture mini
uv run margin scenario --check
uv run margin scenario --month 2026-03 --fixture mini --config tests/fixtures/config --warehouse warehouse/roll.duckdb
uv run margin explain scenario/2026-03/summary/computed_change --fixture mini --warehouse warehouse/roll.duckdb
uv run pytest -q tests/test_injected_errors.py
```

The first close runs every earlier month first, passes every control and matches the answer key; the roll-forward sequence on its own warehouse closes March, locks February and March while loading April, closes and checks April, and `status` lists February and March as `locked` and April as `closed` with their result hashes (`close --recompute` re-closes a `closed` month whose model code was fixed, storing the new hash and the old one as `restated_from`; on a `locked` month it exits 4); `explain` walks a number down to the invoice, usage and cost rows it came from; the injected-error tests show each planted error failing the control that owns it.

## The token-price scenario

The scenario asks what happens to the accounts below the margin floor if the cost of inference follows a real, published trend in list prices. It is a what-if over synthetic accounts, not a forecast and not a statement about any company's costs.

- `margin scenario --month M` runs it on a month that is `closed` or `locked` (an `open` month exits 4). For the accounts flagged `firm` or `soft` in that month it scales the month's inference cost by the series' change at the end of each of four quarters, under three cases (`full`, `half` and `flat`: all, half or none of the change reaches the cost), and records each account's contribution profit, margin and recovery quarter, with a summary per case. Every number has a provenance record (`scenario/M/...`) that `margin explain` walks down to the base month's `cp/` records and the price series. It writes `scenario.*` tables and those records only: it adds no month state and never changes a month's result hash, so closing the month before or after it gives the same hash.
- `margin scenario --check` proves the scenario against a small hand-worked reference (`tests/fixtures/scenario`): it generates the reference books into a temporary directory and warehouse, runs the scenario there and exits 3 on any difference from the worked series, computed change, recovery quarters and margin paths. It touches no warehouse of yours.
- The disclaimer travels with the numbers: the series is a blend of model vendors' list prices with fixed weights (an unweighted mean over three tiers of the input and output price, three parts input to one part output), applied to synthetic accounts. It is not Acme's per-model price and not any customer's cost.
- The series file is `data/reference/list_price_series.csv`: one row per tier and month, 2026-03 through 2026-09, each with the vendor's pricing page as captured by the Internet Archive and the day it was read; `data/reference/README.md` gives the method. Set `MARGIN_LIST_PRICE_SERIES` to read another file of the same shape.

## What it does

- Computes contribution profit per account per month: revenue, less inference, cloud and support cost to serve the account.
- Ties account revenue and each cost component to the general ledger to the cent, and fails the close when any difference remains.
- Allocates shared cloud cost under three rules, and flags accounts below a margin floor as *firm* (below under every rule) or *soft* (below under some), each with a reason code.
- Catches injected errors (duplicate invoice, cutoff slip, double-counted shared cost, sign flip, one-cent rounding slip) by naming the control that fails.
- Attaches a provenance record to every number, so `margin explain <number-id>` prints the chain down to the source rows.
- Applies a published list-price trend to the synthetic accounts below the floor, in a token-price scenario that leaves the closed month untouched.
- Rolls a month forward: `rollforward` locks every earlier closed month and loads the next month's books in one transaction, and `status` lists each month's state and result hash.

Planned, not built yet:

- A static memo with agent-readable data (`llms.txt`, the model's SQL and a cached values file per month), and an `agreement` check that the CLI, SQL, memo and cached values return the same answer for every number.

## Requirements

Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

## Full run (planned for v0.1; does not run yet)

This is the target sequence, not what works today. It needs the production sign-offs and the unbuilt commands in the table below. Today `margin gen --seed 20261013 --out data/books` runs and writes the full books, priced from `data/reference/model_prices.csv`. Use the `mini` fixture commands under Status.

```bash
uv sync --frozen
uv run margin gen --seed 20261013 --out data/books
uv run margin close --month 2026-03 --check
uv run margin rollforward --month 2026-04 && uv run margin close --month 2026-04 --check
uv run margin scenario --check
uv run margin scenario --month 2026-03
uv run margin memo --month 2026-03 --check
uv run margin agreement --month 2026-03 --check
```

`close` reads only the books; `--check` also compares the result with the generator's hidden answer key. A non-zero exit says why: 2 usage (a bad argument, a month outside the books, an unknown ID), 3 a control failed, 4 a precondition is missing (for example no sign-off for the allocation rule in force), 5 the result differs from the answer key, 6 provenance is incomplete, 70 internal (another run holds the warehouse lock, or a bug). Every command accepts `--json`.

| Command | Purpose |
|---|---|
| `margin gen` | Generate the books and the answer key from a seed, or verify them (`--verify`) |
| `margin close` | Compute, control and check a month, closing earlier months first; `--recompute` re-closes a closed month |
| `margin rollforward` | Lock every earlier closed month and load the next |
| `margin explain` | Print the provenance chain of a number |
| `margin inject` | Copy the books with one seeded error |
| `margin scenario` | Token-price scenario for a closed month (`--month`), or its proof against a hand-worked reference (`--check`) |
| `margin memo` | (planned) Build the memo and explain pages as a static site |
| `margin agreement` | (planned) Ask every interface for every number and report any disagreement |
| `margin signoff` | Record the allocation rule's sign-off (human, interactive) |
| `margin status` | List the months with their state and result hash |
| `margin doctor` | (planned) Health |

## Tests

```bash
uv run pytest -q
uv run pytest -q tests/test_injected_errors.py
```

## Operator notes

`scripts/profile-close.sh` closes the `mini` fixture twice in a temporary warehouse and prints one table, per close, of the wall time and where it went (the dbt subprocess, dbt's import, parse and run, and the rest); `--json` prints the same as one object.

## Layout

- `src/margin/`: the `margin` package and the memo templates
- `dbt/`: the dbt project (staging, intermediate and mart models)
- `controls/`: one SQL file per control
- `config/`: allocation and policy config (production sign-offs are not committed yet; the `mini` fixture carries its own under `tests/fixtures/signoff`)
- `data/reference/`: reference data the app reads: support rates, the sourced and dated list-price file, and an empty quote registry today
- `tests/`: unit, property, integration and acceptance tests, and the committed `mini` fixture

## Licence

MIT. See `LICENSE`.
