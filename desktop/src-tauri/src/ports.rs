//! Application-layer collaborator interfaces (hexagonal "ports"). Nothing in
//! this module spawns a process, opens a file, or imports `tauri` —
//! `engine_adapter.rs` implements these against the embedded CLI; `boot.rs`
//! depends only on the traits; tests depend on the fakes at the bottom.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::domain::{
    BootstrapTicket, DomainEvent, FailureCause, FailureCode, HostFacts, RepairAction,
};

/// Shared, cheap-to-check cancellation signal. `boot.rs` owns one per
/// bootstrap attempt and flips it when the owner cancels; the adapter polls
/// it between NDJSON lines so a mid-flight `apply()` can be interrupted
/// before its point of no return (contract app-engine.md §6). `Clone` is
/// cheap (an `Arc`) — the same signal is handed to the driver call AND kept
/// by whatever is listening for the owner's cancel gesture.
#[derive(Clone, Default)]
pub struct CancelSignal(Arc<AtomicBool>);

impl CancelSignal {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    pub fn is_set(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    /// Clears a signal for reuse across a NEW bootstrap attempt over the SAME
    /// shared instance — `boot.rs`'s `retry_bootstrap`/`safent://restart-
    /// engine-requested` handler must call this on the app-MANAGED
    /// `CancelSignal` (never construct a fresh, unmanaged `CancelSignal::new()`
    /// for a retry) or the "Cancelar" command — which only ever reads the ONE
    /// instance registered with `app.manage()` — silently stops doing
    /// anything the moment a retry starts (cancel-after-retry bug).
    pub fn reset(&self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

#[cfg(test)]
mod cancel_signal_tests {
    use super::*;

    #[test]
    fn reset_clears_a_previously_set_signal_for_reuse() {
        let cancel = CancelSignal::new();
        cancel.set();
        assert!(cancel.is_set());

        cancel.reset();

        assert!(!cancel.is_set(), "reset must clear the signal for the next attempt");
    }

    #[test]
    fn reset_is_observed_through_every_clone_of_the_same_shared_signal() {
        // `retry_bootstrap` clones the app-managed instance rather than
        // constructing a new one — this is the exact sharing property that
        // fix depends on: a reset on one clone must be visible through every
        // other clone (same underlying Arc<AtomicBool>), and a clone taken
        // BEFORE reset must still observe it (not a snapshot).
        let cancel = CancelSignal::new();
        let handle_held_by_running_loop = cancel.clone();
        cancel.set();
        assert!(handle_held_by_running_loop.is_set());

        let handle_held_by_cancel_command = cancel.clone();
        cancel.reset();

        assert!(!handle_held_by_running_loop.is_set());
        assert!(!handle_held_by_cancel_command.is_set());
    }
}

/// Observes the host + engine and reports what is actually true right now.
/// Never mutates anything (contract app-engine.md §4, `facts --json`).
pub trait EngineProbe: Send + Sync {
    fn observe(&self) -> Result<HostFacts, EngineError>;
}

/// Applies exactly one `RepairAction`. Implementations MUST be idempotent:
/// calling `apply` twice with the same action against the same host state is
/// safe (every verb in contract app-engine.md §4 is documented idempotent).
pub trait EngineDriver: Send + Sync {
    fn apply(
        &self,
        action: &RepairAction,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> Result<ApplyOutcome, EngineError>;

    /// Deliberate, orderly shutdown of an already-running engine — NOT a
    /// `RepairAction` (nothing here converges toward `DesiredState`; it is the
    /// tray's explicit "Salir", research.md FR-030: cerrar la ventana no para
    /// el motor, pero Salir sí). Idempotent: stopping an already-stopped
    /// engine succeeds.
    fn stop(&self) -> Result<(), EngineError>;
}

/// What a successful `apply` produced. `Ready` carries the one-shot bootstrap
/// ticket (contract app-engine.md §5) — never logged, never stored past the
/// single navigation that consumes it (`BootstrapTicket`'s own invariant;
/// `Debug` is safe to derive here BECAUSE `BootstrapTicket`'s own `Debug`
/// redacts).
#[derive(Debug)]
pub enum ApplyOutcome {
    Progressed,
    Ready(BootstrapTicket),
}

/// Time abstraction so the boot loop's backoff/stall detection is
/// deterministic in tests — no real sleeping, no wall-clock flakiness.
pub trait Clock: Send + Sync {
    /// Not read by `boot.rs`'s loop today (only `sleep` drives backoff) —
    /// kept as the natural pairing for a future elapsed-time diagnostic
    /// (e.g. "still degraded after N minutes") without widening this trait.
    #[allow(dead_code)]
    fn now(&self) -> Instant;
    fn sleep(&self, duration: Duration);
}

/// Where lifecycle progress goes. Production has two implementations: the
/// Tauri event emitter (boot.rs, windowed) and the NDJSON stdout writer
/// (selftest.rs, headless). Tests use `fakes::RecordingNotifier`.
pub trait Notifier: Send + Sync {
    fn notify(&self, event: &DomainEvent);
}

/// Adapter/infrastructure failure — distinct from `domain::FailureCause` (a
/// *business* classification the owner can be shown). `EngineError` is what a
/// port raises when it cannot even determine or apply a fact; `to_failure_cause`
/// is the one place that maps it down into the closed domain vocabulary.
#[derive(Debug)]
pub enum EngineError {
    /// No output for longer than the configured stall timeout, or the whole
    /// invocation ran past its hard cap.
    Timeout { after: Duration },
    /// Could not even spawn/read/write the child process — a TRANSIENT
    /// condition worth retrying (e.g. a pipe read failing mid-stream).
    Io(String),
    /// `spawn` itself failed with `ErrorKind::NotFound`: the CLI binary
    /// does not exist at `path`. Distinct from the generic `Io` on purpose
    /// (MAC-04, verificacion-mac-1.md): a missing executable is a
    /// packaging/path defect that retrying can NEVER fix by itself — before
    /// this variant existed, `selftest.rs` resolving `Contents/MacOS/
    /// runtime` on a real macOS `.app` (the CLI actually ships at
    /// `Contents/Resources/runtime/…`) surfaced as `daemon_unhealthy`/
    /// `retryable:true`, an infinite no-progress retry loop against a path
    /// that could never start existing.
    CliNotFound { path: PathBuf, verb: String },
    /// stdout produced a line that is not a valid `EngineEvent` (contract
    /// app-engine.md §3) — most commonly because the installed CLI predates
    /// `--porcelain` (pre-T004) and printed its usual human text instead.
    UnexpectedOutput { line: String },
    /// The process exited non-zero without a prior `failed` event to explain why.
    ProcessExited {
        code: Option<i32>,
        stderr_tail: String,
    },
    /// This `RepairAction` has no mapping to the CLI contract at all (e.g.
    /// `FocusExistingWindow`, a pure window action `boot.rs` must special-case
    /// and never hand to this port).
    UnsupportedByAdapter { action: RepairAction },
    /// The CLI spoke NDJSON but violated a documented invariant (e.g. `up`
    /// emitted `ready` without ever writing the secret-fd line).
    Protocol(String),
    /// The CLI itself reported a classified failure
    /// (`{t:'failed', code, detail, retryable}`, contract app-engine.md §3) —
    /// already a `FailureCause`, no further mapping needed.
    Reported(FailureCause),
    /// `cancel` was set and the adapter honored it before the point of no
    /// return (contract app-engine.md §6). Distinct from every other
    /// variant: this is not a fault, it is the owner's own decision.
    Cancelled,
    /// `up` emitted `ready` but the secret fd closed without a line
    /// (contract app-engine.md §5). Distinct from `Protocol` on purpose:
    /// `boot.rs` reacts to THIS one specific shape with FR-012's
    /// `Reconnecting{TokenMissing}`, not with the generic degrade path.
    ReadyWithoutTicket,
}

impl EngineError {
    pub fn to_failure_cause(&self) -> FailureCause {
        match self {
            EngineError::Timeout { after } => FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: format!("sin respuesta durante {after:?}"),
                retryable: true,
            },
            EngineError::Io(message) => FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: message.clone(),
                retryable: true,
            },
            // Same honest, non-retryable classification the CLI's own
            // pre-`--porcelain` case gets: the bundled `safent` this
            // adapter needs is not usable as shipped, just for a different
            // reason (absent vs. too old) — MAC-04.
            EngineError::CliNotFound { path, verb } => FailureCause {
                code: FailureCode::CliPorcelainUnsupported,
                message: format!(
                    "no se encontró el CLI empaquetado en {} (verbo '{verb}') \
                     — el paquete está incompleto o mal resuelto",
                    path.display()
                ),
                retryable: false,
            },
            EngineError::ProcessExited { stderr_tail, .. } => FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: stderr_tail.clone(),
                retryable: true,
            },
            EngineError::UnexpectedOutput { .. } | EngineError::Protocol(_) => FailureCause {
                code: FailureCode::CliPorcelainUnsupported,
                message: "el motor instalado no habla el protocolo esperado todavía".to_string(),
                retryable: false,
            },
            EngineError::UnsupportedByAdapter { .. } => FailureCause {
                code: FailureCode::CliPorcelainUnsupported,
                message: "esta acción no está disponible en este adaptador".to_string(),
                retryable: false,
            },
            EngineError::Reported(cause) => cause.clone(),
            EngineError::Cancelled => FailureCause {
                code: FailureCode::CancelledByOwner,
                message: "cancelado por el dueño".to_string(),
                retryable: false,
            },
            EngineError::ReadyWithoutTicket => FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: "el motor no entregó un vale de arranque".to_string(),
                retryable: true,
            },
        }
    }
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EngineError::Timeout { after } => write!(f, "timed out after {after:?}"),
            EngineError::Io(message) => write!(f, "io error: {message}"),
            EngineError::CliNotFound { path, verb } => {
                write!(f, "cli not found at {} for verb '{verb}'", path.display())
            }
            EngineError::UnexpectedOutput { line } => write!(f, "unexpected output: {line}"),
            EngineError::ProcessExited { code, stderr_tail } => {
                write!(f, "process exited with {code:?}: {stderr_tail}")
            }
            EngineError::UnsupportedByAdapter { action } => {
                write!(f, "unsupported by adapter: {action:?}")
            }
            EngineError::Protocol(message) => write!(f, "protocol error: {message}"),
            EngineError::Reported(cause) => write!(f, "reported by CLI: {cause:?}"),
            EngineError::Cancelled => write!(f, "cancelled by owner"),
            EngineError::ReadyWithoutTicket => write!(f, "ready without a line on the secret fd"),
        }
    }
}

