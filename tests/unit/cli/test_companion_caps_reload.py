"""Real POSIX CLI, fake engine only: hard caps never imply ad execution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
PIN = "ghcr.io/devwspito/safent-ads@sha256:" + "a" * 64
IMAGE_ID = "b" * 64
BROKER_ID = "c" * 64
CAPS = b"defaults: {autonomy_enabled: false}\naccounts: {}\n"
DIGEST = hashlib.sha256(CAPS).hexdigest()
SECRET_SENTINEL = "synthetic-private-diagnostic-never-forward"

ENGINE = r"""
import json, os, sys, time
from pathlib import Path
a = sys.argv[1:]
root = Path(os.environ["FAKE_STATE"])
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps(a) + "\n")
if a[0] == "image":
    if os.environ.get("FAKE_IMAGE_MISSING"): sys.exit(1)
    print("sha256:" + os.environ.get("FAKE_LOCAL_ID", "b" * 64))
elif a[0] == "compose":
    if "up" in a:
        if os.environ.get("FAKE_RECREATE_FAIL"):
            print("synthetic-private-diagnostic-never-forward", file=sys.stderr)
            sys.exit(1)
        (root / "recreated").touch()
    elif "ps" in a:
        print(os.environ.get("FAKE_BROKER_IDS", "c" * 64))
    else: sys.exit(2)
elif a[0] == "inspect":
    image = os.environ.get("FAKE_ACTUAL_ID", "b" * 64)
    if (root / "recreated").exists(): image = os.environ.get("FAKE_POST_ID", image)
    project = os.environ.get("FAKE_PROJECT", "safent-ads")
    role = os.environ.get("FAKE_ROLE", "ads-broker")
    mount = os.environ.get("FAKE_MOUNT", str(root / "companions/ads/caps.yaml"))
    writable = os.environ.get("FAKE_WRITABLE", "false")
    print(f"{project}|{role}|sha256:{image}|{mount}|{writable}")
elif a[0] == "exec":
    assert a[1:3] == ["--user", "10001:10003"]
    assert a[3] == "c" * 64
    assert a[4:] == ["python", "-m", "safent_ads.tools.hard_caps_status"]
    if os.environ.get("FAKE_HANG"): time.sleep(20)
    counter = root / "helper-count"
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    if count < int(os.environ.get("FAKE_TRANSIENT_FAILURES", "0")): sys.exit(1)
    print(os.environ["FAKE_REPLY"])
