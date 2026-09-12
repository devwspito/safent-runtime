#!/usr/bin/env bash
# provision.sh — one-time-idempotent host-side provisioning of the safent-ads
# companion (024, plan.md §1.1). Invoked by run-safent.sh BEFORE the Safent
# container starts, and by the `safent` CLI on every run/update.
#
# Product constants (spec.md §7) — NEVER re-chosen at runtime. If the subnet
# or port is already taken on this host, provisioning FAILS LOUD and Safent
# starts WITHOUT the companion (FR-3/FR-6) — it never silently picks another
# subnet/port (owner decision 2).
#
# ── Operation (owner-facing) ─────────────────────────────────────────────
# State lives at $SAFENT_COMPANION_STATE (default ~/.safent/companions/ads):
#   tls/            private CA + leaf for ads.safent.internal
#   bearer          the /mcp bearer (0400)
#   sso/ads-sso.key private Ed25519 half of the session-bridge SSO pair (026,
#                   contracts/sso.md §3) — 0400, generated ONCE alongside the
#                   bearer. The public half is handed to the companion as
#                   ADS_SSO_PUBLIC_KEY in secrets/api.env (same channel as
#                   ADS_MCP_TOKEN — never argv, never a log line). Only
#                   Safent's daemon reads the private half (read-only bind at
#                   /etc/hermes/companions/ads-sso.key, T004).
#   secrets/api.env    ads-api/ads-worker secrets — generated ONCE, edit by
#                      hand only to uncomment TELEGRAM_BOT_TOKEN/
#                      TELEGRAM_OWNER_CHAT_IDS once that path is optional
#   secrets/broker.env ads-broker secrets — generated ONCE
#   caps.yaml       hard spend caps (fail-closed) — edit by hand, see below
#   companions.json the file Safent's own daemon reads (read-only bind)
#
# vendor.env (OPTIONAL, owner-created by hand, 0600, never generated here):
# Safent's OWN Google Cloud OAuth / Meta app credentials, merged into
# broker.env on every re-provision (never overwritten if already merged):
#   GOOGLE_ADS_CLIENT_ID=...
#   GOOGLE_ADS_CLIENT_SECRET=...
#   GOOGLE_ADS_LOGIN_CUSTOMER_ID=...
#   META_APP_ID=...
#   META_APP_SECRET=...
# These are the VENDOR's own platform app credentials — never a customer's
# account credentials, which the owner connects from the ads panel UI
# instead and which end up encrypted in ads-broker's credential store.
#
# caps.yaml starts with `accounts: {}` — NO account can spend anything
# until the owner adds its real platform_account_id under `accounts:`
# (see caps.template.yaml's own comments for the exact shape). Edit
# $STATE/caps.yaml directly; provision.sh never touches it again once it
# exists.
#
# Publishing ghcr.io/devwspito/safent-ads (SAFENT_ADS_IMAGE's default) is
# the OWNER's own release step, from the ads repo's CI — never done from
# here or from a developer machine.
#
# --scaffold (028 T015): stop after the network/TLS/bearer/companions.json/
# caps/pg_password steps — every one of them is local and image-independent
# (no pull, no `podman run` of the ads image). Skips ensure_image,
# ensure_secrets, ensure_sso_keypair, start_companion and wait_for_health,
# so the companion's OWN service never comes up. This is what run-safent.sh
# and the `safent` CLI now call on EVERY Safent start: the four files Safent
# binds read-only (companions.json, ca.crt, bearer, sso/ads-sso.key) always
# exist from first boot, so a LATER `safent companion install` only ever
# writes into mounts that are already there — it never has to recreate
# Safent's own container. Without --scaffold this script still runs the
# FULL sequence (today's exact behaviour), used by `safent companion
# install|repair` (T016) to actually bring the service up.
set -euo pipefail

