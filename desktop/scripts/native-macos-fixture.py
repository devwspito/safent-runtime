#!/usr/bin/python3
"""Explicit disposable QA boundary for a REAL Tauri binary, never production.

CLI speaks the existing porcelain/secret-fd protocol. HTTP serves the unmodified
compiled Community UI with fictional state. No Tauri IPC shim, Podman, provider,
credentials or external network. This certifies native presentation/transport,
NOT backend execution or approval authority. Unknown operations fail closed.
"""

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def emit(value):
    print(json.dumps(value), flush=True)


def config(root):
    return json.loads((root / "native-qa.json").read_text())


def cli():
    root = Path(os.environ["SAFENT_STATE_HOME"])
    current = config(root)
    if current.get("fixture") != "safent-native-qa-v1":
        raise SystemExit("Missing explicit disposable QA marker")
    verb = sys.argv[1]
    if verb == "facts":
        ready = current["mode"] == "ready"
        emit({"t": "facts", "facts": {
            "os": "darwin", "arch": "arm64", "freeDiskBytes": 21474836480,
            "totalMemoryBytes": 17179869184, "runtimeStaged": True, "runtimeHashOk": True,
            "machines": [{"name": "safent-engine", "provider": "applehv", "cpus": 4,
                          "memoryBytes": 8589934592, "rootful": True, "running": True, "ours": True}],
            "engineContainer": {"exists": False, "running": False, "imageDigest": None},
            "localEngineImageDigest": "sha256:fixture-only" if ready else None,
            "localCompanionImageDigest": None, "publishedPort": current["port"],
            "dataVolume": True, "companionScaffold": False,
            "companionContainers": {"running": 0, "total": 0}, "companionHealth": "unknown",
            "daemonHealth": "healthy", "appVersion": "0.9.0", "userNsAllowed": True,
            "helperInstalled": True,
        }})
    elif verb == "ensure-images":
        emit({"t": "stage", "id": "pull_engine", "label": "Descarga ficticia de QA", "total_bytes": 100})
        for progress in range(100):
            current = config(root)
            if current["mode"] == "ready":
                emit({"t": "done", "id": "pull_engine", "ms": 1})
                return
            if current["mode"] == "fail":
                emit({"t": "failed", "id": "pull_engine", "code": "registry_unreachable",
                      "detail": "Fallo de red ficticio de QA", "retryable": True})
                return
            emit({"t": "progress", "id": "pull_engine", "done": progress, "total": 100, "unit": "bytes"})
            time.sleep(1)
        raise SystemExit(1)
    elif verb == "up":
        emit({"t": "stage", "id": "container", "label": "Servidor ficticio local"})
        emit({"t": "done", "id": "container", "ms": 1})
        os.write(3, ("http://127.0.0.1:%s/app/?k=native-qa-only\n" % current["port"]).encode())
        emit({"t": "ready", "endpoint_ref": "secret-fd"})
    elif verb == "stop":
        emit({"t": "done", "id": "container", "ms": 1})
    else:
        raise SystemExit("Unsupported fixture verb")


