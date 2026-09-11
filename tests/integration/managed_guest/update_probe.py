"""Update only fixture scripts on an offline, own runtime disk without mounting it."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

from run_guest import validate_scratch


def main() -> None:
    root = validate_scratch(Path(sys.argv[1]))
    files = ["guest_probe.py"]
    options = set(sys.argv[2:])
    if options & {"managed", "diagnostic"}:
        files.append("managed_checks.py")
    if "diagnostic" in options:
        files.append("diagnostic_check.py")
    if options - {"managed", "kmod", "diagnostic"}:
        raise ValueError("Only managed/kmod/diagnostic fixture options are supported")
    disk = root / "runtime.raw"
    descriptor = os.open(disk, os.O_RDWR | os.O_NOFOLLOW)
    try:
        # QEMU's image locks overlap this whole-file POSIX lock. Refuse a
        # running VM; retain it through debugfs, preventing a concurrent boot.
        fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in files:
            source = Path(__file__).resolve().parent / name
            destination = "/opt/safent-guest-fixture/" + name
            subprocess.run(["debugfs", "-w", "-R", f"rm {destination}", str(disk)], check=True)
            subprocess.run(
                ["debugfs", "-w", "-R", f"write {source} {destination}", str(disk)], check=True
            )
            # debugfs may return zero even when a command failed. Never boot a
            # stale fixture silently; compare the actual offline file bytes.
            actual = subprocess.run(
                ["debugfs", "-R", f"cat {destination}", str(disk)],
                check=True,
                capture_output=True,
            ).stdout
            if actual != source.read_bytes():
                raise RuntimeError("Offline fixture update verification failed")
        if "kmod" in options:
            # This artifact must have been extracted from the signed/checksummed
            # Ubuntu image; never resolve it from the host's executable PATH.
            source = root / "kmod"
            if source.is_symlink() or not 1000 < source.stat().st_size < 1_000_000:
                raise ValueError("Missing verified Ubuntu kmod artifact")
            for instruction in (
                f"write {source} /usr/bin/kmod",
                "set_inode_field /usr/bin/kmod mode 0100755",
                "symlink /usr/sbin/modprobe ../bin/kmod",
                "symlink /usr/sbin/depmod ../bin/kmod",
            ):
                subprocess.run(["debugfs", "-w", "-R", instruction, str(disk)], check=True)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
