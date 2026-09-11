//! "Exportar diagnóstico" (specs/028-safent-app-nativa, UI-NATIVE) — a native
//! save dialog writing a REDACTED bundle: `facts --json`'s host snapshot plus
//! the recent `safent://engine-event` history, never the boot ticket, never
//! the loopback port (`domain::Port`'s own invariant: "the ubiquitous
//! language forbids ever showing this to the owner"), never an env var or a
//! filesystem path from the host.
//!
//! `DiagnosticsLog` is the app-managed ring buffer `boot.rs`'s `TauriNotifier`
//! feeds on every emitted event; `export_diagnostics` is the one Tauri
//! command the failed screen's button invokes.

use std::collections::VecDeque;
use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;

use crate::boot::EngineEventPayload;
use crate::domain::{
    Arch, CompanionHealth, DaemonHealth, FailureCause, HostFacts, HostOs, MachineProvider,
};
use crate::engine_adapter::EmbeddedCliDriver;
use crate::ports::EngineProbe;

const MAX_RECENT_EVENTS: usize = 200;

/// Bounded, app-managed ring buffer of every `EngineEventPayload` this
/// process has emitted so far — capped so a very long-running session never
/// grows this unbounded.
#[derive(Default)]
pub struct DiagnosticsLog(Mutex<VecDeque<EngineEventPayload>>);

impl DiagnosticsLog {
    pub fn record(&self, event: EngineEventPayload) {
        let mut log = self.0.lock().expect("poisoned");
        if log.len() >= MAX_RECENT_EVENTS {
            log.pop_front();
        }
        log.push_back(event);
    }

    fn snapshot(&self) -> Vec<EngineEventPayload> {
        self.0.lock().expect("poisoned").iter().cloned().collect()
    }
}

#[derive(serde::Serialize)]
struct PlatformInfo {
    os: &'static str,
    arch: &'static str,
}

/// Explicit, hand-picked projection of `HostFacts` — deliberately NOT a
/// `#[derive(Serialize)]` on the domain type itself, so a future field added
/// to `HostFacts` needs a decision here rather than silently reaching a
/// diagnostics file a support engineer or the owner could forward to anyone.
/// `published_port` is the one field intentionally absent: see the module
/// doc comment.
#[derive(serde::Serialize)]
struct RedactedHostFacts {
    os: &'static str,
    arch: &'static str,
    free_disk_bytes: u64,
    total_memory_bytes: u64,
    runtime_staged: bool,
    runtime_hash_ok: bool,
    machines: Vec<RedactedMachine>,
    engine_container_exists: bool,
    engine_container_running: bool,
    engine_image_digest: Option<String>,
    local_companion_image_digest: Option<String>,
    data_volume: bool,
    companion_scaffold: bool,
    companion_containers_running: u32,
    companion_containers_total: u32,
    companion_health: &'static str,
    daemon_health: &'static str,
    app_version: String,
    user_ns_allowed: bool,
    helper_installed: bool,
    another_instance_running: bool,
}

#[derive(serde::Serialize)]
struct RedactedMachine {
    provider: String,
    rootful: bool,
    running: bool,
    ours: bool,
    cpus: u32,
    memory_bytes: u64,
}

fn host_os_name(os: HostOs) -> &'static str {
    match os {
        HostOs::MacOs => "darwin",
        HostOs::Linux => "linux",
        HostOs::Unsupported => "unsupported",
    }
}

fn arch_name(arch: Arch) -> &'static str {
    match arch {
        Arch::Arm64 => "arm64",
        Arch::Amd64 => "amd64",
        Arch::Unsupported => "unsupported",
    }
}

fn companion_health_name(health: CompanionHealth) -> &'static str {
    match health {
        CompanionHealth::Unknown => "unknown",
        CompanionHealth::Reachable => "reachable",
        CompanionHealth::Unreachable => "unreachable",
    }
}

fn daemon_health_name(health: DaemonHealth) -> &'static str {
    match health {
        DaemonHealth::Unknown => "unknown",
        DaemonHealth::Healthy => "healthy",
        DaemonHealth::Unhealthy => "unhealthy",
    }
}

