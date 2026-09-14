//! Owner-confirmed hard caps. No web path, YAML, CLI verb or autonomy flag is accepted.
use crate::engine_adapter::{EmbeddedCliConfig, EmbeddedCliDriver};
use crate::ports::CancelSignal;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::{Arc, Condvar, Mutex};
use tauri::{Manager, WebviewWindow};
use tauri_plugin_dialog::MessageDialogButtons;

const UNAVAILABLE: &str = "ads_caps_unavailable";
const INVALID: &str = "ads_caps_invalid";
const CHANGED: &str = "ads_caps_changed";
const MAX_BYTES: usize = 128 * 1024;
const MAX_MINOR: u64 = 1_000_000_000_000;

#[derive(Default)]
pub struct AdsCapsState {
    config: Mutex<Option<EmbeddedCliConfig>>,
    activity: Arc<CapsActivity>,
}
impl AdsCapsState {
    pub(crate) fn configure(&self, config: EmbeddedCliConfig) {
        if let Ok(mut state) = self.config.lock() {
            *state = Some(config);
        }
    }

    pub(crate) fn begin_close(&self) {
        self.activity.begin_close();
    }

    pub(crate) fn wait_idle(&self) {
        self.activity.wait_idle();
    }
}

#[derive(Default)]
struct CapsActivity {
    // (operation in flight, normal exit requested)
    state: Mutex<(bool, bool)>,
    wake: Condvar,
}
impl CapsActivity {
    fn begin(self: &Arc<Self>) -> Result<CapsFlight, String> {
        let mut state = self.state.lock().map_err(|_| UNAVAILABLE.to_string())?;
        if state.0 || state.1 {
            return Err("ads_caps_busy".into());
        }
        state.0 = true;
        Ok(CapsFlight(self.clone()))
    }

    fn begin_close(&self) {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).1 = true;
    }

    fn wait_idle(&self) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        while state.0 {
            state = self.wake.wait(state).unwrap_or_else(|e| e.into_inner());
        }
    }
}

