//! Bootstrap domain — pure Rust, zero I/O. No `Command`, no filesystem, no
//! network, no `tauri` import. Types and invariants come from
//! `specs/028-safent-app-nativa/data-model.md`; the FailureCode vocabulary
//! (minus one adapter-only extension, documented below) from
//! `specs/028-safent-app-nativa/contracts/app-engine.md` §3.
//!
//! `EngineLifecycle` is the aggregate root data-model.md calls "AppState
//! machine": it owns the `EnginePhase` state machine and its own invariants
//! (illegal transitions are rejected, not merely discouraged).

use std::fmt;

// ---------------------------------------------------------------------------
// Small value objects
// ---------------------------------------------------------------------------

/// A byte count. Wrapping `u64` keeps a raw integer from being mistaken for a
/// port, a percentage, or an attempt count at a call site.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Bytes(pub u64);

/// A loopback TCP port. The ubiquitous language forbids ever showing this to
/// the owner (contract app-engine.md §5, spec FR-002) — the type exists so
/// "never displayed" is enforced by what has access to it, not by convention.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Port(pub u16);

/// How many times the action driving the current `Repairing` episode has
/// been attempted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AttemptCount(pub u32);

impl AttemptCount {
    pub const fn zero() -> Self {
        Self(0)
    }
    pub fn increment(self) -> Self {
        Self(self.0 + 1)
    }
}

/// A dotted-numeric version string ("0.2.0"). Validated at construction —
/// never a carrier for arbitrary text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SemVer(String);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidSemVer;

