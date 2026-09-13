# Core / companion topology recovery

## Failure and bounded correction

Native 0.9.12 acceptance found a healthy rebuilt core at `10.201.0.14` on
the legacy unbounded `safent-companions` network. Ads reserves `.10`–`.14`;
the migration correctly refused to remove that core. Health and image pins
alone did not describe this topology defect.

The bundled `safent` CLI now gives the core explicit `.2`, outside those
reservations. It never changes the existing subnet, IPAM pool, Ads Compose
model, migration reservation, database volume or another container.

- `_core_network_matches` participates in desired-container reuse and
  companion health facts. Old topology cannot report companion reachable.
- `_run` checks the existing core's immutable ID, digest-pinned Safent image
  reference and actual image ID, expected named data volume and exactly one
  loopback port. It preserves that volume and port.
- Before removal it checks `.2` against all observed containers. A foreign
  holder or unverifiable inventory fails closed. It rechecks the core
  identity and removes only the observed ID, never a potentially replaced
  name. No volume is removed.
- Install/repair now runs scaffold → core convergence → Ads provision →
  topology/image checks → authenticated registration. The final topology
  check is read-only; a changed core cannot cause a second late recreation.
- Scaffold failure now aborts before removal when the companion is required.
  Explicit `--no-companion` remains available. Core convergence remains
  inside the open scaffold phase, so its heartbeat cannot update a phase
  already marked done.

## Verification

The new CLI regression first failed because healthy core `.14` still
reported companion `reachable`. It now checks install and repair, custom
data volume and port preservation, convergence before Compose, second-pass
idempotence, occupied `.2`, foreign image, bind data instead of named volume,
ambiguous port bindings and identity change before removal.

- `test_agent_install_request.py`: 43 passed, 206.96 seconds, before the
  final scaffold-failure guard and stage-boundary adjustment.
- Porcelain/scaffold/order regression: initially 77 passed and one fixture failure.
  The fresh-bundle test incorrectly declared an existing container without
  any image/data identity. It now explicitly starts without a container,
  asserts no removal and checks `.2`; the entire group rerun passed:
  78 passed, 72.09 seconds.
- Two final scaffold-guard tests passed (0.20 seconds). Their first attempt
  used `start` on an already-running fixture and did not reach `_run`;
  corrected tests use native `up` on an existing stopped core. No production
  behavior was changed to satisfy that fixture mistake.
- Final combined identity/topology/projection/scaffold focal on the final
  production source: 15 passed, 30 deselected, 51.32 seconds.
- `test_companion_core_order.py`: four independent production-function tests
  check exact ordering and prohibit provision/registration after failure.
- `sh -n safent` and `git diff --check` passed. Existing CLI test files have
  repository lint debt; this is not a claim of whole-file Ruff success.

Independent real-Podman QA passed on DGX, log
`/tmp/safent-core-network-qa.WPGrHY/results-final-v2.log`: legacy dynamic
core `.14` upgraded to another image at `.2`; published port, core/DB data
sentinels and all four Ads service identities/states survived. Migration
could then use `.14`, and two reopen passes retained the core ID. A foreign
holder of `.2` caused failure before mutation; all identities, mounts and
network state stayed unchanged. The harness used real Podman with inert
processes, not a running application/LLM. Its 13 containers, two networks
and six volumes were removed by the reviewing agent (QA resources only).

The final guard/stage changes do not alter that successful recreation path;
their separate tests are reported above rather than claiming a second real
Podman acceptance run.
No Mac installation, release, tag, version metadata or image was changed by
this patch. Rust/wire schemas are unchanged; existing native reconciliation
consumes the more accurate CLI facts and closed verbs.

## Limits

Runtime inspection and container creation are not one atomic transaction.
A concurrent external actor can race an address acquisition; the runtime's
IPAM must reject duplicate assignment. Data remains in its named volume and
the failure is not reported as Ready. This is not a general multi-core IP
allocator: another holder of `.2` requires explicit conflict resolution,
not automatic removal or dynamic fallback. Real signed macOS acceptance
remains a release gate, distinct from the ephemeral Podman checks.
