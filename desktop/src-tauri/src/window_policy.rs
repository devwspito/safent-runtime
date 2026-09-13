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
use tauri_plugin_dialog::DialogExt;

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
/// Only the exact bundled Tauri origin and the current boot-ticket origin
/// are accepted. Custom protocols, file/data/javascript and lookalike hosts
/// are never inferred to be bundled assets.
pub fn is_navigation_allowed(authorized: Option<&Url>, target: &Url) -> bool {
    if !target.username().is_empty() || target.password().is_some() {
        return false;
    }
    #[cfg(any(target_os = "windows", target_os = "android"))]
    let bundled = target.scheme() == "http"
        && target.host_str() == Some("tauri.localhost")
        && target.port_or_known_default() == Some(80);
    #[cfg(not(any(target_os = "windows", target_os = "android")))]
    let bundled = target.scheme() == "tauri"
        && target.host_str() == Some("localhost")
        && target.port().is_none();
    bundled || authorized.is_some_and(|origin| same_origin(origin, target))
}

/// Only authorization-code URLs for the two reviewed providers may leave
/// the webview, and their redirect must return to this exact live instance.
pub fn is_external_oauth_allowed(authorized: Option<&Url>, target: &Url) -> bool {
    let Some(origin) = authorized else {
        return false;
    };
    if target.scheme() != "https"
        || target.port_or_known_default() != Some(443)
        || !target.username().is_empty()
        || target.password().is_some()
        || target.fragment().is_some()
    {
        return false;
    }
    let provider = match (target.host_str(), target.path()) {
        (Some("accounts.google.com"), "/o/oauth2/v2/auth") => "google",
        (Some("www.facebook.com"), path) if is_meta_oauth_path(path) => "meta",
        _ => return false,
    };
    let pairs: Vec<_> = target.query_pairs().collect();
    let unique: std::collections::HashSet<_> = pairs.iter().map(|(key, _)| key.as_ref()).collect();
    if unique.len() != pairs.len() {
        return false;
    }
    let value = |name| {
        pairs
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_ref())
    };
    if value("response_type") != Some("code") || value("client_id").map_or(true, str::is_empty) {
        return false;
    }
    let Some(state) = value("state") else {
        return false;
    };
    if !(32..=128).contains(&state.len())
        || !state
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return false;
    }
    let Some(redirect) = value("redirect_uri").and_then(|uri| Url::parse(uri).ok()) else {
        return false;
    };
    same_origin(origin, &redirect)
        && redirect.username().is_empty()
        && redirect.password().is_none()
        && redirect.query().is_none()
        && redirect.fragment().is_none()
        && redirect.path() == format!("/ads/api/v1/platform-accounts/{provider}/reconnect/callback")
}

/// The one provider access-recovery link exposed by the Ads panel. It is not
/// an OAuth endpoint and grants no permission to other Cloud paths or queries.
fn is_external_help_allowed(authorized: Option<&Url>, target: &Url) -> bool {
    authorized.is_some()
        && target.as_str() == "https://console.cloud.google.com/google/ads-apis/overview"
}

fn is_meta_oauth_path(path: &str) -> bool {
    path.strip_prefix("/v")
        .and_then(|rest| rest.strip_suffix("/dialog/oauth"))
        .and_then(|version| version.split_once('.'))
        .is_some_and(|(major, minor)| {
            !major.is_empty()
                && !minor.is_empty()
                && major.bytes().all(|c| c.is_ascii_digit())
                && minor.bytes().all(|c| c.is_ascii_digit())
        })
}

fn oauth_open_failed(app: &AppHandle) {
    // Native dialog: the remote product cannot listen to privileged Tauri events.
    app.dialog().message("No se pudo abrir esta conexión de forma segura. Vuelve a iniciarla desde Anuncios y comprueba que hay un navegador disponible.").title("Conexión de Anuncios").show(|_| {});
}