readonly COMPANION_SUBNET="10.201.0.0/24"
readonly COMPANION_GATEWAY="10.201.0.1"
readonly COMPANION_IP="10.201.0.10"
readonly COMPANION_PORT="8443"
readonly COMPANION_HOST="ads.safent.internal"
readonly COMPANION_NETWORK="safent-companions"
# The image is the OWNER'S release artifact (ghcr.io/devwspito/safent-ads),
# published by the ads team's own pipeline — never built here (no publishing
# from a developer machine). Override for local dev with
# SAFENT_ADS_IMAGE=safent-ads:local (run-safent.sh does this automatically
# when that image already exists locally — see its own comment).
readonly SAFENT_ADS_IMAGE="${SAFENT_ADS_IMAGE:-ghcr.io/devwspito/safent-ads:latest}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${SAFENT_COMPANION_STATE:-$HOME/.safent/companions/ads}"
# SAFENT_PODMAN wins over PATH resolution — same rule as the `safent` CLI
# (contracts/app-engine.md §1): the desktop app ships its OWN pinned podman
# binary and this script must never fall back to whatever a terminal user
# happens to have on PATH once it is invoked from the embedded CLI
# (`safent companion install|repair`, T016).
if [ -n "${SAFENT_PODMAN:-}" ]; then
  RUNTIME="$SAFENT_PODMAN"
else
  RUNTIME="$(command -v podman || command -v docker)"
fi
[ -n "$RUNTIME" ] || { echo "provision.sh: need podman or docker" >&2; exit 1; }

SCAFFOLD_ONLY=0
case "${1:-}" in
  --scaffold) SCAFFOLD_ONLY=1 ;;
  "") ;;
  *) echo "provision.sh: unknown argument '$1' (usage: provision.sh [--scaffold])" >&2; exit 1 ;;
esac

mkdir -p "$STATE/tls" "$STATE/secrets" "$STATE/sso"
chmod 0700 "$STATE" "$STATE/secrets" "$STATE/sso"

log() { echo "[companion:ads] $*"; }
fail() { echo "[companion:ads] FALLO: $*" >&2; exit 1; }

# ── 1. Network — FIXED subnet, fail loud on conflict (owner decision 2) ─────
ensure_network() {
  local existing_subnet
  existing_subnet="$("$RUNTIME" network inspect "$COMPANION_NETWORK" \
    --format '{{(index .Subnets 0).Subnet}}' 2>/dev/null || true)"
  if [ -n "$existing_subnet" ]; then
    [ "$existing_subnet" = "$COMPANION_SUBNET" ] || fail \
      "la red '$COMPANION_NETWORK' ya existe con otra subred ($existing_subnet) — bórrala a mano si quieres reprovisionar"
    log "red '$COMPANION_NETWORK' ya existe ($COMPANION_SUBNET) — OK"
    return 0
  fi
  if ! "$RUNTIME" network create "$COMPANION_NETWORK" \
        --subnet "$COMPANION_SUBNET" --gateway "$COMPANION_GATEWAY" >/dev/null 2>&1; then
    fail "'$COMPANION_SUBNET' ya está en uso por otra red de este host — libérala o el companion no se instala (nunca elegimos otra)"
  fi
  log "red '$COMPANION_NETWORK' creada ($COMPANION_SUBNET)"
}

# ads-api corre como uid 10001 dentro del contenedor y monta tls/ de solo
# lectura: con leaf.key a 0600 del usuario del host el proceso no puede leer
# la clave y entra en bucle de arranque (verificacion fresca T214). Un
# chown al uid del contenedor no es portable (rootless remapea a subuid), asi
# que la clave HOJA queda legible por modo y la protege el directorio de
# estado (0700): ningun otro usuario del host lo atraviesa. La clave de la
# CA (ca.key) no se monta nunca y sigue a 0600.
_open_leaf_key_to_container() {
  chmod 0644 "$STATE/tls/leaf.key"
}