fn machine_provider_name(provider: &MachineProvider) -> String {
    match provider {
        MachineProvider::AppleHv => "applehv".to_string(),
        MachineProvider::Qemu => "qemu".to_string(),
        MachineProvider::HyperV => "hyperv".to_string(),
        MachineProvider::Wsl => "wsl".to_string(),
        MachineProvider::Other(raw) => raw.clone(),
    }
}

fn redact(facts: &HostFacts) -> RedactedHostFacts {
    RedactedHostFacts {
        os: host_os_name(facts.os),
        arch: arch_name(facts.arch),
        free_disk_bytes: facts.free_disk_bytes.0,
        total_memory_bytes: facts.total_memory_bytes.0,
        runtime_staged: facts.runtime_staged,
        runtime_hash_ok: facts.runtime_hash_ok,
        machines: facts
            .machines
            .iter()
            .map(|m| RedactedMachine {
                provider: machine_provider_name(&m.provider),
                rootful: m.rootful,
                running: m.running,
                ours: m.ours,
                cpus: m.cpus,
                memory_bytes: m.memory_bytes.0,
            })
            .collect(),
        engine_container_exists: facts.engine_container.as_ref().is_some_and(|c| c.exists),
        engine_container_running: facts.engine_container.as_ref().is_some_and(|c| c.running),
        engine_image_digest: facts.local_engine_image_digest.clone(),
        local_companion_image_digest: facts.local_companion_image_digest.clone(),
        data_volume: facts.data_volume,
        companion_scaffold: facts.companion_scaffold,
        companion_containers_running: facts.companion_containers.running,
        companion_containers_total: facts.companion_containers.total,
        companion_health: companion_health_name(facts.companion_health),
        daemon_health: daemon_health_name(facts.daemon_health),
        app_version: facts.app_version.as_str().to_string(),
        user_ns_allowed: facts.user_ns_allowed,
        helper_installed: facts.helper_installed,
        another_instance_running: facts.another_instance_running,
    }
}

#[derive(serde::Serialize)]
struct DiagnosticsBundle {
    schema_version: u32,
    generated_at_unix_s: u64,
    app_version: String,
    platform: PlatformInfo,
    host: Option<RedactedHostFacts>,
    host_error: Option<String>,
    recent_events: Vec<EngineEventPayload>,
}

