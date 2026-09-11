"""secret-scan (`.github/workflows/secret-scan.yml`) has been red since
2026-09-09 (spec 025, hallazgo #3): `gitleaks detect` scans the FULL git
history and flags 3 base64 test fixtures introduced in commit `f1cdc9b`
(`tests/unit/ops/test_companion_provision.py:53,54,72`) as `generic-api-key`.
They are not real secrets — decoded, they read "fake-signing-key-b64" /
"fake-public-key-b64" (the fake gen_keys output the faked podman binary
prints) — but `.gitleaks.toml` never allow-listed them.

Rewriting the fixture values in the CURRENT file would NOT fix this: gitleaks
scans history, and commit f1cdc9b's blob still contains the old values, so it
would still flag them there. The only fix is `.gitleaks.toml`'s allowlist, by
EXACT value (never a broad path), matching the repo's own established
convention (see the 6 pre-existing entries).

This test pins that convention hermetically (no gitleaks binary required —
none is on PATH in the pytest environment; validated separately against the
CI-pinned gitleaks 8.18.4 binary, see the fix commit message) and, when
gitleaks IS available (e.g. a dev has it installed), also runs the exact CI
scan as a real end-to-end check.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GITLEAKS_TOML = _REPO_ROOT / ".gitleaks.toml"
_FIXTURE_FILE = _REPO_ROOT / "tests/unit/ops/test_companion_provision.py"

# The 2 base64 fixture values gitleaks' generic-api-key rule flagged (decoded:
# "fake-signing-key-b64" / "fake-public-key-b64").
_FLAGGED_FAKE_VALUES = (
    "ZmFrZS1zaWduaW5nLWtleS1iNjQ=",
    "ZmFrZS1wdWJsaWMta2V5LWI2NA==",
)


def test_flagged_fake_values_are_still_present_in_the_fixture() -> None:
    # Sanity: if these ever stop appearing in the fixture, the allowlist
    # entries below are dead weight and this whole test should be revisited.
    text = _FIXTURE_FILE.read_text(encoding="utf-8")
    for value in _FLAGGED_FAKE_VALUES:
        assert value in text, f"expected fixture value {value!r} in {_FIXTURE_FILE}"


def test_gitleaks_toml_allowlists_the_fake_values_by_exact_value() -> None:
    config = tomllib.loads(_GITLEAKS_TOML.read_text(encoding="utf-8"))
    regexes = config["allowlist"]["regexes"]
    for value in _FLAGGED_FAKE_VALUES:
        assert value in regexes, (
            f"{value!r} must be allow-listed by exact value in .gitleaks.toml "
            "— rewriting the fixture alone does not fix this, gitleaks scans "
            "full history and the old value is already committed (f1cdc9b)"
        )


def test_gitleaks_toml_does_not_weaken_the_rules_globally() -> None:
    config = tomllib.loads(_GITLEAKS_TOML.read_text(encoding="utf-8"))
    # `useDefault = true` keeps every stock gitleaks rule active; the fix must
    # only add exact-value allowlist entries, never disable/relax a rule.
    assert config["extend"]["useDefault"] is True
    for pattern in config["allowlist"]["regexes"]:
        assert len(pattern) >= 12, (
            f"allowlist entry {pattern!r} looks too broad/short to be a "
            "single fixture value — this would weaken the gate"
        )


def test_ci_verifies_pinned_archive_before_extracting_or_executing() -> None:
    workflow = (_REPO_ROOT / ".github/workflows/secret-scan.yml").read_text()
    assert 'GITLEAKS_VERSION: "8.18.4"' in workflow
    assert "ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d" in workflow
    verify = workflow.index("sha256sum --check --strict")
    assert workflow.index("curl -sSfL") < verify < workflow.index("tar -xzf")
    assert verify < workflow.index("sudo install")
    assert '"$GITLEAKS_SHA256" "$scan_tmp/gitleaks.tgz"' in workflow


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks binary not on PATH")
def test_gitleaks_scan_passes_with_the_repo_config() -> None:
    """Real end-to-end check, same invocation as .github/workflows/secret-scan.yml."""
    result = subprocess.run(  # noqa: S603
        [
            shutil.which("gitleaks"),
            "detect",
            "--source", str(_REPO_ROOT),
            "--config", str(_GITLEAKS_TOML),
            "--redact",
            "--no-banner",
            "--exit-code", "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"gitleaks found leaks (rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
    )
