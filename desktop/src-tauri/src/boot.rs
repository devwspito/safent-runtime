//! T011 — the boot service: observe -> plan -> apply -> reobserve, with
//! backoff between retries and cancel support honored only before the point
//! of no return. `BootService` itself has no `tauri` import — only the
//! "Tauri glue" section at the bottom does, and `main.rs`'s one
//! `boot::start(app.handle().clone())` call is its only caller.

use std::sync::Arc;
use std::time::Duration;

use crate::domain::{
    AttemptCount, BootstrapTicket, DesiredState, DomainEvent, EngineLifecycle, EnginePhase,
    FailOutcome, FailureCause, FailureCode, HostFacts, RepairAction, SemVer, Stage, VersionSet,
};
use crate::ports::{
    ApplyOutcome, CancelSignal, Clock, EngineDriver, EngineError, EngineProbe, Notifier,
};
use crate::reconcile;

/// Where `BootService::run` landed.
#[derive(Debug)]
pub enum LoopOutcome {
    /// The product is reachable — `ticket` authenticates the window's ONE
    /// navigation to it (contract app-engine.md §5). `lifecycle` is read
    /// back out by tests asserting the final phase; `run_once` only needs
    /// `ticket`.
    Ready {
        ticket: BootstrapTicket,
        #[allow(dead_code)]
        lifecycle: EngineLifecycle,
    },
    /// `HostFacts::another_instance_running` — nothing here was touched.
    FocusExisting,
    /// The owner cancelled before the point of no return.
    Cancelled {
        #[allow(dead_code)]
        lifecycle: EngineLifecycle,
    },
    /// No progress twice in a row (data-model.md `EngineLifecycle` invariant
    /// 5) — one screen, one "Reintentar"; `lifecycle.last_failure()` has why.
    /// `run_once` does not read `lifecycle` back out today — the UI already
    /// got the cause via the `EngineDegraded` event on `safent://engine-event`
    /// — but a future diagnostics/status command will want it.
    Degraded {
        #[allow(dead_code)]
        lifecycle: EngineLifecycle,
    },
}

/// Bootstrap-scoped: true from the `container` CLI stage onward, matching
/// the point past which the owner's "Cancelar" is disabled in the UI. This
/// is NOT a universal property of `Stage` (the update flow, `src-tauri/src/
/// update/`, declares `backup` as ITS point of no return instead) — it lives
/// here because only the bootstrap context's policy is this loop's to define.
pub(crate) fn bootstrap_point_of_no_return(stage: Stage) -> bool {
    matches!(
        stage,
        Stage::Container
            | Stage::Health
            | Stage::CompanionScaffold
            | Stage::CompanionUp
            | Stage::CompanionReload
            | Stage::Backup
            | Stage::Restore
            | Stage::Cleanup
    )
}

/// Same rule, expressed in `EnginePhase` terms for the loop's own cancel
/// gate (it knows the phase it is about to enter before any CLI stage event
/// arrives).
fn phase_past_point_of_no_return(phase: EnginePhase) -> bool {
    matches!(
        phase,
        EnginePhase::EngineStarting
            | EnginePhase::EngineReady
            | EnginePhase::CompanionProvisioning
            | EnginePhase::CompanionReady
    )
}

fn phase_for(action: &RepairAction) -> Option<EnginePhase> {
    match action {
        RepairAction::StageRuntime => Some(EnginePhase::RuntimeStaging),
        RepairAction::AdoptMachine(_)
        | RepairAction::CreateMachine
        | RepairAction::StartMachine(_)
        | RepairAction::InstallPrivilegedHelper => Some(EnginePhase::EngineProvisioning),
        RepairAction::PullEngine(_) | RepairAction::PullCompanion(_) => {
            Some(EnginePhase::EnginePulling)
        }
        RepairAction::ChoosePort
        | RepairAction::CreateContainer
        | RepairAction::StartContainer
        | RepairAction::RecreateEngine => Some(EnginePhase::EngineStarting),
        RepairAction::EnsureCompanionScaffold
        | RepairAction::ComposeCompanionUp(_)
        | RepairAction::ReloadCompanionPresence => Some(EnginePhase::CompanionProvisioning),
        RepairAction::FocusExistingWindow => None,
    }
}

/// 1s, 2s, 4s, 8s, 16s, capped at 30s — the backoff between repeated
/// attempts of the SAME repair episode (`EngineLifecycle::attempt`).
fn backoff_for(attempt: AttemptCount) -> Duration {
    let exponent = attempt.0.min(5); // 1<<5 = 32, comfortably within u64 — no overflow to guard
    Duration::from_secs((1u64 << exponent).min(30))
}

pub struct BootService {
    probe: Arc<dyn EngineProbe>,
    driver: Arc<dyn EngineDriver>,
    clock: Arc<dyn Clock>,
    desired: DesiredState,
    app_version: SemVer,
}

impl BootService {
    pub fn new(
        probe: Arc<dyn EngineProbe>,
        driver: Arc<dyn EngineDriver>,
        clock: Arc<dyn Clock>,
        desired: DesiredState,
        app_version: SemVer,
    ) -> Self {
        Self {
            probe,
            driver,
            clock,
            desired,
            app_version,
        }
    }

    /// Cancellation is deliberately NOT checked proactively at the top of
    /// this loop: at that point the next action (and therefore its target
    /// phase) is not known yet, so an early check here could reject a
    /// perfectly fine cancel request one action too early, or accept one
    /// that is about to land past the gate. The one place that decides is
    /// `apply_gated`, which knows the SPECIFIC action about to run — a
    /// cancel is honored the moment `apply_gated` hands the driver a live
    /// signal and the driver reports back `EngineError::Cancelled`.
    pub fn run(&self, notifier: &dyn Notifier, cancel: &CancelSignal) -> LoopOutcome {
        let mut lifecycle = EngineLifecycle::fresh();
        lifecycle
            .enter(EnginePhase::Preflight)
            .expect("Fresh -> Preflight is always legal");

        // The action + facts a repair last reported SUCCESS against — not
        // touched by the failure path, which has its own guard
        // (`EngineLifecycle::fail`). Detects a DIFFERENT failure mode a
        // real packaged run hit live: `cmd_stage_runtime` as a no-op
        // returned `Ok(Progressed)` ~400 times in 90s without
        // `runtime_staged` ever becoming true, because the packaged CLI had
        // no manifest to actually stage — every `Err` guard in this loop
        // was irrelevant; nothing ever failed.
        let mut last_effective_repair: Option<(RepairAction, HostFacts)> = None;

        loop {
            let facts = match self.probe.observe() {
                Ok(facts) => facts,
                Err(error) => {
                    match self.handle_failure(
                        &mut lifecycle,
                        None,
                        error.to_failure_cause(),
                        notifier,
                    ) {
                        Some(outcome) => return outcome,
                        None => continue,
                    }
                }
            };

            if facts.another_instance_running {
                return LoopOutcome::FocusExisting;
            }

            if let Some(cause) = reconcile::preflight_violation(&facts, &self.desired) {
                match self.handle_failure(&mut lifecycle, None, cause, notifier) {
                    Some(outcome) => return outcome,
                    None => continue,
                }
            }

            let Some(action) = reconcile::reconcile(&facts, &self.desired)
                .into_iter()
                .next()
            else {
                return self.confirm_ready(&mut lifecycle, notifier, cancel);
            };

            if matches!(action, RepairAction::FocusExistingWindow) {
                return LoopOutcome::FocusExisting;
            }

            // The SAME action reconcile just asked for again, against
            // EXACTLY the facts its own last (successful!) application left
            // behind: whatever it did had no observable effect. Routed
            // through the SAME `handle_failure`/`lifecycle.fail()` path the
            // error branch below uses — `EngineLifecycle`'s own invariant
            // is that `Degraded` is reachable ONLY via `fail()` detecting a
            // REPEAT, never a direct construction — so this gives it one
            // more backoff-and-retry (`FailOutcome::Repairing`, in case the
            // apply was genuinely flaky) exactly like a real error would,
            // and only degrades on the SECOND ineffective cycle in a row.
            if let Some((last_action, last_facts)) = &last_effective_repair {
                if *last_action == action && *last_facts == facts {
                    let cause = FailureCause {
                        code: FailureCode::RepairIneffective,
                        message: format!(
                            "{action:?} se aplicó y no cambió nada observable en el equipo"
                        ),
                        retryable: false,
                    };
                    if let Some(outcome) =
                        self.handle_failure(&mut lifecycle, Some(&action), cause, notifier)
                    {
                        return outcome;
                    }
                    continue;
                }
            }

            match self.apply_gated(&mut lifecycle, &action, notifier, cancel) {
                Ok(ApplyOutcome::Progressed) => {
                    notifier.notify(&DomainEvent::RepairApplied {
                        action: action.clone(),
                    });
                    self.advance_to(&mut lifecycle, &action);
                    last_effective_repair = Some((action, facts));
                }
                Ok(ApplyOutcome::Ready(ticket)) => {
                    self.advance_to(&mut lifecycle, &action);
                    if self.desired.companion_image.is_some() {
                        let _ = lifecycle.enter(EnginePhase::EngineReady);
                        // `up` proves the engine, not the complete product. Do
                        // not navigate before Ads has converged too. Discard
                        // this private ticket; confirm_ready issues a fresh one
                        // after the potentially long companion installation.
                        drop(ticket);
                        last_effective_repair = Some((action, facts));
                        continue;
                    }
                    let _ = lifecycle.enter(EnginePhase::EngineReady);
                    notifier.notify(&DomainEvent::EngineReady {
                        version_set: self.version_set(),
                    });
                    return LoopOutcome::Ready { ticket, lifecycle };
                }
                Err(EngineError::Cancelled) => return self.notify_cancelled(&lifecycle, notifier),
                Err(error) => {
                    // A failure is a DIFFERENT episode than a silent no-op
                    // success — EngineLifecycle::fail() owns detecting
                    // repeated failures on its own attempt-count; starting
                    // fresh here means a transient error right after an
                    // effective repair is never confused with THIS guard.
                    last_effective_repair = None;
                    self.notify_if_reconnecting(&mut lifecycle, &error, notifier);
                    if let Some(outcome) = self.handle_failure(
                        &mut lifecycle,
                        Some(&action),
                        error.to_failure_cause(),
                        notifier,
                    ) {
                        return outcome;
                    }
                }
            }
        }
    }

