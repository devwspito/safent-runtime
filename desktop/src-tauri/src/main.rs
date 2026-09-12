// Prevent an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Safent desktop — a native Tauri shell around the embedded `safent` CLI.
//
// Its ONLY own screens are the local loader's preparation/failure/reconnect
// states (desktop/src, window_policy.rs's single window + tray); everything
// past `engine_ready` is the product web UI, loaded from the loopback
// origin the boot service hands the window (contract app-engine.md §5/§8).
// `boot::start` (specs/028-safent-app-nativa, T007-T013: domain + pure
// reconciler + the embedded-CLI adapter + the observe-plan-apply loop) owns
// ALL bootstrap/repair logic — this file only wires Tauri plumbing (window,
// tray, IPC commands, the host clipboard bridge, the update-availability
// poll) around it. All product + container logic stays in the `safent` CLI /
// the container — the shell adds none.

use std::process::{Command, Stdio};

mod window_policy;
use window_policy::WindowPolicy;

// T014: update orchestrator (contracts/update.md) — plan/orchestrator are pure
// resp. port-driven. The native app-only plugin is wired below; the combined
// engine/Ads/app transaction is not activated by this shell integration.
mod update;

// Bootstrap engine (specs/028-safent-app-nativa, T007/T008/T009/T011): pure
// domain + reconciler + ports/adapter + the observe-plan-apply loop — THE
// default and only boot path (main() below).
mod boot;
mod bootstrap_control;
mod diagnostics;
mod domain;
mod engine_adapter;
mod ports;
mod reconcile;
mod selftest;

/// GUI apps launched from Finder / the dock inherit a MINIMAL PATH (/usr/bin:/bin:…),
/// so the `safent` script cannot find `podman`/`docker` (installed in /opt/homebrew/bin
/// or /usr/local/bin). Hand every child an augmented PATH covering the common locations.
fn augmented_path() -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Ok(p) = std::env::var("PATH") {
        if !p.is_empty() {
            parts.push(p);
        }
    }
    for p in [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/podman/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ] {
        parts.push(p.to_string());
    }
    if let Some(home) = std::env::var_os("HOME") {
        parts.push(format!("{}/.local/bin", home.to_string_lossy()));
    }
    let sep = if cfg!(windows) { ";" } else { ":" };
    parts.join(sep)
}

// ---- host clipboard bridge (for the Live/Teaching VNC view) ----------------------------
// The jailed browser is a Linux Chromium mirrored over VNC, so its clipboard is the jail's
// X CLIPBOARD (xclip bridge in VncView). The missing hop is LOCAL clipboard <-> page:
// navigator.clipboard.readText() under WKWebView pops the paste-permission button, so the
// web UI (when it detects Tauri) asks the SHELL for the host clipboard instead — no prompt.
// Exposed to the remote UI origin only via capabilities/remote-ui.json.

