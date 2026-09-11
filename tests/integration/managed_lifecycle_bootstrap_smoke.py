"""Disposable image only: clean exec + native Hermes + HTTPS loopback fixture.

Run with --network none and enterprise.fixture.test mapped to 127.0.0.1.
No running user service, no real credentials. This is NOT the full systemd/
Landlock daemon boot: production execution remains gated pending that proof.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Explicit fixture-only source overlay. Production clean exec inherits neither
# PYTHONPATH nor arbitrary interpreter/module selectors.
sys.path.insert(0, "/review/src")


def worker():
    from hermes.runtime.managed_llm_bootstrap import complete_process_bootstrap, initialize_process
    from hermes.runtime.managed_llm_profile import current_profile

    db_path, case = Path(sys.argv[2]), sys.argv[3]
    markers = [arg for arg in sys.argv[4:] if arg.startswith("--managed-bootstrap-fd=")]

    def fixture_exec(executable, argv, env):
        marker = argv[-1]
        os.execve(executable, [executable, __file__, "--worker", str(db_path), case, marker], env)

    pending = initialize_process(db_path, argv=markers, exec_fn=fixture_exec)
    # Confinement is asserted by daemon _run in production. Here the external
    # boundary is an entirely disposable --network none image, not a bypass
    # of the real daemon's Landlock assertion or execution gate.
    guard = complete_process_bootstrap(
        pending, profile_factory=lambda gen: current_profile(db_path, gen)
    )
    if guard.boot.mode == "blocked":
        print("RESULT:blocked", flush=True)
        return

    from hermes.runtime.managed_llm import _resolve_managed_binding
    from hermes.runtime.managed_llm_lifecycle import run_admitted_native, watch_authority
    from hermes.runtime.nous_engine import GovernedAIAgent, _resolve_hermes_runtime
    from hermes.runtime.shutdown_deadline import ShutdownDeadline

    binding = _resolve_managed_binding(db_path)
    native, model = _resolve_hermes_runtime(binding)
    assert "OPENAI_API_KEY" not in os.environ and "HERMES_MODEL" not in os.environ
    assert binding.api_key not in (Path(os.environ["HERMES_HOME"]) / "config.yaml").read_text()
    # Existing wrapper, native resolver and SDK; no parallel inference engine.
    # Direct construction is diagnostic only while the production gate is closed.
    agent = GovernedAIAgent(
        model=model,
        api_key=native["api_key"],
        base_url=native["base_url"],
        provider=native["provider"],
        api_mode=native["api_mode"],
        max_iterations=1,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
        skip_background_review=True,
        save_trajectories=False,
        ephemeral_system_prompt="Reply OK.",
        max_tokens=16,
    )

    async def run():
        deadline = ShutdownDeadline(seconds=1.0)

        def stop():
            deadline.arm()
            guard.close()
            guard.interrupt_inflight()
            print("RESTART:required", flush=True)

        monitor = asyncio.create_task(watch_authority(guard, stop, interval=0.05))
        try:
            result = await run_admitted_native(
                guard,
                lambda: agent.run_conversation("Reply OK."),
                lambda: agent._inner.hard_interrupt("Fixture authority changed"),
            )
            if case == "revoke":
                # Do not print a false completed result if native cancellation
                # returned before the hard deadline terminated the process.
                await asyncio.sleep(2)
            assert result.get("final_response") == "OK", (
                "Native fixture response was not successful"
            )
            guard.check()
            print("RESULT:success", flush=True)
        finally:
            monitor.cancel()

    asyncio.run(run())


def main():  # noqa: PLR0915 - self-contained disposable process fixture
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    from cryptography.x509.oid import NameOID

    from hermes.config_sync.policy_document import (
        PolicyBundle,
        PolicyPayload,
        ProviderSpec,
        signing_bytes,
    )
    from hermes.instance.association_store import InstanceAssociation, SQLiteAssociationStore
    from hermes.runtime.managed_llm import apply_signed_gateway
    from hermes.shell_server.providers.repo import SQLiteProviderRepository
    from hermes.shell_server.security.secrets import SecretsVault

    # This script refuses to overwrite an existing key. The fixture image has
    # no mounted state or baked master key; only this disposable layer is changed.
    master = Path("/var/lib/hermes/master.key")
    master.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(master, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(b"L" * 32)

    records = []
    request_started = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            records.append((self.path, self.headers.get("Authorization"), body))
            if self.path.endswith("/api/show"):
                self.send_response(404)
                self.end_headers()
                return
            if "/revoke/" in self.path:
                request_started.set()
                # Deliberately non-cooperative upstream. Parent proves that the
                # process exits despite the outstanding native HTTP thread.
                threading.Event().wait(5)
            try:
                self.send_response(200)
                if not body.get("stream"):
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {
                                "id": "fixture",
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
                                "usage": {
                                    "prompt_tokens": 1,
                                    "completion_tokens": 1,
                                    "total_tokens": 2,
                                },
                            }
                        ).encode()
                    )
                    return
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [({"role": "assistant", "content": "OK"}, None), ({}, "stop")]:
                    frame = {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "company",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

    with tempfile.TemporaryDirectory(prefix="managed-lifecycle-proof-") as directory:
        root = Path(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Safent disposable fixture")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("enterprise.fixture.test")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        certificate = cert.public_bytes(serialization.Encoding.PEM)
        (root / "server.pem").write_bytes(certificate)
        (root / "server.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        # Trust only in this container's writable layer, not through inherited
        # SSL_CERT_FILE or another environment escape from clean bootstrap.
        for trust_path in {certifi.where(), ssl.get_default_verify_paths().cafile}:
            assert trust_path is not None
            with open(trust_path, "ab") as trust:
                trust.write(b"\n" + certificate)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "server.pem", root / "server.key")
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"https://enterprise.fixture.test:{server.server_port}"
        outputs = []
        for case in ("success", "revoke"):
            path = root / f"{case}.db"
            vault = SecretsVault(master_key=b"L" * 32)
            store = SQLiteAssociationStore(db_path=path, vault=vault)
            repo = SQLiteProviderRepository(db_path=path, vault=vault)
            signer = ed25519.Ed25519PrivateKey.generate()
            now = datetime.now(UTC).isoformat()
            store.save(
                association=InstanceAssociation(
                    instance_id="fixture-instance",
                    tenant_id="fixture-org",
                    paired_at=now,
                    cloud_endpoint=origin,
                    signing_pubkey_hex=signer.public_key().public_bytes_raw().hex(),
                    license={},
                    last_applied_version=0,
                    state="active",
                ),
                instance_secret="fictional-pairing",
            )
            payload = PolicyPayload(
                llm_instance_id="fixture-instance",
                providers=[
                    ProviderSpec(
                        alias="company",
                        kind="openai_compatible",
                        default_model="company",
                        credential_kind="instance_gateway",
                        api_key="fictional-scoped-inference",
                        set_active=True,
                        base_url=origin + f"/v1/inference/{case}/v1",
                    )
                ],
            )
            parts = dict(version=1, tenant_id="fixture-org", issued_at=now, payload=payload)
            signed = PolicyBundle(
                **parts, signature_hex=signer.sign(signing_bytes(**parts)).hex()
            ).model_dump_json()
            apply_signed_gateway(
                SimpleNamespace(
                    _association_store=store, _provider_repo=repo, _active_provider_svc=None
                ),
                signed,
            )
            dirty = {
                **os.environ,
                "OPENAI_API_KEY": "fictional-personal-decoy",
                "HERMES_MODEL": "personal",
            }
            process = subprocess.Popen(
                [sys.executable, __file__, "--worker", str(path), case],
                env=dirty,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if case == "revoke":
                assert request_started.wait(20), "Native request did not reach fixture"
                store.mark_revoked()
            try:
                stdout, stderr = process.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise AssertionError(
                    "Fixture process exceeded bounded lifecycle deadline"
                ) from None
            if case == "success":
                if process.returncode != 0 or "RESULT:success" not in stdout:
                    print(stderr, flush=True)
                    raise AssertionError("Native fixture did not complete")
            else:
                assert process.returncode == 75 and "RESTART:required" in stdout, (
                    process.returncode,
                    stderr[-1500:],
                )
                replacement = subprocess.run(
                    [sys.executable, __file__, "--worker", str(path), case],
                    env=dirty,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                assert replacement.returncode == 0 and "RESULT:blocked" in replacement.stdout, (
                    replacement.stderr[-1500:]
                )
            outputs.append({"case": case, "exit": process.returncode})
        assert all(
            auth == "Bearer fictional-scoped-inference" and body.get("model") == "company"
            for path, auth, body in records
            if not path.endswith("/api/show")
        )
        assert any(body.get("stream") for _, _, body in records)
        server.shutdown()
        print(json.dumps({"PASS": outputs, "requests": len(records), "production_gate": "closed"}))


if __name__ == "__main__":
    worker() if "--worker" in sys.argv else main()
