#!/usr/bin/env bash
# test-fetch-verified.sh — proves fetch_verified (lib/fetch-verified.sh)
# actually resumes after a dropped connection, against a REAL local HTTP
# server that cuts the transfer mid-file (tests/flaky_http_server.py), the
# same failure class a real macOS run hit against the 888 MiB machine image
# ("curl: (18) transfer closed with N bytes remaining to read").
#
# Two scenarios:
#   1. server drops the FIRST request, serves every later one (including the
#      Range-resumed retry) correctly -> fetch_verified must still finish
#      with a byte-for-byte, sha256-verified file, and must have needed MORE
#      THAN ONE HTTP request to get there (proves resume happened, not luck).
#   2. server drops EVERY request -> fetch_verified must give up with the
#      documented download-failure exit code in bounded time, not hang and
#      not accept a truncated file.
#
# Uses low retry/delay knobs (env overrides fetch-verified.sh's production
# defaults) so scenario 2 fails in seconds instead of minutes — same code
# path as production, different timing only.
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TESTS_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"

# shellcheck source=../lib/fetch-verified.sh
source "$SCRIPTS_DIR/lib/fetch-verified.sh"

export FETCH_VERIFIED_RETRY=2
export FETCH_VERIFIED_RETRY_DELAY=1
export FETCH_VERIFIED_OUTER_TRIES=2
export FETCH_VERIFIED_CONNECT_TIMEOUT=5

WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  [ -z "$SERVER_PID" ] || kill "$SERVER_PID" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

_start_server() {
  # $1=payload path, $2...=extra flags (e.g. --always-truncate). Sets
  # SERVER_PID/SERVER_LOG/SERVER_PORT as globals — called as a plain
  # statement, NEVER via `$(...)`, so those assignments survive (a command
  # substitution runs in a subshell; the background PID/log path set inside
  # it would vanish the moment the subshell exits).
  local payload="$1"; shift
  SERVER_LOG="$WORK/server-$$-$RANDOM.log"
  "$PYTHON" "$TESTS_DIR/flaky_http_server.py" "$payload" "$@" >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  local waited=0
  while [ ! -s "$SERVER_LOG" ]; do
    sleep 0.1
    waited=$((waited + 1))
    [ "$waited" -lt 50 ] || { echo "[x] server never printed PORT" >&2; exit 1; }
  done
  SERVER_PORT="$(awk '/^PORT /{print $2; exit}' "$SERVER_LOG")"
}

_stop_server() {
  [ -z "$SERVER_PID" ] || kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

fail() {
  echo "[x] FAIL: $1" >&2
  exit 1
}

echo "[*] scenario 1: connection drops on the first request, must still finish verified"
PAYLOAD="$WORK/payload.bin"
head -c 3000000 /dev/urandom >"$PAYLOAD"
WANT_SIZE="$(_filesize "$PAYLOAD")"
WANT_SHA="$(SHA256 "$PAYLOAD")"

_start_server "$PAYLOAD"
DEST="$WORK/downloaded.bin"
if fetch_verified "http://127.0.0.1:$SERVER_PORT/payload" "$DEST" "$WANT_SIZE" "$WANT_SHA"; then
  echo "    fetch_verified: exit 0"
else
  rc=$?
  fail "fetch_verified returned $rc, expected 0 (a dropped-then-resumed transfer must still succeed)"
fi
[ -f "$DEST" ] || fail "no file at $DEST"
[ "$(_filesize "$DEST")" = "$WANT_SIZE" ] || fail "size mismatch: $(_filesize "$DEST") != $WANT_SIZE"
[ "$(SHA256 "$DEST")" = "$WANT_SHA" ] || fail "sha256 mismatch after supposed success"
REQS="$(grep -c '^REQUEST ' "$SERVER_LOG" || true)"
[ "$REQS" -gt 1 ] || fail "server only saw $REQS request(s) — the drop was not actually resumed, just re-requested from scratch by luck"
echo "    verified: size=$WANT_SIZE sha256=$WANT_SHA over $REQS HTTP requests (proves resume, not a fluke)"
_stop_server
echo "[ok] scenario 1 passed"
echo

echo "[*] scenario 2: connection drops on EVERY request, must fail with the documented exit code, bounded"
_start_server "$PAYLOAD" --always-truncate
DEST2="$WORK/never.bin"
set +e
start_ts=$(date +%s)
fetch_verified "http://127.0.0.1:$SERVER_PORT/payload" "$DEST2" "$WANT_SIZE" "$WANT_SHA"
rc=$?
end_ts=$(date +%s)
set -e
elapsed=$((end_ts - start_ts))
[ "$rc" -eq "$EXIT_DOWNLOAD" ] || fail "exit code was $rc, expected EXIT_DOWNLOAD=$EXIT_DOWNLOAD"
[ "$elapsed" -lt 60 ] || fail "took ${elapsed}s — outer retry loop is not bounded in practice"
[ ! -f "$DEST2" ] || fail "a failed download must never leave a file at the final path"
echo "    fetch_verified: exit $rc (EXIT_DOWNLOAD) after ${elapsed}s, no file left at $DEST2"
_stop_server
echo "[ok] scenario 2 passed"
echo
echo "[ok] all fetch_verified resume tests passed"
