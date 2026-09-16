"""Actual native terminal → launcher → transient unit proof, diagnostic guest only."""

import grp
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import diagnostic_check as baseline
from managed_checks import DB, FIXTURE, call, lifecycle, runtime_pid, wait_bus, wait_replacement


class ToolHandler(baseline.Handler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if self.path.endswith("/api/show"):
            self.send_error(404)
            return
        authorized = self.headers.get("Authorization") == "Bearer fictional-scoped-guest-token"
        assert authorized and body.get("model") == "company" and body.get("stream") is True
        messages = body.get("messages", [])
        own = any("GUEST_TOOL_PROOF_" in str(message.get("content", "")) for message in messages)
        done = not own or any(message.get("role") == "tool" for message in messages)
        self.server.records.append({"authorized": authorized, "after_tool": done})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if done:
            delta = {"role": "assistant", "content": "Fixture tool finished."}
            finish = "stop"
        else:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "fixture_tool",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": self.server.tool_command}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        for part, reason in ((delta, None), ({}, finish)):
            frame = {
                "id": "tool-fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "company",
                "choices": [{"index": 0, "delta": part, "finish_reason": reason}],
            }
            self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def save(result):
    Path("/var/lib/safent-tool-guest-partial.json").write_text(json.dumps(result))


def check() -> dict:  # noqa: PLR0915 - chronological isolated integration proof
    assert os.getuid() == 0 and Path("/proc/1/comm").read_text().strip() == "systemd"
    result = {"diagnostic_only": True, "scope": "real-native-terminal-transient-unit"}
    subprocess.run(["systemctl", "stop", "hermes-runtime"], check=True, timeout=20)
    result["inputs"] = baseline.install_fixture_wheel()
    result["substitutions"] = baseline.verify_production_gates()
    subprocess.run(
        [
            "runuser",
            "-u",
            "hermes",
            "--",
            "/usr/bin/python3",
            str(Path(__file__).with_name("managed_checks.py")),
            "seed",
        ],
        check=True,
        timeout=20,
    )
    server = baseline.gateway()
    server.RequestHandlerClass = ToolHandler  # Fixture server only, never SDK/product routing.
    unit = None
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(4)
        try:
            subprocess.run(["systemctl", "start", "hermes-runtime"], check=True, timeout=90)
            old = runtime_pid()
            wait_bus(old)
            applied = call(
                "hermes-user",
                "ApplyManagedLlmGateway",
                "s",
                (FIXTURE / "bundle-1.json").read_text(),
            )
            assert applied["returncode"] == 0
            managed = wait_replacement(old)
            directory = Path("/var/lib/hermes/workspace") / ("guest-tool-proof-" + str(uuid4()))
            directory.mkdir(mode=0o2770)
            os.chown(directory, pwd.getpwnam("hermes").pw_uid, grp.getgrnam("hermes-work").gr_gid)
            directory.chmod(0o2770)
            script = directory / "tool_probe.py"
            shutil.copyfile(Path(__file__).with_name("tool_probe.py"), script)
            script.chmod(0o644)
            facts_file = directory / "facts.json"
            assert (
                Path("/run/hermes/exec-launch.sock").exists()
                and Path("/var/lib/hermes/master.key").exists()
            )
            server.tool_command = " ".join(
                shlex.quote(str(x))
                for x in (
                    "/usr/bin/python3",
                    script,
                    facts_file,
                    managed,
                    listener.getsockname()[1],
                )
            )
            task_id = baseline.enqueue("GUEST_TOOL_PROOF_" + str(uuid4()))
            deadline = time.monotonic() + 40
            while not facts_file.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            assert facts_file.exists(), "Native terminal did not run the pinned tool payload"
            facts = json.loads(facts_file.read_text())
            result["facts"] = facts
            assert facts["uid"] == pwd.getpwnam("hermes-sandbox").pw_uid
            assert all(value["denied"] for value in facts["denied"].values()), (
                "Tool escaped an expected cage denial"
            )
            child = facts["child_pid"]
            relative = next(
                line[3:]
                for line in Path(f"/proc/{child}/cgroup").read_text().splitlines()
                if line.startswith("0::")
            )
            unit = Path(relative).name
            assert re.fullmatch(r"hermes-exec-[0-9a-f]{16}\.service", unit), (
                "Tool child not in real launcher service"
            )
            members = {}
            for path in (Path("/sys/fs/cgroup") / relative.lstrip("/")).rglob("cgroup.procs"):
                for number in path.read_text().split():
                    identity = baseline.process_identity(int(number))
                    if identity is not None:
                        members[int(number)] = identity
            assert child in members and facts["pid"] in members
            result.update(unit=unit, members=members, task_id=task_id)
            result["unit_properties"] = subprocess.run(
                ["systemctl", "show", unit], check=True, capture_output=True, text=True, timeout=10
            ).stdout
            save(result)
            started = time.monotonic()
            revoked = call(
                "hermes-user",
                "ApplyManagedLlmGateway",
                "s",
                (FIXTURE / "bundle-2.json").read_text(),
            )
            assert revoked["returncode"] == 0
            replacement = wait_replacement(managed)
            survivors = [
                pid
                for pid, identity in members.items()
                if baseline.process_identity(pid) == identity
            ]
            result.update(
                pid_transition=[old, managed, replacement],
                seconds=round(time.monotonic() - started, 2),
                survivors=survivors,
                lifecycle=lifecycle(),
            )
            with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as connection:
                result["task_after_restart"] = connection.execute(
                    "SELECT status,last_error FROM agent_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            save(result)
            assert result["seconds"] < 15, "Revocation exceeded the proof's 15-second bound"
            assert not survivors, "Real tool descendants survived runtime revocation/restart"
            assert result["task_after_restart"][0] == "cancelled", (
                "Revoked tool task not terminal cancelled"
            )
            return result
        finally:
            if unit is not None and re.fullmatch(r"hermes-exec-[0-9a-f]{16}\.service", unit):
                # Cleanup only this fixture's exact verified unit, after saving
                # the verdict. Never wait for its payload's natural timeout.
                subprocess.run(
                    ["systemctl", "kill", "--kill-whom=all", "--signal=KILL", unit],
                    check=False,
                    timeout=10,
                )
                subprocess.run(["systemctl", "stop", unit], check=False, timeout=15)
            server.shutdown()
            server.server_close()