# ── 2. CA + leaf (ECDSA P-256, SAN=ads.safent.internal) ─────────────────────
ensure_tls() {
  if [ -f "$STATE/tls/ca.crt" ] && [ -f "$STATE/tls/leaf.crt" ] && [ -f "$STATE/tls/leaf.key" ]; then
    _open_leaf_key_to_container
    return 0
  fi
  log "generando CA privada + hoja TLS para ${COMPANION_HOST}..."
  local ca_key="$STATE/tls/ca.key" ca_crt="$STATE/tls/ca.crt"
  local leaf_key="$STATE/tls/leaf.key" leaf_crt="$STATE/tls/leaf.crt"
  openssl ecparam -genkey -name prime256v1 -noout -out "$ca_key"
  openssl req -x509 -new -key "$ca_key" -days 3650 -sha256 -subj "/CN=Safent Ads companion CA" -out "$ca_crt"
  openssl ecparam -genkey -name prime256v1 -noout -out "$leaf_key"
  openssl req -new -key "$leaf_key" -subj "/CN=$COMPANION_HOST" \
    -addext "subjectAltName=DNS:$COMPANION_HOST" -out "$STATE/tls/leaf.csr"
  openssl x509 -req -in "$STATE/tls/leaf.csr" -CA "$ca_crt" -CAkey "$ca_key" -CAcreateserial \
    -days 825 -sha256 -extfile <(printf 'subjectAltName=DNS:%s' "$COMPANION_HOST") -out "$leaf_crt"
  rm -f "$STATE/tls/leaf.csr"
  chmod 0600 "$ca_key"
  chmod 0644 "$ca_crt" "$leaf_crt"
  _open_leaf_key_to_container
}

# ── 3. Bearer ────────────────────────────────────────────────────────────────
ensure_bearer() {
  [ -f "$STATE/bearer" ] && return 0
  openssl rand -hex 32 > "$STATE/bearer"
  chmod 0400 "$STATE/bearer"
  log "bearer generado"
}

# ── 4. companions.json (exact shape hermes.shell_server.companions validates) ─
write_companions_json() {
  local fingerprint fingerprint_hex
  # OpenSSL is already a hard dependency for the CA and bearer.  Use it for
  # the digest too instead of GNU sha256sum: stock macOS exposes no
  # sha256sum on the GUI app's minimal PATH, which used to disable the Ads
  # scaffold during an otherwise healthy first boot.
  fingerprint_hex="$(openssl x509 -in "$STATE/tls/ca.crt" -outform der \
    | openssl dgst -sha256 -r | awk '{print $1}')"
  [ "${#fingerprint_hex}" -eq 64 ] \
    && [[ "$fingerprint_hex" != *[!0-9a-fA-F]* ]] \
    || fail "no se pudo calcular la huella SHA-256 de la CA"
  fingerprint="sha256:$fingerprint_hex"
  # Written to a temp name first: the live file is 0444 and (when we could
  # chown it) root-owned, so `cat >` onto it would fail — rename in the
  # owner-writable $STATE dir is the only re-provision path that works.
  cat > "$STATE/companions.json.tmp" <<JSON
{"version": 1, "companions": [{
  "slug": "safent-ads",
  "url": "https://$COMPANION_HOST:$COMPANION_PORT/mcp",
  "ip": "$COMPANION_IP",
  "port": $COMPANION_PORT,
  "ca_path": "/etc/hermes/companions/ads-ca.crt",
  "ca_fingerprint": "$fingerprint",
  "bearer_ref": "file:/etc/hermes/companions/ads.bearer"}]}
JSON
  # 0444 + root:root is the shape hermes.shell_server.companions accepts
  # unconditionally. Rootless podman/docker remap us to uid 0 inside the
  # container so the chown is cosmetic there; ROOTFUL podman does not remap
  # at all, and without it the file arrives as uid 1000 — the loader then
  # relies on its second branch (the `:ro` bind mount), which we always
  # provide from run-safent.sh. Never fail provisioning over the chown: the
  # 0444 mode + read-only mount already carry the invariant.
  chmod 0444 "$STATE/companions.json.tmp"
  if [ "$(id -u)" -eq 0 ]; then
    chown 0:0 "$STATE/companions.json.tmp"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n chown 0:0 "$STATE/companions.json.tmp" || \
      log "sin privilegios para chown root:root — vale igual (bind :ro + 0444)"
  else
    log "sin sudo no interactivo — companions.json queda 0444 de tu usuario (bind :ro lo protege)"
  fi
  mv -f "$STATE/companions.json.tmp" "$STATE/companions.json"
}

