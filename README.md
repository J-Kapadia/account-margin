# account-margin

Contribution profit by customer account, rolled forward monthly, with controls.

One command a month turns a usage-priced AI company's books into contribution profit by customer account, tied to the P&L to the cent, with a memo that makes the judgement calls. Open the published memo for any closed month, click any number, and land on a chain that ends at hashed source rows.

Everything is synthetic. The books belong to "Acme AI", a generated usage-priced AI API business (about 300 accounts, 12 months), written by a seeded generator. The only real numbers are cited, published list prices; the file that holds them (`data/reference/model_prices.csv`) lists each model's vendor price page and the date it was read.

## Run it on the fixture

The committed `mini` fixture (12 accounts, February to April 2026) carries its own signed configs, so it runs with no setup beyond `uv sync`:

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
uv run margin memo --month 2026-03 --fixture mini --warehouse warehouse/roll.duckdb --out site --check --crawl
uv run margin agreement --month 2026-03 --fixture mini --warehouse warehouse/roll.duckdb --site site --check --write
uv run margin doctor --fixture mini --config tests/fixtures/config --warehouse warehouse/roll.duckdb
uv run pytest -q tests/test_injected_errors.py
```

The first close runs every earlier month first, passes every control and matches the answer key; the roll-forward sequence on its own warehouse closes March, locks February and March while loading April, closes and checks April, and `status` lists February and March as `locked` and April as `closed` with their result hashes (`close --recompute` re-closes a `closed` month whose model code was fixed, storing the new hash and the old one as `restated_from`; on a `locked` month it exits 4); `explain` walks a number down to the invoice, usage and cost rows it came from; the injected-error tests show each planted error failing the control that owns it; `doctor` exits 0 on the closed warehouse. The memo and agreement commands run after the scenario, as the next sections explain.

## Cloud bill intake

`margin import-focus --in FILE... --out FILE [--json]` maps an AWS Data Exports FOCUS 1.0 CSV export to the `cloud_focus.csv` of a books directory. It reads no warehouse and takes no lock.

- It keeps the pinned FOCUS 1.0 column set, drops every other column of the export and reports their names. Besides the mandatory cost, period, currency and service columns, `cloud_focus.csv` carries six optional ones: `ListCost`, `ContractedCost`, `ChargeCategory`, `CommitmentDiscountCategory`, `CommitmentDiscountType` and `RegionId`; an optional column an export lacks is written empty, a missing mandatory one stops the import and is named.
- Tag keys are lowercased and values kept as written. `line_id` is assigned from the export's file name and the row's position in it.
- Costs are rounded to cents per row, and each month gets one residual `Adjustment` row so that the month's cost totals equal the export's to the cent.
- `--in` is repeatable and accepts a directory of exports. A month present in two files is refused (exit 2): one export file per billing period.
- Nothing is judged: a row the close's controls would fail is written as it is.

On the committed export:

```bash
uv run margin import-focus --in tests/fixtures/focus/export.csv --out "$(mktemp -d)/cloud_focus.csv" --json
```

It prints one envelope, here `"status": "ok"`, `"exit": 0`, `"rows_in": 16`, `"rows_out": 18`, `"residual_rows": 2`, `"columns_dropped"` (`ChargeDescription`, `PricingUnit` and `ProviderName`, count 3) and `"months"` (`2026-02` and `2026-03`).

The output is the books directory's `cloud_focus.csv`, which `close` reads like any other. `data/reference/focus_columns.md` holds the specification pin (FOCUS 1.0, its URL and the day it was read), the sixteen columns with the specification's wording, and how the books use the cost columns.

## The token-price scenario

The scenario asks what happens to the accounts below the margin floor if the cost of inference follows a real, published trend in list prices. It is a what-if over synthetic accounts, not a forecast and not a statement about any company's costs.

- `margin scenario --month M` runs it on a month that is `closed` or `locked` (an `open` month exits 4). For the accounts flagged `firm` or `soft` in that month it scales the month's inference cost by the series' change at the end of each of four quarters, under three cases (`full`, `half` and `flat`: all, half or none of the change reaches the cost), and records each account's contribution profit, margin and recovery quarter, with a summary per case. Every number has a provenance record (`scenario/M/...`) that `margin explain` walks down to the base month's `cp/` records and the price series. It writes `scenario.*` tables and those records only: it adds no month state and never changes a month's result hash, so closing the month before or after it gives the same hash.
- `margin scenario --check` proves the scenario against a small hand-worked reference (`tests/fixtures/scenario`): it generates the reference books into a temporary directory and warehouse, runs the scenario there and exits 3 on any difference from the worked series, computed change, recovery quarters and margin paths. It touches no warehouse of yours.
- The disclaimer travels with the numbers: the series is a blend of model vendors' list prices with fixed weights (an unweighted mean over three tiers of the input and output price, three parts input to one part output), applied to synthetic accounts. It is not Acme's per-model price and not any customer's cost.
- The series file is `data/reference/list_price_series.csv`: one row per tier and month, 2026-03 through 2026-09, each with the vendor's pricing page as captured by the Internet Archive and the day it was read; `data/reference/README.md` gives the method. Set `MARGIN_LIST_PRICE_SERIES` to read another file of the same shape.

## The memo site

`margin memo --month M` builds a static site for a `closed` or `locked` month into `--out` (default `site`). It needs the month's scenario run first (`scenario --month M`), because the memo carries the scenario's numbers: without it the command exits 4.

- The memo is one page per month, `M/index.html`, written from deterministic templates over the provenance-tracked numbers. Every number on it is a link to its explain page (`explain/<number-id>.html`), which prints the same chain `margin explain` does, down to the hashed source rows.
- `values.csv` per month (`M/values.csv`) is the cached value of every number the memo shows, and `llms.txt` at the site root is the agent-readable index: it lists the months with a memo, links each month's values file and names the model's SQL, so an agent can read the answer without parsing HTML.
- `--check` proves the memo traceable after the build: `CHK-MEMO-NUM` (every number on the page resolves through `explain` to source rows whose digests re-derive) and `CHK-MEMO-QUOTE` (the memo quotes only strings registered in `data/reference/quotes.yaml`, which is empty today, so it quotes no outside source). `--crawl` adds `CHK-CRAWL`: it walks the internal links of the output directory offline and fails on a broken one.
- The published site is built from the `mini` fixture and deployed to GitHub Pages by `.github/workflows/pages.yml`: <https://j-kapadia.github.io/account-margin/>.

## The page

`margin serve` serves one page over the pre-closed `mini` books (`MARGIN_SERVE_ROOT`, default `warehouse/serve`). `margin serve --static DIR` renders the same page once into `DIR` and exits without serving: the months with state and hash, a close recorded on a scratch copy of the served warehouse (its lines and the months after it), one panel per planted error with the control its committed capture names, and the memos with their explain pages under `DIR/site/`. Every link is relative and the page has no script, so the directory works at any path.

The founder deploys `DIR` from their own machine to a path of the Cloudflare Pages site. This repository holds no credential and no deploy step, and there is no URL until the founder has deployed it.

## The agreement check

The same number can be read four ways: `margin explain`, the model's SQL over the warehouse, the memo page, and the cached `values.csv`. `margin agreement --month M --site site` asks every number of the month through each of them and reports the rate of agreement. With `--check` any disagreement fails `CHK-AGREEMENT` (exit 3); with `--write` it stores the result in `<site>/M/agreement.json`. It runs after `memo` has built the site, since it reads the memo and `llms.txt` from there.

## Build versus buy

The memo's build-versus-buy section sets what building this tool cost against the list price of products that sell a similar job, and gives a verdict per vendor: *buy* when the vendor's annual list price is below the build cost amortised over 12 months, plus 12 times the monthly run cost, plus a maintenance allowance (10% of the build cost per year); otherwise *build*. Every figure is a `bvb/` number, so `margin explain bvb/maintenance_allowance` walks it down to the source rows.

- `data/reference/build_cost.csv` is the file of build-cost totals (written by the script below; absent until a collection has run), totals only (dollars, tokens, elapsed hours, review minutes, monthly run cost); how each was measured is stated in the memo's source labels (`events`, `git` or `manual`). The per-item ledger behind the totals is not part of the repository. `scripts/collect-build-cost.py --ledger PATH --review PATH --collected-on YYYY-MM-DD [--run-cost-monthly-usd N] [--out PATH]` takes file paths in and writes the totals out.
- `data/reference/vendor_prices.csv` holds each vendor's published annual list price with the pricing page it was read from and the day it was read; `data/reference/README.md` gives the selection rule. A vendor that does not publish a price is not listed and no price is estimated.
- Both files are read from `data/reference/`, or from the directory named by `MARGIN_REFERENCE_DIR`; `tests/fixtures/reference/` holds a small pair for tests and `scripts/definition-of-done.sh` runs the section on it.

## What it does

- Computes contribution profit per account per month: revenue, less inference, cloud and support cost to serve the account.
- Ties account revenue and each cost component to the general ledger to the cent, and fails the close when any difference remains.
- Allocates shared cloud cost under three rules, and flags accounts below a margin floor as *firm* (below under every rule) or *soft* (below under some), each with a reason code.
- Catches injected errors (duplicate invoice, cutoff slip, double-counted shared cost, sign flip, one-cent rounding slip) by naming the control that fails.
- Attaches a provenance record to every number, so `margin explain <number-id>` prints the chain down to the source rows.
- Applies a published list-price trend to the synthetic accounts below the floor, in a token-price scenario that leaves the closed month untouched.
- Rolls a month forward: `rollforward` locks every earlier closed month and loads the next month's books in one transaction, and `status` lists each month's state and result hash.
- Publishes a static memo for the month, with an explain page for every number, an agent-readable `llms.txt` and a cached `values.csv`, and checks that the CLI, SQL, memo and cached values give the same answer for every number (`agreement`).
- Imports a FOCUS 1.0 cloud-bill export as the books' `cloud_focus.csv` (`import-focus`).
- Reports the health of the install and its paths (`doctor`): the Python and locked dependency versions, the books and their manifest, the sign-off, the warehouse and its lock.

## Requirements

Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

## Full run

The full run generates the full-size books from the seed and closes the months from there. It needs the production allocation and policy configs in `config/` signed with `margin signoff` (a human, at a terminal); the fixture run above does not.

```bash
uv sync --frozen
uv run margin gen --seed 20261013 --out data/books
uv run margin close --month 2026-03 --check
uv run margin rollforward --month 2026-04 && uv run margin close --month 2026-04 --check
uv run margin scenario --check
uv run margin scenario --month 2026-03
uv run margin memo --month 2026-03 --check --crawl
uv run margin agreement --month 2026-03 --check --write
uv run margin doctor
```

`close` reads only the books; `--check` also compares the result with the generator's hidden answer key. A non-zero exit says why: 1 `doctor` found warnings only, 2 usage (a bad argument, a month outside the books, an unknown ID), invalid configuration, or a `doctor` failure, 3 a control failed (including `CHK-MEMO-NUM`, `CHK-MEMO-QUOTE` and `CHK-CRAWL` from `memo --check`, and `CHK-AGREEMENT` from `agreement --check`), 4 a precondition is missing (for example no sign-off for the allocation rule in force, or a memo asked for a month whose scenario has not run), 5 the result differs from the answer key, 6 provenance is incomplete (including a digest that does not re-derive, or a value that differs from the cached one, found by `memo --check`), 70 internal (another run holds the warehouse lock, or a bug). Every command accepts `--json`.

| Command | Purpose |
|---|---|
| `margin gen` | Generate the books and the answer key from a seed, or verify them (`--verify`) |
| `margin close` | Compute, control and check a month, closing earlier months first; `--recompute` re-closes a closed month |
| `margin rollforward` | Lock every earlier closed month and load the next |
| `margin import-focus` | Map FOCUS 1.0 cloud-bill CSV exports (files or a directory) to a books directory's `cloud_focus.csv`; `--json` prints the envelope |
| `margin explain` | Print the provenance chain of a number |
| `margin inject` | Copy the books with one seeded error |
| `margin scenario` | Token-price scenario for a closed month (`--month`), or its proof against a hand-worked reference (`--check`) |
| `margin memo` | Build the memo, its explain pages, `llms.txt` and `values.csv` as a static site for a month with a scenario run; `--check` proves it traceable and `--crawl` its links |
| `margin agreement` | Ask every interface for every number of a month and report any disagreement; `--check` fails on one, `--write` stores the result |
| `margin serve` | Serve one page over the pre-closed `mini` books, or render it once into a directory (`--static DIR`) |
| `margin signoff` | Record the allocation rule's sign-off (human, interactive) |
| `margin status` | List the months with their state and result hash |
| `margin doctor` | Health of the install and the paths: exit 0 healthy, 1 warnings, 2 failures |

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
- `data/reference/`: reference data the app reads: support rates, the sourced and dated list-price files, the build-cost totals, vendor prices, and an empty quote registry today
- `tests/`: unit, property, integration and acceptance tests, and the committed `mini` fixture

## Licence

MIT. See `LICENSE`.
