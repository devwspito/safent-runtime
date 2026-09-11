# Native backup archive containment — CLI revision 11

This follow-up to `native-backup-guards-2026-09-11.md` protects host-side
restoration. No user's archive, live container or real volume was restored.

## Host-side changes

- Copy the input into the private work directory before validation, then require
  exactly three unique, regular outer members: `manifest.json`, `data-volume.tar`
  and `state.tar`. Unknown, duplicate, absolute, dot-component, link and special
  members fail before stopping the runtime or replacing its volume.
- Read those fixed members to stdout into files chosen by the CLI. The tar
  implementation never gets to materialize archive-controlled paths on the host.
- State accepts only canonical ASCII directories/regular files under
  `companions/ads` and the fixed `safent-seccomp.json`. No symlinks, hardlinks,
  devices, pipes, duplicate canonical members or regular-file ancestors. A
  backup containing unsupported state is rejected rather than reported usable.
- Validate existing destination ancestors before destructive operations. A
  symlink ancestor/target or file/directory collision rejects the restore.
- Restore state files through private temporary siblings and atomic replacement,
  so an existing hardlink is not overwritten through to its other name. Do not
  apply archive ownership, ACLs, xattrs or special/world-readable mode bits.
  Files are 0600; companion shell scripts under `bin` are 0700; new directories
  inherit umask 077. Existing directory permissions are not rewritten.

This deliberately constrained format uses actual BSD/GNU tar listings, accepting
only the type character and a separate strict ASCII name list. It does not parse
filenames from verbose owner/date columns. Content is always streamed to
CLI-selected destinations, not bulk-extracted.

## Verification

- macOS actual BSD tar, sh, checksums and filesystem: **51 PASS** (10.93s).
  Python 3.12 with isolated pytest/pytest-asyncio, no project installation.
  The initial Mac run exposed the Linux-only engine double's missing `machine`
  replies (11 failures at startup); the double now models an already-running
  rootful test machine. Neither `uname` nor the host archive tools are mocked.
- DGX GNU tar: **125 focused PASS** (57.03s), covering backup/restore,
  porcelain and scoped uninstall. Container commands only reach the logging
  engine double; no Podman volume or machine is mutated.
- Ruff on the modified test, `sh -n safent`, and `git diff --check`: PASS.
- Full runtime suite: **5870 PASS, 19 SKIP, 64 deselected**, 261.25s, 7 warnings.
  Skips retain the explicit host SDK/template/release-only limitations documented
  in the preceding runtime reports; they do not certify the final image.

Immutable DGX snapshot `/tmp/safent-backup-containment.6MUnp4`: complete archive
of runtime `3b3f6ab`, overlaid only with `safent` and the backup/restore test file.
Logs: `containment-focus.log`, `containment-full.log`.

## Remaining release gates

Checksums are not signatures. This does **not** certify arbitrary untrusted
`data-volume.tar` contents: those are passed to the container engine's volume
import implementation and its extraction safeguards remain a separate boundary.
Do not use untrusted backups. Compressed/expanded archive size and member-count
resource limits are not introduced here; use owner-controlled input and enough
scratch space for the private compressed copy plus extracted component archives.

No cross-process lifecycle lock or defense against a concurrent same-user host
filesystem attacker is claimed. No coordinated Ads PG/credential-store backup,
crash-safe update journal, rollback or native all-component installation proof
is added. A partial restoration can still require manual recovery; failure stays
visible and does not launch the runtime after state restore failure. The native
updater and final Community image remain gated on the complete release checks.
