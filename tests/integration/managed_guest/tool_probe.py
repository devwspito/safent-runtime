"""Inert tool payload: inspect its own cage, spawn a sleeper and report identities.

Executed ONLY through the real native terminal tool inside the disposable guest.
Never reads secret contents or connects outside the guest; records permission
verdicts and process metadata in its new, dedicated workspace directory.
"""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path


def main():
    output = Path(sys.argv[1])
    runtime_pid = int(sys.argv[2])
    listener_port = int(sys.argv[3])
    assert output.name == "facts.json" and output.parent.name.startswith("guest-tool-proof-")
    facts = {
        "pid": os.getpid(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cgroup": Path("/proc/self/cgroup").read_text(),
        "denied": {},
    }
    for path in (
        "/var/lib/hermes/master.key",
        "/var/lib/hermes/shell-state.db",
        f"/proc/{runtime_pid}/environ",
        f"/proc/{runtime_pid}/root/var/lib/hermes/master.key",
    ):
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError as exc:
            facts["denied"][path] = {"denied": True, "errno": exc.errno}
        else:
            os.close(descriptor)  # Deliberately do not read any bytes, even on a failure.
            facts["denied"][path] = {"denied": False}
    for label, family, address in (
        ("exec_control_socket", socket.AF_UNIX, "/run/hermes/exec-launch.sock"),
        ("guest_host_listener", socket.AF_INET, ("10.200.0.1", listener_port)),
    ):
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            try:
                client.connect(address)
            except OSError as exc:
                facts["denied"][label] = {"denied": True, "errno": exc.errno}
            else:
                facts["denied"][label] = {"denied": False}
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        [
            "/usr/bin/python3",
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(45)",
        ]
    )
    facts["child_pid"] = child.pid
    facts["ignores_sigterm"] = True
    output.write_text(json.dumps(facts))
    child.wait(timeout=50)


if __name__ == "__main__":
    main()
