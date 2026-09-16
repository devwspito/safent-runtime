# CLI update: no implicit host-wide cleanup

CLI revision 12 removes `_reclaim_space` and its two calls from `safent update`.
The removed code claimed to leave other applications untouched but executed
unfiltered `image prune -f` and `builder prune -f`, plus forced removal of older
Safent image IDs. A repository name is not proof that another instance or a
rollback no longer needs that image.

Updating now pulls before recreating this instance, without pruning images,
build cache or volumes. If the pull fails, the existing container is not removed.
Disk maintenance must become its own explicit, scoped operation; no hidden
cleanup fallback is added. This may require the owner to provide more free space
before updating. It does not implement transactional updates or rollback.

## Evidence

Two regressions first failed against revision 11, recording forced `rmi` and
global prune calls on both successful and failed pulls. They pass after removal.
The test launches the actual CLI with a logging engine double and isolated home;
`launchctl`, `systemctl` and `curl` are blocked doubles. No real daemon, image,
volume, service or cache was removed.

- macOS actual sh plus backup/restore suite: **53 PASS**, 11.85s.
- DGX update, backup/restore, porcelain and scoped uninstall: **127 PASS**, 57.22s.
- Ruff, shell syntax and diff checks: PASS.
- Previous complete runtime suite on `373cc8d`: **5870 PASS,19 SKIP,64 deselected**.
  This two-call removal is covered by the focused suite above; those full-suite
  counts are not misattributed to this later commit.

Immutable snapshot `/tmp/safent-update-scope.I7be1L`: complete `373cc8d` plus
`safent` and `tests/unit/ops/test_safent_update_scope.py`; log `update-focus.log`.
