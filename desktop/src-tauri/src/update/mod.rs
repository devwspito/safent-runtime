//! Update orchestrator — `contracts/update.md`. Owns the answer to "is there
//! a real update" (`plan`, pure) and how a plan is applied end to end
//! (`orchestrator`, coordination over injected ports). `tauri_updater` is the
//! only impure edge: the Tauri updater plugin call and the
//! `runtime-manifest.json` minisign check.
//!
//! T014 delivers this module ready to wire in; the Builder/bootstrap call
//! that actually constructs a `dyn UpdatePorts` from the embedded CLI +
//! plugin and invokes `run_update` is T011's bootstrap_service.rs (a
//! different lane's file). Until that wiring lands, nothing in the `bin`
//! target calls into `update::*` (including these re-exports) outside
//! `#[cfg(test)]`, which `-D warnings` would otherwise flag as dead/unused —
//! every item below IS exercised, by the unit tests in its own file.
#![allow(dead_code, unused_imports)]

pub mod availability;
pub mod orchestrator;
pub mod plan;
pub mod tauri_updater;
pub mod types;

pub use orchestrator::{run_update, BackupHandle, UpdateOutcome, UpdatePorts};
pub use plan::plan_update;
pub use types::{
    container_platform_key, RuntimeManifest, TauriManifest, TauriManifestPlatform, UpdateFailure,
    UpdatePiece, UpdatePieceKind, UpdatePlan, VersionSet,
};
