#!/usr/bin/env bash
# ops/release/tests/run_tests.sh — offline test suite for
# publish-desktop-release.sh: a fake `gh` (fixtures/bin/gh) stands in for
# GitHub entirely, fixtures/build_fixture.py builds fixture artifact trees
# (valid / url_mismatch / bad_signature / missing_dmg), and the
# size-overflow case reuses `valid` with a tiny size-limit override instead
# of a multi-gigabyte fixture file. Run: ops/release/tests/run_tests.sh
set -euo pipefail

TESTS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="$(cd -- "${TESTS_DIR}/.." && pwd)"
SCRIPT="${RELEASE_DIR}/publish-desktop-release.sh"
FAKE_GH_DIR="${TESTS_DIR}/fixtures/bin"
BUILD_FIXTURE="${TESTS_DIR}/fixtures/build_fixture.py"

export PATH="${FAKE_GH_DIR}:${PATH}"

PASS=0
FAIL=0

indent() { sed 's/^/    /'; }

expect() {
  # expect <name> <want-exit 0|nonzero> <must-contain-in-stderr> -- <cmd...>
  local name="$1" want="$2" must_contain="$3"
  shift 3
  [[ "$1" == "--" ]] && shift
  local out rc
  set +e
  out="$("$@" 2>&1)"
  rc=$?
  set -e
  local ok=1
  if [[ "$want" == "0" && "$rc" -ne 0 ]]; then ok=0; fi
  if [[ "$want" == "nonzero" && "$rc" -eq 0 ]]; then ok=0; fi
  if [[ -n "$must_contain" ]] && ! grep -qF "$must_contain" <<<"$out"; then ok=0; fi
  if [[ "$ok" == "1" ]]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name (rc=$rc)"
    indent <<<"$out"
    FAIL=$((FAIL + 1))
  fi
}

run_scenario() {
  # run_scenario <scenario-name-for-build_fixture.py> <tag>
  # Reads $SCENARIO_EXTRA_ARGS (e.g. "--dry-run") set by the caller.
  local scenario="$1" tag="$2"
  local work state artifacts
  work="$(mktemp -d)"
  state="$work/state"
  artifacts="$work/artifacts-src"
  mkdir -p "$state"
  python3 "$BUILD_FIXTURE" "$scenario" "$artifacts" "$tag"

  FAKE_GH_STATE_DIR="$state" \
  FAKE_GH_ARTIFACTS_SRC="$artifacts" \
  RUNTIME_MANIFEST_PUBKEY_FILE="$artifacts/release-manifests/test-pubkey.pub" \
  bash "$SCRIPT" "123456" "$tag" "${SCENARIO_EXTRA_ARGS[@]}"
}

# --- valid: dry-run must pass cleanly ---------------------------------------
SCENARIO_EXTRA_ARGS=(--dry-run)
expect "valid: dry-run passes" 0 "all checks passed" \
  -- run_scenario valid v9.9.1-valid

# --- valid: a real (fake-gh) publish, then idempotent re-run ----------------
run_valid_real() {
  local tag="v9.9.2-valid-real"
  local work state artifacts
  work="$(mktemp -d)"
  state="$work/state"
  artifacts="$work/artifacts-src"
  mkdir -p "$state"
  python3 "$BUILD_FIXTURE" valid "$artifacts" "$tag"

  local out1 rc1 out2 rc2
  set +e
  out1="$(FAKE_GH_STATE_DIR="$state" FAKE_GH_ARTIFACTS_SRC="$artifacts" \
    RUNTIME_MANIFEST_PUBKEY_FILE="$artifacts/release-manifests/test-pubkey.pub" \
    bash "$SCRIPT" "123456" "$tag" 2>&1)"
  rc1=$?
  out2="$(FAKE_GH_STATE_DIR="$state" FAKE_GH_ARTIFACTS_SRC="$artifacts" \
    RUNTIME_MANIFEST_PUBKEY_FILE="$artifacts/release-manifests/test-pubkey.pub" \
    bash "$SCRIPT" "123456" "$tag" 2>&1)"
  rc2=$?
  set -e

  if [[ $rc1 -ne 0 ]]; then
    echo "FAIL: valid real publish (first run rc=$rc1)"; indent <<<"$out1"; FAIL=$((FAIL + 1)); return
  fi
  if ! grep -qF "Published $tag" <<<"$out1"; then
    echo "FAIL: valid real publish (first run did not report Published)"; indent <<<"$out1"; FAIL=$((FAIL + 1)); return
  fi
  if [[ ! -f "$state/releases/$tag/assets/latest.json" ]]; then
    echo "FAIL: valid real publish (latest.json not uploaded)"; FAIL=$((FAIL + 1)); return
  fi
  if [[ $rc2 -ne 0 ]] || ! grep -qF "already published" <<<"$out2"; then
    echo "FAIL: valid real publish (idempotent re-run, rc=$rc2)"; indent <<<"$out2"; FAIL=$((FAIL + 1)); return
  fi
  echo "PASS: valid real publish + idempotent re-run"
  PASS=$((PASS + 1))
}
run_valid_real

# --- size overflow: tiny limit trips even these small dummy files -----------
SCENARIO_EXTRA_ARGS=(--dry-run)
run_size_overflow() {
  local tag="v9.9.3-overflow"
  local work state artifacts
  work="$(mktemp -d)"; state="$work/state"; artifacts="$work/artifacts-src"
  mkdir -p "$state"
  python3 "$BUILD_FIXTURE" valid "$artifacts" "$tag"
  local out rc
  set +e
  out="$(FAKE_GH_STATE_DIR="$state" FAKE_GH_ARTIFACTS_SRC="$artifacts" \
    RUNTIME_MANIFEST_PUBKEY_FILE="$artifacts/release-manifests/test-pubkey.pub" \
    PUBLISH_RELEASE_SIZE_LIMIT_BYTES=16 \
    bash "$SCRIPT" "123456" "$tag" --dry-run 2>&1)"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]] && grep -qF "exceeds the 2 GiB" <<<"$out"; then
    echo "PASS: size overflow rejected"; PASS=$((PASS + 1))
  else
    echo "FAIL: size overflow (rc=$rc)"; indent <<<"$out"; FAIL=$((FAIL + 1))
  fi
}
run_size_overflow

# --- url mismatch ------------------------------------------------------------
expect "url mismatch rejected" nonzero "url mismatch" \
  -- run_scenario url_mismatch v9.9.4-urlmismatch

# --- bad signature ------------------------------------------------------------
expect "bad signature rejected" nonzero "does not verify against" \
  -- run_scenario bad_signature v9.9.5-badsig

# --- missing dmg (bonus) -----------------------------------------------------
expect "missing dmg rejected" nonzero "no .dmg found" \
  -- run_scenario missing_dmg v9.9.6-nodmg

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