impl std::error::Error for EngineError {}

/// Test doubles for `EngineProbe`/`EngineDriver`/`Clock`/`Notifier`.
/// `#[cfg(test)]`, not feature-gated: `cargo test` sets it crate-wide for
/// BOTH this bin's own unit tests AND every `tests/*.rs` integration binary
/// (each pulls this file in via `#[path]` and is itself a `--test` target),
/// so no production build ever carries these doubles, and nothing outside
/// tests needs to opt in. Constitution Principle V: base tests never touch
/// containers/network.
///
/// `allow(dead_code)` at the module level, deliberately: this is shared test
/// infrastructure offering a full API (every `EngineErrorKind`, every
/// assertion helper) for WHICHEVER test needs it — no single test file in
/// this crate is expected to exercise all of it, the same way a test-support
/// crate is not judged by whether every one of its helpers has a caller yet.
#[cfg(test)]
#[allow(dead_code)]
pub mod fakes {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    use super::*;
    use crate::domain::HostFacts;

    /// Returns a scripted sequence of `HostFacts`, one per call, holding the
    /// last one once the script runs out (so a test can under-specify a long
    /// convergence without repeating the final steady state).
    pub struct ScriptedProbe {
        script: Vec<Result<HostFacts, EngineError>>,
        calls: AtomicUsize,
    }

