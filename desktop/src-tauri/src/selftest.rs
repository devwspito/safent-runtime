//! Headless `--selftest`: runs `BootService` to `engine_ready` (and, with
//! `--selftest=companion`, on to a converged companion) with NO window and NO
//! `tauri` import anywhere in this file — main.rs checks argv and calls
//! `run()` BEFORE `tauri::Builder` is ever touched, so this works over SSH on
//! a machine with no display server (the owner's Mac, per this task). Prints
//! one NDJSON line per event to stdout — the SAME `EngineEventPayload`/
//! `ReconnectingPayload` shapes `boot.rs`'s Tauri glue emits over
//! `safent://engine-event`/`safent://reconnecting`, so this doubles as a
//! recorded-transcript fixture for whoever needs one. Exits 0 on success, 1
//! otherwise; never panics on a reachable failure path.

use std::sync::Arc;

use crate::boot::{self, BootService, EngineEventPayload, LoopOutcome};
use crate::domain::{DesiredState, DomainEvent, FailureCause, FailureCode, SemVer};
use crate::engine_adapter::EmbeddedCliDriver;
use crate::ports::{CancelSignal, EngineDriver, EngineProbe, Notifier};

/// Prints `DomainEvent`s as NDJSON, reusing `boot.rs`'s wire shapes exactly —
/// the same 6 kinds the UI lane renders from `safent://engine-event`, plus
/// `reconnecting` on its own line, so a transcript from this binary and a
/// transcript from the windowed run are byte-for-byte comparable.
struct NdjsonNotifier;

impl Notifier for NdjsonNotifier {
    fn notify(&self, event: &DomainEvent) {
        let payload = match event {
            DomainEvent::StageEntered {
                stage,
                label,
                total_bytes,
            } => Some(EngineEventPayload::Stage {
                stage: stage.wire_name(),
                label: label.clone(),
                total_bytes: *total_bytes,
                point_of_no_return: boot::bootstrap_point_of_no_return(*stage),
            }),
            DomainEvent::StageProgressed {
                stage,
                done,
                total,
                unit,
            } => Some(EngineEventPayload::Progress {
                stage: stage.wire_name(),
                done: *done,
                total: *total,
                unit: unit.wire_name(),
            }),
            DomainEvent::StageCompleted { stage, duration_ms } => Some(EngineEventPayload::Done {
                stage: stage.wire_name(),
                ms: *duration_ms,
            }),
            DomainEvent::EngineDegraded { cause } => Some(EngineEventPayload::Failed {
                code: cause.code.wire_name(),
                detail: cause.message.clone(),
                retryable: cause.retryable,
            }),
            DomainEvent::EngineReady { version_set } => Some(EngineEventPayload::Ready {
                app_version: version_set.app.as_str().to_string(),
                engine_digest: version_set.engine.digest.clone(),
                companion_digest: version_set.companion.as_ref().map(|c| c.digest.clone()),
            }),
            DomainEvent::Reconnecting { reason } => {
                print_ndjson(&ReconnectingKind {
                    t: "reconnecting",
                    reason: reason.wire_name(),
                });
                None
            }
            DomainEvent::RepairApplied { .. }
            | DomainEvent::NoProgressDetected { .. }
            | DomainEvent::WindowNavigated
            | DomainEvent::HostDiskObserved { .. }
            | DomainEvent::ImagesPruned { .. } => None,
        };
        if let Some(payload) = payload {
            print_ndjson(&payload);
        }
    }
}

/// `ReconnectingPayload` alone has no `kind`/`t` discriminant (it lives on
/// its OWN Tauri channel, so the channel name is the discriminant there) —
/// stdout has only one stream, so this adds the tag a reader needs to tell
/// it apart from an `EngineEventPayload` line.
#[derive(serde::Serialize)]
struct ReconnectingKind {
    t: &'static str,
    reason: &'static str,
}

fn print_ndjson(value: &impl serde::Serialize) {
    match serde_json::to_string(value) {
        Ok(line) => println!("{line}"),
        Err(_) => println!(
            r#"{{"t":"failed","code":"cli_porcelain_unsupported","detail":"selftest: no pude serializar un evento","retryable":false}}"#
        ),
    }
}

fn degraded(notifier: &dyn Notifier, message: impl Into<String>) {
    notifier.notify(&DomainEvent::EngineDegraded {
        cause: FailureCause {
            code: FailureCode::CliPorcelainUnsupported,
            message: message.into(),
            retryable: false,
        },
    });
}