    /// `apply`, but with a cancel signal that reads as permanently UNSET once
    /// `action`'s phase is past the point of no return — contract
    /// app-engine.md §6: "la cancelación se rechaza [...] lo declara antes,
    /// nunca después". The real signal is left untouched; a request made too
    /// late is simply never honored, not silently lost or errored.
    fn apply_gated(
        &self,
        lifecycle: &mut EngineLifecycle,
        action: &RepairAction,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> Result<ApplyOutcome, EngineError> {
        let target_phase = phase_for(action).unwrap_or_else(|| lifecycle.phase());
        if phase_past_point_of_no_return(target_phase) {
            self.driver.apply(action, notifier, &CancelSignal::new())
        } else {
            self.driver.apply(action, notifier, cancel)
        }
    }

    /// Reconcile found nothing left to do. If a ticket had been minted THIS
    /// run we would already have returned via `ApplyOutcome::Ready` — landing
    /// here on the very first observe means the engine was already fully up
    /// from a PREVIOUS session (adopted, not recreated). `up` is documented
    /// idempotent (contract app-engine.md §4) and is the only place a ticket
    /// is minted (FR-011: renewed on every engine start), so re-invoking it
    /// is exactly what "reopened against an already-running engine" means.
    fn confirm_ready(
        &self,
        lifecycle: &mut EngineLifecycle,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> LoopOutcome {
        self.advance_to(lifecycle, &RepairAction::StartContainer);
        match self.apply_gated(lifecycle, &RepairAction::StartContainer, notifier, cancel) {
            Ok(ApplyOutcome::Ready(ticket)) => {
                if self.desired.companion_image.is_some() {
                    // `up` can recreate a legacy core to migrate its mounts.
                    // Its ticket alone does not prove the previously observed
                    // Ads bridge survived that change. Recheck before exposing
                    // the ticket or emitting product Ready.
                    let confirmed = self.probe.observe().is_ok_and(|facts| {
                        !facts.another_instance_running
                            && reconcile::reconcile(&facts, &self.desired).is_empty()
                    });
                    if !confirmed {
                        notifier.notify(&DomainEvent::EngineDegraded {
                            cause: FailureCause {
                                code: FailureCode::CompanionUnreachable,
                                message: "No se pudo confirmar Anuncios después de preparar Safent"
                                    .into(),
                                retryable: true,
                            },
                        });
                        return LoopOutcome::Degraded {
                            lifecycle: lifecycle.clone(),
                        };
                    }
                    if lifecycle.phase() != EnginePhase::CompanionProvisioning {
                        let _ = lifecycle.enter(EnginePhase::EngineReady);
                        let _ = lifecycle.enter(EnginePhase::CompanionProvisioning);
                    }
                    let _ = lifecycle.enter(EnginePhase::CompanionReady);
                } else {
                    let _ = lifecycle.enter(EnginePhase::EngineReady);
                }
                notifier.notify(&DomainEvent::EngineReady {
                    version_set: self.version_set(),
                });
                LoopOutcome::Ready {
                    ticket,
                    lifecycle: lifecycle.clone(),
                }
            }
            Ok(ApplyOutcome::Progressed) => {
                let cause = FailureCause {
                    code: FailureCode::CliPorcelainUnsupported,
                    message: "up no entregó un vale de arranque".to_string(),
                    retryable: false,
                };
                notifier.notify(&DomainEvent::EngineDegraded { cause });
                LoopOutcome::Degraded {
                    lifecycle: lifecycle.clone(),
                }
            }
            Err(EngineError::Cancelled) => self.notify_cancelled(lifecycle, notifier),
            Err(error) => {
                self.notify_if_reconnecting(lifecycle, &error, notifier);
                notifier.notify(&DomainEvent::EngineDegraded {
                    cause: error.to_failure_cause(),
                });
                LoopOutcome::Degraded {
                    lifecycle: lifecycle.clone(),
                }
            }
        }
    }

    /// Honoured local cancellation is terminal for this attempt, but the owner
    /// may explicitly retry. It never starts another attempt automatically.
    fn notify_cancelled(
        &self,
        lifecycle: &EngineLifecycle,
        notifier: &dyn Notifier,
    ) -> LoopOutcome {
        let mut cause = EngineError::Cancelled.to_failure_cause();
        cause.retryable = true;
        notifier.notify(&DomainEvent::EngineDegraded { cause });
        LoopOutcome::Cancelled {
            lifecycle: lifecycle.clone(),
        }
    }

    /// FR-012's safety net: `up` succeeding without ever delivering a ticket
    /// gets its OWN UI signal (`safent://reconnecting`, reason
    /// `token_missing`) instead of looking like an ordinary repair failure —
    /// still counted toward the no-progress rule by the caller right after
    /// this, so a PERSISTENT case still degrades rather than reconnecting
    /// forever.
    fn notify_if_reconnecting(
        &self,
        lifecycle: &mut EngineLifecycle,
        error: &EngineError,
        notifier: &dyn Notifier,
    ) {
        if matches!(error, EngineError::ReadyWithoutTicket) {
            let _ = lifecycle.enter(EnginePhase::Reconnecting);
            notifier.notify(&DomainEvent::Reconnecting {
                reason: crate::domain::ReconnectReason::TokenMissing,
            });
        }
    }

    fn advance_to(&self, lifecycle: &mut EngineLifecycle, action: &RepairAction) {
        if matches!(
            action,
            RepairAction::EnsureCompanionScaffold
                | RepairAction::PullCompanion(_)
                | RepairAction::ComposeCompanionUp(_)
                | RepairAction::ReloadCompanionPresence
        ) {
            // A resumed boot may first observe an already-running engine.
            // Companion provisioning has its own phase, including downloads;
            // never move the lifecycle backwards into engine provisioning.
            if !matches!(
                lifecycle.phase(),
                EnginePhase::EngineReady
                    | EnginePhase::CompanionProvisioning
                    | EnginePhase::CompanionReady
            ) {
                let _ = lifecycle.enter(EnginePhase::EngineStarting);
                let _ = lifecycle.enter(EnginePhase::EngineReady);
            }
            if lifecycle.phase() != EnginePhase::CompanionProvisioning {
                let _ = lifecycle.enter(EnginePhase::CompanionProvisioning);
            }
            return;
        }
        if let Some(phase) = phase_for(action) {
            if lifecycle.phase() != phase {
                let _ = lifecycle.enter(phase);
            }
        }
    }

    /// Records a failure and either resolves to `Degraded` (returned to the
    /// caller) or sleeps out the backoff and signals "keep looping" (`None`).
    fn handle_failure(
        &self,
        lifecycle: &mut EngineLifecycle,
        action: Option<&RepairAction>,
        cause: FailureCause,
        notifier: &dyn Notifier,
    ) -> Option<LoopOutcome> {
        match lifecycle.fail(action, cause.clone()) {
            Ok(FailOutcome::Repairing) => {
                self.clock.sleep(backoff_for(lifecycle.attempt()));
                None
            }
            Ok(FailOutcome::Degraded) => {
                notifier.notify(&DomainEvent::NoProgressDetected {
                    action: action.cloned(),
                    code: cause.code,
                });
                notifier.notify(&DomainEvent::EngineDegraded { cause });
                Some(LoopOutcome::Degraded {
                    lifecycle: lifecycle.clone(),
                })
            }
            // A rejected transition here is a domain-modeling bug, not a
            // runtime condition (e.g. two failure sources disagreeing about
            // the current episode) — fail loudly into Degraded rather than
            // loop forever or panic the boot thread.
            Err(_illegal) => {
                notifier.notify(&DomainEvent::EngineDegraded { cause });
                Some(LoopOutcome::Degraded {
                    lifecycle: lifecycle.clone(),
                })
            }
        }
    }

    fn version_set(&self) -> VersionSet {
        VersionSet {
            app: self.app_version.clone(),
            engine: self.desired.engine_image.clone(),
            companion: self.desired.companion_image.clone(),
        }
    }
}

// ===========================================================================
// Tauri glue — the ONLY part of this module that imports `tauri`.
// `main.rs`'s one `boot::start(app.handle().clone())` call is the only
// caller of `start`; nothing above this line needs Tauri to compile or test.
// ===========================================================================

use std::path::{Path, PathBuf};

use tauri::{AppHandle, Emitter, Listener, Manager};

use crate::domain::{Bytes, ImageRef, MachineSpec};
use crate::engine_adapter::{EmbeddedCliConfig, EmbeddedCliDriver};
use crate::window_policy::WindowPolicy;

/// The UI lane already codes against these exact channel names.
pub const ENGINE_EVENT_CHANNEL: &str = "safent://engine-event";
pub const RECONNECTING_CHANNEL: &str = "safent://reconnecting";
const RESTART_REQUESTED_EVENT: &str = "safent://restart-engine-requested";
const QUIT_REQUESTED_EVENT: &str = "safent://quit-requested";

/// Mirrors contract app-engine.md §3's `EngineEvent` union — `kind` is the
/// wire tag the UI switches on, matching the CLI's own vocabulary rather
/// than this module's internal `DomainEvent` names.
#[derive(Clone, serde::Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum EngineEventPayload {
    Stage {
        stage: &'static str,
        label: String,
        total_bytes: Option<u64>,
        point_of_no_return: bool,
    },
    Progress {
        stage: &'static str,
        done: u64,
        total: Option<u64>,
        unit: &'static str,
    },
    Done {
        stage: &'static str,
        ms: u64,
    },
    Failed {
        code: &'static str,
        detail: String,
        retryable: bool,
    },
    Ready {
        app_version: String,
        engine_digest: String,
        companion_digest: Option<String>,
    },
}

#[derive(Clone, serde::Serialize)]
pub(crate) struct ReconnectingPayload {
    reason: &'static str,
}

/// Emits `DomainEvent`s to the window. `RepairApplied`/`NoProgressDetected`/
/// `WindowNavigated` are internal bookkeeping, not part of the UI's 6-kind
/// `engine-event` contract, and are not forwarded — best-effort emit (a
/// closed/gone window is not this loop's problem to recover from).
struct TauriNotifier {
    app: AppHandle,
}

impl Notifier for TauriNotifier {
    fn notify(&self, event: &DomainEvent) {
        if let Some(snapshot) = self
            .app
            .state::<crate::diagnostics::DiagnosticsState>()
            .record(event)
        {
            let _ = self.app.emit("safent://bootstrap-state", snapshot);
        }
        self.emit_legacy(event);
    }
}

impl TauriNotifier {
    /// Record immediately for replay, but never dispatch webview events from
    /// an IPC/main-thread failure path. Uses the existing async runtime, not
    /// another bootstrap worker (which may be the resource that just failed).
    fn notify_deferred(&self, event: DomainEvent) {
        let snapshot = self
            .app
            .state::<crate::diagnostics::DiagnosticsState>()
            .record(&event);
        let app = self.app.clone();
        tauri::async_runtime::spawn(async move {
            if let Some(snapshot) = snapshot {
                let _ = app.emit("safent://bootstrap-state", snapshot);
            }
            TauriNotifier { app }.emit_legacy(&event);
        });
    }