# ── 5. Image — the owner's published release, pulled only if absent ─────────
ensure_image() {
  if "$RUNTIME" image inspect "$SAFENT_ADS_IMAGE" >/dev/null 2>&1; then
    log "imagen '$SAFENT_ADS_IMAGE' ya está en local — OK"
  else
    log "descargando '$SAFENT_ADS_IMAGE'…"
    "$RUNTIME" pull "$SAFENT_ADS_IMAGE" || fail "no se pudo descargar '$SAFENT_ADS_IMAGE'"
  fi
  record_provisioned_image
}

# Persist the EXACT image ref this run actually used — the single source of
# truth `safent companion status/rotate/remove` reads back (CLI-10). Without
# this, those verbs fell back to a hard-coded ghcr.io/…/safent-ads:latest that
# could silently diverge from the image `run-safent.sh` actually provisioned
# with (its own dev convenience picks up safent-ads:local when present) — a
# `rotate` would then recreate ads-api against a DIFFERENT image than
# ads-worker was already running, and an image whose alembic history doesn't
# know the DB's current revision dies `Can't locate revision …`. Re-written on
# EVERY provisioning run (this script runs on every `run-safent.sh`/`safent`
# start, not just first install) so it always reflects the image actually in
# use — `safent companion update` is still the only verb that CHOOSES a new
# one; this merely records the choice already made.
record_provisioned_image() {
  printf '%s' "$SAFENT_ADS_IMAGE" > "$STATE/image.tmp"
  mv -f "$STATE/image.tmp" "$STATE/image"
}

# ── 6. Postgres password (compose.yaml's ADS_POSTGRES_PASSWORD) ─────────────
ensure_pg_password() {
  [ -f "$STATE/pg_password" ] || openssl rand -hex 32 > "$STATE/pg_password"
  chmod 0400 "$STATE/pg_password"
}

# ── 7. secrets/api.env + secrets/broker.env — generated ONCE, never touched
# again once api.env exists (both files are always created together in the
# same run, so api.env's presence is the idempotency marker for the pair).
# GOOGLE_*/META_* vendor credentials are the exception: those are merged
# into broker.env on EVERY run from $STATE/vendor.env, because the owner
# may add them after the first install (see merge_vendor_credentials).
ensure_secrets() {
  if [ -f "$STATE/secrets/api.env" ]; then
    log "secretos de api.env/broker.env ya existen — no se regeneran"
  else
    log "generando secretos de ads-api/ads-worker/ads-broker (una sola vez)…"
    local keypair signing_key public_key session_secret totp_key master_key
    # -- (security review 2026-09-10, LOW finding, CWE-88): stops podman/
    # docker from ever reading $SAFENT_ADS_IMAGE as an option.
    keypair="$("$RUNTIME" run --rm --network none -- "$SAFENT_ADS_IMAGE" \
      python -m safent_ads.tools.gen_keys)"
    signing_key="$(printf '%s\n' "$keypair" | sed -n 's/^ADS_APPROVAL_SIGNING_KEY=//p')"
    public_key="$(printf '%s\n' "$keypair" | sed -n 's/^ADS_APPROVAL_PUBLIC_KEY=//p')"
    [ -n "$signing_key" ] && [ -n "$public_key" ] || \
      fail "gen_keys no devolvió el par de claves de aprobación esperado"
    session_secret="$(openssl rand -base64 32)"
    totp_key="$(openssl rand -base64 32)"
    master_key="$(openssl rand -base64 32)"

    umask 077
    cat > "$STATE/secrets/api.env.tmp" <<EOF
ADS_MCP_TOKEN=$(cat "$STATE/bearer")
ADS_SESSION_SECRET=$session_secret
ADS_TOTP_ENC_KEY=$totp_key
ADS_APPROVAL_SIGNING_KEY=$signing_key
# Opcional: el dueño puede activar el bot de Telegram añadiendo aquí
# (el arranque sigue sin ellas mientras esa vía siga siendo opcional):
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_OWNER_CHAT_IDS=[123456789]
EOF
    cat > "$STATE/secrets/broker.env.tmp" <<EOF
ADS_APPROVAL_PUBLIC_KEY=$public_key
ADS_CREDENTIAL_MASTER_KEY=$master_key
ADS_BROKER_ALLOWED_UIDS=10001
ADS_BROKER_HARD_CAPS_FILE=/etc/ads-broker/caps.yaml
ADS_CREDENTIAL_STORE_DIR=/var/lib/ads-broker/credentials
ADS_BROKER_SOCKET=/run/ads-broker/broker.sock
EOF
    chmod 0600 "$STATE/secrets/api.env.tmp" "$STATE/secrets/broker.env.tmp"
    mv -f "$STATE/secrets/api.env.tmp" "$STATE/secrets/api.env"
    mv -f "$STATE/secrets/broker.env.tmp" "$STATE/secrets/broker.env"
    log "secretos generados (0600) en $STATE/secrets/"
  fi
  merge_vendor_credentials
}

