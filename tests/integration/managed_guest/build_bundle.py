"""Prepare readonly guest media in an existing, explicitly named scratch directory.

Prerequisites: independently verified Ubuntu image, exported *own* stopped RC2
container rootfs and offline current-source wheel. Never formats or mounts a host disk.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

from run_guest import validate_scratch


def main() -> None:
    root = validate_scratch(Path(sys.argv[1]))
    here = Path(__file__).resolve().parent
    media = root / "media"
    seed = root / "seed"
    media.mkdir(mode=0o700)
    seed.mkdir(mode=0o700)
    # Hardlink only our already-exported artifact, saving an additional 8GiB copy.
    (media / "rc2-rootfs.tar").hardlink_to(root / "rc2-rootfs.tar")
    shutil.copy2(root / "wheels/hermes_runtime-0.9.0-py3-none-any.whl", media)
    for name in ("guest_probe.py",):
        shutil.copy2(here / name, media)
    shutil.copy2(root / "source/ops/agents-os-edition/systemd/hermes-runtime.service", media)
    shutil.copy2(root / "source/ops/agents-os-edition/dbus/org.hermes.Runtime1.conf", media)
    (media / "safent-guest-proof.service").write_text("""[Unit]
Description=Disposable Safent KVM evidence collector
After=basic.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/safent-guest-fixture/guest_probe.py
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyAMA0
TimeoutStartSec=240
[Install]
WantedBy=multi-user.target
""")
    config = {
        "preserve_hostname": True,
        "manage_etc_hosts": False,
        "package_update": False,
        "package_upgrade": False,
        "ssh_pwauth": False,
        "write_files": [
            {
                "path": "/root/safent-prepare.sh",
                "permissions": "0700",
                "encoding": "b64",
                "content": base64.b64encode((here / "prepare_guest.sh").read_bytes()).decode(),
            }
        ],
        "runcmd": [["/bin/bash", "/root/safent-prepare.sh"]],
    }
    (seed / "user-data").write_text("#cloud-config\n" + json.dumps(config))
    (seed / "meta-data").write_text(
        "instance-id: safent-kvm-fixture\nlocal-hostname: safent-fixture\n"
    )
    (seed / "network-config").write_text("version: 2\nethernets: {}\n")
    for label, directory, filename in (
        ("SAFENT_DATA", media, "data.iso"),
        ("CIDATA", seed, "seed.iso"),
    ):
        subprocess.run(
            [
                "xorriso",
                "-as",
                "mkisofs",
                "-iso-level",
                "3",
                "-r",
                "-J",
                "-V",
                label,
                "-o",
                str(root / filename),
                str(directory),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
