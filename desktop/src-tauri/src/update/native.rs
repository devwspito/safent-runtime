//! App-only updater. The engine/Ads transaction is deliberately NOT invoked.
//! Download URLs and signatures stay host-owned; only a checked ID crosses IPC.
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewWindow};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::{Update, UpdaterExt};

const ENDPOINT: &str =
    "https://github.com/devwspito/safent-runtime/releases/latest/download/latest.json";
const CHECK_TTL: Duration = Duration::from_secs(15 * 60);

#[derive(Debug, serde::Serialize)]
pub struct CheckResult {
    status: &'static str,
    app_version: String,
    version: Option<String>,
    check_id: Option<u64>,
}

struct Pending<T> {
    id: u64,
    checked_at: Instant,
    update: T,
}
struct Snapshot<T> {
    generation: u64,
    pending: Option<Pending<T>>,
}
impl<T> Default for Snapshot<T> {
    fn default() -> Self {
        Self {
            generation: 0,
            pending: None,
        }
    }
}
impl<T> Snapshot<T> {
    fn invalidate(&mut self) -> Result<u64, &'static str> {
        self.pending = None;
        self.generation = self
            .generation
            .checked_add(1)
            .ok_or("updater_unavailable")?;
        Ok(self.generation)
    }
    fn take(&mut self, id: u64, now: Instant) -> Result<T, &'static str> {
        let pending = self.pending.as_ref().ok_or("check_required")?;
        if pending.id != id || now.duration_since(pending.checked_at) >= CHECK_TTL {
            return Err("check_required");
        }
        Ok(self.pending.take().ok_or("check_required")?.update)
    }
}

#[derive(Default)]
pub struct NativeUpdater {
    busy: Arc<AtomicBool>,
    snapshot: Mutex<Snapshot<Update>>,
}
struct Flight(Arc<AtomicBool>);
impl Drop for Flight {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}
fn begin(busy: &Arc<AtomicBool>) -> Result<Flight, &'static str> {
    busy.compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|_| "updater_busy")?;
    Ok(Flight(busy.clone()))
}

/// Fixed release origin and real signing key, never webview/env parameters.
pub fn configuration_ready(config: &serde_json::Value) -> bool {
    let cfg = &config["plugins"]["updater"];
    let Ok(typed) = serde_json::from_value::<tauri_plugin_updater::Config>(cfg.clone()) else {
        return false;
    };
    cfg["endpoints"] == serde_json::json!([ENDPOINT])
        && super::tauri_updater::RuntimeManifestVerifier::from_tauri_pubkey(&typed.pubkey).is_ok()
        && !typed.dangerous_insecure_transport_protocol
        && !typed.dangerous_accept_invalid_certs
        && !typed.dangerous_accept_invalid_hostnames
}

pub fn configured(app: &tauri::AppHandle) -> bool {
    // This is the merged, embedded build configuration, including the signed
    // release pipeline overlay. Source include_str would ignore that overlay.
    configuration_ready(
        &serde_json::json!({"plugins":{"updater":app.config().plugins.0.get("updater")}}),
    )
}

fn valid_artifact(url: &tauri::Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("github.com")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url
            .path()
            .starts_with("/devwspito/safent-runtime/releases/download/")
}

fn local_caller(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|_| "updater_unavailable")?;
    let allowed = window.label() == "main" && local_origin(&url);
    if allowed {
        Ok(())
    } else {
        Err("updater_forbidden".into())
    }
}
fn local_origin(url: &tauri::Url) -> bool {
    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (matches!(url.scheme(), "http" | "https")
            && url.host_str() == Some("tauri.localhost")
            && url.port().is_none())
}

async fn check(app: &tauri::AppHandle) -> Result<CheckResult, String> {
    let state = app.state::<NativeUpdater>();
    let _flight = begin(&state.busy)?;
    let id = state
        .snapshot
        .lock()
        .map_err(|_| "updater_unavailable")?
        .invalidate()?;
    if !configured(app) {
        return Err("updater_not_configured".into());
    }
    let current = app.package_info().version.clone();
    let mut update = app
        .updater_builder()
        .timeout(Duration::from_secs(30))
        .no_proxy()
        .configure_client(|client| client.https_only(true))
        .build()
        .map_err(|_| "updater_not_configured")?
        .check()
        .await
        .map_err(|_| "update_check_failed")?;
    let Some(mut checked) = update.take() else {
        return Ok(CheckResult {
            status: "up_to_date",
            app_version: current.to_string(),
            version: None,
            check_id: None,
        });
    };
    // check() reads unsigned metadata. Only download() authenticates bytes.
    let platform = super::tauri_updater::app_platform_key().ok_or("unsupported_platform")?;
    let manifest = super::tauri_updater::tauri_manifest_from_check(
        Some(super::tauri_updater::CheckedAppUpdate {
            version: checked.version.clone(),
            signature: checked.signature.clone(),
            download_url: checked.download_url.to_string(),
        }),
        &platform,
        &current,
    )
    .map_err(|_| "invalid_update_metadata")?;
    if manifest.version <= current
        || !valid_artifact(&checked.download_url)
        || !super::tauri_updater::tauri_signature_well_formed(&checked.signature)
    {
        return Err("invalid_update_metadata".into());
    }
    checked.timeout = Some(Duration::from_secs(10 * 60));
    let version = checked.version.clone();
    state
        .snapshot
        .lock()
        .map_err(|_| "updater_unavailable")?
        .pending = Some(Pending {
        id,
        checked_at: Instant::now(),
        update: checked,
    });
    Ok(CheckResult {
        status: "available",
        app_version: current.to_string(),
        version: Some(version),
        check_id: Some(id),
    })
}

