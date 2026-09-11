# LLM configuration: serialization across local processes

Scope: daemon/shell configuration commits and Enterprise association writers.
This change does **not** enable managed inference, change the execution gate,
or implement a Hermes process restart/isolation lifecycle.

## Reproduction and correction

The former `threading.RLock` serialized threads in one interpreter only. Two
spawned processes entered `local_configuration_write` simultaneously. A second
reproduction paused a genuine signed apply after signature verification; another
process unpaired the instance before the old snapshot finished applying.
Both tests failed on the original sources (2 failed in 0.78 seconds).

| Before | After | Reason |
| --- | --- | --- |
| Interpreter-only lock | Bounded POSIX `flock` plus per-path reentrant thread lock | Daemon, shell and config-sync coordinate on the same database |
| Association/signature checked before lock | Current association, tenant, instance, signing key, origin and freshness checked while locked | Unpair/revoke/key rotation cannot interleave with signed application |
| Association mutations independent of LLM commits | Constructor and save/revoke/clear/license/directory/version writers share the lock | Prevent stale signed state from being reinserted after authority removal |
| Last-applied version could decrease | Monotonic SQL predicate, active association required | A delayed old update cannot lower the replay floor |
| Unpair used individual autocommit statements | Association, managed policy and cloud provider deletion in one SQLite transaction | Authority removal is atomic; checkpoint/VACUUM follow outside that transaction |

`local_configuration_write` already surrounds local provider/settings and OAuth
credential commits. Their network polling remains outside the lock. Signed apply
also requires provider and association repositories to refer to the same DB.

## Lock contract and operational limits

- New neutral helper: `hermes.security.configuration_lock.configuration_lock`.
- Default acquisition timeout 5 seconds; explicit finite timeout 0–30 seconds.
  Failure is a safe `ConfigurationLockError` (`PermissionError`), not a fallback.
- Stable empty `<database>.configuration.lock`, owner-only `0600`; never removed
  on release. No tokens, PIDs, credentials or request payloads are written there.
- Immediate parent must be owned by the effective user and not group/world
  writable. DB final symlinks/hardlinks and unsafe lock files (including FIFO,
  wrong permissions, nonempty contents and link substitution) fail closed.
  Parent aliases such as macOS `/tmp` canonicalize to one locking identity.
- Directory-relative `O_NOFOLLOW` opens and post-acquisition inode checks guard
  the lock file. Thread reentry permits existing nested SQLite repository calls
  without holding a SQLite transaction in the lock helper.
- Process termination closes the descriptor and releases the kernel lock. Fork
  hooks close only inherited child descriptors, **never** issue `LOCK_UN` against
  the parent's shared open-file description; inherited Python locks are replaced.
- POSIX local filesystem only. No distributed/NFS locking claim, no Windows
  thread-only downgrade. A hostile process running as the database owner is not
  isolated by this mechanism and can already modify the database itself.
- This protects cooperating guarded writers, not arbitrary SQL or new callers
  that bypass the configuration boundary. It does not retroactively erase
  previously exported credentials or backups.

## Verification

Final snapshot: runtime HEAD `e572eeb` plus only the four owned source/test files,
without unrelated frontend work. DGX scratch:
`/tmp/safent-llm-lock-final.vbmBEW`; interpreter `/usr/bin/python3`, Python 3.12.3,
pytest 9.0.2. All secrets and associations in tests are fictional; no real model,
cloud API, production database or deployment was touched.

Focused command (66 passed in 6.49 seconds):

```sh
cd /tmp/safent-llm-lock-final.vbmBEW
PYTHONPATH=src python3 -m pytest tests/unit/test_llm_configuration_lock.py tests/unit/test_managed_llm_gateway.py tests/unit/instance/test_association_store.py -q --tb=short
```

Coverage includes spawned concurrent local commits, signed apply racing unpair,
revocation and signing-key rotation; late OAuth commits; nested locks and SQLite
writes; kernel release after process crash; bounded contention; fork preserving
the parent's lock; current tenant/origin validation; incompatible repository
scope; unsafe file/directory conditions and unsupported locking.

Full regression: **5751 passed, 19 skipped, 64 deselected, 7 warnings in
242.00 seconds**, no failures. The new fork regression intentionally exercises
Python's warned multithreaded-fork scenario; it passes. Other warnings and skips
cover existing SDK/host/release-only checks, not suppressed failures of this
change. This is not a production-image or live-inference certification.

Full regression command:

```sh
PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short > full-runtime-lock.log 2>&1
```

Log: `/tmp/safent-llm-lock-final.vbmBEW/full-runtime-lock.log`.
Ruff checks for the four changed source/test files and `git diff --check` pass.

Additional local macOS check: **23 passed in 4.55 seconds**, Python 3.12.13,
isolated UV dependencies. The first minimal environment lacked pytest-asyncio
and then FastAPI; after supplying the test dependencies the identical sources
passed without code changes. This is supplemental POSIX coverage, not a claim
that the Linux runtime can be deployed unchanged on macOS or Windows.

```sh
PYTHONPATH=src /Users/luiscorrea/.local/bin/uv run --no-project --python 3.12 --with pytest --with pytest-asyncio --with pydantic --with cryptography --with structlog --with httpx --with dbus-fast --with fastapi --with tenacity --with pyyaml python -m pytest tests/unit/test_llm_configuration_lock.py -q --tb=short
```

Owned files:

- `src/hermes/security/configuration_lock.py` (new).
- `src/hermes/runtime/managed_llm.py`.
- `src/hermes/instance/association_store.py`.
- `tests/unit/test_llm_configuration_lock.py` (new).
- This report.
