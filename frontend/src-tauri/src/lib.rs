use tauri::{WebviewUrl, WebviewWindowBuilder};

/// Thin client entry point.
///
/// Safent's native app does NOT bundle the backend — it opens a window onto the
/// Safent web UI served by the runtime (a VM/container, exactly as it runs today).
/// The target URL (including the per-boot `?k=<bootstrap-secret>`) is read from
/// the `SAFENT_URL` env var so the native shell stays a pure thin client and the
/// backend needs no changes.
///
/// Default: the runtime's port published on the host loopback (localhost:17517),
/// which redirects "/" → "/app/" (the React SPA).
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let url = std::env::var("SAFENT_URL")
        .unwrap_or_else(|_| "http://localhost:17517/".to_string());
    let target = url
        .parse()
        .unwrap_or_else(|_| panic!("SAFENT_URL is not a valid URL: {url}"));

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(target))
                .title("Safent")
                .inner_size(1280.0, 832.0)
                .min_inner_size(960.0, 600.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the Safent native client");
}
