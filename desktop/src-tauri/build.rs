fn main() {
    // Declaring the app commands here generates ACL permissions (allow-<kebab-name>) so the
    // capabilities can grant them per-origin: install_podman to the LOCAL loader page only,
    // and the host-clipboard pair to the REMOTE web UI (http://localhost) — Tauri blocks
    // custom commands from remote origins unless a capability explicitly allows them.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "install_podman",
            "read_host_clipboard",
            "write_host_clipboard",
            "cancel_bootstrap",
            "retry_bootstrap",
            "export_diagnostics",
            "get_bootstrap_state",
        ]),
    ))
    .expect("failed to run tauri-build");
}
