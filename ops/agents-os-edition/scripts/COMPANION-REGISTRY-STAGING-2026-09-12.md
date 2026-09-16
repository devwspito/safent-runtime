# Companion registry: root source → protected runtime snapshot

The launcher projects only companion configuration/credentials into a Linux
volume mounted read-only at `/etc/hermes/companions`. This is not a raw host
virtiofs secret bind: caller-dependent UID mapping made the latter unsafe for
secret isolation. Volume creation/projection belongs to the launcher change,
not these files.

- Source: `/etc/hermes/companions/companions.json`. Root bearer staging and early
  nft setup call the **same existing validator with this explicit path**.
- Daemon default: `/run/hermes/companions/companions.json`. No source fallback or
  environment override. Require regular file, UID0, mode0440, non-symlink
  root-owned directory with no group/other writes. Existing source trust rules
  remain intact; daemon-owned readonly files are still rejected.
- Root stage purges old registry/bearer/SSO before reading the source, stages
  only valid endpoints with a readable current bearer, then atomically publishes
  the sanitized JSON last. Missing/invalid source, missing bearer or partial
  staging failure cannot preserve the old credentials. New tempfiles are
  exclusive, 0440 root:hermes, flushed and replaced atomically.
- CA path stays `/etc/hermes/companions/ads-ca.crt`; daemon fingerprint validation
  still rereads it. The public CA must remain daemon-readable (0644 root:root).
  Source registry/bearer/SSO remain root-only; staged directory0750 root:hermes.

Validation: **129 PASS** on DGX Python3.12/pytest9.0.2, including source-trust UID
simulation, injected staged owner/mode/symlink/parent rejection, explicit nft
source call, rotation/failure purge, SSO, health and reload regressions. Ruff
PASS for loader/stager/new tests; nft source change imports/constants compiles
(its pre-existing SIM105 style warning is unchanged). No root-service restart,
VM modification, real token use or image publication in this subtask.

```sh
PYTHONPATH=src python3 -m pytest \
 tests/unit/agents_os/test_companion_sso_assertion.py \
 tests/unit/agents_os/test_companion_health_check.py \
 tests/unit/agents_os/test_companion_health_contract.py \
 tests/unit/agents_os/test_companion_reload.py \
 tests/unit/shell_server/test_companion_registry_staging.py \
 tests/unit/shell_server/test_companion_bearer_staging.py \
 tests/unit/shell_server/test_companions.py -q --tb=short
```

Scratch `/tmp/safent-registry-test.B6XD6J`: full HEAD archive plus owned files.
Tests mock root ownership/chown where required; they do not claim independent
full VM UID-isolation validation. The coordinator owns that real volume test.
