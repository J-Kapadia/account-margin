#!/usr/bin/env bash
# Hygiene check for secrets, private terms and private paths.
#
#   scripts/scrub-check.sh [DIR]                full: tracked files and commit messages
#   scripts/scrub-check.sh --secrets-only [DIR] secret patterns in a directory tree
#
# Full mode (default) needs a git repository and the denylist: MARGIN_DENYLIST_PATH, default
# $HOME/.config/account-margin/denylist, one case-insensitive literal term per line (blank
# lines and lines starting with '#' ignored). It scans every tracked file and every commit
# message for denylist terms and for the patterns in scripts/secret-patterns.txt, and fails
# if any path in .gitignore's Private block is tracked.
#
# --secrets-only scans DIR (default: the repository root) for the secret patterns only. It
# works on a `git archive` extraction (no .git, no history, no denylist); with a .git present
# it scans tracked and untracked-but-not-ignored files only.
#
# Both modes fail if any file sits under docs/research/.
#
# Exit codes: 0 clean; 1 finding (file and line, or commit, never the matched text);
# 2 usage error, or a missing/empty denylist or patterns file (fail closed).
#
# The --release mode (build-log checks) is not implemented: it exits 2 so a caller never
# mistakes it for a pass.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
patterns_src="$here/secret-patterns.txt"

usage() { echo "usage: scrub-check.sh [--secrets-only] [DIR]" >&2; }

mode=full
case "${1:-}" in
  --secrets-only) mode=secrets; shift ;;
  --release)
    usage
    echo "scrub-check: --release is not available yet; failing closed" >&2
    exit 2
    ;;
  -*) usage; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then usage; exit 2; fi
dir=${1:-$here/..}
if [ ! -d "$dir" ]; then
  echo "scrub-check: not a directory: $dir" >&2
  exit 2
fi
cd "$dir"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Terms only: comments and blank lines must not reach grep, since an empty pattern matches
# every line.
terms_only() { grep -Ev '^[[:space:]]*(#|$)' "$1" || true; }

if [ ! -f "$patterns_src" ]; then
  echo "scrub-check: scripts/secret-patterns.txt missing: failing closed" >&2
  exit 2
fi
terms_only "$patterns_src" > "$tmp/patterns"
if [ ! -s "$tmp/patterns" ]; then
  echo "scrub-check: scripts/secret-patterns.txt has no patterns: failing closed" >&2
  exit 2
fi

if [ "$mode" = full ]; then
  deny=${MARGIN_DENYLIST_PATH:-$HOME/.config/account-margin/denylist}
  if [ -f "$deny" ]; then terms_only "$deny" > "$tmp/terms"; else : > "$tmp/terms"; fi
  if [ ! -s "$tmp/terms" ]; then
    echo "scrub-check: denylist missing: failing closed" >&2
    exit 2
  fi
  if [ ! -e .git ]; then
    echo "scrub-check: full mode needs a git repository (use --secrets-only on an extraction)" >&2
    exit 2
  fi
  git ls-files -z --cached > "$tmp/files"
elif [ -e .git ]; then
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
  if [ "$mode" = full ]; then
    while IFS= read -r n; do
      echo "denylist match: $f:$n"
      status=1
    done < <(grep -nIiF -f "$tmp/terms" -- "$f" 2>/dev/null | cut -d: -f1 || true)
  fi
done < "$tmp/files"

if [ "$mode" = full ]; then
  # Commit messages, every ref.
  while IFS= read -r c; do
    git show -s --format=%B "$c" > "$tmp/msg"
    if grep -qiF -f "$tmp/terms" "$tmp/msg"; then
      echo "denylist match: commit message $c"
      status=1
    fi
    if grep -qE -f "$tmp/patterns" "$tmp/msg"; then
      echo "secret pattern match: commit message $c"
      status=1
    fi
  done < <(git rev-list --all)

  # Private block of .gitignore: from the "# Private" comment to the next blank line.
  if [ ! -f .gitignore ] || ! grep -q '^# Private' .gitignore; then
    echo "scrub-check: .gitignore has no Private block: failing closed" >&2
    exit 2
  fi
  while IFS= read -r p; do
    p=${p#/}
    p=${p%/}
    [ -n "$p" ] || continue
    if [ -n "$(git ls-files -- "$p")" ]; then
      echo "private path is tracked: $p"
      status=1
    fi
  done < <(awk '/^# Private/ {on=1; next} on && /^[[:space:]]*$/ {exit} on && !/^[[:space:]]*#/ {print}' .gitignore)
fi

if [ "$status" -eq 0 ]; then
  if [ "$mode" = full ]; then echo "scrub-check: full clean"; else echo "scrub-check: --secrets-only clean"; fi
fi
exit "$status"
