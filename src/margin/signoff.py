"""Sign-off: a committed JSON file binding the SHA-256 of the allocation config
and of the policy config to a named signer role.

The file is named by `combined_sha256 = sha256(rule_sha256 + "\\n" + policy_sha256)`, so the
file in force is the one named by the hash of the current configs and every other file is
history. `write_for` is the pure writer that tests call on temporary copies; the human-only
`margin signoff` command wraps it with a terminal requirement. `verify` is the gate `close` calls
before it reads or writes anything: no matching file, no allocation-dependent step.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .gen_config import ALLOCATION_FILE, POLICY_FILE
from .errors import EXIT_PRECONDITION, EXIT_USAGE, MarginError

SIGNOFF_DIR_ENV = "MARGIN_SIGNOFF_DIR"
GATE_ID = "CTL-SIGNOFF"


def _sha256(path: Path, what: str) -> str:
    if not path.is_file():
        raise MarginError(EXIT_USAGE, f"{what} file not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_hashes(config_dir: Path) -> tuple[str, str]:
    """`(rule_sha256, policy_sha256)` of the allocation and policy files in `config_dir`."""
    return (
        _sha256(config_dir / ALLOCATION_FILE, "allocation"),
        _sha256(config_dir / POLICY_FILE, "policy"),
    )


def combined_sha256(rule_sha256: str, policy_sha256: str) -> str:
    return hashlib.sha256(f"{rule_sha256}\n{policy_sha256}".encode("utf-8")).hexdigest()


def signoff_dir(config_dir: Path) -> Path:
    """`MARGIN_SIGNOFF_DIR`, else `<config dir>/signoff`."""
    return Path(os.environ.get(SIGNOFF_DIR_ENV) or config_dir / "signoff")


def write_for(
    config_dir: Path,
    *,
    signer: str,
    role: str,
    signoff_to: Path | None = None,
    signed_on: str | None = None,
) -> Path:
    """Write `<combined_sha256>.json` for the configs in `config_dir`; return its path.

    The directory defaults to `signoff_dir(config_dir)`. `signed_on` (YYYY-MM-DD) defaults to
    today. Signing the same configs again replaces the file with the same name.
    """
    signer, role = signer.strip(), role.strip()
    if not signer or not role:
        raise MarginError(EXIT_USAGE, "a sign-off needs a signer and a role")
    rule, policy = config_hashes(config_dir)
    record = {
        "rule_sha256": rule,
        "policy_sha256": policy,
        "signer": signer,
        "role": role,
        "signed_on": signed_on or datetime.date.today().isoformat(),
    }
    target_dir = signoff_to or signoff_dir(config_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{combined_sha256(rule, policy)}.json"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class Signoff:
    """The sign-off in force: what provenance records as `signoff_ref` and `signer`."""

    ref: str  # the combined hash, which names the file
    signer: str
    role: str
    signed_on: str


def _read(path: Path) -> dict | None:
    """A sign-off file's record, or None when it is not a readable record of the right shape."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    keys = ("rule_sha256", "policy_sha256", "signer", "role", "signed_on")
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in keys):
        return None
    return record


def most_recent(directory: Path) -> dict | None:
    """The newest sign-off in `directory` by `signed_on`, ties by file name; None when empty.

    Only the failure message reads this: the gate decision never depends on a scan.
    """
    found = []
    for path in directory.glob("*.json") if directory.is_dir() else []:
        record = _read(path)
        if record is not None:
            found.append(((record["signed_on"], path.name), record))
    return max(found, key=lambda item: item[0])[1] if found else None


def verify(config_dir: Path, *, month: str | None = None) -> Signoff:
    """The gate: the file named by the combined hash of the current configs must exist and bind
    exactly these hashes. Otherwise exit 4 with the failing control `CTL-SIGNOFF`.

    Nothing is written. The message names the hashes of the newest sign-off in the directory
    (or `none`) beside the current ones.
    """
    rule, policy = config_hashes(config_dir)
    directory = signoff_dir(config_dir)
    record = _read(directory / f"{combined_sha256(rule, policy)}.json")
    if record is not None and record["rule_sha256"] == rule and record["policy_sha256"] == policy:
        return Signoff(combined_sha256(rule, policy), record["signer"], record["role"], record["signed_on"])
    latest = most_recent(directory)
    signed_rule = latest["rule_sha256"] if latest else "none"
    signed_policy = latest["policy_sha256"] if latest else "none"
    changed = [
        name for name, expected, signed in (
            ("allocation", rule, signed_rule), ("policy", policy, signed_policy),
        ) if expected != signed
    ]
    if latest is None:
        unsigned = "no sign-off found"
    elif changed:
        unsigned = " and ".join(f"{name} config" for name in changed) + " changed since it was signed"
    else:
        unsigned = "no sign-off binds both current configs"
    message = (
        f"allocation rule not signed: rule expected {rule} signed {signed_rule}; "
        f"policy expected {policy} signed {signed_policy}. "
        f"Searched {directory} ({unsigned}); set {SIGNOFF_DIR_ENV} to look elsewhere. "
        "No allocation-dependent step ran."
    )
    raise MarginError(
        EXIT_PRECONDITION, message, month=month,
        controls=[{"id": GATE_ID, "status": "fail", "violating_rows": 1}],
        failed_controls=[{
            "id": GATE_ID, "violating_rows": 1,
            "detail": [
                f"allocation: expected {rule}, signed {signed_rule}",
                f"policy: expected {policy}, signed {signed_policy}",
            ],
        }],
    )
