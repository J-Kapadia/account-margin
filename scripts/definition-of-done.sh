#!/usr/bin/env bash
# Definition of Done: the commands that must pass before a bead may close.
# NEEDLE runs this on a clean `git archive HEAD` extraction (committed state only).
#
#   scripts/definition-of-done.sh [--fast]
#
# --fast skips the tests marked `slow` (none exist yet); the exit commands still run.
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

# Bootstrap: NEEDLE runs this on a bare `git archive` extraction where nothing is installed.
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

# Phase 0 exit commands. The books are generated and gitignored, so they are absent on a clean
# extraction; --fixture small selects data/books/small and warehouse/small.duckdb.
uv run margin gen --fixture small
uv run margin close --month 2026-03 --fixture small --json
uv run margin explain cp/2026-03/acct_0001/revenue --fixture small
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
uv run margin memo --month 2026-03 --fixture small --out "$out"
grep -rq data-num-id "$out"

if [ "$fast" -eq 1 ]; then
  uv run pytest -q -m "not slow"
else
  uv run pytest -q
fi
echo "definition-of-done: passed"