def serve():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--frontend", required=True, type=Path)
    args = parser.parse_args()
    root, frontend = args.state.resolve(), args.frontend.resolve()
    if not str(root).startswith("/private/tmp/safent-native-macos.") and not str(root).startswith("/tmp/safent-native-macos."):
        raise SystemExit("Refusing a non-disposable state directory")
    if not (frontend / "index.html").is_file():
        raise SystemExit("Build the actual Community frontend first")
    root.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    state = {"fixture": "safent-native-qa-v1", "mode": "slow", "model": False,
             "reconnect": False, "approval": False, "cancelled": False}
    conversations = {}
    task_id = str(uuid.uuid4())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/__qa/state":
                return self.reply(state)
            if path.startswith("/app/"):
                file = (frontend / path.removeprefix("/app/")).resolve()
                if not file.is_relative_to(frontend):
                    return self.reply({}, 403)
                if not file.is_file():
                    file = frontend / "index.html"
                body = file.read_bytes()
                if file.name == "index.html":
                    body = body.replace(b"<head>", b'<head><script>window.__SAFENT_TOKEN__="native-qa-only"</script>')
                    body = body.replace(b"<body>", b'<body><div style="position:fixed;right:14px;top:5px;z-index:99999;font:10px system-ui;color:#d7af70;pointer-events:none">QA NATIVA - DATOS FICTICIOS</div>')
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(file))[0] or "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if state["reconnect"]:
                return self.reply({"detail": {"code": "unauthorized"}}, 401)
            if path.startswith("/api/v1/chat/stream/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for seq, word in enumerate(["Respuesta ", "ficticia ", "en ", "WKWebView. "] * 10):
                        if state["cancelled"]:
                            break
                        self.wfile.write(("data: " + json.dumps({"kind": "delta", "delta": word, "seq": seq}) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(2)
                    self.wfile.write(("data: " + json.dumps({"kind": "done", "seq": 100, "cancelled": state["cancelled"]}) + "\n\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            responses = {
                "/api/v1/instance/features": {"edition": "community", "views": []},
                "/api/v1/providers": [{"provider_id": "fixture", "name": "Modelo ficticio QA", "is_active": True}] if state["model"] else [],
                "/api/v1/agents": [{"id": "default", "name": "Safent", "role": "assistant", "is_default": True,
                                    "instructions": "", "primary_mission": "QA ficticia", "language": "es", "color": "#888888", "golden_rules": [], "autonomy_level": "assisted"}],
                "/api/v1/agents/active": {"active_agent_id": "default"},
                "/api/v1/runtime/status": {"state": "idle", "active_task_count": 0, "activity": []},
                "/api/v1/security/kill-switch": {"engaged": False},
                "/api/v1/system/requests": {"requests": []},
                "/api/v1/system/version": {"update_available": False, "current_version": "0.9.0"},
                "/api/v1/chat/conversations": [{"id": key, "title": "Chat ficticio QA"} for key in conversations],
                "/api/v1/tasks/dashboard": {"available": True, "has_more": False, "tasks": [{"task_id": "native-qa-history", "label": "Recorrido nativo ficticio", "status": "completed", "source": "local", "result": "Este resultado es una fixture, no una ejecución Hermes."}]},
                "/api/v1/inbound-delegations": [],
                "/api/v1/approvals/pending": [{"proposal_id": "native-qa-approval", "summary": "Acción ficticia para revisar permisos", "target": "fixture://sin-efectos", "route": "local", "created_at": datetime.now(timezone.utc).isoformat()}] if state["approval"] else [],
                "/api/v1/notifications/unread-count": {"count": 0},
                "/api/v1/workspace/files": [], "/api/v1/mcp/servers": [], "/api/v1/skills": [],
            }
            if path.startswith("/api/v1/chat/conversations/"):
                key = path.rsplit("/", 1)[-1]
                return self.reply({"id": key, "messages": conversations.get(key, [])})
            if path in responses:
                return self.reply(responses[path])
            return self.reply({"detail": {"code": "qa_route_unavailable"}}, 503)

        def do_POST(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            with (root / "requests.jsonl").open("a") as log:
                log.write(json.dumps({"method": "POST", "path": path}) + "\n")
            if path == "/__qa/control":
                with lock:
                    for key in ("mode", "model", "reconnect", "approval"):
                        if key in body:
                            state[key] = body[key]
                    (root / "native-qa.json").write_text(json.dumps(state))
                return self.reply(state)
            if state["reconnect"]:
                return self.reply({"detail": {"code": "unauthorized"}}, 401)
            if path == "/api/v1/chat":
                state["cancelled"] = False
                conversations[body["conversation_id"]] = [{"role": "user", "content": body["user_message"]}]
                return self.reply({"task_id": task_id})
            if path.endswith("/cancel"):
                state["cancelled"] = True
                return self.reply({"ok": True, "status": "cancelled"})
            if path == "/api/v1/approvals/native-qa-approval":
                state["approval"] = False
                return self.reply({"ok": True})
            return self.reply({"detail": {"code": "qa_route_unavailable"}}, 503)

    existing_port = config(root)["port"] if (root / "native-qa.json").exists() else 0
    server = ThreadingHTTPServer(("127.0.0.1", existing_port), Handler)
    state["port"] = server.server_port
    (root / "native-qa.json").write_text(json.dumps(state))
    emit({"fixture": state["fixture"], "port": server.server_port})
    server.serve_forever()


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else cli()
