# Safent — native client (Tauri v2)

**Status: SCAFFOLD. Not built or verified here.** This was authored on a Linux
host; a macOS `.dmg`/`.app` can only be **built and verified on a Mac** (Tauri
bundles native WebView + code-signing per-OS). Treat everything below as the
starting point, not a shipped artifact.

## What it is

A **thin client**: the native window opens onto the Safent web UI that the runtime
already serves (the VM/container you run today). The backend does **not** change —
the app just points a WebView at it.

- Target URL is read from `SAFENT_URL` (see `src/lib.rs`).
- Default: `http://localhost:17517/` — the runtime port published on the host
  loopback. The server redirects `/` → `/app/` (the React SPA).

## Prerequisites (on the Mac)

- Rust (`rustup`), Xcode command-line tools.
- Tauri CLI: `cargo install tauri-cli --version "^2"` (or `npm i -D @tauri-apps/cli@^2`).
- App icons: `cargo tauri icon path/to/logo.png` (generates `icons/`, which are
  git-ignored). The bundle build fails without them.

## Build

```sh
cd frontend
npm install
npm run build            # produces ../dist (the bundled fallback frontend)
cargo tauri build        # → src-tauri/target/release/bundle/dmg/Safent_*.dmg
# dev:  cargo tauri dev
```

## The one open decision — the bootstrap secret

The runtime mints a per-boot bootstrap secret and the owner authenticates with
`…/?k=<secret>`. A native app can't bake a per-boot secret. Pick one:

1. **Prompt + persist** (recommended): on first run ask for host + secret, store
   in the OS keychain, build `SAFENT_URL` from it. Re-prompt when the token is
   rejected (new boot).
2. **Pass at launch**: `SAFENT_URL="http://host:17517/?k=<secret>" Safent.app/...`
   (fine for dev, poor UX for users).

Option 1 is the product path; it needs a small first-run screen + a keychain
plugin (`tauri-plugin-store` or `keyring`). Not implemented in this scaffold.

## Why `frontendDist: ../dist`

Tauri requires a `frontendDist`. We point it at the React build so the app has a
local fallback, but the main window navigates to `SAFENT_URL` (remote thin-client
mode). If you later prefer a **bundled** frontend talking to a remote API, that
needs the React client to use an absolute API base (the VM host) instead of the
current same-origin `/api/v1` — a small frontend change, not a backend one.