    fn emit_legacy(&self, event: &DomainEvent) {
        match event {
            DomainEvent::StageEntered {
                stage,
                label,
                total_bytes,
            } => {
                let _ = self.app.emit(
                    ENGINE_EVENT_CHANNEL,
                    EngineEventPayload::Stage {
                        stage: stage.wire_name(),
                        label: label.clone(),
                        total_bytes: *total_bytes,
                        point_of_no_return: bootstrap_point_of_no_return(*stage),
                    },
                );
            }
            DomainEvent::StageProgressed {
                stage,
                done,
                total,
                unit,
            } => {
                let _ = self.app.emit(
                    ENGINE_EVENT_CHANNEL,
                    EngineEventPayload::Progress {
                        stage: stage.wire_name(),
                        done: *done,
                        total: *total,
                        unit: unit.wire_name(),
                    },
                );
            }
            DomainEvent::StageCompleted { stage, duration_ms } => {
                let _ = self.app.emit(
                    ENGINE_EVENT_CHANNEL,
                    EngineEventPayload::Done {
                        stage: stage.wire_name(),
                        ms: *duration_ms,
                    },
                );
            }
            DomainEvent::EngineDegraded { cause } => {
                let _ = self.app.emit(
                    ENGINE_EVENT_CHANNEL,
                    EngineEventPayload::Failed {
                        code: cause.code.wire_name(),
                        detail: cause.message.clone(),
                        retryable: cause.retryable,
                    },
                );
            }
            DomainEvent::EngineReady { version_set } => {
                let _ = self.app.emit(
                    ENGINE_EVENT_CHANNEL,
                    EngineEventPayload::Ready {
                        app_version: version_set.app.as_str().to_string(),
                        engine_digest: version_set.engine.digest.clone(),
                        companion_digest: version_set.companion.as_ref().map(|c| c.digest.clone()),
                    },
                );
            }
            DomainEvent::Reconnecting { reason } => {
                let _ = self.app.emit(
                    RECONNECTING_CHANNEL,
                    ReconnectingPayload {
                        reason: reason.wire_name(),
                    },
                );
            }
            DomainEvent::RepairApplied { .. }
            | DomainEvent::NoProgressDetected { .. }
            | DomainEvent::WindowNavigated => {}
        }
    }
}

/// No `tauri` in this type either — `pub(crate)` so `selftest.rs` (headless,
/// no window) can build the same real-time `BootService` this module's own
/// `run_once` uses.
pub(crate) struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> std::time::Instant {
        std::time::Instant::now()
    }
    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

/// The owner's "Cancelar" button, wired the same way `install_podman`
/// already is: a plain Tauri command the LOCAL loader page invokes
/// (capabilities/default.json). Exact name is this module's choice — if the
/// already-finished UI lane invokes a different one, it is a one-line rename
/// here, not a design change.
#[tauri::command]
pub fn cancel_bootstrap(
    control: tauri::State<'_, crate::bootstrap_control::BootstrapControl>,
    attempt_id: u64,
) -> Result<(), String> {
    control.cancel(attempt_id).map_err(str::to_owned)
}

/// FR-033's single "Reintentar": re-runs the whole loop from a fresh
/// observation. No separate "resume from where it degraded" state — reconcile
/// re-derives the plan from what is ACTUALLY true on the host each time, so a
/// full re-run correctly skips everything already done and repeats only what
/// still needs it.
#[tauri::command]
pub fn retry_bootstrap(app: AppHandle, attempt_id: u64) -> Result<(), String> {
    spawn_attempt(app, Some(attempt_id), false).map_err(str::to_owned)
}

fn spawn_attempt(
    app: AppHandle,
    expected_id: Option<u64>,
    restart: bool,
) -> Result<(), &'static str> {
    let attempt = app
        .state::<crate::bootstrap_control::BootstrapControl>()
        .begin(expected_id)?;
    app.state::<crate::diagnostics::DiagnosticsState>()
        .start_attempt(attempt.id)?;
    let failure_app = app.clone();
    std::thread::Builder::new()
        .name("safent-bootstrap".into())
        .spawn(move || {
            let _attempt = attempt;
            // Emit from the worker, never the synchronous IPC handler: native
            // webview dispatch may wait on that UI thread. Publish identity
            // before observation so stale gestures cannot target this attempt.
            TauriNotifier { app: app.clone() }.notify(&DomainEvent::StageEntered {
                stage: Stage::Preflight,
                label: "Comprobando este equipo".into(),
                total_bytes: None,
            });
            if restart {
                stop_engine_best_effort(&app);
                TauriNotifier { app: app.clone() }.notify(&DomainEvent::Reconnecting {
                    reason: crate::domain::ReconnectReason::EngineRestarted,
                });
            }
            run_once(app, _attempt.signal.clone());
        })
        .map(|_| ())
        .map_err(|_| {
            TauriNotifier { app: failure_app }.notify_deferred(DomainEvent::EngineDegraded {
                cause: FailureCause {
                    code: FailureCode::ContainerStartFailed,
                    message: "No se pudo iniciar la preparación".into(),
                    retryable: true,
                },
            });
            "bootstrap_spawn_failed"
        })
}

/// Starts the bootstrap loop off the main thread (so the window never
/// freezes) and wires the tray/UI commands the coordinator specified:
/// `safent://restart-engine-requested` (stop -> observe -> plan -> apply
/// again) and `safent://quit-requested` (explicit engine stop, then exit —
/// research.md FR-030: closing the WINDOW alone never stops the engine).
pub fn start(app: AppHandle) {
    app.manage(crate::bootstrap_control::BootstrapControl::default());
    app.manage(crate::companion_requests::CompanionRequests::default());

    let restart_handle = app.clone();
    app.listen(RESTART_REQUESTED_EVENT, move |_event| {
        // Repeated tray restarts never overlap an active bootstrap worker.
        let _ = spawn_attempt(restart_handle.clone(), None, true);
    });

    let quit_handle = app.clone();
    app.listen(QUIT_REQUESTED_EVENT, move |_event| {
        let handle = quit_handle.clone();
        std::thread::spawn(move || {
            handle
                .state::<crate::companion_requests::CompanionRequests>()
                .stop();
            stop_engine_best_effort(&handle);
            handle.exit(0);
        });
    });

    if let Err(error) = spawn_attempt(app.clone(), None, false) {
        // The acquired attempt already recorded/notified its spawn failure.
        if error == "bootstrap_spawn_failed" {
            return;
        }
        TauriNotifier { app }.notify_deferred(DomainEvent::EngineDegraded {
            cause: FailureCause {
                code: FailureCode::ContainerStartFailed,
                message: "No se pudo iniciar la preparación".into(),
                retryable: true,
            },
        });
    }
}

