//! Minimal startup diagnostics, not a runtime support bundle. Deliberately
//! projects typed domain events before storage: no free text, URLs, paths,
//! credentials, environment, files, stderr, logs or chat are collected.
use crate::domain::DomainEvent;
use serde::Serialize;
use std::collections::VecDeque;
use std::io::Write;
use std::path::Path;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::Manager;
use tauri_plugin_dialog::DialogExt;

const MAX_EVENTS: usize = 128;

#[derive(Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum SafeEvent {
    Stage {
        stage: &'static str,
    },
    Progress {
        stage: &'static str,
        done: u64,
        total: Option<u64>,
        unit: &'static str,
    },
    Done {
        stage: &'static str,
        duration_ms: u64,
    },
    Failed {
        code: &'static str,
        retryable: bool,
    },
    NoProgress {
        code: &'static str,
    },
    Ready,
    Reconnecting {
        reason: &'static str,
    },
}

impl SafeEvent {
    fn project(event: &DomainEvent) -> Option<Self> {
        Some(match event {
            DomainEvent::StageEntered { stage, .. } => Self::Stage {
                stage: stage.wire_name(),
            },
            DomainEvent::StageProgressed {
                stage,
                done,
                total,
                unit,
            } => Self::Progress {
                stage: stage.wire_name(),
                done: *done,
                total: *total,
                unit: unit.wire_name(),
            },
            DomainEvent::StageCompleted { stage, duration_ms } => Self::Done {
                stage: stage.wire_name(),
                duration_ms: *duration_ms,
            },
            DomainEvent::EngineDegraded { cause } => Self::Failed {
                code: cause.code.wire_name(),
                retryable: cause.retryable,
            },
            DomainEvent::NoProgressDetected { code, .. } => Self::NoProgress {
                code: code.wire_name(),
            },
            DomainEvent::EngineReady { .. } => Self::Ready,
            DomainEvent::Reconnecting { reason } => Self::Reconnecting {
                reason: reason.wire_name(),
            },
            DomainEvent::RepairApplied { .. } | DomainEvent::WindowNavigated => return None,
        })
    }
}

#[derive(Default)]
struct History {
    events: VecDeque<SafeEvent>,
    dropped_events: u64,
    latest: Option<BootstrapSnapshot>,
    sequence: u64,
    last_stage: Option<&'static str>,
    point_of_no_return: bool,
    attempt_id: u64,
}

/// Minimal replay, shared by the live channel and getter. Free-text event
/// fields are never retained. Sequence belongs to this desktop process.
#[derive(Clone, Serialize)]
pub(crate) struct BootstrapSnapshot {
    sequence: u64,
    attempt_id: u64,
    event: SafeEvent,
    last_stage: Option<&'static str>,
    point_of_no_return: bool,
}

#[derive(Default)]
pub struct DiagnosticsState {
    history: Mutex<History>,
    in_flight: Arc<AtomicBool>,
}

impl DiagnosticsState {
    pub fn start_attempt(&self, attempt_id: u64) -> Result<(), &'static str> {
        let mut history = self.history.lock().map_err(|_| "history_unavailable")?;
        history.attempt_id = attempt_id;
        history.last_stage = None;
        history.point_of_no_return = false;
        Ok(())
    }
    pub fn record(&self, event: &DomainEvent) -> Option<BootstrapSnapshot> {
        let safe = SafeEvent::project(event)?;
        // This telemetry must not break bootstrap. Poisoned history is rejected
        // on export, never silently exported as an empty healthy session.
        if let Ok(mut history) = self.history.lock() {
            if history.events.len() == MAX_EVENTS {
                history.events.pop_front();
                history.dropped_events += 1;
            }
            history.events.push_back(safe.clone());
            if matches!(event, DomainEvent::NoProgressDetected { .. }) {
                return None;
            }
            if let DomainEvent::StageEntered { stage, .. } = event {
                history.last_stage = Some(stage.wire_name());
                history.point_of_no_return = crate::boot::bootstrap_point_of_no_return(*stage);
            }
            history.sequence += 1;
            let snapshot = BootstrapSnapshot {
                sequence: history.sequence,
                attempt_id: history.attempt_id,
                event: safe,
                last_stage: history.last_stage,
                point_of_no_return: history.point_of_no_return,
            };
            history.latest = Some(snapshot.clone());
            return Some(snapshot);
        }
        None
    }

    fn latest(&self) -> Result<Option<BootstrapSnapshot>, &'static str> {
        self.history
            .lock()
            .map(|history| history.latest.clone())
            .map_err(|_| "history_unavailable")
    }

    fn snapshot(&self) -> Result<Vec<u8>, &'static str> {
        #[derive(Serialize)]
        struct Report<'a> {
            schema_version: u8,
            report_kind: &'static str,
            app_version: &'static str,
            os: &'static str,
            arch: &'static str,
            dropped_events: u64,
            events: &'a VecDeque<SafeEvent>,
        }
        let history = self.history.lock().map_err(|_| "history_unavailable")?;
        serde_json::to_vec_pretty(&Report {
            schema_version: 1,
            report_kind: "startup_only",
            app_version: env!("CARGO_PKG_VERSION"),
            os: std::env::consts::OS,
            arch: std::env::consts::ARCH,
            dropped_events: history.dropped_events,
            events: &history.events,
        })
        .map_err(|_| "serialization_failed")
    }

    fn begin(&self) -> Result<ExportGuard, &'static str> {
        self.in_flight
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "export_in_progress")?;
        Ok(ExportGuard(self.in_flight.clone()))
    }
}

