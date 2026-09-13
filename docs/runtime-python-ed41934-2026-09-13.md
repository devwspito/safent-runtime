# Runtime 0.9.13 Python base regression

## Immutable product snapshot

- Runtime: `ed41934f486860b3cf0e4e4496d2cf42ad94c46a`.
- Ads source: `d66ba48b76c5aaefc52d916685ad98281145aca0`, archived separately
  and passed explicitly as `SAFENT_ADS_REPO` (no concurrent checkout changes).
- Scratch: `/tmp/safent-python-ed41934.JhL10R`.
- Python 3.12.3 environment: `/tmp/safent-python-global.gGvi7u/venv`.
- Isolated `env -i`, private `HOME` / `HERMES_HOME`, `PYTHONPATH=src`,
  `HF_HUB_OFFLINE=1`, no advertising-provider credentials.

Initial full base command: `python -m pytest -q`, with the repository's
unchanged default selection. Result: **2 failed, 7517 passed, 21 skipped,
250 deselected, eight warnings, 466.25 seconds** (468.63 wall).
Log: `/tmp/safent-python-ed41934-full.log`.

The default excludes the seven opt-in markers `requires_chromium`,
`requires_llm`, `requires_external_ocr`, `integration`, `requires_network`,
`requires_vm`, and `requires_openshell`. This is the complete base suite,
not certification of those excluded external-system tests.

## Fixture-only correction

Both failures were in `TestRestoreVerifiesTheContainerActuallyCameUp`.
Those tests restore into an absent container but supplied no companion
scaffold. Their Podman double also treated image-file extraction probes as
successful container creation. The new mandatory-scaffold guard correctly
rejected that incomplete setup before the intended creation postcondition.

The fixture now provides an explicit local bundle stub, pins its fake
runtime, and blocks HTTP fallback. Assertions still require the original
clear failure / clean success, and now additionally check one actual core
run, static `.2`, preserved data mount and absence of image-extraction
probes. Production code is unchanged by this correction.

Entire backup/restore group: **51 passed, 2.77 seconds**.
Log: `/tmp/safent-restore-fixture-focal.log`.
The corrected immutable full rerun is pending at this checkpoint; no green
full result is claimed by this commit.

## Separate diagnostic follow-up (not in native 0.9.13)

`cmd_restore` invokes `cmd_start` in the same shell with all output hidden.
An explicit fatal scaffold exit consequently bypasses restore's existing
postcondition message. It exits nonzero after reporting that startup began;
it does not falsely claim success or perform an additional data deletion.
A separate authorized follow-up will isolate that command in a subshell
and verify the existing postcondition/error message. It is not part of
the fixture correction, the signed 0.9.13 product, or its full-suite source.

The 21 skips retain the prior baseline categories: explicit cross-repo
fixtures (2), absent legacy wizard contracts (2), unmigrated protocols (2),
SDK/tools affected by suite-global test stubs (4), Landlock template
parameters (7), absent gitleaks (1), opt-in real projection QA (2), and
release-only manifest gate (1). Eight warnings cover existing dependency,
mock coroutine, marker, duplicate OpenAPI ID and fork behavior.