# Vendor (Safent's own Google Cloud OAuth / Meta app) credentials: owner-provided,
# never generated here. $STATE/vendor.env is written BY HAND by the owner
# (0600, only the explicitly supported keys below) — if present, its lines are merged
# into broker.env, skipping any key that is already there, so re-running
# provisioning after the owner adds the file picks it up without ever
# duplicating or overwriting a line.
merge_vendor_credentials() {
  local vendor="$STATE/vendor.env"
  [ -f "$vendor" ] || return 0
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      GOOGLE_ADS_CLIENT_ID=*|GOOGLE_ADS_CLIENT_SECRET=*|GOOGLE_ADS_LOGIN_CUSTOMER_ID=*|META_APP_ID=*|META_APP_SECRET=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    grep -q "^${key}=" "$STATE/secrets/broker.env" 2>/dev/null && continue
    printf '%s\n' "$line" >> "$STATE/secrets/broker.env"
    log "credencial de vendor '$key' incorporada a broker.env"
  done < "$vendor"
}

# ── 7b. SSO Ed25519 keypair (026, contracts/sso.md §3) — generated ONCE,
# alongside the bearer, with the SAME already-proven pattern as the approval
# keypair above (`python -m safent_ads.tools.gen_keys` inside the companion
# image, no host-side crypto dependency). The private half never leaves this
# host: 0400 at $STATE/sso/ads-sso.key, read only by Safent's daemon via the
# read-only bind run-safent.sh adds. The public half travels to the companion
# through secrets/api.env — the exact same channel ADS_MCP_TOKEN already
# uses — never argv, never a log line.
#
# gen_keys prints STANDARD base64 (ADS_APPROVAL_PUBLIC_KEY=<b64>); the
# companion's Ed25519 verifier (safent_ads.iam.infrastructure.
# ed25519_assertion_verifier.decode_ed25519_public_key) decodes ADS_SSO_
# PUBLIC_KEY as URL-SAFE base64 (matching the assertion's own <b64url(payload)>
# encoding, contracts/sso.md §3). `tr '+/' '-_'` converts alphabets without
# touching the padding — cheap, host-only, no extra dependency.
# ── 7c. SSO key placeholder (028 T015, scaffold mode) ───────────────────────
# An EMPTY, correctly-permissioned file so the bind mount run-safent.sh/the
# `safent` CLI add for /etc/hermes/companions/ads-sso.key always has a
# source, even before ensure_sso_keypair ever runs (that step needs the ads
# image, which scaffold mode deliberately never pulls). The daemon's own
# loader (hermes.agents_os.infrastructure.companion_sso_authority) already
# fails CLOSED and CLEAN on an empty key (Ed25519PrivateKey.from_private_
# bytes raises ValueError -> CompanionSsoKeyUnavailableError, no crash, no
# key material anywhere) — this placeholder is what makes that the observed
# behaviour instead of a missing bind-mount source. `[ -e ]`, not `[ -f ]`:
# treats a placeholder OR a real key identically for "already have a file
# here", `ensure_sso_keypair`'s own idempotency check below distinguishes
# "real key already generated" (non-empty) from "just the placeholder".
ensure_sso_placeholder() {
  [ -e "$STATE/sso/ads-sso.key" ] && return 0
  : > "$STATE/sso/ads-sso.key"
  chmod 0400 "$STATE/sso/ads-sso.key"
}

