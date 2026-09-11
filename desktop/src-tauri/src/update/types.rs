//! Pure data shapes for the update contract — `contracts/update.md` +
//! `data-model.md`'s `UpdatePlan`/`VersionSet`. No I/O, no Tauri types: this
//! module only models what the contract already specifies, so it can be
//! deserialized straight from the two published manifests and compared
//! without touching a network or a filesystem.

use semver::Version;
use serde::Deserialize;
use std::collections::HashMap;

/// What is actually RUNNING right now — never merely installed
/// (data-model.md `VersionSet` invariant: "la version que la app muestra es
/// la que esta corriendo").
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionSet {
    pub app: Version,
    pub engine_digest: String,
    /// `None` when the companion is not installed — it then never enters a plan
    /// (contracts/update.md §3: "El companero solo entra en el plan si esta instalado").
    pub companion_digest: Option<String>,
}

/// One piece of a non-empty plan (contracts/update.md §3 `pieces`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpdatePieceKind {
    App,
    Engine,
    Companion,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdatePiece {
    pub kind: UpdatePieceKind,
    pub size_bytes: Option<u64>,
}

/// The non-empty result of `plan_update` (contracts/update.md §3). There is
/// deliberately no "empty plan" state here — `plan_update` returns `None`
/// instead, which is what "sin plan, no hay boton" (FR-015) means in types.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdatePlan {
    pub from: VersionSet,
    pub to_app: Option<Version>,
    pub to_engine_digest: Option<String>,
    pub to_companion_digest: Option<String>,
    pub pieces: Vec<UpdatePiece>,
    /// contracts/update.md §4 "Orden obligatorio": true when `from.app` is
    /// older than the runtime manifest's `min_app_version` — the wrapper must
    /// then update BEFORE the engine, not after.
    pub app_must_go_first: bool,
}

/// contracts/update.md §1 — one platform entry of the Tauri updater's `latest.json`.
#[derive(Debug, Clone, Deserialize)]
pub struct TauriManifestPlatform {
    pub signature: String,
    pub url: String,
}

/// contracts/update.md §1 — release metadata from `latest.json`.
/// This shape does not prove artifact authenticity. The caller must complete
/// Tauri's verified download before any apply (see `tauri_updater.rs`).
#[derive(Debug, Clone, Deserialize)]
pub struct TauriManifest {
    pub version: Version,
    #[serde(default)]
    pub platforms: HashMap<String, TauriManifestPlatform>,
}

/// contracts/update.md §2 — `runtime-manifest.json`. Keys under `engine` /
/// `companion.<slug>` are `"<os>/<arch>"` (e.g. `"linux/arm64"`) exactly as
/// published — the container image always runs Linux (even on a macOS host,
/// inside the podman machine), so there is no per-host-OS key here.
#[derive(Debug, Clone, Deserialize)]
pub struct RuntimeManifest {
    pub schema_version: u32,
    pub version: Version,
    pub engine: HashMap<String, String>,
    #[serde(default)]
    pub companion: HashMap<String, HashMap<String, String>>,
    pub min_app_version: Version,
}

/// The platform key `RuntimeManifest.engine`/`.companion` are indexed by, for
/// the platform THIS process is running the container engine on. The engine
/// container is always Linux regardless of host OS (see `RuntimeManifest` doc).
pub fn container_platform_key() -> String {
    let arch = match std::env::consts::ARCH {
        "aarch64" => "arm64",
        "x86_64" => "amd64",
        other => other,
    };
    format!("linux/{arch}")
}

/// contracts/update.md §6 — closed, stable error vocabulary. `detail` carries
/// the ONE piece of context a given cause needs; nothing here is free text
/// that could leak a secret (matches app-engine.md §3 invariant 1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpdateFailure {
    ManifestUnverified,
    InsufficientDisk { needed_bytes: u64, free_bytes: u64 },
    PullInterrupted,
    DigestMismatch,
    EngineUnhealthyAfterApply,
    CompanionMigrationFailed,
    RelaunchBlocked,
}
