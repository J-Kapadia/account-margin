"""Scrub check: fails closed without a denylist, names file and line but never the term.

Every case runs scripts/scrub-check.sh against a throwaway git repository and a synthetic
denylist; the real denylist is never read.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPT = ROOT / "scripts" / "scrub-check.sh"
TERM = "zebrafish-quux"
PRIVATE_BLOCK = "# Private: not in this repository\n/docs/research/\n/notes/\n/private-notes.md\n"


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout


def commit(repo: Path, message: str = "chore: add files") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def run(repo: Path, *args: str, denylist: Path | None = None, home: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MARGIN_")}
    env["HOME"] = str(home or repo.parent / "home")
    if denylist is not None:
        env["MARGIN_DENYLIST_PATH"] = str(denylist)
    proc = subprocess.run(["bash", str(SCRIPT), *args, str(repo)], env=env,
                          capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / ".gitignore").write_text("*.log\n\n" + PRIVATE_BLOCK)
    (r / "README.md").write_text("a clean file\n")
    commit(r)
    return r


@pytest.fixture
def denylist(tmp_path) -> Path:
    p = tmp_path / "deny.txt"
    p.write_text(f"# synthetic\n\n{TERM}\n")
    return p


def test_clean_tree_passes(repo, denylist):
    code, out = run(repo, denylist=denylist)
    assert code == 0, out


def test_denylist_unset_fails_closed(repo):
    code, out = run(repo)
    assert code == 2
    assert "denylist missing: failing closed" in out


def test_denylist_missing_file_fails_closed(repo, tmp_path):
    code, out = run(repo, denylist=tmp_path / "nonexistent")
    assert code == 2
    assert "denylist missing: failing closed" in out


def test_denylist_without_terms_fails_closed(repo, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n\n")
    code, out = run(repo, denylist=empty)
    assert code == 2
    assert "denylist missing: failing closed" in out


def test_default_denylist_path_is_under_home(repo, tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "account-margin").mkdir(parents=True)
    (home / ".config" / "account-margin" / "denylist").write_text(TERM + "\n")
    (repo / "scratch.txt").write_text("one\n" + TERM.upper() + "\n")
    commit(repo)
    code, out = run(repo, home=home)
    assert code == 1
    assert "scratch.txt:2" in out


def test_term_in_file_names_file_and_line_not_term(repo, denylist):
    (repo / "scratch.txt").write_text(f"one\ntwo {TERM} three\n")
    commit(repo)
    code, out = run(repo, denylist=denylist)
    assert code == 1
    assert "scratch.txt:2" in out
    assert TERM not in out and TERM.upper() not in out


def test_term_in_commit_message_names_commit_not_term(repo, denylist):
    (repo / "other.txt").write_text("fine\n")
    commit(repo, f"chore: mention {TERM}")
    sha = git(repo, "rev-parse", "HEAD").strip()
    code, out = run(repo, denylist=denylist)
    assert code == 1
    assert sha in out
    assert TERM not in out


def test_secret_pattern_names_file_and_line_not_secret(repo, denylist):
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    (repo / "conf.txt").write_text(f"x\ny={secret}\n")
    commit(repo)
    code, out = run(repo, denylist=denylist)
    assert code == 1
    assert "conf.txt:2" in out
    assert secret not in out


def test_tracked_research_file_fails_in_full_and_secrets_only(repo, denylist):
    (repo / "docs" / "research").mkdir(parents=True)
    (repo / "docs" / "research" / "n.md").write_text("notes\n")
    git(repo, "add", "-f", "docs/research/n.md")
    git(repo, "commit", "-q", "-m", "docs: add notes")
    code, out = run(repo, denylist=denylist)
    assert code == 1
    assert "docs/research/n.md" in out
    code, out = run(repo, "--secrets-only")
    assert code == 1
    assert "docs/research/n.md" in out


def test_tracked_private_block_path_fails(repo, denylist):
    (repo / "notes").mkdir()
    (repo / "notes" / "x.md").write_text("private\n")
    (repo / "private-notes.md").write_text("private\n")
    git(repo, "add", "-f", "notes/x.md", "private-notes.md")
    git(repo, "commit", "-q", "-m", "docs: add notes")
    code, out = run(repo, denylist=denylist)
    assert code == 1
    assert "private path is tracked: notes" in out
    assert "private path is tracked: private-notes.md" in out
    assert "docs/research" not in out


def test_ignored_private_path_untracked_passes(repo, denylist):
    (repo / "notes").mkdir()
    (repo / "notes" / "x.md").write_text("private\n")
    code, out = run(repo, denylist=denylist)
    assert code == 0, out


def test_missing_private_block_fails_closed(repo, denylist):
    (repo / ".gitignore").write_text("*.log\n")
    commit(repo)
    code, out = run(repo, denylist=denylist)
    assert code == 2
    assert "no Private block" in out


def test_full_mode_needs_git(tmp_path, denylist):
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out = run(plain, denylist=denylist)
    assert code == 2


def test_release_mode_is_not_a_pass(repo, denylist):
    code, _ = run(repo, "--release", denylist=denylist)
    assert code == 2
