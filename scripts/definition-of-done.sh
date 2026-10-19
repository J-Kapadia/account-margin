#!/usr/bin/env bash
# Definition of Done: the commands that must pass before a unit of work counts as done.
# Meant to run on a clean `git archive HEAD` extraction (committed state only), not the working tree.
#
#   scripts/definition-of-done.sh [--fast]
#
# --fast skips the tests marked `slow` (those that need the full-size books); the exit commands still run.
# Exit 2 means the toolchain could not be bootstrapped (no uv, and pip could not install it).
set -euo pipefail
cd "$(dirname "$0")/.."

fast=0
case "${1:-}" in
  "") ;;
  --fast) fast=1 ;;
  *) echo "usage: definition-of-done.sh [--fast]" >&2; exit 2 ;;
esac

bash -n scripts/*.sh

# Hygiene guard: generated data and build output must stay out of git.
# `git check-ignore` when .git exists; a clean `git archive` extraction has none,
# so check .gitignore for the exact line instead.
for d in warehouse data/books data/truth site dbt/target dbt/logs .venv; do
  if [ -e .git ]; then
    git check-ignore -q "$d/x" || { echo "definition-of-done: $d/ is not ignored" >&2; exit 1; }
  else
    grep -Fxq "$d/" .gitignore || { echo "definition-of-done: .gitignore lacks the line $d/" >&2; exit 1; }
  fi
done
scripts/scrub-check.sh --secrets-only

echo "definition-of-done: hygiene and planning checks passed"

# Bootstrap: this runs on a bare `git archive` extraction where nothing is installed.
UV_PIN="0.13.0"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  pip install --user "uv==$UV_PIN" >/dev/null 2>&1 || python3 -m pip install --user "uv==$UV_PIN" >/dev/null 2>&1 || true
  hash -r
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "definition-of-done: uv is not installed and 'pip install --user uv==$UV_PIN' failed" >&2
  exit 2
fi
uv sync --frozen

# Fixture closes run on the fixture config and its committed sign-off; the real config/ is unsigned.
# The books are generated and gitignored, so they are absent on a clean extraction;
# --fixture small selects data/books/small and warehouse/small.duckdb.
# The fast tier runs the same five steps on the committed `mini` books instead, in a scratch warehouse
# of its own: `gen --verify` regenerates the books and compares them with the manifest, writing nothing
# under tests/fixtures.
export MARGIN_SIGNOFF_DIR=tests/fixtures/signoff
fixture_config=tests/fixtures/config
out=$(mktemp -d)
work=$(mktemp -d)
trap 'rm -rf "$out" "$work"' EXIT
if [ "$fast" -eq 1 ]; then
  uv run margin gen --fixture mini --verify
  uv run margin close --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$work/fast.duckdb" --json
  uv run margin explain cp/2026-03/acct_0001/revenue --fixture mini --warehouse "$work/fast.duckdb"
  # The memo carries the scenario's numbers and refuses a month without them.
  uv run margin scenario --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$work/fast.duckdb"
  uv run margin memo --month 2026-03 --fixture mini --warehouse "$work/fast.duckdb" --out "$out"
  grep -rq data-num-id "$out"
else
  uv run margin gen --fixture small
  uv run margin close --month 2026-03 --fixture small --config "$fixture_config" --json
  uv run margin explain cp/2026-03/acct_0001/revenue --fixture small
  # The memo carries the scenario's numbers and refuses a month without them.
  uv run margin scenario --month 2026-03 --fixture small --config "$fixture_config"
  uv run margin memo --month 2026-03 --fixture small --out "$out"
  grep -rq data-num-id "$out"
fi

# The committed `mini` books close on the signed fixture config; without a sign-off the gate exits 4.
uv run margin close --month 2026-04 --fixture mini --config "$fixture_config" --warehouse "$work/mini.duckdb" --json
# dbt's own tests over the closed warehouse (the model defaults in dbt_project.yml match the fixture config).
MARGIN_WAREHOUSE_PATH="$work/mini.duckdb" uv run dbt test --project-dir dbt --profiles-dir dbt \
  --no-use-colors --quiet
# --check against the answer key: on the months just closed it re-runs the comparison and changes no state.
uv run margin close --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$work/mini.duckdb" --check --json
# The newest month too: --check includes the provenance assertion (CTL-PROV), and explain re-derives digests.
uv run margin close --month 2026-04 --fixture mini --config "$fixture_config" --warehouse "$work/mini.duckdb" --check --json
uv run margin explain rollup/2026-04/revenue --fixture mini --warehouse "$work/mini.duckdb" --json
uv run margin explain cfg/policy --fixture mini --warehouse "$work/mini.duckdb"
# doctor over the closed warehouse: healthy, exit 0 (the books, the sign-off and the lock are all in order).
uv run margin doctor --fixture mini --config "$fixture_config" --warehouse "$work/mini.duckdb"
# The token-price scenario: proved against its hand-worked reference in its own temporary warehouse,
# then run on the closed March of the `mini` warehouse, with the first record of its summary explained.
MARGIN_SIGNOFF_DIR=tests/fixtures/signoff uv run margin scenario --check
uv run margin scenario --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$work/mini.duckdb" --json
uv run margin explain scenario/2026-03/summary/computed_change --fixture mini --warehouse "$work/mini.duckdb"
# The memo for March, traced and crawled, then the agreement check over the built site; the scenario above
# comes first because the memo carries its numbers. April has no scenario run, so its memo exits 4.
uv run margin memo --month 2026-03 --fixture mini --warehouse "$work/mini.duckdb" --out "$work/site" --check --crawl --json
uv run margin agreement --month 2026-03 --fixture mini --warehouse "$work/mini.duckdb" --site "$work/site" --check --write --json
test -f "$work/site/2026-03/agreement.json"
grep -q 'values 2026-03' "$work/site/llms.txt"
rc=0
uv run margin memo --month 2026-04 --fixture mini --warehouse "$work/mini.duckdb" --out "$work/site-april" || rc=$?
if [ "$rc" -ne 4 ]; then echo "definition-of-done: a memo for a month without a scenario exited $rc, not 4" >&2; exit 1; fi
rc=0
MARGIN_SIGNOFF_DIR="$work" uv run margin close --month 2026-03 --fixture mini --config "$fixture_config" \
  --warehouse "$work/unsigned.duckdb" || rc=$?
if [ "$rc" -ne 4 ]; then echo "definition-of-done: close without a sign-off exited $rc, not 4" >&2; exit 1; fi

# An injected error is caught by the control built for it: exit 3 naming CTL-SIGN. The full set
# of five runs in tests/test_injected_errors.py.
uv run margin inject --fixture mini --error sign_flip --out "$work/injected/books"
rc=0
uv run margin close --month 2026-03 --fixture mini \
  --books "$work/injected/books" --config "$fixture_config" --warehouse "$work/injected.duckdb" --json \
  > "$work/injected.json" || rc=$?
if [ "$rc" -ne 3 ] || ! grep -q '"id": "CTL-SIGN"' "$work/injected.json"; then
  echo "definition-of-done: the injected sign flip did not fail CTL-SIGN with exit 3 (exit $rc)" >&2
  exit 1
fi

# Roll forward on a fresh `mini` warehouse: close March, lock it and load April, close and check April,
# list the months, and re-close April with --recompute (it stores the same hash and the old one as restated_from).
roll="$work/roll.duckdb"
uv run margin close --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$roll" --json
uv run margin rollforward --month 2026-04 --fixture mini --config "$fixture_config" --warehouse "$roll" --json
uv run margin close --month 2026-04 --fixture mini --config "$fixture_config" --warehouse "$roll" --check --json
uv run margin status --fixture mini --warehouse "$roll"
uv run margin close --month 2026-04 --fixture mini --config "$fixture_config" --warehouse "$roll" --recompute --json

# Build versus buy on a fresh `mini` warehouse: the reference directory is the committed reference data
# with the fixture's build-cost totals and vendor prices laid over it, so the figures do not depend on
# what the author has measured or sourced. Close, explain a bvb figure, run the scenario the memo needs,
# build the memo and look for the build-cost figure in the page.
mkdir "$work/reference"
cp data/reference/*.csv "$work/reference/"
cp tests/fixtures/reference/*.csv "$work/reference/"
MARGIN_REFERENCE_DIR="$work/reference" uv run margin close --month 2026-03 --fixture mini --config "$fixture_config" \
  --warehouse "$work/bvb.duckdb" --json
uv run margin explain bvb/maintenance_allowance --fixture mini --warehouse "$work/bvb.duckdb"
uv run margin scenario --month 2026-03 --fixture mini --config "$fixture_config" --warehouse "$work/bvb.duckdb" --json
uv run margin memo --month 2026-03 --fixture mini --warehouse "$work/bvb.duckdb" --out "$work/bvb-site" --json
grep -q 'bvb/build_cost' "$work/bvb-site/2026-03/index.html"

# The lock file must agree with pyproject.toml.
uv lock --check

if [ "$fast" -eq 1 ]; then
  uv run pytest -q -m "not slow"
else
  uv run pytest -q
fi
echo "definition-of-done: passed"
