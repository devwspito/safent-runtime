# Native CLI backup/restore — failure guards, not transactional updater certification

`safent` CLI revision10 corrects reproducible false-success and destructive
continuation paths. No real backup, restore, container stop or volume replacement
was executed against the user's installation.

## Changed

- Backup and restore require an explicitly stopped container before export or
  volume replacement. Engine enumeration/inspection failure is not proof of
  stopped state. A failed stop cannot proceed into these data operations.
- Restore aborts if removing the existing volume fails, instead of importing over
  it after swallowing the error. An unreadable companion-state tar is rejected
  before stopping/removing anything; extraction failure is no longer suppressed.
- Backups are created under umask077 and remain0600 from their first byte, not
  only after tar completes. Existing destination files/symlinks are not
  overwritten when timestamp names collide. Relative output paths are resolved
  before changing to the temporary work directory.
- If the runtime was running before backup but cannot restart, the CLI reports
  failure and the retained backup path, not a blanket success. An initially
  stopped runtime is not unexpectedly started.

## Evidence

First regression run against old code: **3 FAIL, 11 PASS**, reproducing export
despite failed stop, restore despite failed stop and false success after failed
restart. Final focused real CLI/host tar suite: **74 PASS**,54.33s. Full runtime:
**5839 PASS,19 SKIP,64 deselected**,246.83s,7 warnings. Host skips are the same
explicit SDK/template/release-only limitations as the previous bridge report.
Ruff for the changed test file, POSIX shell syntax and diff checks pass.

The tests launch the actual CLI and host tar/checksum tools. Only the container
engine is a stateful logging double; no engine mutation reaches the user's
Podman. An additional tar wrapper records permissions before the CLI's final
chmod. Fixtures cover destination collision/symlink, unavailable engine, failed
stop/restart/removal, malformed state archive with a matching checksum, normal
backup/restore and porcelain behavior.

Final immutable snapshot `/tmp/safent-backup-final.myt8AX`: complete archive of
`66da720` plus `safent` and `tests/unit/ops/test_safent_cli_backup_restore.py`.
Logs `backup-focus.log` and `full-backup.log`. Earlier snapshots and partial
focused results are diagnostics, not the final integrated evidence.

## Still required before native all-component updates

This CLI backup currently covers the runtime data volume and companion local
configuration/seccomp files. It does **not** export the companion PostgreSQL or
credential-store named volumes. Ads has existing `ops/backup.sh`/`ops/restore.sh`
for those resources; their orchestration must be reused and tested with the
runtime backup before claiming a complete app+engine+Ads restore point.

There is no cross-process lifecycle lock, immutable update journal across app
relaunch, final-version health proof or automatic rollback here. A concurrent
external container start is outside the stopped-state snapshot guarantee.
Restore checksums detect corruption, not a trusted signature; complete hostile
archive member/path/type validation and interrupted-restore recovery remain open.
Use only owner-controlled backup directories and trusted backups. These guards
must not be used to enable the currently unavailable native updater or publish
the final Community image.