    impl ScriptedProbe {
        pub fn new(script: Vec<Result<HostFacts, EngineError>>) -> Self {
            assert!(
                !script.is_empty(),
                "ScriptedProbe needs at least one observation"
            );
            Self {
                script,
                calls: AtomicUsize::new(0),
            }
        }

        pub fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl EngineProbe for ScriptedProbe {
        fn observe(&self) -> Result<HostFacts, EngineError> {
            let i = self.calls.fetch_add(1, Ordering::SeqCst);
            let idx = i.min(self.script.len() - 1);
            match &self.script[idx] {
                Ok(facts) => Ok(facts.clone()),
                Err(EngineError::Io(m)) => Err(EngineError::Io(m.clone())),
                Err(_) => Err(EngineError::Io("scripted probe error".to_string())),
            }
        }
    }

    /// Drives `apply()` from a scripted outcome PER `RepairAction` variant
    /// name — good enough to simulate "the same action keeps failing" or "it
    /// succeeds on the Nth attempt" without a real CLI.
    pub struct ScriptedDriver {
        responses: Mutex<Vec<(RepairAction, Result<ApplyOutcome, EngineErrorKind>)>>,
        applied: Mutex<Vec<RepairAction>>,
        stop_calls: AtomicUsize,
        stop_fails: bool,
    }