struct CapsFlight(Arc<CapsActivity>);
impl Drop for CapsFlight {
    fn drop(&mut self) {
        self.0.state.lock().unwrap_or_else(|e| e.into_inner()).0 = false;
        self.0.wake.notify_all();
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Defaults {
    max_step_pct: f64,
    max_changes_per_day: u32,
    autonomy_enabled: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AccountCaps {
    daily_cap_minor: u64,
    monthly_cap_minor: u64,
    floor_minor: u64,
    ceiling_minor: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_step_pct: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    max_changes_per_day: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    autonomy_enabled: Option<bool>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Document {
    defaults: Defaults,
    accounts: BTreeMap<String, AccountCaps>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CapsChange {
    revision: String,
    platform: String,
    account_id: String,
    currency: String,
    daily_cap_minor: u64,
    monthly_cap_minor: u64,
    floor_minor: u64,
    ceiling_minor: u64,
    max_step_pct: f64,
    max_changes_per_day: u32,
}
#[derive(Serialize)]
pub struct CapsSnapshot {
    revision: String,
    loaded_digest: Option<String>,
    accounts: BTreeMap<String, AccountCaps>,
}
#[derive(Serialize)]
pub struct CapsSaved {
    revision: String,
    loaded_digest: String,
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn behavior(step: f64, changes: u32) -> bool {
    step.is_finite() && step > 0.0 && step <= 100.0 && changes > 0 && changes <= 10_000
}
fn validate_document(doc: &Document) -> Result<(), String> {
    if !behavior(doc.defaults.max_step_pct, doc.defaults.max_changes_per_day)
        || doc.accounts.len() > 1000
    {
        return Err(INVALID.into());
    }
    for (key, cap) in &doc.accounts {
        if key.is_empty()
            || key.len() > 128
            || key.chars().any(char::is_control)
            || [
                cap.daily_cap_minor,
                cap.monthly_cap_minor,
                cap.floor_minor,
                cap.ceiling_minor,
            ]
            .iter()
            .any(|n| *n > MAX_MINOR)
            || cap.floor_minor > cap.ceiling_minor
            || !behavior(
                cap.max_step_pct.unwrap_or(doc.defaults.max_step_pct),
                cap.max_changes_per_day
                    .unwrap_or(doc.defaults.max_changes_per_day),
            )
        {
            return Err(INVALID.into());
        }
    }
    Ok(())
}
fn parse(bytes: &[u8]) -> Result<Document, String> {
    if bytes.len() > MAX_BYTES {
        return Err(INVALID.into());
    }
    let text = std::str::from_utf8(bytes).map_err(|_| INVALID.to_string())?;
    // The caps schema has no aliases, anchors or custom tags. Do not expand
    // arbitrary YAML graphs received through a hand-edited host document.
    if text
        .lines()
        .filter(|line| !line.trim_start().starts_with('#'))
        .any(|line| line.contains(['&', '*', '!']))
    {
        return Err(INVALID.into());
    }
    let doc: Document = serde_yaml::from_str(text).map_err(|_| INVALID.to_string())?;
    validate_document(&doc)?;
    Ok(doc)
}
fn exponent(currency: &str) -> Option<u32> {
    match currency {
        "EUR" | "USD" | "GBP" | "CHF" | "CAD" | "AUD" | "NZD" | "MXN" | "BRL" | "ARS" | "COP"
        | "PEN" | "UYU" | "CNY" | "HKD" | "SGD" | "INR" | "ZAR" | "SEK" | "NOK" | "DKK" | "PLN"
        | "CZK" | "HUF" | "RON" | "TRY" | "ILS" | "AED" | "SAR" | "THB" | "PHP" | "IDR" | "MYR" => {
            Some(2)
        }
        _ => None,
    }
}
fn validate_change(change: &CapsChange) -> Result<(), String> {
    let digits = match change.platform.as_str() {
        "google" if change.account_id.len() == 10 => Some(change.account_id.as_str()),
        "meta" => change
            .account_id
            .strip_prefix("act_")
            .filter(|id| (5..=30).contains(&id.len())),
        _ => None,
    };
    if !valid_digest(&change.revision)
        || !digits.is_some_and(|id| id.bytes().all(|b| b.is_ascii_digit()))
        || exponent(&change.currency).is_none()
        || !behavior(change.max_step_pct, change.max_changes_per_day)
        || change.daily_cap_minor == 0
        || change.monthly_cap_minor == 0
        || change.ceiling_minor == 0
        || [
            change.daily_cap_minor,
            change.monthly_cap_minor,
            change.floor_minor,
            change.ceiling_minor,
        ]
        .iter()
        .any(|n| *n > MAX_MINOR)
        || change.floor_minor > change.ceiling_minor
    {
        return Err(INVALID.into());
    }
    Ok(())
}
fn changed_bytes(previous: &[u8], change: &CapsChange) -> Result<Vec<u8>, String> {
    validate_change(change)?;
    if digest(previous) != change.revision {
        return Err(CHANGED.into());
    }
    let mut doc = parse(previous)?;
    doc.accounts.insert(
        change.account_id.clone(),
        AccountCaps {
            daily_cap_minor: change.daily_cap_minor,
            monthly_cap_minor: change.monthly_cap_minor,
            floor_minor: change.floor_minor,
            ceiling_minor: change.ceiling_minor,
            max_step_pct: Some(change.max_step_pct),
            max_changes_per_day: Some(change.max_changes_per_day),
            autonomy_enabled: Some(false),
        },
    );
    let bytes = serde_yaml::to_string(&doc)
        .map_err(|_| INVALID.to_string())?
        .into_bytes();
    parse(&bytes)?;
    Ok(bytes)
}
fn amount(minor: u64, currency: &str) -> String {
    let exp = exponent(currency).unwrap_or(2);
    let divisor = 10_u64.pow(exp);
    if exp == 0 {
        format!("{minor} {currency}")
    } else {
        format!(
            "{}.{:0width$} {currency}",
            minor / divisor,
            minor % divisor,
            width = exp as usize
        )
    }
}
fn confirm_text(change: &CapsChange) -> String {
    format!("Cuenta {} · {}\nMoneda: {}\n\nTope diario: {}\nTope mensual: {}\nPresupuesto mínimo por campaña: {}\nPresupuesto máximo por campaña: {}\nCambio máximo: {} %\nCambios por día: {}\n\nNo activa campañas ni ejecución automática. Cada propuesta seguirá requiriendo aprobación. Anuncios se interrumpirá brevemente mientras se verifican estos límites.",
        change.platform,change.account_id,change.currency,
        amount(change.daily_cap_minor,&change.currency),amount(change.monthly_cap_minor,&change.currency),
        amount(change.floor_minor,&change.currency),amount(change.ceiling_minor,&change.currency),change.max_step_pct,change.max_changes_per_day)
}
fn caller(window: &WebviewWindow) -> Result<EmbeddedCliConfig, String> {
    let url = window.url().map_err(|_| UNAVAILABLE.to_string())?;
    if !window
        .state::<crate::window_policy::WindowPolicy>()
        .allows_host_folder(window.label(), &url)
    {
        return Err(UNAVAILABLE.into());
    }
    window
        .state::<AdsCapsState>()
        .config
        .lock()
        .map_err(|_| UNAVAILABLE.to_string())?
        .clone()
        .filter(|config| config.companion_image.is_some())
        .ok_or(UNAVAILABLE.into())
}

#[tauri::command]
pub async fn get_ads_hard_caps(window: WebviewWindow) -> Result<CapsSnapshot, String> {
    let config = caller(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let file = caps_file::CapsFile::open(&config.state_home)?;
        let bytes = file.read()?;
        let doc = parse(&bytes)?;
        let loaded = EmbeddedCliDriver::new(config).ads_caps_status().ok();
        let mut accounts = doc.accounts;
        for cap in accounts.values_mut() {
            cap.max_step_pct = Some(cap.max_step_pct.unwrap_or(doc.defaults.max_step_pct));
            cap.max_changes_per_day = Some(
                cap.max_changes_per_day
                    .unwrap_or(doc.defaults.max_changes_per_day),
            );
            cap.autonomy_enabled = Some(
                cap.autonomy_enabled
                    .unwrap_or(doc.defaults.autonomy_enabled),
            );
        }
        Ok(CapsSnapshot {
            revision: digest(&bytes),
            loaded_digest: loaded,
            accounts,
        })
    })
    .await
    .map_err(|_| UNAVAILABLE.to_string())?
}

#[tauri::command]
pub async fn save_ads_hard_caps(
    window: WebviewWindow,
    change: CapsChange,
) -> Result<Option<CapsSaved>, String> {
    let config = caller(&window)?;
    validate_change(&change)?;
    // Register before yielding so a normal app exit cannot race the worker.
    // The guard also covers the native confirmation and any verified rollback.
    let flight = window.state::<AdsCapsState>().activity.begin()?;
    let control = window
        .state::<crate::bootstrap_control::BootstrapControl>()
        .inner()
        .clone();
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _flight = flight;
        let _exclusive = control
            .reserve_companion(CancelSignal::new())
            .map_err(|_| "ads_caps_busy".to_string())?;
        let file = caps_file::CapsFile::open(&config.state_home)?;
        let previous = file.read()?;
        let next = changed_bytes(&previous, &change)?;
        if !crate::dialogs::message(&app, confirm_text(&change))
            .title("Confirmar límites de gasto")
            .buttons(MessageDialogButtons::OkCancel)
            .blocking_show()
        {
            return Ok(None);
        }
        let driver = EmbeddedCliDriver::new(config);
        apply_with_rollback(&file, &previous, &next, |hash| {
            driver
                .reload_ads_caps(hash)
                .map_err(|_| UNAVAILABLE.to_string())
        })?;
        let revision = digest(&next);
        Ok(Some(CapsSaved {
            revision: revision.clone(),
            loaded_digest: revision,
        }))
    })
    .await
    .map_err(|_| UNAVAILABLE.to_string())?
}
fn apply_with_rollback(
    file: &caps_file::CapsFile,
    old: &[u8],
    next: &[u8],
    mut reload: impl FnMut(&str) -> Result<String, String>,
) -> Result<(), String> {
    let old_hash = digest(old);
    let new_hash = digest(next);
    let written = file.replace(&old_hash, next);
    if let Err(error) = &written {
        // An fsync/readback failure may happen after rename. Restore only if
        // the bytes now present are exactly ours; never clobber another edit.
        match file.read() {
            Ok(current) if digest(&current) == old_hash => return Err(error.clone()),
            Ok(current) if digest(&current) == new_hash => {}
            _ => return Err("ads_caps_rollback_unknown".into()),
        }
    }
    if written.is_ok()
        && matches!(reload(&new_hash),Ok(ref loaded) if loaded == &new_hash)
        && file
            .read()
            .is_ok_and(|current| digest(&current) == new_hash)
    {
        return Ok(());
    }
    // Never restore over a different owner's intervening edit.
    if file.replace(&new_hash, old).is_err() {
        return Err("ads_caps_rollback_unknown".into());
    }
    if matches!(reload(&old_hash),Ok(ref loaded) if loaded == &old_hash) {
        Err("ads_caps_restored".into())
    } else {
        Err("ads_caps_rollback_unknown".into())
    }
}

#[cfg(unix)]
mod caps_file {
    use super::*;
    use std::ffi::CString;
    use std::fs::{File, OpenOptions};
    use std::io::{Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Path;
    pub struct CapsFile {
        dir: File,
    }
    fn open_at(dir: &File, name: &str, directory: bool) -> Result<File, String> {
        let name = CString::new(name).map_err(|_| UNAVAILABLE.to_string())?;
        let fd = unsafe {
            libc::openat(
                dir.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY
                    | libc::O_CLOEXEC
                    | libc::O_NOFOLLOW
                    | libc::O_NONBLOCK
                    | if directory { libc::O_DIRECTORY } else { 0 },
            )
        };
        if fd < 0 {
            return Err(UNAVAILABLE.into());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    impl CapsFile {
        pub fn open(home: &Path) -> Result<Self, String> {
            let root = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC)
                .open(home)
                .map_err(|_| UNAVAILABLE.to_string())?;
            let companions = open_at(&root, "companions", true)?;
            Ok(Self {
                dir: open_at(&companions, "ads", true)?,
            })
        }
        pub fn read(&self) -> Result<Vec<u8>, String> {
            let mut file = open_at(&self.dir, "caps.yaml", false)?;
            let metadata = file.metadata().map_err(|_| UNAVAILABLE.to_string())?;
            if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() > MAX_BYTES as u64 {
                return Err(INVALID.into());
            }
            let mut bytes = Vec::new();
            (&mut file)
                .take(MAX_BYTES as u64 + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| UNAVAILABLE.to_string())?;
            if bytes.len() > MAX_BYTES {
                return Err(INVALID.into());
            }
            Ok(bytes)
        }
        pub fn replace(&self, expected: &str, bytes: &[u8]) -> Result<(), String> {
            if digest(&self.read()?) != expected {
                return Err(CHANGED.into());
            }
            parse(bytes)?;
            let mut random = [0u8; 16];
            File::open("/dev/urandom")
                .and_then(|mut f| f.read_exact(&mut random))
                .map_err(|_| UNAVAILABLE.to_string())?;
            let temp = CString::new(format!(
                ".caps-{}",
                random
                    .iter()
                    .map(|v| format!("{v:02x}"))
                    .collect::<String>()
            ))
            .unwrap();
            let name = CString::new("caps.yaml").unwrap();
            let fd = unsafe {
                libc::openat(
                    self.dir.as_raw_fd(),
                    temp.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_CLOEXEC
                        | libc::O_NOFOLLOW,
                    0o644,
                )
            };
            if fd < 0 {
                return Err(UNAVAILABLE.into());
            }
            let mut file = unsafe { File::from_raw_fd(fd) };
            let result = (|| {
                // openat's mode is filtered by the app's inherited umask.
                // These non-secret limits must remain readable by the broker
                // UID, including after rollback (same contract as provision.sh).
                if unsafe { libc::fchmod(file.as_raw_fd(), 0o644) } != 0 {
                    return Err(UNAVAILABLE.into());
                }
                file.write_all(bytes)
                    .and_then(|_| file.sync_all())
                    .map_err(|_| UNAVAILABLE.to_string())?;
                if digest(&self.read()?) != expected {
                    return Err(CHANGED.into());
                }
                if unsafe {
                    libc::renameat(
                        self.dir.as_raw_fd(),
                        temp.as_ptr(),
                        self.dir.as_raw_fd(),
                        name.as_ptr(),
                    )
                } != 0
                {
                    return Err(UNAVAILABLE.into());
                }
                self.dir.sync_all().map_err(|_| UNAVAILABLE.to_string())?;
                if digest(&self.read()?) != digest(bytes) {
                    return Err(CHANGED.into());
                }
                Ok(())
            })();
            unsafe {
                libc::unlinkat(self.dir.as_raw_fd(), temp.as_ptr(), 0);
            }
            result
        }
    }
}
#[cfg(not(unix))]
mod caps_file {
    use super::*;
    pub struct CapsFile;
    impl CapsFile {
        pub fn open(_: &std::path::Path) -> Result<Self, String> {
            Err(UNAVAILABLE.into())
        }
        pub fn read(&self) -> Result<Vec<u8>, String> {
            Err(UNAVAILABLE.into())
        }
        pub fn replace(&self, _: &str, _: &[u8]) -> Result<(), String> {
            Err(UNAVAILABLE.into())
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn normal_exit_drains_an_in_flight_operation_and_rejects_new_ones() {
        use std::sync::mpsc;
        use std::time::Duration;
        let activity = Arc::new(CapsActivity::default());
        let flight = activity.begin().unwrap();
        assert!(activity.begin().is_err());
        activity.begin_close();
        assert!(activity.begin().is_err());
        let (sent, received) = mpsc::channel();
        let waiting = activity.clone();
        let worker = std::thread::spawn(move || {
            waiting.wait_idle();
            sent.send(()).unwrap();
        });
        assert!(received.recv_timeout(Duration::from_millis(30)).is_err());
        drop(flight);
        received.recv_timeout(Duration::from_secs(2)).unwrap();
        worker.join().unwrap();
        assert!(activity.begin().is_err());
    }

    #[test]
    fn operation_guard_releases_on_error_and_idle_exit_needs_no_worker() {
        let activity = Arc::new(CapsActivity::default());
        let operation = || -> Result<(), String> {
            let _flight = activity.begin()?;
            Err(UNAVAILABLE.into())
        };
        assert!(operation().is_err());
        activity.wait_idle();
        assert!(activity.begin().is_ok());
        activity.begin_close();
        activity.wait_idle();
        assert!(activity.begin().is_err());
    }
    const ORIGINAL:&str="defaults:\n  max_step_pct: 30\n  max_changes_per_day: 2\n  autonomy_enabled: false\naccounts: {}\n";
    fn change() -> CapsChange {
        CapsChange {
            revision: digest(ORIGINAL.as_bytes()),
            platform: "google".into(),
            account_id: "1234567890".into(),
            currency: "EUR".into(),
            daily_cap_minor: 1000,
            monthly_cap_minor: 10000,
            floor_minor: 0,
            ceiling_minor: 1000,
            max_step_pct: 15.,
            max_changes_per_day: 2,
        }
    }
    fn file() -> (tempfile::TempDir, caps_file::CapsFile) {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("companions/ads")).unwrap();
        std::fs::write(root.path().join("companions/ads/caps.yaml"), ORIGINAL).unwrap();
        let file = caps_file::CapsFile::open(root.path()).unwrap();
        (root, file)
    }
    #[test]
    fn no_default_money_or_autonomy_is_invented() {
        let parsed = parse(ORIGINAL.as_bytes()).unwrap();
        assert!(parsed.accounts.is_empty());
        let changed = parse(&changed_bytes(ORIGINAL.as_bytes(), &change()).unwrap()).unwrap();
        let cap = &changed.accounts["1234567890"];
        assert_eq!(cap.daily_cap_minor, 1000);
        assert_eq!(cap.autonomy_enabled, Some(false));
        assert_eq!(changed.defaults, parsed.defaults);
    }
    #[test]
    fn rejects_unknown_yaml_aliases_and_wrong_numeric_types() {
        for raw in ["defaults: &x {}\naccounts: *x", "{}", "defaults: {max_step_pct: 30, max_changes_per_day: 2, autonomy_enabled: false}\naccounts: {}\nextra: true", "defaults: {max_step_pct: 30, max_changes_per_day: true, autonomy_enabled: false}\naccounts: {}"]{assert!(parse(raw.as_bytes()).is_err());}
    }
    #[test]
    fn input_rejects_paths_amounts_unknown_currency_and_stale_revision() {
        let mut c = change();
        c.account_id = "../x".into();
        assert!(validate_change(&c).is_err());
        c = change();
        c.currency = "ZZZ".into();
        assert!(validate_change(&c).is_err());
        c = change();
        c.floor_minor = 1001;
        assert!(validate_change(&c).is_err());
        c = change();
        c.revision = "0".repeat(64);
        assert!(changed_bytes(ORIGINAL.as_bytes(), &c).is_err());
        c = change();
        c.platform = "meta".into();
        c.account_id = "act_123456".into();
        assert!(validate_change(&c).is_ok());
    }
    #[test]
    fn preserves_unrelated_accounts_and_does_not_enable_auto() {
        let a = changed_bytes(ORIGINAL.as_bytes(), &change()).unwrap();
        let mut b = change();
        b.revision = digest(&a);
        b.platform = "meta".into();
        b.account_id = "act_123456".into();
        let doc = parse(&changed_bytes(&a, &b).unwrap()).unwrap();
        assert_eq!(doc.accounts.len(), 2);
        assert!(doc
            .accounts
            .values()
            .all(|a| a.autonomy_enabled == Some(false)));
    }
    #[test]
    fn owner_confirmation_contains_exact_currency_units() {
        let mut c = change();
        assert!(confirm_text(&c).contains("10.00 EUR"));
        c.currency = "JPY".into();
        assert!(validate_change(&c).is_err());
        c.currency = "KWD".into();
        assert!(validate_change(&c).is_err());
    }
    #[test]
    fn atomic_write_and_exact_loaded_ack() {
        let (_root, f) = file();
        let old = f.read().unwrap();
        let next = changed_bytes(&old, &change()).unwrap();
        apply_with_rollback(&f, &old, &next, |hash| Ok(hash.into())).unwrap();
        assert_eq!(f.read().unwrap(), next);
        assert!(f.replace(&digest(&old), &old).is_err());
    }
    #[test]
    fn replacement_keeps_broker_readable_mode_under_private_umask() {
        use std::os::unix::{fs::PermissionsExt, process::CommandExt};
        if std::env::var_os("SAFENT_CAPS_UMASK_TEST").is_none() {
            let mut child = std::process::Command::new(std::env::current_exe().unwrap());
            child
                .args([
                    "--exact",
                    "ads_caps::tests::replacement_keeps_broker_readable_mode_under_private_umask",
                ])
                .env("SAFENT_CAPS_UMASK_TEST", "1");
            // Isolate the process-global umask from all other parallel tests.
            unsafe {
                child.pre_exec(|| {
                    libc::umask(0o077);
                    Ok(())
                });
            }
            assert!(child.status().unwrap().success());
            return;
        }
        let (root, file) = file();
        let old = file.read().unwrap();
        let next = changed_bytes(&old, &change()).unwrap();
        apply_with_rollback(&file, &old, &next, |hash| Ok(hash.into())).unwrap();
        let path = root.path().join("companions/ads/caps.yaml");
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o644
        );
        let result = apply_with_rollback(&file, &next, &old, |hash| {
            if hash == digest(&old) {
                Err(UNAVAILABLE.into())
            } else {
                Ok(hash.into())
            }
        });
        assert_eq!(result, Err("ads_caps_restored".into()));
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o644
        );
    }
    #[test]
    fn wrong_ack_rolls_back_and_requires_verified_previous_digest() {
        let (_root, f) = file();
        let old = f.read().unwrap();
        let next = changed_bytes(&old, &change()).unwrap();
        let mut calls = 0;
        let result = apply_with_rollback(&f, &old, &next, |hash| {
            calls += 1;
            if calls == 1 {
                Ok("0".repeat(64))
            } else {
                Ok(hash.into())
            }
        });
        assert_eq!(result, Err("ads_caps_restored".into()));
        assert_eq!(calls, 2);
        assert_eq!(f.read().unwrap(), old);
        let result = apply_with_rollback(&f, &old, &next, |_| Err("timeout".into()));
        assert_eq!(result, Err("ads_caps_rollback_unknown".into()));
    }
    #[test]
    fn never_overwrites_an_intervening_owner_edit() {
        let (root, f) = file();
        let old = f.read().unwrap();
        let next = changed_bytes(&old, &change()).unwrap();
        let result = apply_with_rollback(&f, &old, &next, |_| {
            std::fs::write(
                root.path().join("companions/ads/caps.yaml"),
                format!("# edit\n{ORIGINAL}"),
            )
            .unwrap();
            Err("error".into())
        });
        assert_eq!(result, Err("ads_caps_rollback_unknown".into()));
        assert!(String::from_utf8(f.read().unwrap())
            .unwrap()
            .starts_with("# edit"));
    }
    #[test]
    fn symlink_and_hardlink_caps_are_rejected() {
        use std::os::unix::fs::symlink;
        for hard in [false, true] {
            let root = tempfile::tempdir().unwrap();
            std::fs::create_dir_all(root.path().join("companions/ads")).unwrap();
            let target = root.path().join("outside");
            std::fs::write(&target, ORIGINAL).unwrap();
            let path = root.path().join("companions/ads/caps.yaml");
            if hard {
                std::fs::hard_link(target, path).unwrap()
            } else {
                symlink(target, path).unwrap()
            }
            assert!(caps_file::CapsFile::open(root.path())
                .unwrap()
                .read()
                .is_err());
        }
    }
}