#[derive(Debug, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InstallResult {
    Cancelled,
}

async fn install(app: &tauri::AppHandle, check_id: u64) -> Result<InstallResult, String> {
    let state = app.state::<NativeUpdater>();
    let _flight = begin(&state.busy)?;
    if !configured(app) {
        return Err("updater_not_configured".into());
    }
    // Shared exclusion with bootstrap: cannot replace the bundled CLI while
    // bootstrap/repair is executing it, nor start repair during replacement.
    let control = app
        .try_state::<crate::bootstrap_control::BootstrapControl>()
        .ok_or("bootstrap_unavailable")?;
    let _bootstrap_guard = control.reserve_update()?;
    let (checked, expires_at) = {
        let mut snapshot = state.snapshot.lock().map_err(|_| "updater_unavailable")?;
        let expires_at = snapshot
            .pending
            .as_ref()
            .ok_or("check_required")?
            .checked_at
            + CHECK_TTL;
        (snapshot.take(check_id, Instant::now())?, expires_at)
    };
    let handle = app.clone();
    let version = checked.version.clone();
    let confirmed = tauri::async_runtime::spawn_blocking(move || {
        handle.dialog().message(format!(
            "¿Descargar e instalar Safent {version}? La app se cerrara y reiniciara. Guarda lo que estes escribiendo. El motor y Ads no se actualizan con esta accion."
        )).title("Actualizar la app Safent")
            .buttons(MessageDialogButtons::OkCancelCustom("Instalar y reiniciar".into(), "Ahora no".into()))
            .blocking_show()
    }).await.map_err(|_| "confirmation_failed")?;
    if !confirmed {
        return Ok(InstallResult::Cancelled);
    }
    if Instant::now() >= expires_at {
        return Err("check_required".into());
    }
    // No install path accepts caller bytes; plugin verifies minisign BEFORE
    // yielding this buffer. Failure consumes the snapshot; no automatic retry.
    let bytes = checked
        .download(|_, _| {}, || {})
        .await
        .map_err(|_| "update_download_or_signature_failed")?;
    tauri::async_runtime::spawn_blocking(move || checked.install(bytes))
        .await
        .map_err(|_| "update_install_failed")?
        .map_err(|_| "update_install_failed")?;
    app.restart();
}

#[tauri::command]
pub async fn check_native_update(
    app: tauri::AppHandle,
    window: WebviewWindow,
) -> Result<CheckResult, String> {
    local_caller(&window)?;
    check(&app).await
}
#[tauri::command]
pub async fn install_native_update(
    app: tauri::AppHandle,
    window: WebviewWindow,
    check_id: u64,
) -> Result<InstallResult, String> {
    local_caller(&window)?;
    install(&app, check_id).await
}