/// MAC-04 (verificacion-mac-1.md): delegates to `boot::runtime_dir_next_to_exe`
/// — the SAME structural resolver `resolve_config`'s own fallback-of-a-
/// fallback uses — instead of the ad hoc `exe.parent().join("runtime")` this
/// file used to compute inline. That inline version resolved to
/// `Contents/MacOS/runtime` on a real macOS `.app` (the CLI actually ships
/// at `Contents/Resources/runtime/…`, a SIBLING of `MacOS/`), so a
/// double-click of the notarized DMG could never find its own bundled CLI.
fn fallback_runtime_dir_for(exe: Option<std::path::PathBuf>) -> std::path::PathBuf {
    exe.map(|e| boot::runtime_dir_next_to_exe(&e))
        .unwrap_or_else(|| std::path::PathBuf::from("runtime"))
}

/// Ties `EmbeddedCliDriver` + `SystemClock` together the same way
/// `boot::run_once` does for the windowed path, but returns whether
/// `EngineReady` was reached instead of navigating anywhere. `runtime_dir`
/// is resolved ONCE by the caller (`run`, MAC-03/MAC-04) and threaded
/// through here rather than recomputed — the SAME directory `run` already
/// used to find `runtime-bundle.json` for the engine/companion digests.
fn run_to_ready(
    notifier: &dyn Notifier,
    desired: DesiredState,
    runtime_dir: std::path::PathBuf,
) -> bool {
    let config = boot::resolve_config_with_fallback(
        runtime_dir,
        desired.engine_image.clone(),
        desired.companion_image.clone(),
    );
    let driver = Arc::new(EmbeddedCliDriver::new(config));
    let probe: Arc<dyn EngineProbe> = driver.clone();
    let engine_driver: Arc<dyn EngineDriver> = driver;
    let app_version = SemVer::parse(env!("CARGO_PKG_VERSION"))
        .unwrap_or_else(|_| SemVer::parse("0.0.0").unwrap());
    let service = BootService::new(
        probe,
        engine_driver,
        Arc::new(boot::SystemClock),
        desired,
        app_version,
    );

    match service.run(notifier, &CancelSignal::new()) {
        LoopOutcome::Ready { .. } => true,
        LoopOutcome::FocusExisting => {
            degraded(
                notifier,
                "ya hay una instancia con el motor a cargo — selftest no puede continuar",
            );
            false
        }
        LoopOutcome::Cancelled { .. } => {
            degraded(
                notifier,
                "selftest: cancelado inesperadamente (nadie debería haber pedido cancelar)",
            );
            false
        }
        LoopOutcome::Degraded { .. } => false, // EngineDegraded was already notified by the loop itself
    }
}

/// `want_companion = false` for `--selftest`, `true` for
/// `--selftest=companion`. Returns the process exit code.
pub fn run(want_companion: bool) -> i32 {
    let notifier = NdjsonNotifier;
    let runtime_dir =
        boot::final_runtime_dir(fallback_runtime_dir_for(std::env::current_exe().ok()));

    let desired = match boot::desired_state_from_runtime(&runtime_dir) {
        Ok(desired) => desired,
        Err(cause) => {
            notifier.notify(&DomainEvent::EngineDegraded { cause });
            return 1;
        }
    };

    let engine_only = DesiredState {
        companion_image: None,
        ..desired.clone()
    };
    if !run_to_ready(&notifier, engine_only, runtime_dir.clone()) {
        return 1;
    }
    if !want_companion {
        return 0;
    }

    if desired.companion_image.is_none() {
        degraded(
            &notifier,
            "--selftest=companion pedido sin SAFENT_COMPANION_DIGEST en el entorno",
        );
        return 1;
    }
    i32::from(!run_to_ready(&notifier, desired, runtime_dir))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// MAC-04: proves THIS file's wiring, not just `boot::runtime_dir_next_
    /// to_exe`'s own correctness (already covered in `boot.rs`'s test
    /// module) — against the pre-fix inline `exe.parent().join("runtime")`
    /// this would have asserted `Contents/MacOS/runtime` and failed.
    #[test]
    fn fallback_runtime_dir_for_a_macos_bundle_exe_finds_the_resources_sibling() {
        let exe = PathBuf::from("/Applications/Safent.app/Contents/MacOS/safent-desktop");
        assert_eq!(
            fallback_runtime_dir_for(Some(exe)),
            PathBuf::from("/Applications/Safent.app/Contents/Resources/runtime")
        );
    }

    #[test]
    fn fallback_runtime_dir_for_a_linux_deb_layout_exe_stays_next_to_the_binary() {
        let exe = PathBuf::from("/opt/Safent/safent-desktop");
        assert_eq!(
            fallback_runtime_dir_for(Some(exe)),
            PathBuf::from("/opt/Safent/runtime")
        );
    }

    #[test]
    fn fallback_runtime_dir_for_none_falls_back_to_a_bare_relative_runtime() {
        assert_eq!(fallback_runtime_dir_for(None), PathBuf::from("runtime"));
    }
}