ensure_sso_keypair() {
  # -s (non-empty), not -f: a scaffold-mode placeholder (ensure_sso_
  # placeholder above) is a zero-byte file at this exact path — it must
  # NOT satisfy this check, or `safent companion install` would see "the
  # key already exists" and never generate the real one (T015/T016 boundary).
  [ -s "$STATE/sso/ads-sso.key" ] && return 0
  log "generando par Ed25519 de SSO (puente de sesión, 026)…"
  local keypair seed_std pub_std pub_urlsafe
  # -- (LOW finding, CWE-88): see ensure_secrets's own identical comment.
  keypair="$("$RUNTIME" run --rm --network none -- "$SAFENT_ADS_IMAGE" \
    python -m safent_ads.tools.gen_keys)"
  seed_std="$(printf '%s\n' "$keypair" | sed -n 's/^ADS_APPROVAL_SIGNING_KEY=//p')"
  pub_std="$(printf '%s\n' "$keypair" | sed -n 's/^ADS_APPROVAL_PUBLIC_KEY=//p')"
  [ -n "$seed_std" ] && [ -n "$pub_std" ] || \
    fail "gen_keys no devolvió el par Ed25519 de SSO esperado"

  umask 077
  printf '%s\n' "$seed_std" > "$STATE/sso/ads-sso.key.tmp"
  chmod 0400 "$STATE/sso/ads-sso.key.tmp"
  mv -f "$STATE/sso/ads-sso.key.tmp" "$STATE/sso/ads-sso.key"

  pub_urlsafe="$(printf '%s' "$pub_std" | tr '+/' '-_')"
  _write_sso_public_key_to_api_env "$pub_urlsafe"
  log "par Ed25519 de SSO generado (0400) en $STATE/sso/ads-sso.key"
}

# Idempotent single-line writer: appends ADS_SSO_PUBLIC_KEY=<value> to
# secrets/api.env unless a line for that key already exists — mirrors
# merge_vendor_credentials' own "skip if present" discipline so re-running
# provisioning never duplicates or overwrites the line.
_write_sso_public_key_to_api_env() {
  local pub="$1" api_env="$STATE/secrets/api.env"
  grep -q '^ADS_SSO_PUBLIC_KEY=' "$api_env" 2>/dev/null && return 0
  printf 'ADS_SSO_PUBLIC_KEY=%s\n' "$pub" >> "$api_env"
}

# ── 8. caps.yaml — hard caps template, installed once, owner edits by hand ──
ensure_caps() {
  [ -f "$STATE/caps.yaml" ] && return 0
  cp "$HERE/caps.template.yaml" "$STATE/caps.yaml"
  chmod 0644 "$STATE/caps.yaml"
  if [ "$(id -u)" -eq 0 ]; then
    chown 0:0 "$STATE/caps.yaml"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo -n chown 0:0 "$STATE/caps.yaml" || \
      log "sin privilegios para chown root:root — vale igual (bind :ro)"
  fi
  log "caps.yaml creado desde la plantilla — sin cuentas autorizadas todavía (fail-closed)"
}

