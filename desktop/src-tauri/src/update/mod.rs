//! `native` wires the real app-only Tauri updater with native confirmation.
//! `tauri_updater` owns format conversion and runtime-manifest verification.
//! The joint engine/Ads/app `plan`/`orchestrator` remains unactivated: its
//! synchronous ports have no durable continuation after process relaunch.
#![allow(dead_code, unused_imports)]

pub mod availability;
pub mod native;
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