fn open_oauth_in_system_browser(app: &AppHandle, url: &Url) {
    // No shell and no PATH lookup. Never log the URL (it contains OAuth state).
    #[cfg(target_os = "macos")]
    let result = std::process::Command::new("/usr/bin/open")
        .arg("--")
        .arg(url.as_str())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
    #[cfg(target_os = "linux")]
    let result = std::process::Command::new("/usr/bin/xdg-open")
        .arg(url.as_str())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    match result {
        Ok(mut child) => {
            let app = app.clone();
            std::thread::spawn(move || {
                if !child.wait().is_ok_and(|status| status.success()) {
                    oauth_open_failed(&app);
                }
            });
        }
        Err(_) => {
            oauth_open_failed(app);
        }
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = url;
        oauth_open_failed(app);
    }
}

/// Builds the single product window: bundled shell UI first, navigation
/// locked to `policy`'s authorized origin, closing hides instead of quitting
/// (FR-030, research.md: "cerrar la ventana no detiene el motor").
pub fn create_main_window(app: &AppHandle, policy: WindowPolicy) -> tauri::Result<WebviewWindow> {
    let popup_policy = policy.clone();
    let oauth_app = app.clone();
    let window =
        WebviewWindowBuilder::new(app, MAIN_WINDOW_LABEL, WebviewUrl::App("index.html".into()))
            .title("Safent")
            .inner_size(1280.0, 860.0)
            .min_inner_size(900.0, 600.0)
            .initialization_script(BLOCK_CONTEXT_MENU_JS)
            .initialization_script(crate::update::availability::initialization_script(
                crate::update::native::configured(app),
            ))
            .on_navigation(move |url| is_navigation_allowed(policy.authorized().as_ref(), url))
            .on_new_window(move |url, _features| {
                let origin = popup_policy.authorized();
                if is_external_oauth_allowed(origin.as_ref(), &url)
                    || is_external_help_allowed(origin.as_ref(), &url)
                {
                    open_oauth_in_system_browser(&oauth_app, &url);
                } else if matches!(
                    url.host_str(),
                    Some("accounts.google.com" | "www.facebook.com")
                ) {
                    oauth_open_failed(&oauth_app);
                }
                tauri::webview::NewWindowResponse::Deny
            })
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
    let update_item = MenuItem::with_id(
        app,
        "check-native-update",
        "Buscar actualizaciones de la app…",
        true,
        None::<&str>,
    )?;
    let menu = Menu::with_items(app, &[&open_item, &update_item, &restart_item, &quit_item])?;

    let mut tray = TrayIconBuilder::new()
        .menu(&menu)
        .show_menu_on_left_click(true)
        .tooltip("Safent")
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => focus_existing(app),
            "check-native-update" => crate::update::native::check_from_tray(app.clone()),
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

    #[test]
    fn only_exact_google_ads_access_help_may_open_outside_the_live_app() {
        let origin = Url::parse("http://127.0.0.1:35335/").unwrap();
        let help = Url::parse("https://console.cloud.google.com/google/ads-apis/overview").unwrap();
        assert!(is_external_help_allowed(Some(&origin), &help));
        assert!(!is_external_help_allowed(None, &help));
        assert!(!is_external_oauth_allowed(Some(&origin), &help));
        assert!(!is_navigation_allowed(Some(&origin), &help));
        for target in [
            "http://console.cloud.google.com/google/ads-apis/overview",
            "https://console.cloud.google.com:444/google/ads-apis/overview",
            "https://console.cloud.google.com.evil.example/google/ads-apis/overview",
            "https://user@console.cloud.google.com/google/ads-apis/overview",
            "https://console.cloud.google.com/google/ads-apis/overview/",
            "https://console.cloud.google.com/google/ads-apis/overview?next=https://evil.example",
            "https://console.cloud.google.com/google/ads-apis/overview#fragment",
            "https://console.cloud.google.com/compute/instances",
            "https://console.cloud.google.com/google/ads-apis/%6fverview",
        ] {
            assert!(
                !is_external_help_allowed(Some(&origin), &Url::parse(target).unwrap()),
                "{target}"
            );
        }
    }

    fn oauth_url(provider: &str, redirect: &str) -> Url {
        let endpoint = if provider == "google" {
            "https://accounts.google.com/o/oauth2/v2/auth"
        } else {
            "https://www.facebook.com/v26.0/dialog/oauth"
        };
        let mut target = Url::parse(endpoint).unwrap();
        target
            .query_pairs_mut()
            .append_pair("client_id", "client")
            .append_pair("response_type", "code")
            .append_pair("state", &"a".repeat(43))
            .append_pair("redirect_uri", redirect);
        target
    }

    #[test]
    fn oauth_popup_only_opens_reviewed_providers_returning_to_this_instance() {
        let origin = Url::parse("http://127.0.0.1:35335/").unwrap();
        for provider in ["google", "meta"] {
            let redirect = format!(
                "http://127.0.0.1:35335/ads/api/v1/platform-accounts/{provider}/reconnect/callback"
            );
            let target = oauth_url(provider, &redirect);
            assert!(is_external_oauth_allowed(Some(&origin), &target));
            assert!(!is_external_oauth_allowed(None, &target));
            assert!(!is_navigation_allowed(Some(&origin), &target));
            for invalid in [
                "https://evil.example/callback",
                "http://127.0.0.1:9999/ads/api/v1/platform-accounts/google/reconnect/callback",
                "http://127.0.0.1:35335/api/v1/auth/login",
            ] {
                assert!(!is_external_oauth_allowed(
                    Some(&origin),
                    &oauth_url(provider, invalid)
                ));
            }
            let mut forged = target.clone();
            forged.query_pairs_mut().append_pair("state", "duplicate");
            assert!(!is_external_oauth_allowed(Some(&origin), &forged));
            forged = target.clone();
            forged
                .set_host(Some("accounts.google.com.evil.example"))
                .unwrap();
            assert!(!is_external_oauth_allowed(Some(&origin), &forged));
            forged = target;
            forged.set_username("userinfo").unwrap();
            assert!(!is_external_oauth_allowed(Some(&origin), &forged));
        }
        assert!(is_meta_oauth_path("/v27.1/dialog/oauth"));
        for invalid in [
            "/v27/dialog/oauth",
            "/v../dialog/oauth",
            "/v26.0/other",
            "/v26.0/dialog/oauth/extra",
        ] {
            assert!(!is_meta_oauth_path(invalid));
        }
    }

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
    fn denies_untrusted_schemes_and_bundled_origin_lookalikes() {
        for target in [
            "data:text/html,hello",
            "javascript:alert(1)",
            "file:///tmp/page.html",
            "about:blank",
            "custom://localhost/index.html",
            "tauri://evil/index.html",
            "tauri://user@localhost/index.html",
            "tauri://localhost:88/index.html",
            "http://tauri.localhost.evil/index.html",
        ] {
            assert!(!is_navigation_allowed(None, &url(target)), "{target}");
        }
    }

    #[test]
    fn always_allows_the_app_own_bundled_asset_scheme() {
        // Whatever exact custom scheme Tauri resolves for WebviewUrl::App on
        // this platform/version — never http(s) — must never be blocked by
        // this policy; it is the shell's own preparation/failure screens.
        #[cfg(not(any(target_os = "windows", target_os = "android")))]
        assert!(is_navigation_allowed(
            None,
            &url("tauri://localhost/index.html")
        ));
        #[cfg(not(any(target_os = "windows", target_os = "android")))]
        assert!(is_navigation_allowed(
            Some(&url("http://127.0.0.1:17517/")),
            &url("tauri://localhost/index.html")
        ));
        #[cfg(any(target_os = "windows", target_os = "android"))]
        assert!(is_navigation_allowed(
            None,
            &url("http://tauri.localhost/index.html")
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
