//! Native message dialogs anchored to the main window.
//!
//! A message dialog built on the bare `AppHandle` has no parent window. On
//! macOS the dialog plugin then falls back to a system user-notification
//! alert that can fail to present at all and reports "cancelled" when it
//! does — the updater took that for the owner choosing "not now" and went
//! silent (owner's Mac, 2026-09-14). Anchoring the dialog to the main window
//! uses the standard sheet path instead, which always shows.
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogBuilder};

/// Message dialog parented to the main window when it exists.
pub fn message(app: &AppHandle, text: impl Into<String>) -> MessageDialogBuilder<tauri::Wry> {
    let builder = app.dialog().message(text);
    match app.get_webview_window(crate::window_policy::MAIN_WINDOW_LABEL) {
        Some(window) => builder.parent(&window),
        None => builder,
    }
}