/// `safent://quit-requested` (FR-030: "Salir" explicitly stops the engine,
/// unlike closing the window) and the restart command both need this — best
/// effort, since a stop that cannot be confirmed still must not block the
/// window from closing or the restart from proceeding.
fn stop_engine_best_effort(app: &AppHandle) {
    let runtime_dir = resolve_runtime_dir(app);
    if let Ok(desired) = desired_state_from_runtime(&runtime_dir) {
        let config = resolve_config_with_fallback(
            runtime_dir,
            desired.engine_image,
            desired.companion_image,
        );
        let _ = EmbeddedCliDriver::new(config).stop();
    }
}

fn run_once(app: AppHandle, cancel: CancelSignal) {
    let notifier = TauriNotifier { app: app.clone() };
    let runtime_dir = resolve_runtime_dir(&app);
    let desired = match native_desired_state_from_runtime(&runtime_dir) {
        Ok(desired) => desired,
        Err(cause) => {
            notifier.notify(&DomainEvent::EngineDegraded { cause });
            return;
        }
    };
    let config = resolve_config_with_fallback(
        runtime_dir,
        desired.engine_image.clone(),
        desired.companion_image.clone(),
    );
    let driver = Arc::new(EmbeddedCliDriver::new(config.clone()));
    let probe: Arc<dyn EngineProbe> = driver.clone();
    let engine_driver: Arc<dyn EngineDriver> = driver;
    let service = BootService::new(
        probe,
        engine_driver,
        Arc::new(SystemClock),
        desired,
        app_version(),
    );

    match service.run(&notifier, &cancel) {
        LoopOutcome::Ready { ticket, .. } => {
            let request_app = app.clone();
            let control = app
                .state::<crate::bootstrap_control::BootstrapControl>()
                .inner()
                .clone();
            let consumer = app.state::<crate::companion_requests::CompanionRequests>();
            if consumer
                .start(control, move |signal| {
                    let notifier = CompanionRequestNotifier {
                        app: request_app.clone(),
                    };
                    let driver = EmbeddedCliDriver::new(config.clone());
                    if let Err(error) = driver.consume_companion_requests(&notifier, signal) {
                        if !signal.is_set() {
                            notifier.notify(&DomainEvent::EngineDegraded {
                                cause: error.to_failure_cause(),
                            });
                        }
                    }
                })
                .is_err()
            {
                notifier.notify(&DomainEvent::EngineDegraded {
                    cause: FailureCause {
                        code: FailureCode::ContainerStartFailed,
                        message: "No se pudo iniciar la recuperación de Anuncios".into(),
                        retryable: true,
                    },
                });
                return;
            }
            navigate_to_ticket(&app, &ticket);
        }
        LoopOutcome::FocusExisting
        | LoopOutcome::Cancelled { .. }
        | LoopOutcome::Degraded { .. } => {}
    }
}

/// Request progress has its own durable state in the shared consumer. Reuse
/// the existing CLI event stream without resetting loader attempt/snapshot.
struct CompanionRequestNotifier {
    app: AppHandle,
}
impl Notifier for CompanionRequestNotifier {
    fn notify(&self, event: &DomainEvent) {
        TauriNotifier {
            app: self.app.clone(),
        }
        .emit_legacy(event);
    }
}

fn navigate_to_ticket(app: &AppHandle, ticket: &BootstrapTicket) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let Ok(url) = ticket.expose().parse::<tauri::Url>() else {
        // A malformed ticket URL leaves the loader screen up rather than
        // navigating anywhere unsafe — reconcile/EngineLifecycle already
        // treat "ready without a usable ticket" as reconnecting, not as
        // this path.
        return;
    };
    // window_policy's on_navigation denies any http(s) target that was never
    // declared authorized (contract app-engine.md §7/§8) — this MUST run
    // before `navigate`, exactly like the legacy install_podman flow already
    // did for its own navigation, or the engine's own ticketed URL gets
    // rejected by the policy that exists to protect it.
    if let Some(policy) = app.try_state::<WindowPolicy>() {
        policy.set_authorized_origin(url.clone());
    }
    let _ = window.navigate(url);
}

fn app_version() -> SemVer {
    SemVer::parse(env!("CARGO_PKG_VERSION")).unwrap_or_else(|_| SemVer::parse("0.0.0").unwrap())
}

/// One entry of `runtime-bundle.json`'s `engine_image`/`companion_image`
/// fields (MAC-03, verificacion-mac-1.md) — `digest` is `Option` because an
/// UNSET one is a real, expected state (a checkout the release pipeline has
/// not pinned yet), not a parse error. `platform` (added alongside
/// `SAFENT_ENGINE_DIGEST`/`SAFENT_COMPANION_DIGEST` build-time injection,
/// `stage-runtime.sh`'s `platform_for_target`) is the Linux VM architecture
/// (`linux/arm64`/`linux/amd64`) the recorded digest was actually published
/// for — `Option` too, so an older bundle staged before this field existed
/// still parses (no platform recorded -> no cross-check, same as before).
#[derive(serde::Deserialize)]
struct BundleImageRef {
    repo: String,
    digest: Option<String>,
    #[serde(default)]
    platform: Option<String>,
}

/// The subset of `runtime-bundle.json` (`stage-runtime.sh`'s
/// `_write_runtime_bundle_manifest`) this module reads — `#[serde(default)]`
/// on both fields because older bundles staged before this pass have
/// neither key at all, and that must fail closed with `EngineDigestMissing`
/// exactly like a present-but-null `digest` does, never `serde_json::Error`.
#[derive(serde::Deserialize, Default)]
struct RuntimeBundleManifest {
    #[serde(default)]
    engine_image: Option<BundleImageRef>,
    #[serde(default)]
    companion_image: Option<BundleImageRef>,
}

fn engine_digest_missing(message: impl Into<String>) -> FailureCause {
    FailureCause {
        code: FailureCode::EngineDigestMissing,
        message: message.into(),
        retryable: false,
    }
}

/// True if `recorded` (a `BundleImageRef.platform`) is either absent (an
/// older bundle staged before `stage-runtime.sh` recorded this field at
/// all — no cross-check possible, same trust as before that field existed)
/// or matches the Linux VM architecture THIS running binary's engine
/// actually needs. `update::container_platform_key()` is the SAME OS/arch
/// convention `stage-runtime.sh`'s own `platform_for_target` used to write
/// it in the first place (linux/arm64 on both aarch64-apple-darwin's VM
/// guest and aarch64-unknown-linux-gnu's native host; linux/amd64 on
/// x86_64-unknown-linux-gnu) — reused, not reimplemented, so the two never
/// silently drift apart.
fn platform_matches(recorded: &Option<String>) -> bool {
    match recorded {
        None => true,
        Some(p) => *p == crate::update::container_platform_key(),
    }
}

/// Reads the engine/companion image references straight from the shipped
/// `runtime-bundle.json` (`runtime_dir.join("runtime-bundle.json")` — the
/// SAME directory `resolve_runtime_dir`/`resolve_config_with_fallback`
/// resolve the CLI/podman paths from). The release pipeline pins the real
/// digests into `runtime-manifest.lock`'s `engine_image`/`companion_image`
/// (mirroring the existing `machine_image` entry's pattern — a human/
/// ops-release-publisher fact, never queried live here); `stage-runtime.sh`
/// copies them into `runtime-bundle.json` at staging time, alongside a
/// `platform` recording which Linux VM architecture that digest is
/// actually for (SAFENT_ENGINE_DIGEST/SAFENT_COMPANION_DIGEST build-time
/// injection can, in principle, be handed the wrong platform's digest by a
/// misconfigured pipeline step — this refuses to trust it rather than
/// pulling the wrong image).
fn images_from_runtime_bundle(
    runtime_dir: &Path,
) -> Result<(ImageRef, Option<ImageRef>), FailureCause> {
    let manifest_path = runtime_dir.join("runtime-bundle.json");
    let raw = std::fs::read_to_string(&manifest_path).map_err(|_| {
        engine_digest_missing(format!(
            "no se encontró {} — el paquete no incluye el manifiesto del runtime",
            manifest_path.display()
        ))
    })?;
    let manifest: RuntimeBundleManifest = serde_json::from_str(&raw)
        .map_err(|e| engine_digest_missing(format!("runtime-bundle.json no es válido: {e}")))?;
    let engine = manifest
        .engine_image
        .ok_or_else(|| engine_digest_missing("runtime-bundle.json no trae engine_image"))?;
    if !platform_matches(&engine.platform) {
        return Err(engine_digest_missing(format!(
            "runtime-bundle.json.engine_image.platform ({:?}) no coincide con la \
             arquitectura de este equipo ({}) — el paquete no es para esta plataforma",
            engine.platform,
            crate::update::container_platform_key()
        )));
    }
    let engine_digest = engine.digest.ok_or_else(|| {
        engine_digest_missing(
            "runtime-bundle.json.engine_image.digest es null — el pipeline de release \
             aún no fijó el digest publicado",
        )
    })?;
    let engine_image = ImageRef::new(engine.repo, engine_digest).map_err(|_| {
        engine_digest_missing("runtime-bundle.json.engine_image.digest no tiene forma sha256:...")
    })?;
    // Companion is best-effort (matches its existing Option semantics): a
    // platform mismatch degrades to "no companion pinned", same as an
    // absent/malformed digest already did, never a hard boot failure.
    let companion_image = manifest
        .companion_image
        .filter(|c| platform_matches(&c.platform))
        .and_then(|c| c.digest.map(|d| (c.repo, d)))
        .and_then(|(repo, digest)| ImageRef::new(repo, digest).ok());
    Ok((engine_image, companion_image))
}