impl SemVer {
    pub fn parse(raw: &str) -> Result<Self, InvalidSemVer> {
        let ok = !raw.is_empty()
            && raw.len() <= 20
            && raw
                .split('.')
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
        if ok {
            Ok(Self(raw.to_string()))
        } else {
            Err(InvalidSemVer)
        }
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for SemVer {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// A container image pinned by digest — never a mutable tag (contract
/// app-engine.md: "SAFENT_IMAGE ... Digest, jamás una etiqueta").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageRef {
    pub repository: String,
    pub digest: String, // "sha256:<hex>"
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidImageRef;

impl ImageRef {
    pub fn new(
        repository: impl Into<String>,
        digest: impl Into<String>,
    ) -> Result<Self, InvalidImageRef> {
        let repository = repository.into();
        let digest = digest.into();
        if repository.is_empty() || !digest.starts_with("sha256:") || digest.len() <= 7 {
            return Err(InvalidImageRef);
        }
        Ok(Self { repository, digest })
    }

    /// `repository@sha256:...` — the exact form `SAFENT_IMAGE`/`SAFENT_ADS_IMAGE`
    /// take (contract app-engine.md §1): a digest reference, never a tag.
    pub fn reference(&self) -> String {
        format!("{}@{}", self.repository, self.digest)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct MachineName(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostOs {
    MacOs,
    Linux,
    Unsupported,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Arch {
    Arm64,
    Amd64,
    Unsupported,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MachineProvider {
    AppleHv,
    Qemu,
    HyperV,
    Wsl,
    Other(String),
}

/// One machine (podman machine VM on macOS) observed on the host — ours,
/// adoptable, or neither. `cpus`/`memory_bytes` extend data-model.md's
/// `(name, provider, rootful, running, ours)` tuple: without them "de otro
/// tamaño" (spec FR-032) cannot be told apart from a machine that genuinely
/// serves. `provider`/`cpus`/`memory_bytes` come straight from `podman
/// machine list --format json`'s own `VMType`/`CPUs`/`Memory` (contract
/// app-engine.md §3, `machines[]` vocabulary) — no `os_version`: MAC2-01
/// (verificacion-mac-2.md) found neither `machine list` nor `machine
/// inspect` expose ANY per-machine "OS version" concept at all, on real
/// podman 6.1.1 output; comparing one was comparing a value the CLI could
/// never truthfully report either way, so `is_satisfied_by` NEVER matched
/// and the planner treated every correctly-created machine as permanent
/// drift (`RecreateEngine` on every single boot — MAC2-01/MAC2-06).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MachineFact {
    pub name: MachineName,
    pub provider: MachineProvider,
    pub rootful: bool,
    pub running: bool,
    pub ours: bool,
    pub cpus: u32,
    pub memory_bytes: Bytes,
}

/// What a machine must offer to be adopted instead of replaced. On macOS, the
/// "rootful" requirement is why a rootless preexisting machine never serves
/// (research.md "Linux sin VM..." / "reconciliador auto-sanador": rootful is
/// created *inside* the VM, never asked of the owner).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MachineSpec {
    pub provider: MachineProvider,
    pub cpus: u32,
    pub memory_bytes: Bytes,
}

impl MachineSpec {
    pub fn is_satisfied_by(&self, machine: &MachineFact) -> bool {
        machine.rootful
            && machine.provider == self.provider
            && machine.cpus >= self.cpus
            && machine.memory_bytes >= self.memory_bytes
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainerFact {
    pub exists: bool,
    pub running: bool,
    pub image_digest: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompanionHealth {
    Unknown,
    Reachable,
    Unreachable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DaemonHealth {
    Unknown,
    Healthy,
    Unhealthy,
}

/// Trustworthiness of the app's own persisted bookkeeping
/// (`~/.safent/app/state.json`). `reconcile` deliberately never branches on
/// this field — every fact it needs is re-observed live — but it exists so a
/// missing/corrupt cache is an explicit, testable `HostFacts` snapshot
/// instead of an implicit assumption. See `reconcile::local_state_is_irrelevant`.
// `Missing`/`Corrupt` are constructed only by reconcile.rs's own tests, on
// purpose — production `EngineProbe` always reports `Trusted` (the CLI has
// no notion of this cache; see engine_adapter::map_host_facts). Their whole
// job is proving reconcile() is provably indifferent to them.
#[allow(dead_code)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalStateFact {
    Trusted,
    Missing,
    Corrupt,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CompanionContainers {
    pub running: u32,
    pub total: u32,
}

/// The observed snapshot of the host, at one instant. `reconcile` reads only
/// this + `DesiredState` — never a cache, never a clock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostFacts {
    pub os: HostOs,
    pub arch: Arch,
    pub free_disk_bytes: Bytes,
    pub total_memory_bytes: Bytes,
    pub runtime_staged: bool,
    pub runtime_hash_ok: bool,
    pub machines: Vec<MachineFact>,
    pub engine_container: Option<ContainerFact>,
    pub local_engine_image_digest: Option<String>,
    pub local_companion_image_digest: Option<String>,
    pub published_port: Option<Port>,
    pub data_volume: bool,
    pub companion_scaffold: bool,
    pub companion_containers: CompanionContainers,
    pub companion_health: CompanionHealth,
    pub daemon_health: DaemonHealth,
    pub app_version: SemVer,
    pub user_ns_allowed: bool,
    pub helper_installed: bool,
    pub local_state: LocalStateFact,
    pub another_instance_running: bool,
}

/// What has to be true for the product to be ready, expressed in digests and
/// names, never in steps (data-model.md "Estado deseado").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesiredState {
    pub engine_image: ImageRef,
    /// `None` ⇒ the companion is not desired right now (not installed, not
    /// requested). `Some` only after an `InstallRequest` has been accepted.
    pub companion_image: Option<ImageRef>,
    /// `None` on Linux — there is no machine to provision.
    pub machine: Option<MachineSpec>,
    pub min_free_disk_bytes: Bytes,
    pub min_total_memory_bytes: Bytes,
}

// ---------------------------------------------------------------------------
// RepairAction — closed vocabulary (data-model.md)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RepairAction {
    StageRuntime,
    AdoptMachine(MachineName),
    CreateMachine,
    StartMachine(MachineName),
    InstallPrivilegedHelper,
    PullEngine(ImageRef),
    PullCompanion(ImageRef),
    ChoosePort,
    CreateContainer,
    StartContainer,
    EnsureCompanionScaffold,
    ComposeCompanionUp(ImageRef),
    /// Constructed by the daemon dbus verb consumer (T017, `RT` repo) — a
    /// different bounded context from this crate's reconcile/boot, which
    /// never emits it (engine_adapter's `cli_invocation_for` matches it
    /// exhaustively and fails closed, proven by a test).
    #[allow(dead_code)]
    ReloadCompanionPresence,
    RecreateEngine,
    FocusExistingWindow,
}

// ---------------------------------------------------------------------------
// Failure vocabulary
// ---------------------------------------------------------------------------

/// Closed, stable — mirrors contract app-engine.md §3 exactly, plus one
/// adapter-only extension.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureCode {
    UnsupportedOs,
    UnsupportedArch,
    InsufficientDisk,
    InsufficientMemory,
    RuntimeHashMismatch,
    MachineCreateFailed,
    MachineStartFailed,
    UsernsBlocked,
    HelperDenied,
    RegistryUnreachable,
    DigestMismatch,
    PullInterrupted,
    PortExhausted,
    ContainerStartFailed,
    DaemonUnhealthy,
    CompanionNetworkConflict,
    CompanionMigrationFailed,
    CompanionUnreachable,
    BackupFailed,
    RestoreFailed,
    ClockSkew,
    /// NOT part of contracts/app-engine.md's 20-code CLI vocabulary.
    /// Synthesized locally by the embedded-CLI adapter (`engine_adapter.rs`)
    /// when the bundled `safent` binary predates `--porcelain`/`facts --json`
    /// (pre-T004). Unreachable once every shipped CLI speaks porcelain.
    CliPorcelainUnsupported,
    /// NOT part of the CLI's vocabulary either — synthesized by `boot.rs`
    /// when the OWNER cancelled a repair action before its point of no
    /// return. Deliberately `retryable: false` at the `FailureCause` level
    /// (auto-repair must never re-fight a deliberate cancel); the owner's
    /// explicit "Reintentar" still works, because that is a new decision,
    /// not a retry of the one just cancelled.
    CancelledByOwner,
    /// NOT part of the CLI's vocabulary either — synthesized by `boot.rs`
    /// when the SAME `RepairAction` reports success (`ApplyOutcome::
    /// Progressed`) twice in a row against UNCHANGED `HostFacts`: a
    /// successful-but-ineffective repair is indistinguishable from a hang
    /// to the owner, and is a DIFFERENT failure mode than
    /// `EngineLifecycle::fail`'s existing guard, which only ever sees
    /// `Err`s. Verified live: `cmd_stage_runtime` as a no-op (no bundled
    /// manifest) looped ~400 times in 90s, never erroring, never
    /// progressing — specs/028-safent-app-nativa/verificacion-paquete-linux.md.
    RepairIneffective,
    /// NOT part of the CLI's vocabulary — synthesized by `engine_adapter.rs`
    /// when a `failed` event's own code is a poor match for what podman's
    /// OWN stderr actually says. Verified live (packaging review item 3):
    /// a bundled/system podman storage-lock collision surfaced to the
    /// owner as `registry_unreachable` / "No se pudo descargar la imagen"
    /// — a misleading diagnosis pointing at network connectivity for a
    /// purely local storage problem, with the real detail (the podman
    /// process's own stderr) captured by the adapter but never shown.
    LocalStorageConflict,
    /// NOT part of the CLI's vocabulary — synthesized by `boot.rs` when the
    /// engine/companion image digest a packaged run needs is not available
    /// from the shipped `runtime-bundle.json` (missing file, malformed
    /// JSON, or a `digest` field the release pipeline has not pinned yet).
    /// MAC-03 (verificacion-mac-1.md): before this variant existed, a
    /// missing digest source (`SAFENT_ENGINE_DIGEST`, an env var nothing in
    /// the real packaging pipeline ever sets) surfaced as the unrelated,
    /// misleading `cli_porcelain_unsupported` — this name says exactly
    /// what is missing instead.
    EngineDigestMissing,
    /// NOT part of the CLI's vocabulary — synthesized by `engine_adapter.rs`
    /// when a `failed` event's stderr shows the seccomp profile itself
    /// could not be opened/obtained. MAC3-03 (verificacion-mac-3.md):
    /// reproduced live on a real Mac as a raw podman error surfacing
    /// through `daemon_unhealthy` — "Error: opening seccomp profile
    /// failed: open <path>: no such file or directory" — because the CLI's
    /// `_run` (`safent`) had no dedicated failure branch for it at all: the
    /// whole script runs under `set -e`, so `podman run` failing this way
    /// aborted before any `_die_porcelain` call ever ran. Also raised for
    /// the CLI's own honest self-report (`_ensure_seccomp`'s last-resort
    /// branch, "Could not obtain the seccomp profile") when the bundled,
    /// image-baked, downloaded, and cached sources are all unavailable.
    /// Either origin points at the SAME missing resource, never at the
    /// container's own health — this name says exactly that.
    SeccompProfileMissing,
}

impl FailureCode {
    /// The exact spelling from contract app-engine.md §3 for the 20 CLI
    /// codes; a snake_case name of our own for the two adapter/boot-only
    /// extensions (documented at their variant) so the UI still gets a
    /// stable, closed string to switch on.
    pub fn wire_name(&self) -> &'static str {
        match self {
            FailureCode::UnsupportedOs => "unsupported_os",
            FailureCode::UnsupportedArch => "unsupported_arch",
            FailureCode::InsufficientDisk => "insufficient_disk",
            FailureCode::InsufficientMemory => "insufficient_memory",
            FailureCode::RuntimeHashMismatch => "runtime_hash_mismatch",
            FailureCode::MachineCreateFailed => "machine_create_failed",
            FailureCode::MachineStartFailed => "machine_start_failed",
            FailureCode::UsernsBlocked => "userns_blocked",
            FailureCode::HelperDenied => "helper_denied",
            FailureCode::RegistryUnreachable => "registry_unreachable",
            FailureCode::DigestMismatch => "digest_mismatch",
            FailureCode::PullInterrupted => "pull_interrupted",
            FailureCode::PortExhausted => "port_exhausted",
            FailureCode::ContainerStartFailed => "container_start_failed",
            FailureCode::DaemonUnhealthy => "daemon_unhealthy",
            FailureCode::CompanionNetworkConflict => "companion_network_conflict",
            FailureCode::CompanionMigrationFailed => "companion_migration_failed",
            FailureCode::CompanionUnreachable => "companion_unreachable",
            FailureCode::BackupFailed => "backup_failed",
            FailureCode::RestoreFailed => "restore_failed",
            FailureCode::ClockSkew => "clock_skew",
            FailureCode::CliPorcelainUnsupported => "cli_porcelain_unsupported",
            FailureCode::CancelledByOwner => "cancelled_by_owner",
            FailureCode::RepairIneffective => "repair_ineffective",
            FailureCode::LocalStorageConflict => "local_storage_conflict",
            FailureCode::EngineDigestMissing => "engine_digest_missing",
            FailureCode::SeccompProfileMissing => "seccomp_profile_missing",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FailureCause {
    pub code: FailureCode,
    pub message: String,
    pub retryable: bool,
}

// ---------------------------------------------------------------------------
// Stage / progress
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    Preflight,
    RuntimeStaging,
    Machine,
    PullEngine,
    PullCompanion,
    Container,
    Health,
    CompanionScaffold,
    CompanionUp,
    CompanionReload,
    Backup,
    Restore,
    Cleanup,
}

impl Stage {
    /// The exact `StageId` spelling from contract app-engine.md §3 — the
    /// single source both the Tauri event payload (boot.rs) and any future
    /// diagnostic serialization use, so that string table exists ONCE.
    pub fn wire_name(&self) -> &'static str {
        match self {
            Stage::Preflight => "preflight",
            Stage::RuntimeStaging => "runtime_staging",
            Stage::Machine => "machine",
            Stage::PullEngine => "pull_engine",
            Stage::PullCompanion => "pull_companion",
            Stage::Container => "container",
            Stage::Health => "health",
            Stage::CompanionScaffold => "companion_scaffold",
            Stage::CompanionUp => "companion_up",
            Stage::CompanionReload => "companion_reload",
            Stage::Backup => "backup",
            Stage::Restore => "restore",
            Stage::Cleanup => "cleanup",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProgressUnit {
    Bytes,
    Layers,
    Steps,
}

impl ProgressUnit {
    pub fn wire_name(&self) -> &'static str {
        match self {
            ProgressUnit::Bytes => "bytes",
            ProgressUnit::Layers => "layers",
            ProgressUnit::Steps => "steps",
        }
    }
}

// ---------------------------------------------------------------------------
// EngineLifecycle — the aggregate root ("AppState machine")
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnginePhase {
    Fresh,
    Preflight,
    RuntimeStaging,
    EngineProvisioning,
    EnginePulling,
    EngineStarting,
    EngineReady,
    CompanionProvisioning,
    /// Reached once boot.rs actually drives companion convergence (no real
    /// invocation path asks for one today outside an explicit
    /// `SAFENT_COMPANION_DIGEST`/shipped `companion_image` override — the
    /// owner-facing flow that always wants one lands with 029's
    /// onboarding). Exercised today only by this module's own
    /// transition-table tests.
    #[allow(dead_code)]
    CompanionReady,
    /// Owned entirely by the update orchestrator (`src-tauri/src/update/`,
    /// not this crate's — see contract update.md). This lifecycle only
    /// PERMITS the transition; it never triggers it.
    #[allow(dead_code)]
    Updating,
    Reconnecting,
    Repairing,
    Degraded,
}

fn is_stage_phase(phase: EnginePhase) -> bool {
    stage_rank(phase).is_some()
}

/// Where a phase sits in the fixed bootstrap chain (Preflight through
/// EngineStarting), or `None` outside it. Used to allow jumping past
/// already-satisfied intermediate stages — e.g. reconcile's first unmet gap
/// on a resumed boot can be `EngineStarting` directly while the lifecycle is
/// still sitting at `Preflight`, because runtime/machine/images were already
/// fine and never produced their own transition. What `allowed()` must still
/// refuse is going BACKWARD or sideways to an unrelated phase.
fn stage_rank(phase: EnginePhase) -> Option<u8> {
    use EnginePhase::*;
    match phase {
        Preflight => Some(0),
        RuntimeStaging => Some(1),
        EngineProvisioning => Some(2),
        EnginePulling => Some(3),
        EngineStarting => Some(4),
        _ => None,
    }
}

fn allowed(from: EnginePhase, to: EnginePhase) -> bool {
    use EnginePhase::*;
    if let (Some(f), Some(t)) = (stage_rank(from), stage_rank(to)) {
        return t > f;
    }
    match (from, to) {
        (Fresh, Preflight) => true,
        (EngineStarting, EngineReady) => true,
        // `up` completed (contract app-engine.md §5) but the secret-fd
        // closed without a line — FR-012's safety net, reachable on the
        // VERY FIRST bootstrap, not only after the product was already showing.
        (EngineStarting, Reconnecting) => true,
        (EngineReady, CompanionProvisioning) => true,
        (EngineReady, Updating) => true,
        (EngineReady, Reconnecting) => true,
        (CompanionProvisioning, CompanionReady) => true,
        (CompanionProvisioning, Degraded) => true,
        (CompanionReady, CompanionProvisioning) => true,
        (CompanionReady, Updating) => true,
        (Updating, EngineReady) => true,
        (Updating, Degraded) => true,
        (Reconnecting, EngineReady) => true,
        (from, Repairing) if is_stage_phase(from) => true,
        (Repairing, to) if is_stage_phase(to) => true,
        (Repairing, Degraded) => true,
        (Degraded, Repairing) => true,
        _ => false,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IllegalTransition {
    pub from: EnginePhase,
    pub to: EnginePhase,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailOutcome {
    /// First (or non-repeating) failure — the lifecycle is now `Repairing`
    /// and the same action will be retried.
    Repairing,
    /// The same action failed with the same code twice in a row while
    /// `Repairing` — no progress. The lifecycle is now `Degraded`.
    Degraded,
}

/// Aggregate root of the Bootstrap context (data-model.md `EngineLifecycle`).
/// Owns its own transition invariants: illegal transitions are rejected, and
/// `degraded` is reachable ONLY through `fail()` detecting no progress —
/// never by a direct `enter(Degraded)` from a stage phase.
///
/// Deliberately does NOT track the current stage or its progress: those are
/// ephemeral, high-frequency event data that flow straight from the adapter
/// to the `Notifier` (contract app-engine.md §3, "progress llega al menos
/// cada 5 s") — polling them back off a mutable aggregate would be a second,
/// redundant source of truth. The aggregate's own state is exactly what its
/// invariants need: the phase, and the bookkeeping `fail()` uses to detect
/// "no progress".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineLifecycle {
    phase: EnginePhase,
    /// Set only while `phase == Repairing`: which phase a successful retry
    /// resumes (kept for diagnostics; transition legality does not need it).
    repair_target: Option<EnginePhase>,
    attempt: AttemptCount,
    last_action: Option<RepairAction>,
    last_failure: Option<FailureCause>,
}

impl EngineLifecycle {
    pub fn fresh() -> Self {
        Self {
            phase: EnginePhase::Fresh,
            repair_target: None,
            attempt: AttemptCount::zero(),
            last_action: None,
            last_failure: None,
        }
    }

    pub fn phase(&self) -> EnginePhase {
        self.phase
    }

    pub fn attempt(&self) -> AttemptCount {
        self.attempt
    }

    /// Public diagnostic getter — no consumer in this crate yet (the
    /// `EngineDegraded` DomainEvent already carries the cause to the UI via
    /// `safent://engine-event`); kept for a future status-query command
    /// (contract app-engine.md §7's `safentAppStatus()`) or exported
    /// diagnostics (FR-029).
    #[allow(dead_code)]
    pub fn last_failure(&self) -> Option<&FailureCause> {
        self.last_failure.as_ref()
    }

    /// Move to `to`. Leaving `Repairing` for anything but `Degraded` closes
    /// the repair episode: attempt/failure bookkeeping resets.
    pub fn enter(&mut self, to: EnginePhase) -> Result<(), IllegalTransition> {
        if !allowed(self.phase, to) {
            return Err(IllegalTransition {
                from: self.phase,
                to,
            });
        }
        if self.phase == EnginePhase::Repairing && to != EnginePhase::Degraded {
            self.repair_target = None;
            self.attempt = AttemptCount::zero();
            self.last_action = None;
            self.last_failure = None;
        }
        self.phase = to;
        Ok(())
    }

    /// Record that `action` failed with `cause`. Detects "no progress" —
    /// the SAME action failing with the SAME code while already
    /// `Repairing` — and transitions to `Degraded` only then (data-model.md
    /// `EngineLifecycle` invariant 5). `action` is `None` for a preflight
    /// violation, which has no corresponding `RepairAction`.
    pub fn fail(
        &mut self,
        action: Option<&RepairAction>,
        cause: FailureCause,
    ) -> Result<FailOutcome, IllegalTransition> {
        let same_as_last = self.last_action.as_ref() == action
            && self.last_failure.as_ref().map(|f| f.code) == Some(cause.code);

        if self.phase == EnginePhase::Repairing && same_as_last {
            self.enter(EnginePhase::Degraded)?;
            self.last_failure = Some(cause);
            return Ok(FailOutcome::Degraded);
        }

        if self.phase != EnginePhase::Repairing {
            self.repair_target = Some(self.phase);
            self.enter(EnginePhase::Repairing)?;
        }
        self.attempt = self.attempt.increment();
        self.last_action = action.cloned();
        self.last_failure = Some(cause);
        Ok(FailOutcome::Repairing)
    }

    /// The owner's one "Reintentar", for a design that keeps ONE
    /// `EngineLifecycle` alive across a pause-at-Degraded. `boot.rs`'s
    /// current loop does not do that — `retry_bootstrap` just re-runs the
    /// whole loop from `EngineLifecycle::fresh()`, which reconcile's live
    /// re-observation makes just as correct (nothing already done is
    /// repeated) — so this is exercised by this module's own tests only
    /// today. Kept as the documented, tested contract for whichever caller
    /// ends up wanting resume-in-place instead.
    #[allow(dead_code)]
    pub fn retry(&mut self) -> Result<(), IllegalTransition> {
        self.enter(EnginePhase::Repairing)
    }
}

// ---------------------------------------------------------------------------
// Domain events (data-model.md "Domain events")
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionSet {
    pub app: SemVer,
    pub engine: ImageRef,
    pub companion: Option<ImageRef>,
}

/// Regla transversal: ningún variante lleva el vale de arranque, el puerto ni
/// una URL local (data-model.md). `BootstrapTicket` is deliberately absent
/// from every payload here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DomainEvent {
    StageEntered {
        stage: Stage,
        label: String,
        total_bytes: Option<u64>,
    },
    StageProgressed {
        stage: Stage,
        done: u64,
        total: Option<u64>,
        unit: ProgressUnit,
    },
    StageCompleted {
        stage: Stage,
        duration_ms: u64,
    },
    EngineReady {
        version_set: VersionSet,
    },
    EngineDegraded {
        cause: FailureCause,
    },
    RepairApplied {
        action: RepairAction,
    },
    NoProgressDetected {
        action: Option<RepairAction>,
        code: FailureCode,
    },
    /// Emitted by window_policy.rs (T013, not this crate) when it performs
    /// the ONE navigation a valid ticket authorizes — this module only
    /// defines the shape.
    #[allow(dead_code)]
    WindowNavigated,
    /// FR-012's safety net: a load without a valid ticket resolves to ONE
    /// honest state, never a burst of failed requests. Carried on its own
    /// Tauri channel (`safent://reconnecting`, boot.rs), not
    /// `safent://engine-event` — the UI treats it as a distinct screen, not
    /// another stage.
    Reconnecting {
        reason: ReconnectReason,
    },
}

/// Why the ticket the window was holding is no longer valid.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconnectReason {
    /// `up` finished but the secret fd closed without a line (contract
    /// app-engine.md §5) — reachable on the very first bootstrap.
    TokenMissing,
    /// The engine had to restart while the window was already on
    /// `EngineReady`; the ticket it minted before is no longer the one the
    /// engine will accept.
    EngineRestarted,
}

impl ReconnectReason {
    pub fn wire_name(&self) -> &'static str {
        match self {
            ReconnectReason::TokenMissing => "token_missing",
            ReconnectReason::EngineRestarted => "engine_restarted",
        }
    }
}

/// A one-shot bootstrap credential (contract app-engine.md §5). Lives only in
/// memory for the duration of a single engine startup; NEVER persisted,
/// logged, or attached to a `DomainEvent`. `Debug` redacts on purpose.
pub struct BootstrapTicket(String);

impl BootstrapTicket {
    pub fn new(value: String) -> Self {
        Self(value)
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for BootstrapTicket {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "BootstrapTicket(REDACTED)")
    }
}

impl Drop for BootstrapTicket {
    fn drop(&mut self) {
        // Best-effort scrub, not a cryptographic guarantee: `String` does not
        // promise a zeroed heap buffer on free without a dedicated crate.
        self.0.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn image() -> ImageRef {
        ImageRef::new("ghcr.io/devwspito/safent", "sha256:aaaa").unwrap()
    }

    fn cause(code: FailureCode) -> FailureCause {
        FailureCause {
            code,
            message: "boom".into(),
            retryable: true,
        }
    }

    // ---- MachineSpec::is_satisfied_by compares only what podman itself
    // reports (MAC2-01, verificacion-mac-2.md) --------------------------

    #[test]
    fn a_correctly_created_machine_satisfies_its_own_spec() {
        let machine = MachineFact {
            name: MachineName("safent-engine".into()),
            provider: MachineProvider::AppleHv,
            rootful: true,
            running: true,
            ours: true,
            cpus: 4,
            memory_bytes: Bytes(8 * 1024 * 1024 * 1024), // cmd_ensure_machine's real --memory 8192
        };
        let spec = MachineSpec {
            provider: MachineProvider::AppleHv,
            cpus: 4,
            memory_bytes: Bytes(6 * 1024 * 1024 * 1024),
        };
        assert!(
            spec.is_satisfied_by(&machine),
            "a machine matching provider/cpus/memory (>=) must satisfy the spec \
             regardless of anything podman itself never reports per-machine"
        );
    }

    #[test]
    fn a_rootless_machine_never_satisfies_the_spec_even_if_everything_else_matches() {
        let machine = MachineFact {
            name: MachineName("safent-engine".into()),
            provider: MachineProvider::AppleHv,
            rootful: false,
            running: true,
            ours: true,
            cpus: 4,
            memory_bytes: Bytes(8 * 1024 * 1024 * 1024),
        };
        let spec = MachineSpec {
            provider: MachineProvider::AppleHv,
            cpus: 4,
            memory_bytes: Bytes(6 * 1024 * 1024 * 1024),
        };
        assert!(!spec.is_satisfied_by(&machine));
    }

    #[test]
    fn a_different_provider_or_insufficient_resources_does_not_satisfy_the_spec() {
        let spec = MachineSpec {
            provider: MachineProvider::AppleHv,
            cpus: 4,
            memory_bytes: Bytes(6 * 1024 * 1024 * 1024),
        };
        let base = MachineFact {
            name: MachineName("safent-engine".into()),
            provider: MachineProvider::AppleHv,
            rootful: true,
            running: true,
            ours: true,
            cpus: 4,
            memory_bytes: Bytes(8 * 1024 * 1024 * 1024),
        };
        let wrong_provider = MachineFact {
            provider: MachineProvider::Qemu,
            ..base.clone()
        };
        let too_few_cpus = MachineFact {
            cpus: 2,
            ..base.clone()
        };
        let too_little_memory = MachineFact {
            memory_bytes: Bytes(1024 * 1024 * 1024),
            ..base
        };
        assert!(!spec.is_satisfied_by(&wrong_provider));
        assert!(!spec.is_satisfied_by(&too_few_cpus));
        assert!(!spec.is_satisfied_by(&too_little_memory));
    }

    // ---- SemVer / ImageRef guard their own invariants -----------------

    #[test]
    fn semver_rejects_non_numeric_and_empty() {
        assert!(SemVer::parse("0.2.0").is_ok());
        assert!(SemVer::parse("").is_err());
        assert!(SemVer::parse("0.2.0-beta").is_err());
        assert!(SemVer::parse(&"1.".repeat(20)).is_err());
    }

    #[test]
    fn image_ref_requires_digest_form() {
        assert!(ImageRef::new("repo", "sha256:abcd").is_ok());
        assert!(ImageRef::new("repo", "latest").is_err());
        assert!(ImageRef::new("", "sha256:abcd").is_err());
    }

    #[test]
    fn image_ref_reference_is_repository_at_digest() {
        let image = ImageRef::new("ghcr.io/devwspito/safent", "sha256:abcd").unwrap();
        assert_eq!(image.reference(), "ghcr.io/devwspito/safent@sha256:abcd");
    }

    // ---- EngineLifecycle: legal transitions ----------------------------

    #[test]
    fn happy_path_chain_is_legal() {
        let mut lc = EngineLifecycle::fresh();
        assert_eq!(lc.phase(), EnginePhase::Fresh);
        for to in [
            EnginePhase::Preflight,
            EnginePhase::RuntimeStaging,
            EnginePhase::EngineProvisioning,
            EnginePhase::EnginePulling,
            EnginePhase::EngineStarting,
            EnginePhase::EngineReady,
        ] {
            lc.enter(to).unwrap();
            assert_eq!(lc.phase(), to);
        }
    }

    #[test]
    fn a_resumed_boot_can_skip_straight_to_a_later_stage_already_satisfied() {
        // Runtime/machine/images were already fine on a resumed boot — the
        // FIRST unmet gap reconcile finds is EngineStarting directly, with
        // no intermediate transition ever entered for the stages that
        // needed no repair action at all.
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::EngineStarting).unwrap();
        assert_eq!(lc.phase(), EnginePhase::EngineStarting);
    }

    #[test]
    fn stage_phases_can_never_go_backward() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        lc.enter(EnginePhase::EngineProvisioning).unwrap();
        assert!(lc.enter(EnginePhase::RuntimeStaging).is_err());
        assert!(lc.enter(EnginePhase::Preflight).is_err());
    }

    #[test]
    fn companion_and_update_and_reconnect_branches_are_legal() {
        let mut lc = EngineLifecycle::fresh();
        for to in [
            EnginePhase::Preflight,
            EnginePhase::RuntimeStaging,
            EnginePhase::EngineProvisioning,
            EnginePhase::EnginePulling,
            EnginePhase::EngineStarting,
            EnginePhase::EngineReady,
        ] {
            lc.enter(to).unwrap();
        }

        let mut companion = lc.clone();
        companion.enter(EnginePhase::CompanionProvisioning).unwrap();
        companion.enter(EnginePhase::CompanionReady).unwrap();

        let mut updating = lc.clone();
        updating.enter(EnginePhase::Updating).unwrap();
        updating.enter(EnginePhase::EngineReady).unwrap(); // rollback or success, both land here

        let mut reconnect = lc.clone();
        reconnect.enter(EnginePhase::Reconnecting).unwrap();
        reconnect.enter(EnginePhase::EngineReady).unwrap();
    }

    // ---- EngineLifecycle: illegal transitions --------------------------

    #[test]
    fn cannot_skip_ahead_to_engine_ready() {
        let mut lc = EngineLifecycle::fresh();
        let err = lc.enter(EnginePhase::EngineReady).unwrap_err();
        assert_eq!(
            err,
            IllegalTransition {
                from: EnginePhase::Fresh,
                to: EnginePhase::EngineReady
            }
        );
    }

    #[test]
    fn stage_phase_cannot_jump_straight_to_degraded() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        lc.enter(EnginePhase::EngineProvisioning).unwrap();
        assert!(
            lc.enter(EnginePhase::Degraded).is_err(),
            "must pass through Repairing first"
        );
    }

    #[test]
    fn companion_ready_cannot_go_back_to_engine_ready() {
        let mut lc = EngineLifecycle::fresh();
        for to in [
            EnginePhase::Preflight,
            EnginePhase::RuntimeStaging,
            EnginePhase::EngineProvisioning,
            EnginePhase::EnginePulling,
            EnginePhase::EngineStarting,
            EnginePhase::EngineReady,
            EnginePhase::CompanionProvisioning,
            EnginePhase::CompanionReady,
        ] {
            lc.enter(to).unwrap();
        }
        assert!(lc.enter(EnginePhase::EngineReady).is_err());
    }

    #[test]
    fn degraded_requires_retry_before_resuming() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.fail(None, cause(FailureCode::UnsupportedOs)).unwrap();
        lc.fail(None, cause(FailureCode::UnsupportedOs)).unwrap(); // -> Degraded
        assert_eq!(lc.phase(), EnginePhase::Degraded);
        assert!(lc.enter(EnginePhase::Preflight).is_err());
        lc.retry().unwrap();
        assert_eq!(lc.phase(), EnginePhase::Repairing);
    }

    // ---- fail(): the no-progress rule (data-model.md invariant 5) ------

    #[test]
    fn first_failure_enters_repairing_not_degraded() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        let outcome = lc
            .fail(
                Some(&RepairAction::StageRuntime),
                cause(FailureCode::RegistryUnreachable),
            )
            .unwrap();
        assert_eq!(outcome, FailOutcome::Repairing);
        assert_eq!(lc.phase(), EnginePhase::Repairing);
        assert_eq!(lc.attempt(), AttemptCount(1));
    }

    #[test]
    fn same_action_same_code_twice_is_degraded() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        let action = RepairAction::StageRuntime;
        lc.fail(Some(&action), cause(FailureCode::RegistryUnreachable))
            .unwrap();
        let outcome = lc
            .fail(Some(&action), cause(FailureCode::RegistryUnreachable))
            .unwrap();
        assert_eq!(outcome, FailOutcome::Degraded);
        assert_eq!(lc.phase(), EnginePhase::Degraded);
        assert_eq!(
            lc.last_failure().unwrap().code,
            FailureCode::RegistryUnreachable
        );
    }

    #[test]
    fn different_action_after_a_failure_does_not_trip_no_progress() {
        // Facts moved forward between the two failures (a different action
        // was chosen), so this must NOT look like "stuck".
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        lc.fail(
            Some(&RepairAction::StageRuntime),
            cause(FailureCode::RegistryUnreachable),
        )
        .unwrap();
        let outcome = lc
            .fail(
                Some(&RepairAction::PullEngine(image())),
                cause(FailureCode::RegistryUnreachable),
            )
            .unwrap();
        assert_eq!(outcome, FailOutcome::Repairing);
        assert_eq!(lc.phase(), EnginePhase::Repairing);
    }

    #[test]
    fn different_code_after_a_failure_does_not_trip_no_progress() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        let action = RepairAction::StageRuntime;
        lc.fail(Some(&action), cause(FailureCode::RegistryUnreachable))
            .unwrap();
        let outcome = lc
            .fail(Some(&action), cause(FailureCode::InsufficientDisk))
            .unwrap();
        assert_eq!(outcome, FailOutcome::Repairing);
    }

    #[test]
    fn preflight_violations_use_no_action_and_still_trip_no_progress() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.fail(None, cause(FailureCode::InsufficientDisk)).unwrap();
        let outcome = lc.fail(None, cause(FailureCode::InsufficientDisk)).unwrap();
        assert_eq!(outcome, FailOutcome::Degraded);
    }

    #[test]
    fn successful_retry_resets_attempt_bookkeeping() {
        let mut lc = EngineLifecycle::fresh();
        lc.enter(EnginePhase::Preflight).unwrap();
        lc.enter(EnginePhase::RuntimeStaging).unwrap();
        lc.fail(
            Some(&RepairAction::StageRuntime),
            cause(FailureCode::RegistryUnreachable),
        )
        .unwrap();
        assert_eq!(lc.attempt(), AttemptCount(1));
        lc.enter(EnginePhase::EngineProvisioning).unwrap(); // the retried action succeeded
        assert_eq!(lc.attempt(), AttemptCount::zero());
        assert!(lc.last_failure().is_none());
    }

    // ---- BootstrapTicket never prints its value -------------------------

    #[test]
    fn bootstrap_ticket_debug_is_redacted() {
        let ticket = BootstrapTicket::new("http://127.0.0.1:9/?k=super-secret".to_string());
        let printed = format!("{ticket:?}");
        assert!(!printed.contains("super-secret"));
        assert_eq!(ticket.expose(), "http://127.0.0.1:9/?k=super-secret");
    }
}