/// Return the platform command that prints the clipboard to stdout. Windows is deliberately
/// absent: PowerShell's Get/Set-Clipboard mangle UTF-8 via OEM codepages while "succeeding",
/// which would shadow the WebView2 navigator.clipboard path that already works there — the
/// commands return Err on Windows so the frontend falls back to it (zero regression).
#[cfg(not(target_os = "windows"))]
fn clipboard_read_cmd() -> (&'static str, &'static [&'static str]) {
    #[cfg(target_os = "macos")]
    return ("pbpaste", &[]);
    #[cfg(not(target_os = "macos"))]
    return (
        "sh",
        &[
            "-c",
            "wl-paste --no-newline 2>/dev/null || xclip -selection clipboard -o 2>/dev/null",
        ],
    );
}

/// Return the platform command that sets the clipboard from stdin.
#[cfg(not(target_os = "windows"))]
fn clipboard_write_cmd() -> (&'static str, &'static [&'static str]) {
    #[cfg(target_os = "macos")]
    return ("pbcopy", &[]);
    #[cfg(not(target_os = "macos"))]
    return (
        "sh",
        &[
            "-c",
            "wl-copy 2>/dev/null || xclip -selection clipboard -i 2>/dev/null",
        ],
    );
}

/// Apply the platform env the clipboard tools need: augmented PATH, and on macOS a UTF-8
/// LC_CTYPE — Finder-launched apps inherit the C locale, under which pbpaste/pbcopy
/// transliterate non-ASCII (ñ/€/emoji) to '?'.
#[cfg(not(target_os = "windows"))]
fn clipboard_env(cmd: &mut Command) {
    cmd.env("PATH", augmented_path());
    #[cfg(target_os = "macos")]
    cmd.env("LC_CTYPE", "UTF-8");
}

/// Read the HOST clipboard as text (empty string when empty/non-text). `async` so the
/// blocking child process runs off the UI thread (a sync #[tauri::command] executes inline
/// in the webview IPC callback — a hung clipboard tool would freeze the window).
#[tauri::command]
async fn read_host_clipboard() -> Result<String, String> {
    #[cfg(target_os = "windows")]
    return Err("host clipboard bridge unavailable on Windows — use navigator.clipboard".into());
    #[cfg(not(target_os = "windows"))]
    {
        let (prog, args) = clipboard_read_cmd();
        let mut cmd = Command::new(prog);
        cmd.args(args);
        clipboard_env(&mut cmd);
        let out = cmd
            .output()
            .map_err(|e| format!("clipboard read failed: {e}"))?;
        if !out.status.success() {
            return Ok(String::new()); // empty clipboard exits non-zero on some tools — not an error
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }
}

/// Write text to the HOST clipboard (via stdin — no arg-quoting pitfalls). `async` for the
/// same off-the-UI-thread reason as read_host_clipboard.
#[tauri::command]
async fn write_host_clipboard(text: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        let _ = text;
        return Err(
            "host clipboard bridge unavailable on Windows — use navigator.clipboard".into(),
        );
    }
    #[cfg(not(target_os = "windows"))]
    {
        use std::io::Write;
        let (prog, args) = clipboard_write_cmd();
        let mut cmd = Command::new(prog);
        cmd.args(args);
        clipboard_env(&mut cmd);
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("clipboard write failed: {e}"))?;
        if let Some(mut sin) = child.stdin.take() {
            sin.write_all(text.as_bytes())
                .map_err(|e| format!("clipboard write failed: {e}"))?;
        }
        let status = child
            .wait()
            .map_err(|e| format!("clipboard write failed: {e}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!(
                "clipboard write exited with {}",
                status.code().unwrap_or(-1)
            ))
        }
    }
}

/// `--selftest` / `--selftest=companion`: runs boot.rs's loop headlessly and
/// exits — checked BEFORE `tauri::Builder` is ever touched, so this needs no
/// display server (run over SSH on the owner's Mac). Any other argv (none,
/// `--help`, a Tauri-internal flag) falls through to the normal windowed app.
fn selftest_arg() -> Option<bool> {
    match std::env::args().nth(1).as_deref() {
        Some("--selftest") => Some(false),
        Some("--selftest=companion") => Some(true),
        _ => None,
    }
}

fn main() {
    if let Some(want_companion) = selftest_arg() {
        std::process::exit(selftest::run(want_companion));
    }
    let policy = WindowPolicy::new();

    tauri::Builder::default()
        // Must be the first plugin registered (tauri-plugin-single-instance's own
        // requirement) — FR-003/SC-010: a second launch focuses the one window
        // instead of creating another, never a second Safent.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            window_policy::focus_existing(app);
        }))
        .manage(policy.clone())
        .manage(diagnostics::DiagnosticsState::default())
        .manage(update::native::NativeUpdater::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            read_host_clipboard,
            write_host_clipboard,
            boot::cancel_bootstrap,
            boot::retry_bootstrap,
            diagnostics::export_diagnostics,
            diagnostics::get_bootstrap_state,
            update::native::check_native_update,
            update::native::install_native_update
        ])
        .setup(move |app| {
            // NOTE: do NOT replace the default macOS menu. A custom menu that drops the
            // standard Edit submenu breaks keyboard routing to WKWebView entirely (no
            // typing anywhere). The Cmd+V-into-Live/Teaching paste must be solved in the
            // frontend (VncView), not by touching the menu — see the paste TODO there.
            window_policy::create_main_window(app.handle(), policy.clone())?;
            window_policy::install_tray(app.handle())?;

            // THE boot path (specs/028-safent-app-nativa): observe -> plan -> apply ->
            // reobserve, off the main thread, navigating the just-created "main" window
            // to the ticketed product URL once ready (boot::navigate_to_ticket) — see
            // that function for why it also authorizes the origin with `policy` and
            // starts the update-availability poll itself, at the right moment.
            boot::start(app.handle().clone());

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Safent desktop");
}