/// Where the engine/companion image digests a boot needs come from — MAC-03
/// (verificacion-mac-1.md): before this fix they came ONLY from
/// `SAFENT_ENGINE_DIGEST`/`SAFENT_COMPANION_DIGEST` env vars that nothing in
/// the real packaging pipeline ever sets (no CI workflow, launcher, or
/// `.desktop`/`.plist` file exports either — grepped the whole repo), so a
/// double-click of the notarized DMG hit this unconditionally. The digest a
/// packaged app pulls is a build-time fact, not a runtime guess: it now
/// comes from `runtime-bundle.json`, shipped next to the CLI inside
/// `runtime_dir` (see `images_from_runtime_bundle`). The env vars still
/// work, but ONLY as an explicit override for tests/local dev that do not
/// have a real staged bundle to point at — never production's only source.
/// Missing/malformed fails closed into `Degraded` with the honest
/// `engine_digest_missing`, never a panic.
pub fn desired_state_from_runtime(runtime_dir: &Path) -> Result<DesiredState, FailureCause> {
    let (engine_image, companion_image) = match std::env::var("SAFENT_ENGINE_DIGEST") {
        Ok(engine_digest) => {
            let engine_repo = std::env::var("SAFENT_ENGINE_IMAGE_REPO")
                .unwrap_or_else(|_| "ghcr.io/devwspito/safent".to_string());
            let engine_image = ImageRef::new(engine_repo, engine_digest).map_err(|_| {
                engine_digest_missing("SAFENT_ENGINE_DIGEST no tiene forma de digest sha256:...")
            })?;
            let companion_repo = std::env::var("SAFENT_COMPANION_IMAGE_REPO")
                .unwrap_or_else(|_| "ghcr.io/devwspito/safent-ads".to_string());
            let companion_image = std::env::var("SAFENT_COMPANION_DIGEST")
                .ok()
                .and_then(|digest| ImageRef::new(companion_repo, digest).ok());
            (engine_image, companion_image)
        }
        Err(_) => images_from_runtime_bundle(runtime_dir)?,
    };

    const GIB: u64 = 1024 * 1024 * 1024;
    Ok(DesiredState {
        engine_image,
        companion_image,
        machine: desired_machine_spec(),
        min_free_disk_bytes: Bytes(4 * GIB),
        min_total_memory_bytes: Bytes(4 * GIB),
    })
}

/// Factory Community includes Ads. Only explicit headless/dev callers may
/// request an engine-only DesiredState; the windowed product fails closed if
/// its bundle did not pin a usable companion for this platform.
fn native_desired_state_from_runtime(runtime_dir: &Path) -> Result<DesiredState, FailureCause> {
    let desired = desired_state_from_runtime(runtime_dir)?;
    if desired.companion_image.is_none() {
        return Err(engine_digest_missing(
            "el paquete no incluye un digest válido de Anuncios para esta plataforma",
        ));
    }
    Ok(desired)
}

