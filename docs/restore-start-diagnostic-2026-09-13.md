# Restore startup diagnostic follow-up

This change is separate from native 0.9.13 (`ed41934`) and its fixture-only
full-suite snapshot (`62a5f65`). It does not change a tag, image or release.

## Demonstrated defect

After importing a valid backup, `cmd_restore` invokes `cmd_start` with its
output suppressed. Fatal startup guards call `exit`, so the same-shell
invocation could terminate before restore's existing postcondition check.
The command failed nonzero and did not claim success, but its last visible
message was only “Starting Safent”. There was no additional data deletion.

The new regression supplies a local bundle whose scaffold deliberately
fails. Before the change it reproduces the missing diagnostic. It also
asserts one data import, no volume removal and no core creation.

## Bounded correction

Run only `cmd_start` in a subshell. Its fatal exit cannot bypass restore's
existing `_exists` / `_running` check. The original verified failure now
reports that data was imported but no Safent container is running. Startup
guards, companion requirements, data handling and success checks are not
weakened. No raw startup stderr or bootstrap ticket is exposed.

- Red: one failing new regression, 51 deselected.
- Green: entire backup/restore file, **52 passed in 2.94 seconds**.
- `sh -n safent` and `git diff --check`: passed.
- Logs: `/tmp/safent-restore-diagnostic-red.log` and
  `/tmp/safent-restore-diagnostic-final.log` on DGX.

This is a CLI failure-message fix, not native application acceptance or a
new backup format. The ongoing 0.9.13 full suite intentionally does not
include this source delta; its result must not be attributed to this commit.
