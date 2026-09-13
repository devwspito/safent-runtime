"""Read-only verification of live image identity and core mounts; no env/secrets."""
import json
import re
import subprocess
from pathlib import Path

podman = "/Users/luiscorrea/.safent/podman/native-v1/bin/safent-engine/podman"
resources = Path("/Applications/Safent.app/Contents/Resources/runtime")
bundle = json.loads((resources / "runtime-bundle.json").read_text())
for file in ("safent", "provision.sh", "compose.yaml", "run-safent.sh"):
    assert (resources / file).read_bytes() == (Path("/Users/luiscorrea/.safent/runtime/6.1.1") / file).read_bytes(), (file, "runtime cache mismatch")
def call(*args):
    return subprocess.check_output([podman, *args], text=True, timeout=30).strip()
def content_id(ref):
    return call("image", "inspect", "--format", "{{.Id}}", ref).removeprefix("sha256:")
engine_id = content_id(bundle["engine_image"]["repo"] + "@" + bundle["engine_image"]["digest"])
ads_id = content_id(bundle["companion_image"]["repo"] + "@" + bundle["companion_image"]["digest"])
db_pin = re.search(r"docker\.io/library/postgres@sha256:[0-9a-f]{64}", (resources / "compose.yaml").read_text()).group()
db_id = content_id(db_pin)
expected_ips = {"api": "10.201.0.10", "db": "10.201.0.11", "broker": "10.201.0.12", "worker": "10.201.0.13"}
preserved_companion_ids = {
    "safent-ads-ads-db-1": "66ea8cf8803ab130dc289f59a5d033226b426a7f559954b1d5e152a73530f44a",
    "safent-ads-ads-api-1": "1761bd8402558aee62c402a2aa157cd80fc1c2e5c08837db1c0c3a27e558a870",
    "safent-ads-ads-worker-1": "d280d112a28dbedd72f73a92890a20cc2fff40560376e7522d0a8513e33e8dc3",
    "safent-ads-ads-broker-1": "720b7caced6969180036cc5c45730b59c85056a24e55a8e82bbc4479c43e15dc",
    "safent-ads-ads-migrate-1": "fe489bfc1cf99934f4915ec3b1ee97291bb9d2246083e0aec8d1eecdeff2bf4d",
}
results = []
for name, expected in [("safent", engine_id), ("safent-ads-ads-db-1", db_id),
                       ("safent-ads-ads-api-1", ads_id), ("safent-ads-ads-worker-1", ads_id),
                       ("safent-ads-ads-broker-1", ads_id), ("safent-ads-ads-migrate-1", ads_id)]:
    fmt = '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.ExitCode}}'
    cid, image, status, exit_code = call("inspect", "--format", fmt, name).split("|")
    if name in preserved_companion_ids:
        assert cid == preserved_companion_ids[name], (name, "companion identity changed on core-only update")
    assert image.removeprefix("sha256:") == expected, (name, "image mismatch")
    if name.endswith("migrate-1"):
        assert status == "exited" and exit_code == "0", name
    else:
        assert status == "running", (name, status)
    for role, ip in expected_ips.items():
        if name == "safent-ads-ads-" + role + "-1":
            actual_ip = call("inspect", "--format", '{{with index .NetworkSettings.Networks "safent-companions"}}{{.IPAddress}}{{end}}', name)
            assert actual_ip == ip, (name, "wrong reservation", actual_ip)
    results.append({"name": name, "id": cid, "status": status, "image_matches": True})
assert call("inspect", "--format", "{{.State.Health.Status}}", "safent-ads-ads-db-1") == "healthy"
assert call("inspect", "--format", '{{with index .NetworkSettings.Networks "safent-companions"}}{{.IPAddress}}{{end}}', "safent") == "10.201.0.2", "core network identity mismatch"
mounts = json.loads(call("inspect", "--format", "{{json .Mounts}}", "safent"))
data = next(m for m in mounts if m["Destination"] == "/var/lib/hermes")
projection = next(m for m in mounts if m["Destination"] == "/etc/hermes/companions")
assert data["Type"] == "volume" and data["Name"] == "safent-data"
assert projection["Type"] == "volume" and projection["Name"] == "safent-companion-runtime" and not projection["RW"]
port = call("port", "safent", "7517")
assert re.fullmatch(r"127\.0\.0\.1:[0-9]+", port), port
assert port == "127.0.0.1:33015", "published port changed"
call("exec", "-u", "0", "safent", "/bin/sh", "-ec",
     "for f in /etc/hermes/companions/ads.bearer /etc/hermes/companions/ads-sso.key /run/hermes/companions/safent-ads.bearer /run/hermes/companions/ads-sso.key; do test -s \"$f\"; done")
call("exec", "-u", "886", "safent", "/bin/sh", "-ec",
     "for f in /etc/hermes/companions/ads.bearer /etc/hermes/companions/ads-sso.key /run/hermes/companions/safent-ads.bearer /run/hermes/companions/ads-sso.key; do test ! -r \"$f\"; done")
print(json.dumps({"containers": results, "port": port, "data_preserved": True,
                  "projection_readonly": True, "agent_uid_cannot_read_secrets": True}, indent=2))
