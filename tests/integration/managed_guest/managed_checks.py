"""Fictional signed assignment through the real guest's authorized D-Bus path.

This fixture never patches either production inference gate. It tests managed
clean exec, READY, closed admission and signed revocation on the existing daemon.
Only run inside the dedicated native KVM image created by this harness.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DB = Path("/var/lib/hermes/shell-state.db")
FIXTURE = Path("/var/lib/hermes/guest-fixture")


def seed() -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from hermes.config_sync.policy_document import (
        PolicyBundle,
        PolicyPayload,
        ProviderSpec,
        signing_bytes,
    )
    from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
    from hermes.shell_server.security.secrets import SecretsVault

    assert os.getuid() == 880
    FIXTURE.mkdir(mode=0o755, exist_ok=True)
    signer = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC).isoformat()
    store = SQLiteAssociationStore(db_path=DB, vault=SecretsVault())
    previous = store.get()
    if previous is not None:
        assert (previous.instance_id, previous.tenant_id) == (
            "guest-fixture-instance",
            "guest-fixture-org",
        ), "Fixture must never overwrite a real association"
        # Retry only our own fictional fixture, through the real unpair writer.
        # Runtime is stopped by check() before this setup step.
        store.clear()
    store.save(
        association=InstanceAssociation(
            instance_id="guest-fixture-instance",
            tenant_id="guest-fixture-org",
            paired_at=now,
            cloud_endpoint="https://enterprise.fixture.test",
            signing_pubkey_hex=signer.public_key().public_bytes_raw().hex(),
            license={},
            last_applied_version=0,
            state="active",
        ),
        instance_secret="fictional-pairing-not-inference",
    )
    for version, providers in (
        (
            1,
            [
                ProviderSpec(
                    alias="company",
                    kind="openai_compatible",
                    default_model="company",
                    credential_kind="instance_gateway",
                    api_key="fictional-scoped-guest-token",
                    set_active=True,
                    base_url="https://enterprise.fixture.test/v1/inference/guest/v1",
                )
            ],
        ),
        (2, []),
    ):
        payload = PolicyPayload(llm_instance_id="guest-fixture-instance", providers=providers)
        parts = {
            "version": version,
            "tenant_id": "guest-fixture-org",
            "issued_at": now,
            "payload": payload,
        }
        bundle = PolicyBundle(**parts, signature_hex=signer.sign(signing_bytes(**parts)).hex())
        target = FIXTURE / f"bundle-{version}.json"
        target.write_text(bundle.model_dump_json())
        target.chmod(0o644)  # Fictional token only; operator needs the signed test envelope.


def call(user: str, method: str, signature: str = "", *args: str) -> dict:
    argv = [
        "runuser",
        "-u",
        user,
        "--",
        "busctl",
        "--system",
        "--json=short",
        "call",
        "org.hermes.Runtime",
        "/org/hermes/Runtime",
        "org.hermes.Runtime1",
        method,
    ]
    if signature:
        argv += [signature, *args]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    # Never print the signed envelope or credential, even though fictitious.
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def runtime_pid() -> int:
    result = subprocess.run(
        ["systemctl", "show", "hermes-runtime", "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return int(result.stdout.strip() or "0")


def wait_bus(pid: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        reply = subprocess.run(
            [
                "busctl",
                "--system",
                "--json=short",
                "call",
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                "s",
                "org.hermes.Runtime",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if reply.returncode == 0 and json.loads(reply.stdout)["data"] == [pid]:
            return
        time.sleep(0.2)
    raise AssertionError("READY daemon did not acquire the real D-Bus name")


def wait_replacement(old_pid: int) -> int:
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        pid = runtime_pid()
        active = (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", "hermes-runtime"], timeout=10, check=False
            ).returncode
            == 0
        )
        if pid and pid != old_pid and active:
            assert not Path(f"/proc/{old_pid}").exists(), "Old runtime PID survived replacement"
            wait_bus(pid)
            return pid
        time.sleep(1)
    raise AssertionError("Replacement did not reach real READY")


def lifecycle() -> dict:
    with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute(
                "SELECT generation,mode,booted_generation,restart_pending "
                "FROM managed_llm_lifecycle WHERE id=1"
            ).fetchone()
        )


def profile_evidence(pid: int) -> dict:
    import stat

    entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    environment = dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)
    path = Path(os.fsdecode(environment[b"HERMES_HOME"]))
    assert path.parent == DB.parent / "managed-profiles"
    config = path / "config.yaml"
    assert path.stat().st_uid == 880 and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert config.stat().st_uid == 880 and stat.S_IMODE(config.stat().st_mode) == 0o600
    assert b"fictional-scoped-guest-token" not in config.read_bytes()
    return {
        "directory_mode": "0700",
        "config_mode": "0600",
        "owner_uid": 880,
        "token_in_config": False,
        "profile": path.name,
    }


def check() -> dict:
    assert subprocess.check_output(["systemd-detect-virt"], text=True).strip() in {"kvm", "qemu"}
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    result = {}
    subprocess.run(["systemctl", "stop", "hermes-runtime"], check=True, timeout=20)
    subprocess.run(
        ["runuser", "-u", "hermes", "--", "/usr/bin/python3", __file__, "seed"],
        check=True,
        timeout=20,
    )
    subprocess.run(["systemctl", "start", "hermes-runtime"], check=True, timeout=90)
    old = runtime_pid()
    wait_bus(old)
    assignment = (FIXTURE / "bundle-1.json").read_text()
    denied = call("root", "ApplyManagedLlmGateway", "s", assignment)
    result["denied"] = denied
    assert denied["returncode"] != 0 and "denied" in denied["stderr"].lower(), (
        "Root must receive an actual authorization denial, not a missing service"
    )
    result["root_apply_denied"] = True
    accepted = call("hermes-user", "ApplyManagedLlmGateway", "s", assignment)
    result["apply"] = accepted
    Path("/var/lib/safent-managed-checks-partial.json").write_text(json.dumps(result))
    assert accepted["returncode"] == 0, "Authorized signed D-Bus apply failed"
    managed_pid = wait_replacement(old)
    result["pid_transition"] = [old, managed_pid]
    result["managed_lifecycle"] = lifecycle()
    result["profile"] = profile_evidence(managed_pid)
    assert result["managed_lifecycle"]["mode"] == "managed"
    assert result["managed_lifecycle"]["restart_pending"] == 0
    queued = call(
        "hermes-user",
        "Enqueue",
        "ssissss",
        "chat_message",
        "Reply OK.",
        "0",
        "guest-proof-" + str(uuid4()),
        str(uuid4()),
        "",
        "",
    )
    result["enqueue"] = queued
    Path("/var/lib/safent-managed-checks-partial.json").write_text(json.dumps(result))
    assert queued["returncode"] == 0
    task_id = json.loads(queued["stdout"])["data"][0]
    # The production audit/TSA path may spend ~30s exhausting its offline
    # network timeout before persisting this error; keep it real and bounded.
    deadline = time.monotonic() + 60
    task = None
    while time.monotonic() < deadline:
        with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as conn:
            task = conn.execute(
                "SELECT status,last_error FROM agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if task and task[1]:
            break
        time.sleep(0.5)
    result["task"] = task
    Path("/var/lib/safent-managed-checks-partial.json").write_text(json.dumps(result))
    assert task and "Enterprise LLM execution is unavailable" in (task[1] or ""), (
        "Real daemon did not demonstrate its closed managed gate"
    )
    revoked = call(
        "hermes-user", "ApplyManagedLlmGateway", "s", (FIXTURE / "bundle-2.json").read_text()
    )
    result["revoke"] = revoked
    assert revoked["returncode"] == 0
    blocked_pid = wait_replacement(managed_pid)
    result["pid_transition"].append(blocked_pid)
    result["blocked_lifecycle"] = lifecycle()
    assert result["blocked_lifecycle"]["mode"] == "blocked"
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        seed()
    else:
        print(json.dumps(check()), flush=True)
