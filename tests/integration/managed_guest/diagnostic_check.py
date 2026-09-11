"""DISPOSABLE DIAGNOSTIC ONLY: two explicit gate substitutions, real daemon.

Never installed by the production wheel. No flag, environment variable, policy
field or tool can enable these substitutions in the product. The unchanged-gate
guest proof must pass first; host preserves that disk before making this copy.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import importlib.util
import json
import os
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from managed_checks import (
    DB,
    FIXTURE,
    call,
    lifecycle,
    profile_evidence,
    runtime_pid,
    wait_bus,
    wait_replacement,
)

MANIFEST = Path("/var/lib/safent-diagnostic-gates.json")


def substitute_gates() -> list:
    if MANIFEST.exists():
        entries = json.loads(MANIFEST.read_text())
        for entry in entries:
            assert hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() == entry["after"]
        return entries
    entries = []
    for module, name, replacement in (
        (
            "managed_llm",
            "resolve_managed_config",
            "return _resolve_managed_binding(db_path, alias)",
        ),
        ("nous_engine", "_assert_managed_execution_ready", "return None"),
    ):
        path = Path(importlib.util.find_spec("hermes.runtime." + module).origin)
        original = path.read_text()
        function = next(
            node
            for node in ast.parse(original).body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        statement = function.body[-1]
        assert isinstance(statement, ast.Raise)
        assert (
            ast.unparse(statement)
            == "raise ManagedProviderUnavailableError(MANAGED_EXECUTION_UNAVAILABLE)"
        )
        lines = original.splitlines(keepends=True)
        assert statement.lineno == statement.end_lineno
        lines[statement.lineno - 1] = "    " + replacement + "\n"
        changed = "".join(lines)
        compile(changed, str(path), "exec")
        entries.append(
            {
                "path": str(path),
                "function": name,
                "before": hashlib.sha256(original.encode()).hexdigest(),
                "after": hashlib.sha256(changed.encode()).hexdigest(),
                "diff": "".join(
                    difflib.unified_diff(
                        original.splitlines(True),
                        changed.splitlines(True),
                        fromfile=module + ".original",
                        tofile=module + ".diagnostic",
                    )
                ),
            }
        )
        path.write_text(changed)
    MANIFEST.write_text(json.dumps(entries, indent=2))
    return entries


class Gateway(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.records = []
        self.idle_started = threading.Event()
        self.release_idle = threading.Event()
        self.revoked = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= 2_000_000:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size) or "{}")
        if self.path.endswith("/api/show"):
            self.send_error(404)
            return
        authorized = self.headers.get("Authorization") == "Bearer fictional-scoped-guest-token"
        text = json.dumps(body.get("messages", []))
        idle = "GUEST_NATIVE_IDLE_" in text
        self.server.records.append(
            {
                "path": self.path,
                "authorized": authorized,
                "model": body.get("model"),
                "stream": body.get("stream"),
                "fields": sorted(body),
                "idle": idle,
                "success_probe": "GUEST_NATIVE_SUCCESS_" in text,
            }
        )
        if not authorized or self.server.revoked or body.get("model") != "company":
            self.send_error(401)
            return
        if idle:
            self.server.idle_started.set()
            self.server.release_idle.wait(90)
        try:
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                data = {
                    "id": "guest",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "company",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "OK"},
                            "finish_reason": None,
                        }
                    ],
                }
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                data["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                self.wfile.write(("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = {
                    "id": "guest",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "company",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
                self.wfile.write(json.dumps(data).encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass


def gateway() -> Gateway:
    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Repeated runs retain the guest trust store. A unique CA subject avoids
    # OpenSSL selecting a prior same-subject CA with a different private key.
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Safent fixture " + str(uuid4()))])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("enterprise.fixture.test")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    directory = Path("/var/lib/safent-diagnostic-tls")
    directory.mkdir(mode=0o700, exist_ok=True)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (directory / "cert.pem").write_bytes(pem)
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (directory / "key.pem").chmod(0o600)
    for trust in {certifi.where(), ssl.get_default_verify_paths().cafile} - {None}:
        with open(trust, "ab") as stream:
            stream.write(b"\n" + pem)
    with Path("/etc/hosts").open("a") as stream:
        stream.write("\n127.0.0.1 enterprise.fixture.test\n")
    server = Gateway(("127.0.0.1", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def enqueue(nonce: str) -> str:
    result = call(
        "hermes-user",
        "Enqueue",
        "ssissss",
        "chat_message",
        "Reply OK. " + nonce,
        "0",
        nonce,
        str(uuid4()),
        "",
        "",
    )
    assert result["returncode"] == 0, result["stderr"]
    return json.loads(result["stdout"])["data"][0]


def process_identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def runtime_cgroup_members(pid: int) -> dict[int, str]:
    relative = next(
        line[3:]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    expected = subprocess.run(
        ["systemctl", "show", "hermes-runtime", "-p", "ControlGroup", "--value"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    assert relative == expected and Path(relative).name == "hermes-runtime.service", (
        "Runtime PID must belong to its actual systemd unit cgroup"
    )
    root = Path("/sys/fs/cgroup") / relative.lstrip("/")
    members = {}
    for file in root.rglob("cgroup.procs"):
        for number in file.read_text().split():
            identity = process_identity(int(number))
            if identity is not None:
                members[int(number)] = identity
    assert pid in members
    return members


def check() -> dict:  # noqa: PLR0915 - sequential, disposable daemon lifecycle proof
    import sqlite3

    assert os.getuid() == 0 and Path("/proc/1/comm").read_text().strip() == "systemd"
    subprocess.run(["systemctl", "stop", "hermes-runtime"], check=True, timeout=20)
    substitutions = substitute_gates()
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
    server = gateway()
    result = {"diagnostic_only": True, "substitutions": substitutions}
    try:
        subprocess.run(["systemctl", "start", "hermes-runtime"], check=True, timeout=90)
        old = runtime_pid()
        wait_bus(old)
        applied = call(
            "hermes-user", "ApplyManagedLlmGateway", "s", (FIXTURE / "bundle-1.json").read_text()
        )
        assert applied["returncode"] == 0, applied["stderr"]
        managed = wait_replacement(old)
        result["profile"] = profile_evidence(managed)
        task_id = enqueue("GUEST_NATIVE_SUCCESS_" + str(uuid4()))
        deadline = time.monotonic() + 90
        row = None
        while time.monotonic() < deadline:
            with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT status,last_error FROM agent_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            if row and row[0] == "completed":
                break
            time.sleep(0.5)
        result["success_task"] = row
        result["requests"] = server.records
        Path("/var/lib/safent-diagnostic-partial.json").write_text(json.dumps(result))
        assert row and row[0] == "completed" and any(r["success_probe"] for r in server.records), (
            "Real managed daemon did not complete native TLS inference"
        )
        idle_task_id = enqueue("GUEST_NATIVE_IDLE_" + str(uuid4()))
        assert server.idle_started.wait(35), "Native idle request did not reach loopback fixture"
        old_cgroup = runtime_cgroup_members(managed)
        revoked_at = time.monotonic()
        server.revoked = True  # Fixture upstream gate, not a claim of real Enterprise transport.
        revoked = call(
            "hermes-user", "ApplyManagedLlmGateway", "s", (FIXTURE / "bundle-2.json").read_text()
        )
        assert revoked["returncode"] == 0
        replacement = wait_replacement(managed)
        remaining = [
            pid for pid, identity in old_cgroup.items() if process_identity(pid) == identity
        ]
        result["old_runtime_cgroup"] = old_cgroup
        result["old_runtime_cgroup_survivors"] = remaining
        assert not remaining, "Old runtime cgroup process survived revocation"
        result["pid_transition"] = [old, managed, replacement]
        result["revoke_to_ready_seconds"] = round(time.monotonic() - revoked_at, 2)
        result["blocked_lifecycle"] = lifecycle()
        with sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True) as conn:
            result["interrupted_task_observed"] = conn.execute(
                "SELECT status,last_error FROM agent_tasks WHERE task_id=?", (idle_task_id,)
            ).fetchone()
        assert result["blocked_lifecycle"]["mode"] == "blocked"
        assert all(r["authorized"] and r["model"] == "company" for r in server.records)
        return result
    finally:
        server.release_idle.set()
        server.shutdown()
        server.server_close()
