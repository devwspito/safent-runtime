#!/usr/bin/env bash
# fetch-verified.sh — one function, `fetch_verified`, for every download this
# repo's build-time scripts do. Sourced, never executed directly.
#
#   fetch_verified <url> <final_path> <want_size_bytes> <want_sha256>
#
# On return 0, <final_path> exists, is exactly <want_size_bytes> long, and its
# sha256 is <want_sha256> — never a partial or unverified file (an interrupted
# transfer never reaches <final_path>: it accumulates at "<final_path>.part"
# and is only `mv`-ed into place, atomically, once verified in full).
#
# Two independent layers of retry, because they answer different questions:
#   1. curl's OWN --retry (network-level "is the connection still bad RIGHT
#      NOW") — resumes via --continue-at - across its own retries, so a drop
#      mid-transfer (this function exists because of exactly that: a real
#      macOS pipeline run hit "curl: (18) transfer closed with 929837083
#      bytes remaining to read" 31 MB from the end of an 888 MiB download and
#      the OLD, non-resuming logic threw the whole download away) picks back
#      up from ".part"'s current size instead of restarting from zero.
#   2. The outer `_curl_resume_loop` — defense in depth for a connection bad
#      enough to exhaust curl's own retry budget; each outer attempt is a
#      fresh curl invocation, which ALSO resumes from ".part" via -C -, so
#      nothing already downloaded is lost between outer attempts either.
#   3. `fetch_verified`'s own outer loop is for a THIRD, different failure:
#      the transfer completes (right byte count) but the sha256 is wrong —
#      corruption, not incompleteness. Resuming a corrupt range would just
#      keep the corruption, so this path deletes ".part" and starts that one
#      download over from zero, ONCE, not forever.
#
# Exit codes this file's functions return (the sourcing script re-raises
# these — see the table this script's own header documents for the full set
# stage-runtime.sh exits with):
#   0  verified file at <final_path>
#   2  network exhausted every retry without completing (EXIT_DOWNLOAD)
#   3  sha256 mismatch persisted after a full from-zero re-download (EXIT_INTEGRITY)
set -uo pipefail

EXIT_DOWNLOAD=2
EXIT_INTEGRITY=3

SHA256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

_filesize() {
  stat -c '%s' "$1" 2>/dev/null || stat -f '%z' "$1" 2>/dev/null || echo 0
}

# Fresh curl invocation per outer attempt; each one resumes "$2" (via -C -)
# from wherever the previous attempt left off. Bounded to 3 outer attempts —
# curl's own --retry already covers ordinary flakiness inside ONE attempt, so
# the outer loop is defense in depth, not the primary mechanism, and stays
# bounded instead of hanging forever on a truly dead network. Every number
# here is overridable (env) with production defaults matching exactly what
# was specified for this fix — tests/test-fetch-verified.sh lowers them so a
# connection that never recovers fails in seconds, not minutes, without
# exercising different logic than production does.
FETCH_VERIFIED_RETRY="${FETCH_VERIFIED_RETRY:-8}"
FETCH_VERIFIED_RETRY_DELAY="${FETCH_VERIFIED_RETRY_DELAY:-5}"
FETCH_VERIFIED_CONNECT_TIMEOUT="${FETCH_VERIFIED_CONNECT_TIMEOUT:-30}"
FETCH_VERIFIED_SPEED_LIMIT="${FETCH_VERIFIED_SPEED_LIMIT:-10240}"
FETCH_VERIFIED_SPEED_TIME="${FETCH_VERIFIED_SPEED_TIME:-60}"
FETCH_VERIFIED_OUTER_TRIES="${FETCH_VERIFIED_OUTER_TRIES:-3}"

_curl_resume_loop() {
  local url="$1" part="$2" outer_try
  for ((outer_try = 1; outer_try <= FETCH_VERIFIED_OUTER_TRIES; outer_try++)); do
    if curl --fail --location \
      --retry "$FETCH_VERIFIED_RETRY" --retry-delay "$FETCH_VERIFIED_RETRY_DELAY" --retry-all-errors \
      --continue-at - \
      --connect-timeout "$FETCH_VERIFIED_CONNECT_TIMEOUT" \
      --speed-limit "$FETCH_VERIFIED_SPEED_LIMIT" --speed-time "$FETCH_VERIFIED_SPEED_TIME" \
      -o "$part" "$url"; then
      return 0
    fi
    local rc=$?
    echo "[!] $(basename "$part" .part): download interrupted (curl exit $rc)," \
      "outer attempt $outer_try/$FETCH_VERIFIED_OUTER_TRIES, resuming from $(_filesize "$part") bytes..." >&2
  done
  return 1
}

fetch_verified() {
  local url="$1" final="$2" want_size="$3" want_sha="$4" part
  part="${final}.part"
  mkdir -p "$(dirname "$final")"

  local corrupt_retry
  for corrupt_retry in 1 2; do
    if ! _curl_resume_loop "$url" "$part"; then
      echo "[x] $(basename "$final"): download failed after exhausting every retry: $url" >&2
      return "$EXIT_DOWNLOAD"
    fi

    local got_size
    got_size="$(_filesize "$part")"
    if [ "$got_size" != "$want_size" ]; then
      echo "[!] $(basename "$final"): curl reported success but size is $got_size," \
        "want $want_size — treating as incomplete, retrying" >&2
      rm -f "$part"
      continue
    fi

    local got_sha
    got_sha="$(SHA256 "$part")"
    if [ "$got_sha" = "$want_sha" ]; then
      mv -f "$part" "$final"
      return 0
    fi
    echo "[!] $(basename "$final"): right size ($got_size bytes) but sha256 is $got_sha," \
      "want $want_sha — corrupt, not incomplete. Deleting and re-downloading from zero" \
      "(attempt $corrupt_retry/2)" >&2
    rm -f "$part"
  done

  echo "[x] $(basename "$final"): sha256 mismatch persisted after a full from-zero re-download: $url" >&2
  return "$EXIT_INTEGRITY"
}