fn unix_seconds_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Observes the host the SAME way every boot iteration does (`facts --json`,
/// `EngineProbe::observe`, contract app-engine.md §4: "No modifica nada") —
/// never a second, ad hoc way of asking the same question.
fn observe_host(app: &AppHandle) -> (Option<RedactedHostFacts>, Option<String>) {
    let runtime_dir = crate::boot::resolve_runtime_dir(app);
    let desired = match crate::boot::desired_state_from_runtime(&runtime_dir) {
        Ok(desired) => desired,
        Err(cause) => return (None, Some(describe(&cause))),
    };
    let config = crate::boot::resolve_config_with_fallback(
        runtime_dir,
        desired.engine_image,
        desired.companion_image,
    );
    match EmbeddedCliDriver::new(config).observe() {
        Ok(facts) => (Some(redact(&facts)), None),
        Err(error) => (None, Some(error.to_string())),
    }
}

fn describe(cause: &FailureCause) -> String {
    cause.message.clone()
}

fn build_bundle(app: &AppHandle, recent_events: Vec<EngineEventPayload>) -> DiagnosticsBundle {
    let (host, host_error) = observe_host(app);
    DiagnosticsBundle {
        schema_version: 1,
        generated_at_unix_s: unix_seconds_now(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
        platform: PlatformInfo {
            os: std::env::consts::OS,
            arch: std::env::consts::ARCH,
        },
        host,
        host_error,
        recent_events,
    }
}

fn default_file_name(generated_at_unix_s: u64) -> String {
    format!("safent-diagnostics-{generated_at_unix_s}.json")
}

fn write_diagnostics_file(path: &Path, body: &str) -> Result<(), String> {
    std::fs::write(path, body)
        .map_err(|e| format!("no se pudo escribir {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Wired to the failed screen's "Exportar diagnóstico" button
/// (capabilities/default.json's `allow-export-diagnostics`). `Ok(false)`
/// means the owner closed the native dialog without choosing a
/// destination — a normal outcome, never an error the UI should surface.
#[tauri::command]
pub async fn export_diagnostics(
    app: AppHandle,
    log: tauri::State<'_, DiagnosticsLog>,
) -> Result<bool, String> {
    let recent_events = log.snapshot();
    let app_for_blocking = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let bundle = build_bundle(&app_for_blocking, recent_events);
        let body = serde_json::to_string_pretty(&bundle)
            .map_err(|e| format!("no se pudo preparar el diagnóstico: {e}"))?;
        let file_name = default_file_name(bundle.generated_at_unix_s);
        let Some(chosen) = app_for_blocking
            .dialog()
            .file()
            .set_file_name(file_name)
            .add_filter("JSON", &["json"])
            .blocking_save_file()
        else {
            return Ok(false);
        };
        let path = chosen
            .into_path()
            .map_err(|e| format!("ruta de guardado inválida: {e}"))?;
        write_diagnostics_file(&path, &body)?;
        Ok(true)
    })
    .await
    .map_err(|e| format!("no se pudo exportar el diagnóstico: {e}"))?
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{Bytes, CompanionContainers, ContainerFact, LocalStateFact, MachineFact, MachineName, Port, SemVer};

    fn sample_facts() -> HostFacts {
        HostFacts {
            os: HostOs::Linux,
            arch: Arch::Amd64,
            free_disk_bytes: Bytes(20_000_000_000),
            total_memory_bytes: Bytes(16_000_000_000),
            runtime_staged: true,
            runtime_hash_ok: true,
            machines: vec![MachineFact {
                name: MachineName("safent-engine".into()),
                provider: MachineProvider::AppleHv,
                rootful: true,
                running: true,
                ours: true,
                cpus: 4,
                memory_bytes: Bytes(6_000_000_000),
            }],
            engine_container: Some(ContainerFact {
                exists: true,
                running: true,
                image_digest: Some("sha256:engine".into()),
            }),
            local_engine_image_digest: Some("sha256:engine".into()),
            local_companion_image_digest: None,
            // The one field a diagnostics bundle must NEVER leak.
            published_port: Some(Port(47013)),
            data_volume: true,
            companion_scaffold: false,
            companion_containers: CompanionContainers::default(),
            companion_health: CompanionHealth::Unknown,
            daemon_health: DaemonHealth::Healthy,
            app_version: SemVer::parse("0.9.0").unwrap(),
            user_ns_allowed: true,
            helper_installed: true,
            local_state: LocalStateFact::Trusted,
            another_instance_running: false,
        }
    }

    #[test]
    fn redacted_bundle_never_serializes_the_loopback_port() {
        let redacted = redact(&sample_facts());
        let json = serde_json::to_string(&redacted).unwrap();

        assert!(
            !json.contains("47013") && !json.contains("port"),
            "the redacted diagnostics view must never carry the loopback port: {json}"
        );
    }

    #[test]
    fn redacted_bundle_carries_the_facts_a_support_engineer_actually_needs() {
        let redacted = redact(&sample_facts());

        assert_eq!(redacted.os, "linux");
        assert_eq!(redacted.arch, "amd64");
        assert!(redacted.runtime_staged);
        assert!(redacted.engine_container_running);
        assert_eq!(redacted.engine_image_digest.as_deref(), Some("sha256:engine"));
        assert_eq!(redacted.machines.len(), 1);
        assert_eq!(redacted.machines[0].provider, "applehv");
    }

    #[test]
    fn diagnostics_log_caps_at_the_bounded_size_dropping_the_oldest_first() {
        let log = DiagnosticsLog::default();
        for i in 0..(MAX_RECENT_EVENTS + 10) {
            log.record(EngineEventPayload::Done {
                stage: "container",
                ms: i as u64,
            });
        }

        let snapshot = log.snapshot();
        assert_eq!(snapshot.len(), MAX_RECENT_EVENTS);
        match &snapshot[0] {
            EngineEventPayload::Done { ms, .. } => assert_eq!(*ms, 10),
            other => panic!("expected the oldest surviving entry, got {other:?}"),
        }
    }

    #[test]
    fn default_file_name_is_stable_and_json() {
        assert_eq!(default_file_name(1_700_000_000), "safent-diagnostics-1700000000.json");
    }
}