    /// `EngineError` is not `Clone` (it carries owned diagnostic strings by
    /// design); tests describe failures with this small, clonable shape
    /// instead and `ScriptedDriver` builds the real error on demand.
    #[derive(Debug, Clone)]
    pub enum EngineErrorKind {
        Timeout,
        Io(String),
        ProcessExited(String),
    }

    impl EngineErrorKind {
        fn into_engine_error(self) -> EngineError {
            match self {
                EngineErrorKind::Timeout => EngineError::Timeout {
                    after: Duration::from_secs(30),
                },
                EngineErrorKind::Io(m) => EngineError::Io(m),
                EngineErrorKind::ProcessExited(m) => EngineError::ProcessExited {
                    code: Some(1),
                    stderr_tail: m,
                },
            }
        }
    }

    impl ScriptedDriver {
        pub fn new(responses: Vec<(RepairAction, Result<ApplyOutcome, EngineErrorKind>)>) -> Self {
            Self {
                responses: Mutex::new(responses),
                applied: Mutex::new(Vec::new()),
                stop_calls: AtomicUsize::new(0),
                stop_fails: false,
            }
        }

        /// A driver whose `stop()` always fails — for testing that a failed
        /// engine stop never blocks the window from closing (best-effort).
        pub fn with_failing_stop(mut self) -> Self {
            self.stop_fails = true;
            self
        }

        /// Every action that was actually applied, in order — lets a test
        /// assert the loop asked for what it should have, not just the result.
        pub fn applied(&self) -> Vec<RepairAction> {
            self.applied.lock().expect("poisoned").clone()
        }

        pub fn stop_calls(&self) -> usize {
            self.stop_calls.load(Ordering::SeqCst)
        }
    }

    impl EngineDriver for ScriptedDriver {
        fn apply(
            &self,
            action: &RepairAction,
            _notifier: &dyn Notifier,
            cancel: &CancelSignal,
        ) -> Result<ApplyOutcome, EngineError> {
            if cancel.is_set() {
                return Err(EngineError::Cancelled);
            }
            self.applied.lock().expect("poisoned").push(action.clone());
            let mut responses = self.responses.lock().expect("poisoned");
            let pos = responses.iter().position(|(a, _)| a == action);
            match pos {
                Some(i) => {
                    let (_, outcome) = responses.remove(i);
                    match outcome {
                        Ok(ApplyOutcome::Progressed) => Ok(ApplyOutcome::Progressed),
                        Ok(ApplyOutcome::Ready(ticket)) => Ok(ApplyOutcome::Ready(ticket)),
                        Err(kind) => Err(kind.into_engine_error()),
                    }
                }
                None => Err(EngineError::Io(format!(
                    "ScriptedDriver: no response scripted for {action:?}"
                ))),
            }
        }

        fn stop(&self) -> Result<(), EngineError> {
            self.stop_calls.fetch_add(1, Ordering::SeqCst);
            if self.stop_fails {
                Err(EngineError::Io(
                    "ScriptedDriver: stop() scripted to fail".to_string(),
                ))
            } else {
                Ok(())
            }
        }
    }

    /// Never sleeps for real; records every requested duration so a test can
    /// assert the backoff schedule without waiting for it.
    #[derive(Default)]
    pub struct FakeClock {
        sleeps: Mutex<Vec<Duration>>,
    }

    impl FakeClock {
        pub fn new() -> Self {
            Self::default()
        }

        pub fn requested_sleeps(&self) -> Vec<Duration> {
            self.sleeps.lock().expect("poisoned").clone()
        }
    }

    impl Clock for FakeClock {
        fn now(&self) -> Instant {
            Instant::now()
        }

        fn sleep(&self, duration: Duration) {
            self.sleeps.lock().expect("poisoned").push(duration);
        }
    }

    /// Collects every event notified, in order, for assertions.
    #[derive(Default)]
    pub struct RecordingNotifier {
        events: Mutex<Vec<DomainEvent>>,
    }

    impl RecordingNotifier {
        pub fn new() -> Self {
            Self::default()
        }

        pub fn events(&self) -> Vec<DomainEvent> {
            self.events.lock().expect("poisoned").clone()
        }
    }

    impl Notifier for RecordingNotifier {
        fn notify(&self, event: &DomainEvent) {
            self.events.lock().expect("poisoned").push(event.clone());
        }
    }
}
