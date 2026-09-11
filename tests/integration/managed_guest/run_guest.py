"""Bounded two-boot KVM runner; no host mounts/services, NIC, or implicit cleanup.

All mutable disks/sockets/logs must live in a dedicated /tmp scratch. Only the
QEMU process this invocation creates may be terminated. Runtime disk is created
with O_EXCL and formatted by the preparatory guest, never by this host program.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


def validate_scratch(candidate: Path) -> Path:
    if candidate.is_symlink():
        raise ValueError("Scratch must not be a symlink")
    root = candidate.resolve(strict=True)
    if root.parent != Path("/tmp").resolve() or not root.name.startswith("safent-managed-guest."):
        raise ValueError("Expected dedicated /tmp/safent-managed-guest.* scratch")
    if not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Scratch must be private and owned by the invoking user")
    return root


def query_kvm(path: Path) -> dict:
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Own QMP socket did not appear")
        time.sleep(0.1)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        reader = connection.makefile("rb")
        json.loads(reader.readline())
        for number, name in enumerate(("qmp_capabilities", "query-kvm")):
            connection.sendall(json.dumps({"execute": name, "id": number}).encode() + b"\n")
            while True:
                reply = json.loads(reader.readline())
                if reply.get("id") == number:
                    break
            if "error" in reply:
                raise RuntimeError("Own QMP verification failed")
        result = reply["return"]
        if result != {"enabled": True, "present": True}:
            raise RuntimeError("Guest did not use actual KVM acceleration")
        return result


def validate_runtime_report(serial: str) -> dict:
    reports = [
        line.split("SAFENT_GUEST_REPORT ", 1)[1]
        for line in serial.splitlines()
        if "SAFENT_GUEST_REPORT " in line
    ]
    if len(reports) != 1:
        raise RuntimeError("Expected exactly one runtime proof report; not a pass")
    report = json.loads(reports[0])
    if report.get("ready") is not True or report.get("managed_checks_error"):
        raise RuntimeError("Guest runtime proof failed; inspect retained report")
    return report


def main() -> None:  # noqa: PLR0912, PLR0915 - one contiguous owned-VM lifecycle
    parser = argparse.ArgumentParser()
    parser.add_argument("scratch", type=Path)
    parser.add_argument("phase", choices=("prepare", "runtime"))
    args = parser.parse_args()
    root = validate_scratch(args.scratch)
    from input_manifest import read_inputs

    inputs = read_inputs(root)
    disk = root / "runtime.raw"
    cmd = [
        "qemu-system-aarch64",
        "-machine",
        "virt,accel=kvm",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "4096",
        "-display",
        "none",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-serial",
        f"file:{root / (args.phase + '-serial.log')}",
        "-qmp",
        f"unix:{root / (args.phase + '.qmp')},server=on,wait=off",
    ]
    if args.phase == "prepare":
        with disk.open("xb") as stream:
            stream.truncate(20 * 1024**3)
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(root / "ubuntu-24.04-minimal-cloudimg-arm64.img"),
                str(root / "prepare.qcow2"),
            ],
            check=True,
        )
        shutil.copy2("/usr/share/AAVMF/AAVMF_VARS.fd", root / "uefi-vars.fd")
        cmd += [
            "-drive",
            "if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.fd",
            "-drive",
            f"if=pflash,format=raw,file={root / 'uefi-vars.fd'}",
            "-drive",
            f"if=virtio,format=qcow2,file={root / 'prepare.qcow2'}",
            "-drive",
            f"if=none,id=runtime,format=raw,file={disk}",
            "-device",
            "virtio-blk-pci,drive=runtime,serial=SAFENT_FIXTURE",
        ]
        for index, image in enumerate(("data.iso", "seed.iso")):
            cmd += [
                "-drive",
                f"if=none,id=cd{index},format=raw,readonly=on,file={root / image}",
                "-device",
                f"virtio-blk-pci,drive=cd{index}",
            ]
    else:
        # debugfs is offline/read-only by default; QEMU is not running yet.
        for name, guest in (
            ("vmlinuz", "/boot/vmlinuz-fixture"),
            ("initrd", "/boot/initrd-fixture"),
        ):
            target = root / name
            if target.exists():
                raise ValueError("Refusing to overwrite prior boot evidence")
            subprocess.run(["debugfs", "-R", f"dump {guest} {target}", str(disk)], check=True)
            if name == "initrd" and not target.exists():
                continue
            if target.stat().st_size < 1024:
                raise ValueError("Guest kernel/initrd extraction failed")
        cmd += [
            "-kernel",
            str(root / "vmlinuz"),
            "-append",
            "root=/dev/vda rw console=ttyAMA0 systemd.show_status=yes",
            "-drive",
            f"if=virtio,format=raw,file={disk}",
        ]
        if (root / "initrd").exists():
            cmd += ["-initrd", str(root / "initrd")]
    manifest = root / (args.phase + "-command.json")
    with manifest.open("x") as stream:
        json.dump(
            {"argv": cmd, "phase": args.phase, "inputs": inputs, "timeout_seconds": 600},
            stream,
            indent=2,
        )
    started = time.monotonic()
    with (root / (args.phase + "-qemu.log")).open("xb") as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=log)
        (root / (args.phase + "-pid")).write_text(str(process.pid))
        try:
            acceleration = query_kvm(root / (args.phase + ".qmp"))
            (root / (args.phase + "-kvm.json")).write_text(json.dumps(acceleration))
            code = process.wait(timeout=600)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            raise
    print(
        json.dumps(
            {
                "phase": args.phase,
                "returncode": code,
                "elapsed": round(time.monotonic() - started, 2),
            }
        ),
        flush=True,
    )
    if code:
        raise SystemExit(code)
    if args.phase == "prepare" and "SAFENT_PREPARE_PASS" not in (
        root / "prepare-serial.log"
    ).read_text(errors="replace"):
        raise RuntimeError("Guest exited without successful preparation evidence")
    if args.phase == "runtime":
        validate_runtime_report((root / "runtime-serial.log").read_text(errors="replace"))


if __name__ == "__main__":
    main()
