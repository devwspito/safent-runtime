"""Local read-only health probe with synthetic canaries; never print raw logs."""
import json
import subprocess
import urllib.request
import uuid

podman = "/Users/luiscorrea/.safent/podman/native-v1/bin/safent-engine/podman"
def call(*args):
    return subprocess.check_output([podman, *args], text=True, stderr=subprocess.STDOUT, timeout=30)

port = call("port", "safent", "7517").strip()
assert port.startswith("127.0.0.1:")
canary = "SAFE_SYNTHETIC_CANARY_" + uuid.uuid4().hex
url = "http://" + port + "/healthz?code=" + canary + "&state=" + canary
with urllib.request.urlopen(url, timeout=15) as response:
    assert response.status == 200
    response.read()
logs = call("exec", "-u", "0", "safent", "journalctl", "--unit", "hermes-shell-server.service",
            "--since", "30 seconds ago", "--no-pager", "--output", "cat")
assert canary not in logs, "Synthetic query reached HTTP logs"
assert 'GET /healthz HTTP/1.1' in logs, "Expected sanitized HTTP evidence missing"
print(json.dumps({"health": 200, "synthetic_query_not_logged": True, "http_evidence_preserved": True,
                  "source": "hermes-shell-server.service journal"}))