/// Tray is a native human gesture, not an IPC permission for the product page.
pub fn check_from_tray(app: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let result = check(&app).await;
        let message = match result {
            Ok(CheckResult {
                check_id: Some(id), ..
            }) => match install(&app, id).await {
                Ok(InstallResult::Cancelled) => return,
                Err(code) => error_message(&code),
            },
            Ok(_) => "No hay una version mas reciente de la app nativa.",
            Err(code) => error_message(&code),
        };
        app.dialog()
            .message(message)
            .title("Actualizaciones de Safent")
            .show(|_| {});
    });
}
fn error_message(code: &str) -> &'static str {
    match code {
        "updater_busy" => "Ya hay una comprobacion o actualizacion en curso.",
        "bootstrap_in_progress" => "Espera a que termine el arranque o reparacion antes de actualizar.",
        "updater_not_configured" => "Esta compilacion no tiene una configuracion de actualizacion firmada valida.",
        "update_install_failed" => "No se pudo completar la instalacion. Cierra y vuelve a abrir Safent; comprueba la version instalada antes de reintentar.",
        _ => "No se pudo verificar o descargar la actualizacion. La instalacion no se ha iniciado. Vuelve a comprobar cuando tengas conexion.",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn snapshot_is_exact_one_shot_and_expires() {
        let mut snapshot = Snapshot::default();
        let id = snapshot.invalidate().unwrap();
        let now = Instant::now();
        snapshot.pending = Some(Pending {
            id,
            checked_at: now,
            update: "signed-host-snapshot",
        });
        assert_eq!(snapshot.take(id + 1, now), Err("check_required"));
        assert_eq!(snapshot.take(id, now), Ok("signed-host-snapshot"));
        assert_eq!(snapshot.take(id, now), Err("check_required"));
        snapshot.pending = Some(Pending {
            id,
            checked_at: now,
            update: "old",
        });
        assert_eq!(snapshot.take(id, now + CHECK_TTL), Err("check_required"));
        snapshot.invalidate().unwrap();
        assert_eq!(snapshot.take(id, now), Err("check_required"));
    }
    #[test]
    fn failed_or_cancelled_attempt_releases_singleflight() {
        let busy = Arc::new(AtomicBool::new(false));
        let flight = begin(&busy).unwrap();
        assert!(begin(&busy).is_err());
        drop(flight);
        assert!(begin(&busy).is_ok());
    }
    #[test]
    fn simultaneous_check_install_admissions_have_one_winner() {
        let busy = Arc::new(AtomicBool::new(false));
        let barrier = Arc::new(std::sync::Barrier::new(3));
        let threads: Vec<_> = (0..2)
            .map(|_| {
                let busy = busy.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    let guard = begin(&busy);
                    barrier.wait();
                    guard.is_ok()
                })
            })
            .collect();
        barrier.wait();
        barrier.wait();
        assert_eq!(
            threads
                .into_iter()
                .map(|thread| usize::from(thread.join().unwrap()))
                .sum::<usize>(),
            1
        );
    }
    #[test]
    fn updater_permissions_never_reach_remote_product_page() {
        let local: serde_json::Value =
            serde_json::from_str(include_str!("../../capabilities/default.json")).unwrap();
        let remote: serde_json::Value =
            serde_json::from_str(include_str!("../../capabilities/remote-ui.json")).unwrap();
        for command in ["allow-check-native-update", "allow-install-native-update"] {
            assert!(local["permissions"]
                .as_array()
                .unwrap()
                .contains(&serde_json::json!(command)));
            assert!(!remote["permissions"]
                .as_array()
                .unwrap()
                .contains(&serde_json::json!(command)));
        }
        for capability in [local, remote] {
            assert!(!capability["permissions"]
                .as_array()
                .unwrap()
                .iter()
                .any(|p| p.as_str().unwrap_or("").starts_with("updater:")));
        }
    }
    #[test]
    fn untrusted_origins_and_artifacts_are_rejected() {
        for url in [
            "http://localhost:17517",
            "http://127.0.0.1:17517",
            "https://evil.test",
            "file:///tmp/index.html",
        ] {
            assert!(!local_origin(&url.parse().unwrap()));
        }
        assert!(local_origin(
            &"tauri://localhost/index.html".parse().unwrap()
        ));
        assert!(local_origin(
            &"http://tauri.localhost/index.html".parse().unwrap()
        ));
        let good =
            "https://github.com/devwspito/safent-runtime/releases/download/v0.9.2/Safent.tar.gz";
        assert!(valid_artifact(&good.parse().unwrap()));
        for bad in [
            good.replace("https:", "http:"),
            good.replace("github.com", "evil.test"),
            good.replace("devwspito", "attacker"),
            format!("{good}?token=x"),
        ] {
            assert!(!valid_artifact(&bad.parse().unwrap()));
        }
    }
    #[test]
    fn config_requires_real_key_fixed_endpoint_and_tls() {
        use base64::Engine;
        let key = base64::engine::general_purpose::STANDARD.encode(
            "untrusted comment: test key\nRWT/DhbH+Js0LLsDbBHk04vOWy8yegcD+AYxhHFhiD3HlNqFuI5hpd7N\n");
        let good = serde_json::json!({"plugins":{"updater":{"pubkey":key,"endpoints":[ENDPOINT]}}});
        assert!(configuration_ready(&good));
        for field in [
            "dangerousInsecureTransportProtocol",
            "dangerousAcceptInvalidCerts",
            "dangerousAcceptInvalidHostnames",
            "dangerous-insecure-transport-protocol",
            "dangerous-accept-invalid-certs",
            "dangerous-accept-invalid-hostnames",
        ] {
            let mut bad = good.clone();
            bad["plugins"]["updater"][field] = true.into();
            assert!(!configuration_ready(&bad));
        }
        let mut bad = good.clone();
        bad["plugins"]["updater"]["pubkey"] = "__TAURI_UPDATER_PUBKEY__".into();
        assert!(!configuration_ready(&bad));
        let mut bad = good;
        bad["plugins"]["updater"]["endpoints"] = serde_json::json!(["https://evil.test"]);
        assert!(!configuration_ready(&bad));
    }
}
