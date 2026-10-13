#!/usr/bin/env bash
# Hygiene check for secrets in a directory tree.
#
#   scripts/scrub-check.sh --secrets-only [DIR]
#
# Scans DIR (default: the repository root) for the patterns in
# scripts/secret-patterns.txt, and fails if any file sits under docs/research/.
# Works on a `git archive` extraction (no .git, no history, no denylist). With a
# .git present it scans tracked and untracked-but-not-ignored files only.
#
# Exit codes: 0 clean; 1 finding (file and line only, never the matched text);
# 2 usage error or missing/empty patterns file (fail closed).
#
# The full and --release modes (denylist, build-log checks) are Phase 1 and are
# not implemented: they exit 2 so a caller never mistakes them for a pass.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
patterns_src="$here/secret-patterns.txt"

usage() { echo "usage: scrub-check.sh --secrets-only [DIR]" >&2; }

if [ "${1:-}" != "--secrets-only" ]; then
  usage
  echo "scrub-check: only --secrets-only is available yet; failing closed" >&2
  exit 2
fi
shift
if [ "$#" -gt 1 ]; then usage; exit 2; fi
dir=${1:-$here/..}
if [ ! -d "$dir" ]; then
  echo "scrub-check: not a directory: $dir" >&2
  exit 2
fi
cd "$dir"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if [ ! -f "$patterns_src" ]; then
  echo "scrub-check: scripts/secret-patterns.txt missing: failing closed" >&2
  exit 2
fi
# An empty pattern would match every line, so comments and blanks must not reach grep.
grep -Ev '^[[:space:]]*(#|$)' "$patterns_src" > "$tmp/patterns" || true
if [ ! -s "$tmp/patterns" ]; then
  echo "scrub-check: scripts/secret-patterns.txt has no patterns: failing closed" >&2
  exit 2
fi

# File list, NUL separated, relative to the scanned directory.
if [ -e .git ]; then
  git ls-files -z --cached --others --exclude-standard > "$tmp/files"
else
  find . -name .git -prune -o -type f -print0 | sed -z 's|^\./||' > "$tmp/files"
fi

status=0
while IFS= read -r -d '' f; do
  if [ -L "$f" ] || [ ! -f "$f" ]; then continue; fi
  case "$f" in
    docs/research/*)
      echo "docs/research: research notes belong outside the repository: $f"
      status=1
      ;;
  esac
  # -n prefixes each match with its line number; cut keeps only that number, so the
  # matched text and the rest of the line never reach the output.
  while IFS= read -r n; do
    echo "secret pattern match: $f:$n"
    status=1
  done < <(grep -nIE -f "$tmp/patterns" -- "$f" 2>/dev/null | cut -d: -f1 || true)
done < "$tmp/files"

if [ "$status" -eq 0 ]; then
  echo "scrub-check: --secrets-only clean"
fi
exit "$status"
