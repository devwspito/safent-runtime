// T013 — window policy: no browser, single instance, tray/menu-bar.
//
// Scope (see specs/028-safent-app-nativa/{spec.md,data-model.md} and
// contracts/app-engine.md §7): this module owns the ONE window's identity
// and navigation surface. It does NOT know how to prepare, restart or quit
// the engine — that is desktop/src-tauri/src/{domain,boot}.rs (a different
// lane, not present in this worktree yet). Cross-module intent is signalled
// with Tauri events, never a direct call into a module this one must not
// depend on.
use std::sync::{Arc, Mutex};

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{
    AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};

pub const MAIN_WINDOW_LABEL: &str = "main";

/// ASSUMED integration point (flagged in the handoff report): the engine
/// lifecycle owner should `app.listen(RESTART_ENGINE_EVENT, ...)`.
pub const RESTART_ENGINE_EVENT: &str = "safent://restart-engine-requested";
/// ASSUMED integration point: emitted before this module calls
/// `AppHandle::exit`. research.md's FR-030 resolution gives the engine a
/// bounded, orderly shutdown (`SIGRTMIN+3`, 30s) — the core lane decides
/// whether it needs to intercept `RunEvent::ExitRequested` to finish that
/// before the process actually dies; this module cannot own that sequencing
/// without depending on the engine lifecycle module.
pub const QUIT_REQUESTED_EVENT: &str = "safent://quit-requested";

/// Disables the platform webview's own right-click menu (FR-002 — "sin menú
/// contextual de navegador") on every page this window ever shows: the
/// shell's own preparation/failure/reconnect screens AND the remote product
/// origin once navigation succeeds, since an `initialization_script` runs on
/// every navigation, not just the first.
const BLOCK_CONTEXT_MENU_JS: &str =
    "document.addEventListener('contextmenu', function (e) { e.preventDefault(); });";

/// The one HTTP(S) origin the window is currently allowed to navigate to —
/// the loopback endpoint from a just-consumed boot ticket (contract §5).
/// Shared, mutable, and set from OUTSIDE this module once a ticket exists;
/// `None` means no HTTP(S) navigation is legitimate yet.
#[derive(Clone, Default)]
pub struct WindowPolicy {
    authorized_origin: Arc<Mutex<Option<Url>>>,
}

impl WindowPolicy {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set_authorized_origin(&self, origin: Url) {
        if let Ok(mut guard) = self.authorized_origin.lock() {
            *guard = Some(origin);
        }
    }

    fn authorized(&self) -> Option<Url> {
        self.authorized_origin
            .lock()
            .ok()
            .and_then(|guard| guard.clone())
    }
}

fn same_origin(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme()
        && a.host_str() == b.host_str()
        && a.port_or_known_default() == b.port_or_known_default()
}

/// Pure — the security-critical rule from contract §7 ("la página remota...
/// deniega cualquier navegación a otro origen"), independently testable
/// with `cargo test` and no running Tauri app.
///
/// Any non-http(s) scheme is treated as the app's own bundled shell UI
/// (Tauri's internal asset scheme differs by platform/version) and is
/// always allowed; every http(s) target must match the currently authorized
/// origin exactly (scheme + host + port — the path/query, i.e. the `?k=`
/// ticket, is deliberately NOT compared here).
pub fn is_navigation_allowed(authorized: Option<&Url>, target: &Url) -> bool {
    if target.scheme() != "http" && target.scheme() != "https" {
        return true;
    }
    authorized.is_some_and(|origin| same_origin(origin, target))
}

/// Builds the single product window: bundled shell UI first, navigation
/// locked to `policy`'s authorized origin, closing hides instead of quitting
/// (FR-030, research.md: "cerrar la ventana no detiene el motor").
pub fn create_main_window(app: &AppHandle, policy: WindowPolicy) -> tauri::Result<WebviewWindow> {
    let window =
        WebviewWindowBuilder::new(app, MAIN_WINDOW_LABEL, WebviewUrl::App("index.html".into()))
            .title("Safent")
            .inner_size(1280.0, 860.0)
            .min_inner_size(900.0, 600.0)
            .initialization_script(BLOCK_CONTEXT_MENU_JS)
            .initialization_script(crate::update::availability::initialization_script())
            .on_navigation(move |url| is_navigation_allowed(policy.authorized().as_ref(), url))
            .build()?;

    let hidden_window = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = hidden_window.hide();
        }
    });

    Ok(window)
}

