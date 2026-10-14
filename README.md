# account-margin

Contribution profit by customer account, rolled forward monthly, with controls.

One command a month turns a usage-priced AI company's books into contribution profit by customer account, tied to the P&L to the cent, with a memo that makes the judgement calls. Open the published memo for any closed month, click any number, and land on a chain that ends at hashed source rows.

Everything is synthetic. The books belong to "Acme AI", a generated usage-priced AI API business (about 300 accounts, 12 months), written by a seeded generator. The only real numbers are cited, published list prices.

## Status

Work in progress. Built and tested now: the seeded generator and answer key; the close (dbt models, allocation of shared cloud cost under three rules, firm and soft margin flags with reason codes); the tie-out, allocation, price-pin and sign-off controls; `close --check` against the answer key; a provenance record for every number with `explain`; and the injected-error tests. Still to come: roll-forward and month locking, the token-price scenario, the full memo site, `agreement`, `doctor` and `status`.

The production allocation and policy configs are not signed yet, so run it today on the committed `mini` fixture (12 accounts, February to April 2026), which carries its own signed configs:

```bash
uv sync --frozen
export MARGIN_SIGNOFF_DIR=tests/fixtures/signoff
uv run margin close --fixture mini --config tests/fixtures/config --month 2026-04 --check
uv run margin explain cp/2026-03/acct_0001/contribution_profit --fixture mini
uv run pytest -q tests/test_injected_errors.py
```

The close runs every earlier month first, passes every control and matches the answer key; `explain` walks a number down to the invoice, usage and cost rows it came from; the injected-error tests show each planted error failing the control that owns it.

## What it does

- Computes contribution profit per account per month: revenue, less inference, cloud and support cost to serve the account.
- Ties account revenue and each cost component to the general ledger to the cent, and fails the close when any difference remains.
- Allocates shared cloud cost under three rules, and flags accounts below a margin floor as *firm* (below under every rule) or *soft* (below under some), each with a reason code.
- Rolls a month forward: locks the prior month's inputs and results, loads the next month, and closes it.
- Catches injected errors (duplicate invoice, cutoff slip, double-counted shared cost, sign flip, one-cent rounding slip) by naming the control that fails.
- Attaches a provenance record to every number, so `margin explain <number-id>` prints the chain down to the source rows.
- Runs a token-price scenario that applies a published list-price trend to the synthetic accounts.
- Publishes a static memo with agent-readable data (`llms.txt`, the model's SQL and a cached values file per month), and checks that the CLI, SQL, memo and cached values return the same answer for every number.

## Requirements

Python 3.12 and [`uv`](https://docs.astral.sh/uv/).

## Full run (when v0.1 is complete)

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

`close` reads only the books; `--check` also compares the result with the generator's hidden answer key. A non-zero exit says why: 3 a control failed, 4 a precondition is missing (for example no sign-off for the allocation rule in force), 5 the result differs from the answer key, 6 provenance is incomplete. Every command accepts `--json`.

| Command | Purpose |
|---|---|
| `margin gen` | Generate the books and the answer key from a seed, or verify them (`--verify`) |
| `margin close` | Compute, control and check a month, closing earlier months first |
| `margin rollforward` | Lock the prior month and load the next |
| `margin explain` | Print the provenance chain of a number |
| `margin inject` | Copy the books with one seeded error |
| `margin scenario` | Token-price scenario |
| `margin memo` | Build the memo and explain pages as a static site |
| `margin agreement` | Ask every interface for every number and report any disagreement |
| `margin signoff` | Record the allocation rule's sign-off (human, interactive) |
| `margin doctor`, `margin status` | Health and state |

## Tests

```bash
uv run pytest -q
uv run pytest -q tests/test_injected_errors.py
```

## Layout

- `src/margin/`: the `margin` package and the memo templates
- `dbt/`: the dbt project (staging, intermediate and mart models)
- `controls/`: one SQL file per control
- `config/`: allocation and policy config, and the sign-offs bound to them
- `data/reference/`: reference data the app reads (list prices, support rates and the like, each sourced and dated)
- `tests/`: unit, property, integration and acceptance tests, and the committed `mini` fixture

## Licence

MIT. See `LICENSE`.
