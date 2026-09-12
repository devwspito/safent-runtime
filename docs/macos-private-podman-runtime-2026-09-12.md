# macOS: private Podman runtime and Compose

Base: canonical DGX `fix/safent-review-20260911`, `0b94f3f`; staging changes
rebased on `6f8a83c`. No release/tag/publication or installed-app replacement.

## Boundary

Every macOS Podman call made by `safent`, including legacy start/up/repair
and the separate companion provisioner, uses a generated private executable
transport. The parent Safent HOME is unchanged. The child has:

| Input | Effective value |
| --- | --- |
| XDG config/data/cache | `SAFENT_STATE_HOME/podman/native-v1/{config,data,cache}` |
| Containers/storage/registries config | Explicit private files; no inherited overrides/drop-ins |
| Connections | `native-v1/connections.json` |
| Registry auth / Docker config | `native-v1/auth.json`, `native-v1/docker` |
| TMPDIR and XDG runtime | Short mode0700 `/tmp/safent-pm-<uid>-<profile-checksum>` (physical path) |
| Child HOME | `<private-runtime>/home` (short socket links, not the user's HOME) |
| Machine provider | `applehv` for every command, not only init |
| Container API identity | Exactly `<MACHINE_NAME>-root`; absence is an error, never default fallback |
| Compose | Exactly `<bundle>/docker-compose`, no inherited provider or PATH search |

The temporary runtime has a profile marker and owner/symlink checks. A checksum
collision fails closed, never adopts another profile. Machine names whose
ignition socket would exceed Podman's 103-byte limit fail before machine calls.
Per-name transports avoid overwriting another named install's connection.
Persistent machine keys remain in private XDG_DATA_HOME, not temporary HOME.

Inherited container/Docker endpoints, SSH agent/key/proxy selectors, config
overrides, storage selectors, HTTP proxies and certificate overrides are removed.
The child's PATH contains its transport, the bundle and OS system directories.
Machine records can select only this install's exact name, including uninstall.
Named running state AND a responding exact private connection are required for
successful ensure-machine. No unrelated VM is stopped automatically.

The runtime requires `podman-private-build.json` capability
`safent-private-machine-v1`, schema1, matching the actual binary SHA256. The
sidecar alone is not a trust root: staging validates the pinned build and the
native bundle's signature seals it. Stock Podman lacks the required guarantee:
its helper claim code uses the OS username and a global socket independently of
HOME/XDG. The separately reviewed private-build patch disables that claim.

This creates a new private namespace. Existing user/global machines, connections,
images and volumes are neither imported nor deleted; old Safent data in that
namespace is not automatically migrated. Normal Podman macOS shared host mounts
remain available for the existing product workflow; this is client/control-plane
isolation, not a new host-filesystem sandbox or a defense against the same user
deliberately replacing Safent's own writable files.

## Compose and actual companion provisioning

Docker Compose **5.5.1**, darwin-aarch64, official release binary:
SHA256 `998735c9b6fe68a4f05895e6ea73d71ad06f9fc7046383ad89e47346781b6af5`,
30,532,210 bytes. The lock pins the executable, Apache-2.0 license, upstream SBOM
and provenance JSON; staging uses the existing verified downloader for all four.
The provenance is distributed with pinned integrity, not claimed as independently
verified Sigstore identity. The binary is covered by normal native signing.

Real Mac QA also fixed two existing provisioner defects: `${COMPANION_HOST}...`
avoids bash3.2 interpreting a following Unicode ellipsis as part of the variable;
the host health probe uses the published loopback port on Darwin, retaining the
private CA and original TLS hostname (and bypassing host proxies). Linux retains
the bridge IP. No TLS verification was disabled.

## Evidence

- DGX scratch `/tmp/safent-podman-isolation.3FURM3`: **221 PASS** (CLI,
  companion provisioning, backup/restore and three Compose staging tests).
  Command: `PYTHONPATH=src /home/luiscorrea-dev/Desktop/safent-ads/.venv/bin/python -m pytest tests/unit/cli tests/unit/ops/test_companion_provision.py tests/unit/ops/test_safent_cli_backup_restore.py desktop/scripts/tests/test_compose_provider.py -q`.
- Rust **225 PASS**: 137 binary unit tests +48 CLI contract +40 real-shell
  contract tests. `cargo test --test engine_adapter_real_cli_contract --test engine_adapter_cli_contract --bin safent-desktop`.
- Real Mac, patched Podman6.1.1 build
  `7e0a99204954942f1a1145f09a682eeb45eea543393aad9a2ee69f1b2cc10716`:
  fresh private inventory/connections empty; `info` rejected missing exact
  connection even with hostile inherited endpoints. Then QA applehv VM started,
  private root `info` succeeded while the user's libkrun default stayed running.
- Actual packaged Compose path reported5.5.1. Real `provision.sh` pulled public
  Ads **v0.2.2**, migrated PostgreSQL and started broker/API/worker. The final
  provisioner and independent CA-verified probe both returned `/mcp/health`401.
  No vendor OAuth or Ads accounts were configured; spend caps remained empty.
- User default helper PIDs **20042/20043** remained unchanged before/during/after.
  User connection JSON SHA256 stayed
  `9281be5693757e4d9ef903e7e2bccd1b7d76c06a9f2981583c1eb2023bf87c38`;
  default machine config stayed
  `971013d3463c4a61116ccbca453020ec6dd2fca8cb0085e36612861c94722ce8`;
  old global safent-engine config stayed
  `db45671f73acb547e77063cbff8a8fc12b1482785c957f6bfc1cb085e08d90b5`.
- Only QA `safent-isolation-qa-engine` was stopped/removed through its private
  transport. No global Podman connections, sockets or machines were mutated.
  Remaining QA state and generated QA credentials were moved, recoverably, to
  `~/.Trash/safent-podman-qa-C8N0qH`; no vendor credentials were used.
  The entire installed native app was not rebuilt/notarized in this cut.

## Primary sources inspected 2026-09-12

[Podman6.1.1 machine directories](https://github.com/containers/podman/blob/v6.1.1/pkg/machine/env/dir.go),
[Darwin TMPDIR](https://github.com/containers/podman/blob/v6.1.1/pkg/machine/env/dir_darwin.go),
[HOME socket shortening](https://github.com/containers/podman/blob/v6.1.1/pkg/machine/define/vmfile.go),
[global helper claim](https://github.com/containers/podman/blob/v6.1.1/pkg/machine/shim/claim_darwin.go),
[Compose connection routing](https://github.com/containers/podman/blob/v6.1.1/cmd/podman/compose.go),
[official Compose5.5.1 release](https://github.com/docker/compose/releases/tag/v5.5.1).
Source checkout verified at Podman commit
`8303f2e25b675ea7f82099d615c60969aec15870`.