fn focus(window: &WebviewWindow) {
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

/// `tauri_plugin_single_instance`'s callback (FR-003, SC-010): a second
/// launch focuses the one window instead of creating another.
pub fn focus_existing(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
        focus(&window);
    }
}

/// «Abrir» · «Reiniciar el motor» · «Salir de Safent» — the exact three
/// items research.md's FR-030 resolution names (§"Decisión: FR-030").
pub fn install_tray(app: &AppHandle) -> tauri::Result<()> {
    let open_item = MenuItem::with_id(app, "open", "Abrir", true, None::<&str>)?;
    let restart_item = MenuItem::with_id(
        app,
        "restart-engine",
        "Reiniciar el motor",
        true,
        None::<&str>,
    )?;
    let quit_item = MenuItem::with_id(app, "quit", "Salir de Safent", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open_item, &restart_item, &quit_item])?;

    let mut tray = TrayIconBuilder::new()
        .menu(&menu)
        .show_menu_on_left_click(true)
        .tooltip("Safent")
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => focus_existing(app),
            "restart-engine" => {
                let _ = app.emit(RESTART_ENGINE_EVENT, ());
            }
            "quit" => {
                let _ = app.emit(QUIT_REQUESTED_EVENT, ());
                app.exit(0);
            }
            _ => {}
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(s: &str) -> Url {
        Url::parse(s).expect("valid test URL")
    }

    #[test]
    fn denies_http_when_no_origin_is_authorized_yet() {
        assert!(!is_navigation_allowed(
            None,
            &url("http://127.0.0.1:17517/?k=secret")
        ));
    }

    #[test]
    fn allows_the_exact_authorized_origin_regardless_of_path_or_ticket() {
        let authorized = url("http://127.0.0.1:17517/");
        assert!(is_navigation_allowed(
            Some(&authorized),
            &url("http://127.0.0.1:17517/?k=abc123")
        ));
        assert!(is_navigation_allowed(
            Some(&authorized),
            &url("http://127.0.0.1:17517/app/chat")
        ));
    }

    #[test]
    fn denies_a_different_port_even_on_loopback() {
        let authorized = url("http://127.0.0.1:17517/");
        assert!(!is_navigation_allowed(
            Some(&authorized),
            &url("http://127.0.0.1:9999/?k=abc")
        ));
    }

    #[test]
    fn denies_a_different_host() {
        let authorized = url("http://127.0.0.1:17517/");
        assert!(!is_navigation_allowed(
            Some(&authorized),
            &url("http://evil.example/?k=abc")
        ));
    }

    #[test]
    fn denies_scheme_upgrade_to_https_even_if_host_and_port_match() {
        let authorized = url("http://127.0.0.1:17517/");
        assert!(!is_navigation_allowed(
            Some(&authorized),
            &url("https://127.0.0.1:17517/")
        ));
    }

    #[test]
    fn always_allows_the_app_own_bundled_asset_scheme() {
        // Whatever exact custom scheme Tauri resolves for WebviewUrl::App on
        // this platform/version — never http(s) — must never be blocked by
        // this policy; it is the shell's own preparation/failure screens.
        assert!(is_navigation_allowed(
            None,
            &url("tauri://localhost/index.html")
        ));
        assert!(is_navigation_allowed(
            Some(&url("http://127.0.0.1:17517/")),
            &url("tauri://localhost/index.html")
        ));
    }

    #[test]
    fn set_authorized_origin_is_visible_to_is_navigation_allowed_through_the_policy() {
        let policy = WindowPolicy::new();
        let target = url("http://127.0.0.1:23456/?k=xyz");
        assert!(!is_navigation_allowed(
            policy.authorized().as_ref(),
            &target
        ));

        policy.set_authorized_origin(url("http://127.0.0.1:23456/"));
        assert!(is_navigation_allowed(policy.authorized().as_ref(), &target));
    }
}
