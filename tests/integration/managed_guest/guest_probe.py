"""Actual guest PID1/systemd baseline; no replacement of runtime or confinement."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def command(*args: str, timeout: int = 20) -> dict:
    process = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    return {"returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}


def main() -> None:
    report = {"scope": "native-kvm-not-container-delivery", "gate": "unchanged"}
    report["pid1"] = Path("/proc/1/comm").read_text().strip()
    report["virtualization"] = command("systemd-detect-virt")
    report["kernel"] = command("uname", "-a")
    report["interfaces"] = command("ip", "-j", "link", "show")
    assert report["pid1"] == "systemd"
    # Direct ARM kernel boot reports qemu rather than kvm without SMBIOS;
    # acceleration itself is independently asserted by host QMP query-kvm.
    assert report["virtualization"]["stdout"].strip() in {"kvm", "qemu"}
    # No masking or replacement of image services: failed or skipped prerequisites
    # are recorded as failures, never counted as native runtime proof.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status = command(
            "systemctl",
            "show",
            "hermes-runtime.service",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
        )
        if "ActiveState=active" in status["stdout"]:
            break
        time.sleep(2)
    report["runtime"] = command(
        "systemctl",
        "show",
        "hermes-runtime.service",
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "MainPID",
        "-p",
        "Result",
        "-p",
        "ConditionResult",
        "-p",
        "Type",
        "-p",
        "NotifyAccess",
        "-p",
        "User",
        "-p",
        "Group",
        "-p",
        "SupplementaryGroups",
        "-p",
        "CapabilityBoundingSet",
        "-p",
        "ProtectSystem",
        "-p",
        "ProtectHome",
        "-p",
        "NoNewPrivileges",
        "-p",
        "SystemCallFilter",
        "-p",
        "KillMode",
        "-p",
        "TimeoutStopUSec",
        "-p",
        "ControlGroup",
    )
    report["units"] = command("systemctl", "--no-pager", "--failed")
    report["modules_journal"] = command(
        "journalctl", "--no-pager", "-b", "-u", "systemd-modules-load", "-n", "60"
    )
    report["kernel_modprobe"] = Path("/proc/sys/kernel/modprobe").read_text().strip()
    report["modprobe_executable"] = Path(report["kernel_modprobe"]).exists()
    report["kernel_journal"] = command("journalctl", "--no-pager", "-b", "-k", "-n", "80")
    report["effective_unit"] = command("systemctl", "cat", "hermes-runtime.service")
    report["ready"] = (
        "ActiveState=active" in report["runtime"]["stdout"]
        and "SubState=running" in report["runtime"]["stdout"]
    )
    if report["ready"] and Path("/opt/safent-guest-fixture/managed_checks.py").exists():
        try:
            if Path("/opt/safent-guest-fixture/diagnostic_check.py").exists():
                from diagnostic_check import check

                report["gate"] = "TWO diagnostic-only substitutions; NOT production"
            else:
                from managed_checks import check

            report["managed_checks"] = check()
        except Exception as exc:
            report["managed_checks_error"] = f"{type(exc).__name__}: {exc}"
            partial = Path("/var/lib/safent-managed-checks-partial.json")
            if partial.exists():
                report["managed_checks_partial"] = json.loads(partial.read_text())
            partial = Path("/var/lib/safent-diagnostic-partial.json")
            if partial.exists():
                report["diagnostic_partial"] = json.loads(partial.read_text())
    report["journal"] = command(
        "journalctl", "--no-pager", "-b", "-u", "hermes-runtime", "-n", "250"
    )
    report["helper_journal"] = command(
        "journalctl",
        "--no-pager",
        "-b",
        "-u",
        "hermes-browser-netns",
        "-u",
        "hermes-egress-proxy",
        "-n",
        "150",
    )
    Path("/var/lib/safent-guest-proof.json").write_text(json.dumps(report, indent=2))
    print("SAFENT_GUEST_REPORT " + json.dumps(report), flush=True)
    os.sync()
    command("systemctl", "poweroff")


if __name__ == "__main__":
    main()