struct ExportGuard(Arc<AtomicBool>);
impl Drop for ExportGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[derive(Debug, PartialEq, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum ExportOutcome {
    Saved,
    Cancelled,
}

#[tauri::command]
pub(crate) fn get_bootstrap_state(
    state: tauri::State<'_, DiagnosticsState>,
) -> Result<Option<BootstrapSnapshot>, String> {
    state.latest().map_err(str::to_owned)
}

fn save_report(destination: Option<&Path>, report: &[u8]) -> Result<ExportOutcome, &'static str> {
    let Some(destination) = destination else {
        return Ok(ExportOutcome::Cancelled);
    };
    if !destination.is_absolute() {
        return Err("invalid_destination");
    }
    match std::fs::symlink_metadata(destination) {
        Ok(meta) if !meta.is_file() || meta.file_type().is_symlink() => {
            return Err("invalid_destination")
        }
        Err(err) if err.kind() != std::io::ErrorKind::NotFound => return Err("write_failed"),
        _ => {}
    }
    let parent = destination.parent().ok_or("invalid_destination")?;
    // A sibling temporary file prevents failed writes from truncating an existing
    // report. Unix tempfile defaults to 0600. Atomic persist replaces the selected
    // file, never follows its symlink; the OS save dialog confirms replacement.
    let mut file = tempfile::NamedTempFile::new_in(parent).map_err(|_| "write_failed")?;
    file.write_all(report).map_err(|_| "write_failed")?;
    file.as_file().sync_all().map_err(|_| "write_failed")?;
    file.persist(destination).map_err(|_| "write_failed")?;
    Ok(ExportOutcome::Saved)
}

/// No caller-supplied path or report: both authority and contents stay native.
/// Only the bundled LOCAL loader capability grants this command.
#[tauri::command]
pub async fn export_diagnostics(
    app: tauri::AppHandle,
    state: tauri::State<'_, DiagnosticsState>,
) -> Result<ExportOutcome, String> {
    let guard = state.begin().map_err(str::to_owned)?;
    let report = state.snapshot().map_err(str::to_owned)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        let window = app.get_webview_window("main").ok_or("window_unavailable")?;
        let choice = app
            .dialog()
            .file()
            .set_parent(&window)
            .set_title("Guardar diagnóstico de arranque")
            .set_file_name("safent-arranque.json")
            .add_filter("Diagnóstico JSON", &["json"])
            .blocking_save_file();
        let path = choice
            .map(|value| value.into_path())
            .transpose()
            .map_err(|_| "invalid_destination")?;
        save_report(path.as_deref(), &report)
    })
    .await
    .map_err(|_| "export_failed".to_owned())?
    .map_err(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{FailureCause, FailureCode, Stage};

    #[test]
    fn free_text_and_environment_secrets_never_enter_report() {
        let state = DiagnosticsState::default();
        state.record(&DomainEvent::StageEntered {
            stage: Stage::Health,
            label: "Bearer SECRET /Users/private https://host/?k=token chat text".into(),
            total_bytes: None,
        });
        state.record(&DomainEvent::EngineDegraded {
            cause: FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: "password=SECRET /private/keychain".into(),
                retryable: true,
            },
        });
        let bytes = state.snapshot().unwrap();
        let text = String::from_utf8(bytes).unwrap();
        for forbidden in [
            "SECRET",
            "Bearer",
            "private",
            "token",
            "chat text",
            "label",
            "detail",
            "stderr",
            "password",
        ] {
            assert!(!text.contains(forbidden), "{forbidden}");
        }
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            value["events"][0],
            serde_json::json!({"kind":"stage","stage":"health"})
        );
        assert_eq!(value["events"][1]["code"], "daemon_unhealthy");
    }

    #[test]
    fn bounded_history_reports_truncation() {
        let state = DiagnosticsState::default();
        for _ in 0..140 {
            state.record(&DomainEvent::StageCompleted {
                stage: Stage::Health,
                duration_ms: 3,
            });
        }
        let value: serde_json::Value = serde_json::from_slice(&state.snapshot().unwrap()).unwrap();
        assert_eq!(value["events"].as_array().unwrap().len(), MAX_EVENTS);
        assert_eq!(value["dropped_events"], 12);
    }

    #[test]
    fn early_failure_is_replayed_without_private_detail() {
        let state = DiagnosticsState::default();
        assert!(state.latest().unwrap().is_none());
        state.record(&DomainEvent::EngineDegraded {
            cause: FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: "SECRET /private/keychain".into(),
                retryable: true,
            },
        });
        let snapshot = serde_json::to_value(state.latest().unwrap()).unwrap();
        assert_eq!(snapshot["sequence"], 1);
        assert_eq!(snapshot["event"]["kind"], "failed");
        assert!(!snapshot.to_string().contains("SECRET"));
        assert!(snapshot["event"].get("detail").is_none());
    }

    #[test]
    fn retry_stage_and_snapshot_share_one_monotonic_sequence() {
        let state = DiagnosticsState::default();
        state.start_attempt(1).unwrap();
        state.record(&DomainEvent::EngineDegraded {
            cause: FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: String::new(),
                retryable: true,
            },
        });
        let old = state.latest().unwrap().unwrap();
        assert_eq!(old.attempt_id, 1);
        state.start_attempt(2).unwrap();
        let live = state
            .record(&DomainEvent::StageEntered {
                stage: Stage::Container,
                label: "SECRET".into(),
                total_bytes: None,
            })
            .unwrap();
        assert!(live.sequence > old.sequence);
        assert_eq!(live.attempt_id, 2);
        assert_eq!(serde_json::to_value(&live).unwrap()["attempt_id"], 2);
        assert_eq!(state.latest().unwrap().unwrap().sequence, live.sequence);
        assert!(live.point_of_no_return);
        assert_eq!(live.last_stage, Some("container"));
        state.start_attempt(3).unwrap();
        let reset = state
            .record(&DomainEvent::StageEntered {
                stage: Stage::Preflight,
                label: "SECRET".into(),
                total_bytes: None,
            })
            .unwrap();
        assert!(reset.sequence > live.sequence);
        assert_eq!(reset.attempt_id, 3);
        assert!(!reset.point_of_no_return);
        assert_eq!(reset.last_stage, Some("preflight"));
    }

    #[test]
    fn single_flight_releases_on_cancel_or_error() {
        let state = DiagnosticsState::default();
        let guard = state.begin().unwrap();
        assert!(state.begin().is_err());
        drop(guard);
        assert!(state.begin().is_ok());
    }

    #[test]
    fn cancelled_selection_never_writes() {
        assert_eq!(save_report(None, b"{}"), Ok(ExportOutcome::Cancelled));
    }

    #[test]
    fn selected_file_is_written_atomically_with_private_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("report.json");
        std::fs::write(&path, b"old").unwrap();
        assert_eq!(
            save_report(Some(&path), b"{\"schema_version\":1}"),
            Ok(ExportOutcome::Saved)
        );
        assert_eq!(std::fs::read(&path).unwrap(), b"{\"schema_version\":1}");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn invalid_destination_is_not_success() {
        let dir = tempfile::tempdir().unwrap();
        assert!(save_report(Some(dir.path()), b"{}").is_err());
        assert!(save_report(Some(Path::new("relative.json")), b"{}").is_err());
        assert!(save_report(Some(&dir.path().join("missing/report.json")), b"{}").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_destination_never_changes_target() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("untouched");
        let link = dir.path().join("report.json");
        std::fs::write(&target, b"original").unwrap();
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(save_report(Some(&link), b"{}").is_err());
        assert_eq!(std::fs::read(target).unwrap(), b"original");
    }

    #[test]
    fn export_permission_is_only_local_loader() {
        let local: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        let remote: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/remote-ui.json")).unwrap();
        assert!(local["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p == "allow-export-diagnostics"));
        assert!(remote["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p != "allow-export-diagnostics"));
        assert_eq!(remote["local"], false);
        assert!(!remote.to_string().contains("allow-get-bootstrap-state"));
        assert!(local.to_string().contains("allow-get-bootstrap-state"));
    }
}