# Security review 2026-09-10 (MEDIUM finding, CWE-754): the SAME
# migration-head guard `safent companion update`/`rotate` apply (see that
# script's own comment for the full rationale) — this is the "repair"/
# start path the review's own fix bullet calls out: provision.sh runs on
# EVERY `run-safent.sh`/`safent start`, so a stale SAFENT_ADS_IMAGE could
# otherwise bring up an image older than the database through THIS path
# without ever going through `update`. Fail-SOFT here on purpose, unlike
# the CLI verbs: this whole script's posture is FR-3 (a provisioning
# problem never blocks Safent's OWN boot, only the companion) — an
# unreachable DB/image means we cannot yet tell, and blocking the owner's
# entire container boot over an availability-only risk (the review's own
# rating: "no confidentiality or integrity loss") would be strictly worse
# than the bug it guards against. Only fires when ads-db already exists (a
# re-provision/restart) — nothing to compare on a brand-new database.
_refuse_if_image_predates_the_database() {
  "$RUNTIME" container exists safent-ads-ads-db-1 2>/dev/null || return 0
  local db_rev history
  db_rev="$("$RUNTIME" exec safent-ads-ads-db-1 \
    psql -U ads -d ads -tAc 'SELECT version_num FROM alembic_version;' 2>/dev/null \
    | tr -d '[:space:]')"
  [ -n "$db_rev" ] || return 0
  # -- (LOW finding, CWE-88): see ensure_secrets's own identical comment.
  history="$("$RUNTIME" run --rm --network none -- "$SAFENT_ADS_IMAGE" alembic history 2>/dev/null || true)"
  if [ -z "$history" ]; then
    log "no se pudo leer el historial de alembic de '$SAFENT_ADS_IMAGE' — se continúa (FR-3, no bloquea el arranque)"
    return 0
  fi
  if printf '%s\n' "$history" | grep -qw -- "$db_rev"; then
    return 0
  fi
  fail "'$SAFENT_ADS_IMAGE' no conoce la revisión '$db_rev' (ya aplicada en la base de datos) — imagen MÁS ANTIGUA que la BD, no se arranca el companion con ella (usa 'safent companion update' con la imagen correcta; Safent arranca igual, sin companion — FR-3)"
}

start_companion() {
  export SAFENT_STATE="$STATE"
  export SAFENT_ADS_IMAGE
  export ADS_POSTGRES_PASSWORD
  ADS_POSTGRES_PASSWORD="$(cat "$STATE/pg_password")"
  _refuse_if_image_predates_the_database
  "$RUNTIME" compose -p safent-ads -f "$HERE/compose.yaml" up -d
}

# ── 9. Wait for /mcp/health — the endpoint exists in the shipped image now
# (bearer-protected, constant-time compare). We never send a bearer here (no
# secret on a curl command line/env of a script that could be traced), so a
# BARE 401 is the expected "up and answering" response; 200 would mean an
# unauthenticated deployment, which never happens with this image, but is
# accepted too so this check never chases an implementation detail. --resolve
# pins the SAN-matching hostname to the fixed companion IP without needing an
# /etc/hosts entry on THIS host (that entry belongs to the container, not us).
wait_for_health() {
  local i=0 code health_ip="$COMPANION_IP"
  # The bridge lives INSIDE the macOS VM. Its fixed loopback publication is
  # the host route; keep the private CA and hostname verification unchanged.
  [ "$(uname -s)" != Darwin ] || health_ip=127.0.0.1
  while [ $i -lt 60 ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 \
        --cacert "$STATE/tls/ca.crt" \
        --noproxy "$COMPANION_HOST" \
        --resolve "$COMPANION_HOST:$COMPANION_PORT:$health_ip" \
        "https://$COMPANION_HOST:$COMPANION_PORT/mcp/health" 2>/dev/null || true)"
    case "$code" in
      200|401) log "companion listo (/mcp/health -> $code)"; return 0 ;;
    esac
    i=$((i + 1))
    sleep 1
  done
  log "companion aún no responde /mcp/health (Safent arranca igual — FR-3)"
}

ensure_network
ensure_tls
ensure_bearer
write_companions_json
ensure_pg_password
ensure_caps
ensure_sso_placeholder

if [ "$SCAFFOLD_ONLY" -eq 1 ]; then
  log "andamiaje listo (red + companions.json + TLS + bearer) — companion NO arrancado (usa 'safent companion install')"
  exit 0
fi

ensure_image
ensure_secrets
ensure_sso_keypair
start_companion
wait_for_health
log "aprovisionamiento OK — $STATE/companions.json listo para el bind read-only"
