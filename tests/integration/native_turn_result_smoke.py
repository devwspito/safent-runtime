"""Real Hermes 0.21.1 result contract in an isolated --network none RC2 process.

Fresh personal *fixture* profile per case, loopback only, fictional credentials.
Uses the actual Nous factory without changing managed gates. No live inference.
Run with PYTHONPATH=/review/src in the pinned image; output contains only flags.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID


def worker() -> None:
    from hermes.domain.reasoning_failure import NativeTurnFailedError
    from hermes.prompts.persona import PersonaSpec
    from hermes.runtime.model_config import ModelConfig
    from hermes.runtime.native_turn_result import classify_native_result
    from hermes.runtime.nous_engine import NousReasoningEngine
    from hermes.tasks.domain.task_cancel_registry import OperationCancelled

    case = json.load(sys.stdin)
    entered = threading.Event()
    release = threading.Event()
    records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path.endswith("/api/show"):
                self.send_error(404)
                return
            assert body["model"] == "company" and body.get("stream") is True
            records.append(self.headers.get("Authorization") == "Bearer fictional-contract-token")
            entered.set()
            if case == "cancel":
                release.wait(15)
            if case in {"401", "402", "429"}:
                self.send_response(int(case))
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"fictional-secret-error-body","type":"invalid_request_error"}}'
                )
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [({"role": "assistant", "content": "OK"}, None), ({}, "stop")]:
                    chunk = {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "company",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    loop = asyncio.new_event_loop()
    try:
        engine = NousReasoningEngine(
            persona=PersonaSpec(
                name="Fixture",
                role="assistant",
                language="en",
                register="",
                primary_mission="Reply OK",
            ),
            enabled_toolsets=[],
        )
        config = ModelConfig(
            model="custom/company",
            native_provider="custom",
            api_key="fictional-contract-token",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            max_tokens=16,
        )
        agent = engine._build_governed_agent(config, "Reply OK.", loop, UUID(int=1))

        def cancel():
            assert entered.wait(20)
            agent._inner.hard_interrupt("Fixture interruption")

        if case == "cancel":
            threading.Thread(target=cancel, daemon=True).start()
        result = agent.run_conversation("Reply OK.")
        observed = {
            k: result[k]
            for k in (
                "completed",
                "failed",
                "interrupted",
                "partial",
                "failure_reason",
                "failure_retryable",
                "turn_exit_reason",
            )
            if k in result
        }
        try:
            classification = classify_native_result(result)
        except OperationCancelled:
            classification = "cancelled"
        except NativeTurnFailedError as exc:
            classification = "failed"
            observed.update(code=exc.code, retryable=exc.retryable)
            assert "fictional-secret" not in str(exc)
        expected = (
            "completed" if case == "success" else "cancelled" if case == "cancel" else "failed"
        )
        assert classification == expected, (case, observed)
        if case in {"401", "402"}:
            assert observed["retryable"] is False
        assert records and all(records)
        print(
            "NATIVE_RESULT:"
            + json.dumps({"case": case, "classification": classification, "flags": observed}),
            flush=True,
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        loop.close()


def main() -> None:
    from importlib.metadata import version

    import yaml

    assert version("hermes-agent") == "0.21.1"
    with tempfile.TemporaryDirectory(prefix="native-result-contract-") as directory:
        for case in ("success", "401", "402", "429", "cancel"):
            profile = Path(directory) / case
            profile.mkdir()
            (profile / "config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "agent": {"api_max_retries": 1},
                        "providers": {"custom": {"request_timeout_seconds": 3}},
                    }
                )
            )
            environment = {
                "PATH": os.environ["PATH"],
                "PYTHONPATH": "/review/src",
                "HERMES_HOME": str(profile),
                "HOME": str(profile),
                "PYTHONUNBUFFERED": "1",
            }
            process = subprocess.run(
                [sys.executable, __file__, "--worker"],
                input=json.dumps(case),
                env=environment,
                capture_output=True,
                text=True,
                timeout=75,
                check=False,
            )
            lines = [
                line for line in process.stdout.splitlines() if line.startswith("NATIVE_RESULT:")
            ]
            assert process.returncode == 0 and len(lines) == 1, (
                f"Native case {case} failed (raw SDK output withheld)"
            )
            print(lines[0], flush=True)


if __name__ == "__main__":
    worker() if "--worker" in sys.argv else main()
