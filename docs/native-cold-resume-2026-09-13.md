# Native Community — cold restart of core and Ads

## Reproduction on the installed 0.9.14

All managed containers were stopped after the VM had stopped. Core image and
data were intact. Read-only Podman 6.1.1 inspection on the Mac confirmed:

- `podman port <core-id> 7517` returned exit 0 and an empty string.
- `HostConfig.PortBindings` retained exactly `127.0.0.1:33015` for `7517/tcp`.
- The live `NetworkSettings.IPAddress` was empty and `IPAMConfig` was null;
  network membership and the original static `--ip 10.201.0.2` were retained.
- Native UI reported `companion_network_conflict` in `container` stage.

The planner correctly requested StartContainer, but the CLI's `up` only reused
running cores. It attempted replacement, and its guard rejected the empty
active-port response. The failure was a cold-start implementation defect, not a
requirement for the user to install or configure another container engine.

After the parent task resumed the verified existing core, a second cold-start
defect surfaced: the Ads migration guard queried an existing but stopped DB
before Compose started it. `set -euo pipefail` aborted the script on that query.
The UI correctly stopped rather than pretending that Ads was ready.

## Changes

- Read the saved loopback port only when the active port lookup succeeds but is
  empty; reject wildcard bindings, multiple bindings, invalid ports and drift.
- Resume a stopped same-image core by immutable ID after validating its image
  content, data volume, read-only private projection, network membership and
  recorded static address. Recheck identity immediately before start, then
  require the actual running image, projection and address. No recreation
  fallback if resuming fails. Real image upgrades retain the guarded replacement
  path. The explicit engine-only CLI mode remains unchanged.
- Quote creation arguments before checking network flags; never execute or log
  the recorded command. Dynamic/live IP alone cannot establish a stopped core's
  configuration.
- Resolve network reservations before Ads mutations. Start only `ads-db` using
  Compose `--no-recreate`, wait for PostgreSQL readiness (45 bounded checks),
  then verify schema/image compatibility before starting migrations or other
  Ads services. The bundled Mac Compose supports `--no-recreate` (help checked).
- A missing version table is distinguished from a failed database query. A
  known revision with unreadable or incompatible image history refuses startup;
  restarting does not bypass downgrade protection.

No app version, tags, images, credentials, provider authorization, network,
volume or installed signed bundle was changed by this source-fix subtask.
The CLI revision changes from 14 to 15 for normal launcher distribution.

## Regression evidence

The first 16 new core tests ran against the old source: 3 failed, 13 passed.
Against the fix all 16 passed; a further image-content mismatch rejection was
added. DB regression tests against the old source: 5 failed, 6 passed; all 11
passed against the fix. These execute the production shell functions with
closed, stateful runtime fixtures; they do not stand in for packaged Mac QA.

Additional targeted validation: 24 cold-resume/image convergence tests passed;
32 database/network/order tests passed; 7 CLI protocol/reopen tests passed.

An intermediate broad run produced 178 passes and two invalid-environment
failures: a running shell read a file while it was being copied, and the shared
virtualenv's editable install pointed at a different repository. Source files
were frozen and the suite was repeated with explicit
`PYTHONPATH=/home/luiscorrea-dev/Desktop/lumen-runtime-next/src`.
The clean final run passed **180 tests, 0 failures, in 225.91 seconds** across
`test_agent_install_request.py`, `test_companion_provision.py`,
`test_companion_core_order.py`, `test_companion_network_reservations.py`, and
`test_companion_cold_database.py`. Shell syntax and `git diff --check` also passed.

## Remaining acceptance

Publish a new signed native release carrying these source changes, and test a
real VM stop/restart with all services stopped. Require the same valid core/DB
IDs, port, volumes and business data after recovery; no manual start commands
should be needed. Reopening a window while containers are already running is a
separate test and does not prove this cold-restart case.

Factory OAuth is a separate integration track and is not completed by this fix.