else: sys.exit(2)
"""

CLOCK = r"""
import os
from pathlib import Path
p = Path(os.environ["FAKE_STATE"]) / "clock"
n = int(p.read_text()) if p.exists() else 0
p.write_text(str(n + 1))
print(1700000000 + n)
"""


@pytest.fixture()
def setup(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    state = tmp_path / "state"
    companion = state / "companions/ads"
    (companion / "bin").mkdir(parents=True)
    (companion / "caps.yaml").write_bytes(CAPS)
    (companion / "image").write_text(PIN)
    (companion / "bin/compose.yaml").write_text(
        "services:\n  ads-broker:\n    image: ${SAFENT_ADS_IMAGE:-safent-ads:local}\n"
    )
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name, body in (
        ("podman", ENGINE),
        ("date", CLOCK),
        ("sleep", "import time\ntime.sleep(0.005)\n"),
    ):
        script = fakebin / name
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o755)
    log = tmp_path / "engine.jsonl"
    env = {
        **{key: os.environ[key] for key in ("PATH", "HOME", "LANG") if key in os.environ},
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "SAFENT_PODMAN": str(fakebin / "podman"),
        "SAFENT_STATE_HOME": str(state),
        "SAFENT_COMPANION_STATE": str(companion),
        "SAFENT_ADS_IMAGE": PIN,
        "FAKE_STATE": str(state),
        "FAKE_LOG": str(log),
        "FAKE_REPLY": json.dumps({"caps_digest": DIGEST}),
    }
    return env, companion, log


def run(
    setup: tuple[dict[str, str], Path, Path], *args: str, **changes: str
) -> tuple[subprocess.CompletedProcess[str], list[dict], list[list[str]]]:
    env, companion, log = setup
    result = subprocess.run(
        ["sh", str(ROOT / "safent"), "companion", *args, "--porcelain"],
        check=False,
        env={**env, **changes},
        text=True,
        capture_output=True,
        timeout=15,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert SECRET_SENTINEL not in result.stdout + result.stderr
    assert not list(companion.parents[1].glob(".caps-status.*"))
    return result, events, calls


def assert_no_ack(events: list[dict]) -> None:
    assert not any(e["t"] in {"caps_status", "caps_reloaded"} for e in events)
    assert events[-1]["t"] == "failed"


def test_reload_only_exact_broker_no_pull_or_privilege_changes(setup):
    result, events, calls = run(setup, "caps-reload", DIGEST)
    assert result.returncode == 0, result.stderr
    assert [e for e in events if e["t"] == "caps_reloaded"] == [
        {"t": "caps_reloaded", "caps_digest": DIGEST}
    ]
    recreates = [c for c in calls if c[0] == "compose" and "up" in c]
    assert len(recreates) == 1
    assert recreates[0][recreates[0].index("up") :] == [
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--no-build",
        "--pull",
        "never",
        "ads-broker",
    ]
    assert not any(
        c[0] in {"pull", "run", "rm", "stop", "restart", "volume", "network"} for c in calls
    )
    assert all("--privileged" not in c and "--cap-add" not in c for c in calls)
    assert [e["t"] for e in events].count("done") == 1


def test_status_reports_loaded_memory_not_disk_and_never_recreates(setup):
    old_digest = "d" * 64
    result, events, calls = run(
        setup, "caps-status", FAKE_REPLY=json.dumps({"caps_digest": old_digest})
    )
    assert result.returncode == 0
    assert events[-1] == {"t": "caps_status", "caps_digest": old_digest}
    assert not any("up" in c for c in calls)


@pytest.mark.parametrize(
    "args",
    [
        ("caps-status", "extra"),
        ("caps-reload",),
        ("caps-reload", "a" * 63),
        ("caps-reload", "A" * 64),
        ("caps-reload", "--privileged"),
        ("caps-reload", DIGEST, "https://evil.invalid"),
    ],
)
def test_closed_argv_fails_before_engine_calls(setup, args):
    result, events, calls = run(setup, *args)
    assert result.returncode != 0
    assert_no_ack(events)
    assert not calls


@pytest.mark.parametrize(
    "changes",
    [
        {"SAFENT_ADS_IMAGE": "ghcr.io/devwspito/safent-ads@sha256:" + "d" * 64},
        {"FAKE_IMAGE_MISSING": "1"},
        {"FAKE_LOCAL_ID": "bad"},
        {"FAKE_BROKER_IDS": BROKER_ID + "\n" + "d" * 64},
        {"FAKE_PROJECT": "foreign"},
        {"FAKE_ROLE": "ads-api"},
        {"FAKE_ACTUAL_ID": "d" * 64},
        {"FAKE_MOUNT": "/foreign/caps.yaml"},
        {"FAKE_WRITABLE": "true"},
    ],
)
def test_pin_identity_mount_and_single_target_are_required(setup, changes):
    result, events, calls = run(setup, "caps-reload", DIGEST, **changes)
    assert result.returncode != 0
    assert_no_ack(events)
    assert not any("up" in c or c[0] == "exec" for c in calls)


@pytest.mark.parametrize(
    "mutation",
    ["missing_pin", "mutable_pin", "symlink_pin", "symlink_caps", "wrong_hash", "wrong_compose"],
)
def test_invalid_files_never_recreate(setup, mutation):
    _, companion, _ = setup
    if mutation == "missing_pin":
        (companion / "image").unlink()
    elif mutation == "mutable_pin":
        (companion / "image").write_text("ghcr.io/devwspito/safent-ads:latest")
    elif mutation == "symlink_pin":
        (companion / "image").rename(companion / "old-image")
        (companion / "image").symlink_to(companion / "old-image")
    elif mutation == "symlink_caps":
        (companion / "caps.yaml").rename(companion / "old-caps")
        (companion / "caps.yaml").symlink_to(companion / "old-caps")
    elif mutation == "wrong_hash":
        (companion / "caps.yaml").write_text("different")
    else:
        (companion / "bin/compose.yaml").write_text(
            "services:\n  ads-broker:\n    image: arbitrary:latest\n"
        )
    result, events, calls = run(setup, "caps-reload", DIGEST)
    assert result.returncode != 0
    assert_no_ack(events)
    assert not any("up" in c for c in calls)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        SECRET_SENTINEL,
        "{}",
        json.dumps({"caps_digest": "A" * 64}),
        json.dumps({"caps_digest": DIGEST, "secret": SECRET_SENTINEL}),
        '{"caps_digest":"' + DIGEST + '","caps_digest":"' + DIGEST + '"}',
        json.dumps({"caps_digest": DIGEST}) + "\n{}",
    ],
)
def test_status_rejects_malformed_unknown_duplicate_and_multiline_reply(setup, reply):
    result, events, _ = run(setup, "caps-status", FAKE_REPLY=reply)
    assert result.returncode != 0
    assert_no_ack(events)


@pytest.mark.parametrize(
    "changes",
    [
        {"FAKE_RECREATE_FAIL": "1"},
        {"FAKE_HANG": "1"},
        {"FAKE_REPLY": json.dumps({"caps_digest": "d" * 64})},
        {"FAKE_POST_ID": "d" * 64},
    ],
)
def test_no_success_on_failure_timeout_or_unconfirmed_state(setup, changes):
    result, events, _ = run(setup, "caps-reload", DIGEST, **changes)
    assert result.returncode != 0
    assert_no_ack(events)
    assert events[-1]["retryable"] is True


def test_retry_waits_for_real_broker_ack_and_keeps_one_recreate(setup):
    result, events, calls = run(setup, "caps-reload", DIGEST, FAKE_TRANSIENT_FAILURES="1")
    assert result.returncode == 0, result.stdout
    assert events[-1] == {"t": "caps_reloaded", "caps_digest": DIGEST}
    assert len([c for c in calls if c[0] == "compose" and "up" in c]) == 1
    assert len([c for c in calls if c[0] == "exec"]) == 2
