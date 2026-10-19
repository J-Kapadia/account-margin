"""The packaging files: the pinned interpreter, the archive substitution that keeps the commit hash,
the release version and a README that names every command."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
import tomllib

import pytest

from conftest import ROOT, margin


def test_the_packaging_files_have_their_exact_contents():
    assert (ROOT / ".python-version").read_text() == "3.12\n"
    assert (ROOT / ".gitattributes").read_text() == ".gitsha export-subst\n"
    # A checkout holds the marker; an extraction of `git archive` holds the substituted hash.
    assert re.fullmatch(r"\$Format:%H\$\n|[0-9a-f]{40}\n", (ROOT / ".gitsha").read_text())


def test_the_project_version_is_the_release_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == "0.1.0"


@pytest.fixture
def extraction(tmp_path):
    """`git archive HEAD` unpacked where there is no `.git`."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("git or the repository is not available")
    archive = tmp_path / "head.tar"
    subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "-o", str(archive), "HEAD"], check=True)
    out = tmp_path / "extract"
    out.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    return out


def head() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_marker_is_committed_unsubstituted():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("git or the repository is not available")
    blob = subprocess.run(
        ["git", "-C", str(ROOT), "show", "HEAD:.gitsha"], capture_output=True, text=True, check=True
    ).stdout
    assert blob == "$Format:%H$\n"


def test_an_archive_extraction_holds_the_commit_hash(extraction):
    sha = (extraction / ".gitsha").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert sha == head()


def test_git_sha_reads_the_extracted_file_when_there_is_no_repository(extraction, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}  # MARGIN_GIT_SHA unset
    code = "import sys; sys.path.insert(0, sys.argv[1]); from margin import prov; print(prov.git_sha())"
    run = subprocess.run(
        [sys.executable, "-c", code, str(extraction / "src")], capture_output=True, text=True, env=env, check=True
    )
    assert run.stdout.strip() == head()


def test_git_sha_ignores_an_unsubstituted_marker(tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(ROOT / "src", copy / "src")
    (copy / ".gitsha").write_text("$Format:%H$\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    code = "import sys; sys.path.insert(0, sys.argv[1]); from margin import prov; print(prov.git_sha())"
    run = subprocess.run(
        [sys.executable, "-c", code, str(copy / "src")], capture_output=True, text=True, env=env, check=True
    )
    assert run.stdout.strip() == "unknown"


def test_the_readme_has_no_planned_marker_and_names_every_command():
    readme = (ROOT / "README.md").read_text()
    assert "planned" not in readme.lower()
    help_text = margin("--help").stdout
    commands = re.findall(r"^  ([\w-]+)\s", help_text.split("Commands:")[1], flags=re.M)
    assert commands
    for name in commands:
        assert f"`margin {name}`" in readme, name
