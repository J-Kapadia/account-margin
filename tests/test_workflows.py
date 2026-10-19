"""The Pages workflow and the other workflows are accepted by parsing them: a deployment cannot be run here."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

POST_DEPLOY = (
    'curl -fsS "$SITE_URL/llms.txt" | grep -q fct_contribution_profit',
    'curl -fsS "$SITE_URL/2026-03/agreement.json" | python3 -c '
    "'import json,sys; from decimal import Decimal; "
    "sys.exit(Decimal(json.load(sys.stdin)[\"disagreement_rate\"]) != 0)'",
)


def _paths():
    return sorted(WORKFLOWS.glob("*.yml"))


def _load(path):
    return yaml.safe_load(path.read_text())


def _triggers(workflow):
    # YAML 1.1 reads the bare key `on` as the boolean True.
    on = workflow[True] if True in workflow else workflow["on"]
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {name: None for name in on}
    return on


def _runs(job):
    return "\n".join(step["run"] for step in job["steps"] if "run" in step)


@pytest.fixture(scope="module")
def pages():
    return _load(WORKFLOWS / "pages.yml")


def test_workflows_exist_and_parse():
    names = {p.name for p in _paths()}
    assert {"ci.yml", "pages.yml"} <= names
    for path in _paths():
        workflow = _load(path)
        assert isinstance(workflow, dict) and workflow["jobs"], path.name


def test_pages_permissions_are_exactly_the_deploy_set(pages):
    assert pages["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}


def test_pages_triggers_on_main_tags_and_dispatch(pages):
    triggers = _triggers(pages)
    assert "main" in triggers["push"]["branches"]
    assert "v*" in triggers["push"]["tags"]
    assert "workflow_dispatch" in triggers


def test_pages_serialises_deploys_without_cancelling(pages):
    assert pages["concurrency"] == {"group": "pages", "cancel-in-progress": False}


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.name)
def test_no_workflow_runs_untrusted_code_with_secrets(path):
    assert "pull_request_target" not in _triggers(_load(path))
    secrets = re.findall(r"secrets\.\w+", path.read_text())
    assert [s for s in secrets if s != "secrets.GITHUB_TOKEN"] == []


@pytest.mark.parametrize("path", _paths(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_major_version(path):
    uses = [
        step["uses"]
        for job in _load(path)["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", ref), ref


def test_deploy_only_runs_in_the_public_repository(pages):
    deploy = pages["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert "github.event.repository.private == false" in deploy["if"]
    assert deploy["environment"]["name"] == "github-pages"
    assert any(s.get("id") == "deployment" and s["uses"].startswith("actions/deploy-pages@") for s in deploy["steps"])


def test_post_deploy_check_runs_both_commands(pages):
    steps = pages["jobs"]["deploy"]["steps"]
    check = next(s for s in steps if "run" in s)
    assert check["env"]["SITE_URL"] == "${{ steps.deployment.outputs.page_url }}"
    lines = check["run"].splitlines()
    assert lines[0] == 'SITE_URL="${SITE_URL%/}"'
    for command in POST_DEPLOY:
        assert command in check["run"]


def test_build_job_builds_the_mini_site_and_uploads_it(pages):
    build = pages["jobs"]["build"]
    assert build["env"]["MARGIN_SIGNOFF_DIR"] == "tests/fixtures/signoff"
    runs = _runs(build)
    assert "--month 2026-03" in runs
    assert "--fixture mini" in runs
    for command in ("memo", "agreement"):
        assert re.search(rf"margin {command} .*--month 2026-0[34]", runs)
    assert re.search(r"margin memo .*--check --crawl", runs)
    assert re.search(r"margin agreement .*--check --write", runs)
    upload = [s for s in build["steps"] if s.get("uses", "").startswith("actions/upload-pages-artifact@")]
    assert [s["with"]["path"] for s in upload] == ["site"]


def test_build_job_orders_scenario_before_memo_before_agreement(pages):
    runs = _runs(pages["jobs"]["build"]).splitlines()
    order = [
        (re.search(r"margin (\w+) ", line).group(1), re.search(r"--month (\S+)", line).group(1))
        for line in runs
        if re.search(r"margin (scenario|memo|agreement) ", line)
    ]
    assert order == [
        ("scenario", "2026-03"), ("memo", "2026-03"), ("agreement", "2026-03"),
        ("scenario", "2026-04"), ("memo", "2026-04"), ("agreement", "2026-04"),
    ]


def test_every_build_step_is_timed(pages):
    for step in pages["jobs"]["build"]["steps"]:
        if "run" in step:
            commands = [l for l in step["run"].splitlines() if not l.startswith("W=")]
            assert all(l.startswith("time ") for l in commands), step["run"]