fn desired_machine_spec() -> Option<MachineSpec> {
    #[cfg(target_os = "macos")]
    {
        use crate::domain::MachineProvider;
        const GIB: u64 = 1024 * 1024 * 1024;
        Some(MachineSpec {
            provider: MachineProvider::AppleHv,
            cpus: 4,
            memory_bytes: Bytes(6 * GIB),
        })
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

/// Resolves the bundled runtime's directory for a windowed run, read off
/// the Tauri resource dir. `desktop/RUNTIME-BUNDLE.md`'s own documented
/// formula — verified there against the real `glob` crate, not assumed —
/// is `resource_dir().join("runtime").join("podman")`, NO target-triple
/// component: `bundle.resources`'s single glob pattern flattens the
/// per-triple staged tree (`resources/runtime/<triple>/{bin,libexec,etc}/...`,
/// what `stage-runtime.sh` produces) into `$RESOURCES/runtime/<basename>` —
/// only one triple's files ever ship in a given build, so there is nothing
/// left to select between at runtime. Exposed on its own (MAC-03,
/// verificacion-mac-1.md) so a caller that needs to read
/// `runtime-bundle.json` — which sits INSIDE this same directory — gets the
/// exact same path `resolve_config_with_fallback` will independently derive
/// the CLI/podman paths from, rather than resolving it a second, possibly
/// divergent way. The `unwrap_or_else` branch (Tauri itself failing to
/// report a resource dir, an edge case it guards against once packaged
/// correctly) falls back to the SAME structural resolver `selftest.rs` uses
/// (`runtime_dir_next_to_exe`) rather than a bare relative literal.
pub fn resolve_runtime_dir(app: &AppHandle) -> PathBuf {
    let fallback = app
        .path()
        .resource_dir()
        .map(|dir| dir.join("runtime"))
        .unwrap_or_else(|_| {
            std::env::current_exe()
                .map(|exe| runtime_dir_next_to_exe(&exe))
                .unwrap_or_else(|_| PathBuf::from("runtime"))
        });
    final_runtime_dir(fallback)
}

/// `SAFENT_RUNTIME_DIR` overrides `fallback` — the same override
/// `resolve_config_with_fallback` needs for the CLI/podman paths AND
/// `images_from_runtime_bundle` needs for `runtime-bundle.json` (MAC-03):
/// both must agree on the SAME directory, or a test/dev override honored by
/// only one of them silently looks for the manifest somewhere the CLI paths
/// do not (and vice versa).
pub fn final_runtime_dir(fallback: PathBuf) -> PathBuf {
    std::env::var_os("SAFENT_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or(fallback)
}

/// Shared, structural resolver for "where does the runtime this executable
/// ships with live" — the ONE place both `resolve_config`'s own fallback-of-
/// a-fallback and `selftest.rs::run_to_ready` (headless: no `AppHandle`, no
/// window, no resource bundle to ask Tauri for) derive it from
/// `std::env::current_exe()`.
///
/// MAC-04 (verificacion-mac-1.md): before this fix `selftest.rs` computed
/// `exe.parent().join("runtime")` unconditionally — correct for the Linux/
/// `.deb` layout (`safent-desktop` really does sit directly next to
/// `runtime/`) but WRONG for a macOS `.app`: the executable lives at
/// `Contents/MacOS/<bin>`, and every shipped resource — `runtime/` included
/// — is a SIBLING of `MacOS/` at `Contents/Resources/`, a directory
/// `Contents/MacOS/runtime` is never created inside. Detects the bundle
/// shape STRUCTURALLY (the executable's parent directory is literally named
/// `MacOS`, exactly how every Apple bundle names it, Tauri's bundler
/// included) rather than `cfg!(target_os)`, so both layouts are exercised by
/// the SAME test binary regardless of which OS runs it.
pub fn runtime_dir_next_to_exe(exe: &Path) -> PathBuf {
    let bin_dir = exe.parent().unwrap_or(exe);
    let is_macos_bundle = bin_dir.file_name().and_then(|n| n.to_str()) == Some("MacOS");
    if is_macos_bundle {
        if let Some(contents_dir) = bin_dir.parent() {
            return contents_dir.join("Resources").join("runtime");
        }
    }
    bin_dir.join("runtime")
}

/// `SAFENT_RUNTIME_DIR`/`SAFENT_CLI_PATH`/`SAFENT_PODMAN_PATH`/
/// `SAFENT_STATE_HOME` override `fallback_runtime_dir` — the same pattern
/// main.rs's legacy flow already uses for `SAFENT_BIN`. No `tauri` import:
/// `selftest.rs` (headless, no window, no resource bundle to ask Tauri for)
/// calls this directly with its own fallback.
pub fn resolve_config_with_fallback(
    fallback_runtime_dir: PathBuf,
    engine_image: ImageRef,
    companion_image: Option<ImageRef>,
) -> EmbeddedCliConfig {
    let runtime_dir = final_runtime_dir(fallback_runtime_dir);
    let cli_path = std::env::var_os("SAFENT_CLI_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| runtime_dir.join("safent"));
    let podman_path = std::env::var_os("SAFENT_PODMAN_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| runtime_dir.join("podman"));
    let state_home = std::env::var_os("SAFENT_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".safent"));
    EmbeddedCliConfig::with_defaults(
        cli_path,
        podman_path,
        state_home,
        engine_image,
        companion_image,
    )
}

pub fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{
        Arch, Bytes, CompanionContainers, CompanionHealth, ContainerFact, DaemonHealth, HostFacts,
        HostOs, ImageRef, LocalStateFact, Port,
    };
    use crate::ports::fakes::{
        EngineErrorKind, FakeClock, RecordingNotifier, ScriptedDriver, ScriptedProbe,
    };

    const GIB: u64 = 1024 * 1024 * 1024;

    fn engine_image() -> ImageRef {
        ImageRef::new("ghcr.io/devwspito/safent", "sha256:engine-good").unwrap()
    }

    fn desired() -> DesiredState {
        DesiredState {
            engine_image: engine_image(),
            companion_image: None,
            machine: None, // Linux desired state — no machine chain to satisfy
            min_free_disk_bytes: Bytes(4 * GIB),
            min_total_memory_bytes: Bytes(8 * GIB),
        }
    }

    /// MAC-04: the Linux/.deb layout — `safent-desktop` sits directly next
    /// to `runtime/`, no bundle indirection at all.
    #[test]
    fn runtime_dir_next_to_exe_uses_the_bin_dir_directly_on_a_linux_layout() {
        let exe = Path::new("/opt/Safent/safent-desktop");
        assert_eq!(
            runtime_dir_next_to_exe(exe),
            PathBuf::from("/opt/Safent/runtime")
        );
    }

    /// MAC-04 (verificacion-mac-1.md): the macOS `.app` layout — the
    /// executable lives at `Contents/MacOS/<bin>`; `runtime/` is a SIBLING
    /// of `MacOS/` at `Contents/Resources/runtime`, never inside `MacOS/`
    /// itself. Before this fix, `selftest.rs` resolved
    /// `Contents/MacOS/runtime` here — a path that is never created.
    #[test]
    fn runtime_dir_next_to_exe_finds_resources_sibling_on_a_macos_bundle_layout() {
        let exe = Path::new("/Applications/Safent.app/Contents/MacOS/safent-desktop");
        assert_eq!(
            runtime_dir_next_to_exe(exe),
            PathBuf::from("/Applications/Safent.app/Contents/Resources/runtime")
        );
    }

    fn converged_facts() -> HostFacts {
        HostFacts {
            os: HostOs::Linux,
            arch: Arch::Amd64,
            free_disk_bytes: Bytes(20 * GIB),
            total_memory_bytes: Bytes(16 * GIB),
            runtime_staged: true,
            runtime_hash_ok: true,
            machines: vec![],
            engine_container: Some(ContainerFact {
                exists: true,
                running: true,
                image_digest: Some("sha256:engine-good".into()),
            }),
            local_engine_image_digest: Some("sha256:engine-good".into()),
            local_companion_image_digest: None,
            published_port: Some(Port(37013)),
            data_volume: true,
            companion_scaffold: false,
            companion_containers: CompanionContainers::default(),
            companion_health: CompanionHealth::Unknown,
            daemon_health: DaemonHealth::Healthy,
            app_version: SemVer::parse("0.2.0").unwrap(),
            user_ns_allowed: true,
            helper_installed: true,
            local_state: LocalStateFact::Trusted,
            another_instance_running: false,
        }
    }

    fn service(probe: ScriptedProbe, driver: ScriptedDriver) -> (BootService, Arc<FakeClock>) {
        let clock = Arc::new(FakeClock::new());
        let svc = BootService::new(
            Arc::new(probe),
            Arc::new(driver),
            clock.clone(),
            desired(),
            SemVer::parse("0.2.0").unwrap(),
        );
        (svc, clock)
    }

    fn ticket() -> BootstrapTicket {
        BootstrapTicket::new("http://127.0.0.1:37013/?k=test-ticket".to_string())
    }

    #[test]
    fn factory_boot_does_not_deliver_engine_ticket_before_ads_is_healthy() {
        let ads = ImageRef::new("ghcr.io/devwspito/safent-ads", "sha256:ads-good").unwrap();
        let mut wanted = desired();
        wanted.companion_image = Some(ads.clone());
        let mut fresh = converged_facts();
        fresh.engine_container = None;
        fresh.published_port = None;
        let engine_only = converged_facts();
        let mut scaffold = engine_only.clone();
        scaffold.companion_scaffold = true;
        let mut pulled = scaffold.clone();
        pulled.local_companion_image_digest = Some(ads.digest.clone());
        let mut healthy = pulled.clone();
        healthy.companion_containers = CompanionContainers {
            running: 4,
            total: 4,
        };
        healthy.companion_health = CompanionHealth::Reachable;
        let driver = Arc::new(ScriptedDriver::new(vec![
            (
                RepairAction::CreateContainer,
                Ok(ApplyOutcome::Ready(ticket())),
            ),
            (
                RepairAction::EnsureCompanionScaffold,
                Ok(ApplyOutcome::Progressed),
            ),
            (
                RepairAction::PullCompanion(ads.clone()),
                Ok(ApplyOutcome::Progressed),
            ),
            (
                RepairAction::ComposeCompanionUp(ads),
                Ok(ApplyOutcome::Progressed),
            ),
            (
                RepairAction::StartContainer,
                Ok(ApplyOutcome::Ready(BootstrapTicket::new(
                    "http://127.0.0.1:37013/?k=fresh-after-ads".into(),
                ))),
            ),
        ]));
        let svc = BootService::new(
            Arc::new(ScriptedProbe::new(vec![
                Ok(fresh),
                Ok(engine_only),
                Ok(scaffold),
                Ok(pulled),
                Ok(healthy),
            ])),
            driver.clone(),
            Arc::new(FakeClock::new()),
            wanted,
            SemVer::parse("0.9.5").unwrap(),
        );
        let notifier = RecordingNotifier::new();
        let result = svc.run(&notifier, &CancelSignal::new());
        let LoopOutcome::Ready { ticket, lifecycle } = result else {
            panic!("expected complete factory boot");
        };
        assert!(ticket.expose().ends_with("fresh-after-ads"));
        assert_eq!(lifecycle.phase(), EnginePhase::CompanionReady);
        assert_eq!(driver.applied().len(), 5);
        assert_eq!(
            notifier
                .events()
                .iter()
                .filter(|event| matches!(event, DomainEvent::EngineReady { .. }))
                .count(),
            1
        );
    }

    #[test]
    fn already_converged_reissues_up_once_for_a_fresh_ticket() {
        // No cache to trust, so the loop re-observes for real — an ALREADY
        // fully running engine (adopted from a previous session) means
        // reconcile converges on the very first observation, and the only
        // way to get a ticket is re-invoking `up` (idempotent).
        let probe = ScriptedProbe::new(vec![Ok(converged_facts())]);
        let driver = ScriptedDriver::new(vec![(
            RepairAction::StartContainer,
            Ok(ApplyOutcome::Ready(ticket())),
        )]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();
        let cancel = CancelSignal::new();

        match service.run(&notifier, &cancel) {
            LoopOutcome::Ready { ticket, lifecycle } => {
                assert_eq!(ticket.expose(), "http://127.0.0.1:37013/?k=test-ticket");
                assert_eq!(lifecycle.phase(), EnginePhase::EngineReady);
            }
            other => panic!("expected Ready, got {other:?}"),
        }
        assert!(notifier
            .events()
            .iter()
            .any(|e| matches!(e, DomainEvent::EngineReady { .. })));
    }

    #[test]
    fn factory_boot_never_navigates_when_running_ads_fails_health() {
        let ads = ImageRef::new("ghcr.io/devwspito/safent-ads", "sha256:ads-good").unwrap();
        let mut wanted = desired();
        wanted.companion_image = Some(ads.clone());
        let mut facts = converged_facts();
        facts.companion_scaffold = true;
        facts.local_companion_image_digest = Some(ads.digest.clone());
        facts.companion_containers = CompanionContainers {
            running: 4,
            total: 4,
        };
        facts.companion_health = CompanionHealth::Unreachable;
        let driver = Arc::new(ScriptedDriver::new(vec![
            (
                RepairAction::ComposeCompanionUp(ads.clone()),
                Err(EngineErrorKind::Io("health unavailable".into())),
            ),
            (
                RepairAction::ComposeCompanionUp(ads),
                Err(EngineErrorKind::Io("health unavailable".into())),
            ),
        ]));
        let svc = BootService::new(
            Arc::new(ScriptedProbe::new(vec![Ok(facts.clone()), Ok(facts)])),
            driver.clone(),
            Arc::new(FakeClock::new()),
            wanted,
            SemVer::parse("0.9.5").unwrap(),
        );
        let notifier = RecordingNotifier::new();
        assert!(matches!(
            svc.run(&notifier, &CancelSignal::new()),
            LoopOutcome::Degraded { .. }
        ));
        assert_eq!(driver.applied().len(), 2);
        assert!(!notifier
            .events()
            .iter()
            .any(|event| matches!(event, DomainEvent::EngineReady { .. })));
    }

    #[test]
    fn factory_final_ticket_does_not_hide_health_lost_during_core_up() {
        let ads = ImageRef::new("ghcr.io/devwspito/safent-ads", "sha256:ads-good").unwrap();
        let mut wanted = desired();
        wanted.companion_image = Some(ads.clone());
        let mut before = converged_facts();
        before.companion_scaffold = true;
        before.local_companion_image_digest = Some(ads.digest);
        before.companion_containers = CompanionContainers {
            running: 4,
            total: 4,
        };
        before.companion_health = CompanionHealth::Reachable;
        let mut after = before.clone();
        after.companion_health = CompanionHealth::Unreachable;
        let svc = BootService::new(
            Arc::new(ScriptedProbe::new(vec![Ok(before), Ok(after)])),
            Arc::new(ScriptedDriver::new(vec![(
                RepairAction::StartContainer,
                Ok(ApplyOutcome::Ready(ticket())),
            )])),
            Arc::new(FakeClock::new()),
            wanted,
            SemVer::parse("0.9.5").unwrap(),
        );
        let notifier = RecordingNotifier::new();
        assert!(matches!(
            svc.run(&notifier, &CancelSignal::new()),
            LoopOutcome::Degraded { .. }
        ));
        assert!(!notifier
            .events()
            .iter()
            .any(|event| matches!(event, DomainEvent::EngineReady { .. })));
    }

    #[test]
    fn converges_through_a_realistic_chain_of_actions() {
        let mut fresh = converged_facts();
        fresh.runtime_staged = false;
        fresh.runtime_hash_ok = false;
        fresh.engine_container = None;
        fresh.local_engine_image_digest = None;
        fresh.published_port = None;

        let mut runtime_staged = fresh.clone();
        runtime_staged.runtime_staged = true;
        runtime_staged.runtime_hash_ok = true;

        let mut image_pulled = runtime_staged.clone();
        image_pulled.local_engine_image_digest = Some("sha256:engine-good".into());

        let probe = ScriptedProbe::new(vec![
            Ok(fresh),             // -> StageRuntime
            Ok(runtime_staged),    // -> PullEngine
            Ok(image_pulled), // -> ChoosePort (no container, no port on record: CreateContainer)
            Ok(converged_facts()), // after `up`, fully converged
        ]);
        let driver = ScriptedDriver::new(vec![
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
            (
                RepairAction::PullEngine(engine_image()),
                Ok(ApplyOutcome::Progressed),
            ),
            (
                RepairAction::CreateContainer,
                Ok(ApplyOutcome::Ready(ticket())),
            ),
        ]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();

        match service.run(&notifier, &CancelSignal::new()) {
            LoopOutcome::Ready { lifecycle, .. } => {
                assert_eq!(lifecycle.phase(), EnginePhase::EngineReady)
            }
            other => panic!("expected Ready, got {other:?}"),
        }
    }

    #[test]
    fn a_second_instance_focuses_the_existing_window_without_touching_anything() {
        let mut facts = converged_facts();
        facts.another_instance_running = true;
        let probe = ScriptedProbe::new(vec![Ok(facts)]);
        let driver = ScriptedDriver::new(vec![]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();

        assert!(matches!(
            service.run(&notifier, &CancelSignal::new()),
            LoopOutcome::FocusExisting
        ));
    }

    #[test]
    fn same_action_failing_twice_degrades_with_backoff_between_attempts() {
        let mut fresh = converged_facts();
        fresh.runtime_staged = false;
        fresh.runtime_hash_ok = false;

        let probe = ScriptedProbe::new(vec![Ok(fresh)]);
        let driver = ScriptedDriver::new(vec![(
            RepairAction::StageRuntime,
            Err(EngineErrorKind::Io("registro no disponible".to_string())),
        )]);
        // ScriptedDriver consumes its scripted response on first use; a
        // second `apply()` call for an action with no script left errors
        // with a distinct message, which still counts as "the same action,
        // a nonzero-th failure" for the purposes of this test since we only
        // assert on the FINAL outcome + that backoff was requested at least
        // once — the exact code differing on call 2 does not matter here.
        let (service, clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();

        let outcome = service.run(&notifier, &CancelSignal::new());
        assert!(
            matches!(outcome, LoopOutcome::Degraded { .. }),
            "{outcome:?}"
        );
        assert!(
            !clock.requested_sleeps().is_empty(),
            "must back off between attempts, not spin"
        );
    }

    /// The real packaged-Linux bug (specs/028-safent-app-nativa/
    /// verificacion-paquete-linux.md): `cmd_stage_runtime` as a no-op
    /// returns SUCCESS every time (`Ok(Progressed)`) without ever changing
    /// `runtime_staged`. Every `Err`-based guard in this loop is silent —
    /// nothing ever fails — so unbounded `Ok(Progressed)` against unchanged
    /// facts needs its OWN guard. Scripts `StageRuntime` to "succeed" far
    /// more times than a fixed loop cap could excuse away as coincidence,
    /// to prove the loop stops ITSELF, not that the script merely ran out.
    #[test]
    fn same_action_succeeding_repeatedly_without_changing_facts_degrades_after_one_apply() {
        let mut fresh = converged_facts();
        fresh.runtime_staged = false;
        fresh.runtime_hash_ok = false;

        // Facts never change (ScriptedProbe repeats its last entry forever).
        let probe = ScriptedProbe::new(vec![Ok(fresh)]);
        let driver = ScriptedDriver::new(vec![
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
            (RepairAction::StageRuntime, Ok(ApplyOutcome::Progressed)),
        ]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();

        let outcome = service.run(&notifier, &CancelSignal::new());
        match outcome {
            LoopOutcome::Degraded { lifecycle } => {
                assert_eq!(
                    lifecycle.last_failure().map(|f| f.code),
                    Some(FailureCode::RepairIneffective)
                );
            }
            other => panic!("expected Degraded(RepairIneffective), got {other:?}"),
        }
        assert!(
            notifier.events().iter().any(|e| matches!(
                e,
                DomainEvent::EngineDegraded {
                    cause: FailureCause {
                        code: FailureCode::RepairIneffective,
                        ..
                    }
                }
            )),
            "{:?}",
            notifier.events()
        );
        assert!(notifier
            .events()
            .iter()
            .any(|e| matches!(e, DomainEvent::EngineDegraded { .. })));
    }

    #[test]
    fn cancelling_before_the_point_of_no_return_stops_the_loop() {
        let mut fresh = converged_facts();
        fresh.runtime_staged = false;
        fresh.runtime_hash_ok = false;

        let probe = ScriptedProbe::new(vec![Ok(fresh)]);
        let driver = ScriptedDriver::new(vec![]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();
        let cancel = CancelSignal::new();
        cancel.set();

        assert!(matches!(
            service.run(&notifier, &cancel),
            LoopOutcome::Cancelled { .. }
        ));
        assert!(
            notifier.events().iter().any(|event| matches!(
                event,
                DomainEvent::EngineDegraded {
                    cause: FailureCause {
                        code: FailureCode::CancelledByOwner,
                        retryable: true,
                        ..
                    }
                }
            )),
            "honoured cancellation must reach the renderer, not leave it preparing"
        );
    }

    #[test]
    fn controlled_boot_lifecycle_cancel_retry_cancel_notifies_each_attempt() {
        let control = crate::bootstrap_control::BootstrapControl::default();
        let mut previous = None;
        for _ in 0..2 {
            let attempt = control.begin(previous).unwrap();
            assert!(!attempt.signal.is_set());
            assert!(control.begin(Some(attempt.id)).is_err());
            control.cancel(attempt.id).unwrap();
            let mut facts = converged_facts();
            facts.runtime_staged = false;
            facts.runtime_hash_ok = false;
            let (service, _) = service(
                ScriptedProbe::new(vec![Ok(facts)]),
                ScriptedDriver::new(vec![]),
            );
            let notifier = RecordingNotifier::new();
            assert!(matches!(
                service.run(&notifier, &attempt.signal),
                LoopOutcome::Cancelled { .. }
            ));
            assert_eq!(
                notifier
                    .events()
                    .iter()
                    .filter(|event| matches!(
                        event,
                        DomainEvent::EngineDegraded {
                            cause: FailureCause {
                                code: FailureCode::CancelledByOwner,
                                retryable: true,
                                ..
                            }
                        }
                    ))
                    .count(),
                1
            );
            previous = Some(attempt.id);
            drop(attempt);
            assert_eq!(
                control.cancel(previous.unwrap()),
                Err("bootstrap_not_running")
            );
        }
    }

    #[test]
    fn cancelling_past_the_point_of_no_return_is_rejected() {
        // Already inside `up` (EngineStarting, past the point of no return) —
        // the driver is scripted to ignore cancellation the way the real
        // adapter would past that point, and this test proves BootService
        // does not even ask it to: it hands down a fresh, unset signal.
        let mut image_pulled = converged_facts();
        image_pulled.engine_container = None;
        image_pulled.published_port = None;

        let probe = ScriptedProbe::new(vec![Ok(image_pulled)]);
        let driver = ScriptedDriver::new(vec![(
            RepairAction::CreateContainer,
            Ok(ApplyOutcome::Ready(ticket())),
        )]);
        let (service, _clock) = service(probe, driver);
        let notifier = RecordingNotifier::new();
        let control = crate::bootstrap_control::BootstrapControl::default();
        let attempt = control.begin(None).unwrap();
        control.cancel(attempt.id).unwrap();
        let cancel = attempt.signal.clone(); // shared command signal, ignored past the gate

        match service.run(&notifier, &cancel) {
            LoopOutcome::Ready { .. } => {}
            other => panic!(
                "expected Ready (cancel rejected past the point of no return), got {other:?}"
            ),
        }
    }
}

/// MAC-03 (verificacion-mac-1.md): `desired_state_from_runtime` reads
/// `runtime-bundle.json`'s `engine_image`/`companion_image`, falling back
/// to `SAFENT_ENGINE_DIGEST`/`SAFENT_COMPANION_DIGEST` ONLY as an explicit
/// override — a separate module (not `mod tests` above) because these
/// mutate real process env vars (`std::env::set_var` is not thread-safe
/// across `cargo test`'s parallel threads in general) and need their own
/// serializing mutex, the same technique
/// `engine_adapter_real_cli_contract.rs` uses.
#[cfg(test)]
mod desired_state_from_runtime_tests {
    use super::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());
    const ENGINE_DIGEST_VARS: &[&str] = &[
        "SAFENT_ENGINE_DIGEST",
        "SAFENT_ENGINE_IMAGE_REPO",
        "SAFENT_COMPANION_DIGEST",
        "SAFENT_COMPANION_IMAGE_REPO",
    ];

    /// # Safety
    /// Caller must hold `ENV_LOCK` for the duration of every effect this
    /// clears — mirrors `engine_adapter_real_cli_contract.rs`'s own
    /// `set_env` doc comment.
    unsafe fn clear_digest_env() {
        for var in ENGINE_DIGEST_VARS {
            unsafe { std::env::remove_var(var) };
        }
    }

    fn unique_dir(label: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "safent-bundle-manifest-{}-{}-{label}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).expect("create unique test dir");
        dir
    }

    fn write_bundle_json(dir: &Path, body: &str) {
        std::fs::write(dir.join("runtime-bundle.json"), body).expect("write runtime-bundle.json");
    }

    #[test]
    fn reads_the_engine_digest_from_a_shipped_runtime_bundle_manifest() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("valid");
        write_bundle_json(
            &dir,
            r#"{"podman_version":"6.1.1","entries":[],
                "engine_image":{"repo":"ghcr.io/devwspito/safent","digest":"sha256:engine-good"}}"#,
        );

        let desired = desired_state_from_runtime(&dir)
            .expect("a valid manifest with a pinned digest must resolve");

        assert_eq!(
            desired.engine_image.reference(),
            "ghcr.io/devwspito/safent@sha256:engine-good"
        );
        assert!(desired.companion_image.is_none());
        assert!(
            native_desired_state_from_runtime(&dir).is_err(),
            "the windowed product cannot silently omit bundled Ads"
        );
    }

    #[test]
    fn reads_both_engine_and_companion_digests_when_both_are_pinned() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("with-companion");
        write_bundle_json(
            &dir,
            r#"{"podman_version":"6.1.1","entries":[],
                "engine_image":{"repo":"ghcr.io/devwspito/safent","digest":"sha256:engine-good"},
                "companion_image":{"repo":"ghcr.io/devwspito/safent-ads","digest":"sha256:ads-good"}}"#,
        );

        let desired = native_desired_state_from_runtime(&dir)
            .expect("factory native product requires both digests");

        assert_eq!(
            desired
                .companion_image
                .expect("companion_image must be Some")
                .reference(),
            "ghcr.io/devwspito/safent-ads@sha256:ads-good"
        );
    }

    #[test]
    fn a_missing_bundle_manifest_fails_closed_with_engine_digest_missing() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("no-manifest-at-all");

        let cause = desired_state_from_runtime(&dir)
            .expect_err("no runtime-bundle.json at all must not silently succeed");

        assert_eq!(cause.code, FailureCode::EngineDigestMissing);
        assert!(!cause.retryable);
    }

    #[test]
    fn a_null_engine_digest_in_the_manifest_fails_closed_with_engine_digest_missing() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("null-digest");
        // Exactly what stage-runtime.sh writes for a checkout the release
        // pipeline has not pinned a real digest into yet (MAC-03's own
        // documented seam) — a legitimate state, not a parse error.
        write_bundle_json(
            &dir,
            r#"{"podman_version":"6.1.1","entries":[],
                "engine_image":{"repo":"ghcr.io/devwspito/safent","digest":null}}"#,
        );

        let cause = desired_state_from_runtime(&dir)
            .expect_err("a null digest is not yet pinned — must fail closed, not guess");

        assert_eq!(cause.code, FailureCode::EngineDigestMissing);
        assert!(!cause.retryable);
    }

    #[test]
    fn an_env_override_is_used_even_when_the_shipped_manifest_is_missing_or_broken() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe {
            clear_digest_env();
            std::env::set_var("SAFENT_ENGINE_DIGEST", "sha256:from-env-override");
        }
        let dir = unique_dir("no-manifest-env-override");

        let result = desired_state_from_runtime(&dir);

        // SAFETY: ENV_LOCK still held — clear before any assertion can panic
        // and skip it, so a failure here cannot leak the override to a
        // later test.
        unsafe { clear_digest_env() };

        let desired = result.expect("an explicit env override must work without a real bundle");
        assert_eq!(
            desired.engine_image.reference(),
            "ghcr.io/devwspito/safent@sha256:from-env-override"
        );
    }

    /// A digest recorded for THIS machine's own platform (what
    /// `stage-runtime.sh`'s `platform_for_target` writes for a native build)
    /// resolves exactly like a manifest with no `platform` field at all —
    /// the field is a cross-check, never an additional requirement to opt
    /// into.
    #[test]
    fn an_engine_platform_matching_this_machine_resolves_normally() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("platform-match");
        let here = crate::update::container_platform_key();
        write_bundle_json(
            &dir,
            &format!(
                r#"{{"podman_version":"6.1.1","entries":[],
                    "engine_image":{{"repo":"ghcr.io/devwspito/safent","digest":"sha256:engine-good","platform":"{here}"}}}}"#
            ),
        );

        let desired = desired_state_from_runtime(&dir)
            .expect("a platform recorded for THIS machine's own arch must resolve");

        assert_eq!(
            desired.engine_image.reference(),
            "ghcr.io/devwspito/safent@sha256:engine-good"
        );
    }

    /// The scenario this whole cross-check exists for: a digest injected by
    /// SAFENT_ENGINE_DIGEST/stage-runtime.sh for the WRONG Linux VM
    /// architecture (e.g. a cross-compiled x86_64 build accidentally handed
    /// an arm64 pin, or vice versa) must never be trusted just because it
    /// parses — `wrong` is deliberately a REAL platform string, not garbage,
    /// so this proves the two real values are told apart, not merely that
    /// malformed input is rejected.
    #[test]
    fn an_engine_platform_for_a_different_machine_fails_closed_instead_of_pulling_the_wrong_image()
    {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("platform-mismatch");
        let wrong = if crate::update::container_platform_key() == "linux/arm64" {
            "linux/amd64"
        } else {
            "linux/arm64"
        };
        write_bundle_json(
            &dir,
            &format!(
                r#"{{"podman_version":"6.1.1","entries":[],
                    "engine_image":{{"repo":"ghcr.io/devwspito/safent","digest":"sha256:engine-good","platform":"{wrong}"}}}}"#
            ),
        );

        let cause = desired_state_from_runtime(&dir)
            .expect_err("a digest pinned for a different platform must never be trusted");

        assert_eq!(cause.code, FailureCode::EngineDigestMissing);
        assert!(!cause.retryable);
        assert!(
            cause.message.contains("plataforma"),
            "message should call out the platform mismatch specifically, got: {}",
            cause.message
        );
    }

    /// Companion mirrors its existing best-effort `Option` semantics
    /// (already true for an absent/malformed digest): a platform mismatch
    /// degrades to "no companion pinned", never a hard boot failure — only
    /// the engine image is load-bearing enough to fail closed on.
    #[test]
    fn a_companion_platform_mismatch_silently_drops_the_companion_but_the_engine_still_resolves() {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: ENV_LOCK held for the whole test body.
        unsafe { clear_digest_env() };
        let dir = unique_dir("companion-platform-mismatch");
        let here = crate::update::container_platform_key();
        let wrong = if here == "linux/arm64" {
            "linux/amd64"
        } else {
            "linux/arm64"
        };
        write_bundle_json(
            &dir,
            &format!(
                r#"{{"podman_version":"6.1.1","entries":[],
                    "engine_image":{{"repo":"ghcr.io/devwspito/safent","digest":"sha256:engine-good","platform":"{here}"}},
                    "companion_image":{{"repo":"ghcr.io/devwspito/safent-ads","digest":"sha256:ads-good","platform":"{wrong}"}}}}"#
            ),
        );

        let desired = desired_state_from_runtime(&dir)
            .expect("a companion platform mismatch must never fail the whole boot");

        assert_eq!(
            desired.engine_image.reference(),
            "ghcr.io/devwspito/safent@sha256:engine-good"
        );
        assert!(
            desired.companion_image.is_none(),
            "a companion pinned for the wrong platform must degrade to None, not be trusted"
        );
    }
}
