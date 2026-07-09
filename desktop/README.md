# Safent Desktop (Tauri) — thin, reversible shell

A native desktop window (Tauri v2) that shows **the exact same web UI** the browser/
`curl` install already serves. It adds **zero product logic**: it resolves the local
daemon URL via the `safent` CLI and loads it in the platform webview
(WebKitGTK on Linux, WKWebView on macOS, WebView2 on Windows).

## Why this is easily reversible to the curl UI

- **All container + secret logic stays in the `safent` CLI** (single source). The desktop
  app only calls `safent url` (which starts the container if needed and prints the
  `http://localhost:PORT/?k=<secret>` URL) and navigates the webview to it.
- **The runtime is untouched.** No new endpoints, no Tauri-only code paths in the daemon.
- **Deleting this `desktop/` directory removes the desktop app entirely** — the
  `curl | sh` installer, the `safent` CLI, and the browser UI keep working identically.

So the desktop app is a *pure alternative front door* to the same daemon. If Tauri does
not pan out, `rm -rf desktop/` and nothing else changes.

## Run (dev)

```sh
# 1. Have a Safent container running (canonical: `safent`, or any container).
# 2. Point the shell at it and launch:
cd desktop/src-tauri
#   Option A — let it drive the canonical container via the CLI:
SAFENT_BIN=/absolute/path/to/safent cargo run
#   Option B — point straight at an already-running daemon (dev/testing):
SAFENT_URL="http://localhost:37013/?k=<secret>" cargo run
```

Resolution order inside the app:
1. `SAFENT_URL` env (explicit override), else
2. `<SAFENT_BIN or "safent"> url` (ensures running + prints the URL).

## Build / package

Cross-platform signed installers are produced by the CI signing pipeline (the one in
`agents-autonomy` already carries the Windows/Apple/Linux signatures). Locally:

```sh
cd desktop
npx @tauri-apps/cli@2 build           # unsigned local bundle
```

## What this deliberately does NOT do (yet)

- Bundle a container runtime (assumes `podman`/`docker`, exactly like `safent`).
- Inject the Tauri IPC into the daemon page (it runs as a plain web app — that is the point).
- Manage pairing/enterprise (still `safent pair` / the web UI).
