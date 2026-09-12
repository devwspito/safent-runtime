//! The auto-healing planner (data-model.md "Reconciliación",
//! research.md "reconciliador auto-sanador"). Two pure functions, no I/O:
//!
//! - [`preflight_violation`] — is there a terminal or transient gate the
//!   product cannot get past right now (unsupported host, not enough
//!   disk/memory)? Checked BEFORE either function ever plans a download
//!   (spec FR-008).
//! - [`reconcile`] — given that preflight passes, what is the single next
//!   `RepairAction` that makes progress toward `DesiredState`? Empty means
//!   converged.
//!
//! Both take only `&HostFacts` + `&DesiredState` — no clock, no cache, no
//! previous plan. That is deliberate: every fact reconcile needs is
//! re-observed live by `EngineProbe` on every loop iteration (boot.rs), so a
//! missing or corrupt `state.json` cannot desync the plan from reality (see
//! `local_state_never_changes_the_plan` below) — the SAME property that makes
//! resuming after an interrupted pull or a half-created machine "just work"
//! without a special code path for "was interrupted" vs. "never started".

use crate::domain::{
    CompanionContainers, DesiredState, FailureCause, FailureCode, HostFacts, MachineFact,
    RepairAction,
};

/// A gate the product cannot get past right now. `None` means the host is
/// good enough to start planning infrastructure.
pub fn preflight_violation(facts: &HostFacts, desired: &DesiredState) -> Option<FailureCause> {
    use crate::domain::{Arch, HostOs};

    if facts.os == HostOs::Unsupported {
        return Some(unretryable(
            FailureCode::UnsupportedOs,
            "este sistema operativo no está servido",
        ));
    }
    if facts.arch == Arch::Unsupported {
        return Some(unretryable(
            FailureCode::UnsupportedArch,
            "esta arquitectura no está servida",
        ));
    }
    if facts.os == HostOs::MacOs && facts.arch != Arch::Arm64 {
        // Spec 028 Out of Scope: Mac Intel. Declared here, before any download.
        return Some(unretryable(
            FailureCode::UnsupportedArch,
            "Mac Intel no está servido todavía",
        ));
    }
    if facts.free_disk_bytes < desired.min_free_disk_bytes {
        return Some(retryable(
            FailureCode::InsufficientDisk,
            "espacio en disco insuficiente",
        ));
    }
    if facts.total_memory_bytes < desired.min_total_memory_bytes {
        return Some(retryable(
            FailureCode::InsufficientMemory,
            "memoria insuficiente",
        ));
    }
    None
}

/// The single next repair action, or an empty plan when `facts` already
/// satisfies `desired`. Checked in priority order: a second instance beats
/// everything else; then the engine chain (runtime → machine/userns →
/// images → container); then the companion, which is independent of the
/// engine once its scaffold exists (029 CL-002 — installing it never recreates
/// Safent).
pub fn reconcile(facts: &HostFacts, desired: &DesiredState) -> Vec<RepairAction> {
    if facts.another_instance_running {
        return vec![RepairAction::FocusExistingWindow];
    }
    if !facts.runtime_staged || !facts.runtime_hash_ok {
        return vec![RepairAction::StageRuntime];
    }

    let next = machine_gap(facts, desired)
        .or_else(|| privileged_helper_gap(facts))
        .or_else(|| images_gap(facts, desired))
        .or_else(|| container_gap(facts))
        .or_else(|| companion_gap(facts, desired));

    match next {
        Some(action) => vec![action],
        None => vec![],
    }
}

fn retryable(code: FailureCode, message: &str) -> FailureCause {
    FailureCause {
        code,
        message: message.to_string(),
        retryable: true,
    }
}

fn unretryable(code: FailureCode, message: &str) -> FailureCause {
    FailureCause {
        code,
        message: message.to_string(),
        retryable: false,
    }
}

/// macOS only (`desired.machine` is `None` on Linux). A machine "serves" when
/// it is rootful and meets-or-exceeds the desired spec — rootless, wrong
/// size, and wrong version all fail this check identically, and all three
/// converge on the SAME action: leave the foreign machine untouched and
/// create ours (research.md: "Si la máquina ajena no sirve ... se crea la
/// nuestra ... y se deja la ajena intacta").
fn machine_gap(facts: &HostFacts, desired: &DesiredState) -> Option<RepairAction> {
    let spec = desired.machine.as_ref()?;

    if let Some(m) = facts.machines.iter().find(|m| spec.is_satisfied_by(m)) {
        return start_if_needed(m);
    }
    match facts.machines.iter().find(|m| m.ours) {
        Some(m) if m.running => Some(RepairAction::RecreateEngine), // drift: ours no longer matches the spec
        Some(m) => Some(RepairAction::StartMachine(m.name.clone())),
        None => Some(RepairAction::CreateMachine),
    }
}

fn start_if_needed(m: &MachineFact) -> Option<RepairAction> {
    if m.running {
        None // adopted silently — read-only use, nothing to do (governance table: "Nada: es transparente")
    } else if m.ours {
        Some(RepairAction::StartMachine(m.name.clone()))
    } else {
        Some(RepairAction::AdoptMachine(m.name.clone()))
    }
}

/// Linux only. research.md "Linux sin VM": the helper is installed exactly
/// once, only when the kernel actually blocks unprivileged user namespaces
/// for the bundled (rootless) podman.
fn privileged_helper_gap(facts: &HostFacts) -> Option<RepairAction> {
    use crate::domain::HostOs;
    if facts.os == HostOs::Linux && !facts.user_ns_allowed && !facts.helper_installed {
        Some(RepairAction::InstallPrivilegedHelper)
    } else {
        None
    }
}

/// The engine image only — the companion's image lives in `companion_gap`.
/// Two DISTINCT gaps on purpose: "not present locally yet" always means
/// pull (covers both a fresh machine and an interrupted pull — reconcile
/// cannot tell them apart, and does not need to: podman's own layer cache
/// makes a repeated pull resume, not restart). "Present locally but not what
/// the running container uses" means the container needs recreating, NOT
/// another pull — pulling again would loop forever, since pulling never
/// changes what is currently running.
fn images_gap(facts: &HostFacts, desired: &DesiredState) -> Option<RepairAction> {
    let want = &desired.engine_image;
    let have_locally = facts.local_engine_image_digest.as_deref() == Some(want.digest.as_str());
    if !have_locally {
        return Some(RepairAction::PullEngine(want.clone()));
    }
    // No container yet is NOT a stale digest — it is container_gap's job (create/choose a
    // port). Only a container that ALREADY EXISTS with the wrong digest needs recreating.
    if let Some(container) = &facts.engine_container {
        if container.image_digest.as_deref() != Some(want.digest.as_str()) {
            return Some(RepairAction::RecreateEngine);
        }
    }
    None
}

/// A `published_port` on record with no container behind it is a stale
/// memory (FR-009: something else may hold it by now) — ask for a fresh pick
/// instead of trusting it. No container and no port on record yet is the
/// ordinary first-boot path.
fn container_gap(facts: &HostFacts) -> Option<RepairAction> {
    match &facts.engine_container {
        None if facts.published_port.is_some() => Some(RepairAction::ChoosePort),
        None => Some(RepairAction::CreateContainer),
        Some(c) if !c.exists => Some(RepairAction::CreateContainer),
        Some(c) if !c.running => Some(RepairAction::StartContainer),
        Some(_) => None,
    }
}

/// Native Community always requires its bundled companion. Engine-only
/// desired states remain available for explicit headless/dev callers.
/// Scaffold precedes image/compose; a running container count alone never
/// establishes availability of the service.
fn companion_gap(facts: &HostFacts, desired: &DesiredState) -> Option<RepairAction> {
    let want = desired.companion_image.as_ref()?;

    if !facts.companion_scaffold {
        return Some(RepairAction::EnsureCompanionScaffold);
    }
    let have_locally = facts.local_companion_image_digest.as_deref() == Some(want.digest.as_str());
    if !have_locally {
        return Some(RepairAction::PullCompanion(want.clone()));
    }
    if under_provisioned(&facts.companion_containers)
        || facts.companion_health != crate::domain::CompanionHealth::Reachable
    {
        return Some(RepairAction::ComposeCompanionUp(want.clone()));
    }
    None
}

fn under_provisioned(c: &CompanionContainers) -> bool {
    c.running < c.total.max(1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{
        Arch, Bytes, CompanionHealth, ContainerFact, DaemonHealth, HostOs, ImageRef,
        LocalStateFact, MachineName, MachineProvider, Port, SemVer,
    };

    const GIB: u64 = 1024 * 1024 * 1024;

    fn engine_image() -> ImageRef {
        ImageRef::new("ghcr.io/devwspito/safent", "sha256:engine-good").unwrap()
    }

    fn companion_image() -> ImageRef {
        ImageRef::new("ghcr.io/devwspito/safent-ads", "sha256:companion-good").unwrap()
    }

    fn our_machine() -> MachineFact {
        MachineFact {
            name: MachineName("safent".into()),
            provider: MachineProvider::AppleHv,
            rootful: true,
            running: true,
            ours: true,
            cpus: 4,
            memory_bytes: Bytes(8 * GIB),
        }
    }

    fn desired_macos() -> DesiredState {
        DesiredState {
            engine_image: engine_image(),
            companion_image: None,
            machine: Some(crate::domain::MachineSpec {
                provider: MachineProvider::AppleHv,
                cpus: 4,
                memory_bytes: Bytes(8 * GIB),
            }),
            min_free_disk_bytes: Bytes(4 * GIB),
            min_total_memory_bytes: Bytes(8 * GIB),
        }
    }

    fn desired_linux() -> DesiredState {
        DesiredState {
            machine: None,
            ..desired_macos()
        }
    }

    #[test]
    fn factory_ads_running_count_without_health_never_converges() {
        let mut wanted = desired_macos();
        wanted.companion_image = Some(companion_image());
        let mut facts = converged_macos_facts();
        facts.companion_scaffold = true;
        facts.local_companion_image_digest = Some(companion_image().digest);
        facts.companion_containers = CompanionContainers {
            running: 4,
            total: 4,
        };
        for health in [CompanionHealth::Unknown, CompanionHealth::Unreachable] {
            facts.companion_health = health;
            assert_eq!(
                reconcile(&facts, &wanted),
                vec![RepairAction::ComposeCompanionUp(companion_image())]
            );
        }
        facts.companion_health = CompanionHealth::Reachable;
        assert!(reconcile(&facts, &wanted).is_empty());
    }

    /// Every field at the value it would hold once the product is fully
    /// ready. Each scenario test clones this and changes exactly ONE thing —
    /// table-driven via a shared "everything else is fine" baseline instead
    /// of restating the whole struct 16 times.
    fn converged_macos_facts() -> HostFacts {
        HostFacts {
            os: HostOs::MacOs,
            arch: Arch::Arm64,
            free_disk_bytes: Bytes(20 * GIB),
            total_memory_bytes: Bytes(16 * GIB),
            runtime_staged: true,
            runtime_hash_ok: true,
            machines: vec![our_machine()],
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

    fn converged_linux_facts() -> HostFacts {
        HostFacts {
            os: HostOs::Linux,
            arch: Arch::Amd64,
            machines: vec![],
            ..converged_macos_facts()
        }
    }

    #[test]
    fn converged_fixture_is_actually_converged() {
        assert_eq!(
            reconcile(&converged_macos_facts(), &desired_macos()),
            Vec::<RepairAction>::new()
        );
        assert_eq!(
            reconcile(&converged_linux_facts(), &desired_linux()),
            Vec::<RepairAction>::new()
        );
    }

    #[test]
    fn mac2_06_reopening_the_app_with_a_healthy_engine_never_destroys_it() {
        // MAC2-06/MAC-12 (verificacion-mac-2.md): opening the app while the
        // engine was already up (a healthy, digest-matching container on a
        // machine that satisfies its own spec) destroyed and recreated the
        // whole engine — 13 s, a NEW port, in-flight work lost. Root cause
        // was MAC2-01 (machine_gap's is_satisfied_by never matching a real
        // machine because of an unsatisfiable os_version comparison),
        // which made machine_gap return RecreateEngine on EVERY observation
        // before images_gap/container_gap — the ones that already treat a
        // healthy container as converged — were ever reached. This is the
        // exact "reopen with the motor already alive" facts shape.
        //
        // MAC3-02 (verificacion-mac-3.md): fixing MAC2-01 moved the SAME bug
        // one layer down, from machine_gap to images_gap — `cmd_facts` read
        // `inspect -f '{{.Image}}'` (podman's local IMAGE ID, never a
        // digest) into this exact field, so `converged_macos_facts()`'s
        // `image_digest: Some("sha256:engine-good")` below was an untested
        // ASSUMPTION about the wire shape, not a verified one — this test
        // stayed green while a real Mac kept destroying a healthy engine.
        // `safent`'s `cmd_facts` now reads `{{.ImageDigest}}` (confirmed
        // against real podman 6.1.1), so this fixture's shape is no longer
        // just assumed: `engine_adapter_real_cli_contract.rs`'s
        // `real_facts_reports_the_container_image_digest_never_the_local_image_id`
        // proves the REAL CLI now actually emits a digest-shaped value here,
        // not the image ID `{{.Image}}` would have given.
        let facts = converged_macos_facts();
        assert!(
            facts.machines[0].running,
            "fixture sanity: our machine is up"
        );
        assert!(
            facts
                .engine_container
                .as_ref()
                .is_some_and(|c| c.running && c.exists),
            "fixture sanity: the engine container is up and healthy"
        );
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            Vec::<RepairAction>::new(),
            "a healthy, digest-matching engine on a satisfying machine must be \
             left alone — no RecreateEngine, no action at all"
        );
    }

    // ---- 1. fresh machine ------------------------------------------------

    #[test]
    fn fresh_machine_stages_runtime_first() {
        let facts = HostFacts {
            runtime_staged: false,
            runtime_hash_ok: false,
            machines: vec![],
            engine_container: None,
            local_engine_image_digest: None,
            published_port: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::StageRuntime]
        );
    }

    // ---- 2-4. pre-existing podman machine: rootless / wrong size / other version

    #[test]
    fn preexisting_machine_rootless_is_left_alone_and_ours_is_created() {
        let facts = HostFacts {
            machines: vec![MachineFact {
                name: MachineName("libkrun-machine".into()),
                rootful: false,
                ours: false,
                ..our_machine()
            }],
            engine_container: None,
            local_engine_image_digest: None,
            published_port: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::CreateMachine]
        );
    }

    #[test]
    fn preexisting_machine_wrong_size_is_left_alone_and_ours_is_created() {
        let facts = HostFacts {
            machines: vec![MachineFact {
                name: MachineName("tiny-machine".into()),
                ours: false,
                cpus: 1,
                memory_bytes: Bytes(2 * GIB),
                ..our_machine()
            }],
            engine_container: None,
            local_engine_image_digest: None,
            published_port: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::CreateMachine]
        );
    }

    #[test]
    fn preexisting_machine_wrong_provider_is_left_alone_and_ours_is_created() {
        // MAC2-01 (verificacion-mac-2.md): "another kind of machine" is now
        // expressed as a real, observable mismatch (provider) — podman
        // itself has no per-machine "os version" to compare a foreign
        // machine against. A foreign qemu machine (e.g. the owner's own,
        // unrelated to the applehv one this app creates) must still be
        // left alone, never adopted, regardless of what it is.
        let facts = HostFacts {
            machines: vec![MachineFact {
                name: MachineName("old-machine".into()),
                ours: false,
                provider: MachineProvider::Qemu,
                ..our_machine()
            }],
            engine_container: None,
            local_engine_image_digest: None,
            published_port: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::CreateMachine]
        );
    }

    #[test]
    fn foreign_machine_that_serves_is_adopted_silently() {
        let facts = HostFacts {
            machines: vec![MachineFact {
                name: MachineName("libkrun-machine".into()),
                ours: false,
                ..our_machine()
            }],
            ..converged_macos_facts()
        };
        // No AdoptMachine action: read-only adoption is a no-op (governance table: "Nada: es transparente").
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            Vec::<RepairAction>::new()
        );
    }

    #[test]
    fn our_machine_stopped_is_started_not_recreated() {
        let facts = HostFacts {
            machines: vec![MachineFact {
                running: false,
                ..our_machine()
            }],
            engine_container: None,
            local_engine_image_digest: None,
            published_port: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::StartMachine(MachineName("safent".into()))]
        );
    }

    // ---- 5. port in use ----------------------------------------------------

    #[test]
    fn stale_port_on_record_with_no_container_asks_to_choose_again() {
        let facts = HostFacts {
            engine_container: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::ChoosePort]
        );
    }

    // ---- 6. half-provisioned container -------------------------------------

    #[test]
    fn half_provisioned_container_is_started_not_recreated() {
        let facts = HostFacts {
            engine_container: Some(ContainerFact {
                exists: true,
                running: false,
                image_digest: Some("sha256:engine-good".into()),
            }),
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::StartContainer]
        );
    }

    // ---- 7. interrupted image pull ------------------------------------------

    #[test]
    fn interrupted_pull_is_retried_by_asking_for_the_same_digest() {
        let facts = HostFacts {
            engine_container: None,
            local_engine_image_digest: None,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::PullEngine(engine_image())]
        );
        // Idempotent: calling again with the identical (still-interrupted) facts
        // yields the identical action — this + podman's own layer cache IS the
        // resumption, with no "was interrupted" flag anywhere in this planner.
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::PullEngine(engine_image())]
        );
    }

    // ---- 8. stale image digest ----------------------------------------------

    #[test]
    fn stale_running_digest_recreates_instead_of_pulling_again() {
        let facts = HostFacts {
            engine_container: Some(ContainerFact {
                exists: true,
                running: true,
                image_digest: Some("sha256:engine-OLD".into()),
            }),
            // The correct digest is ALREADY local — pulling again would never converge.
            local_engine_image_digest: Some("sha256:engine-good".into()),
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::RecreateEngine]
        );
    }

    // ---- 9-11. companion: absent / half / ready ------------------------------

    fn desired_with_companion() -> DesiredState {
        DesiredState {
            companion_image: Some(companion_image()),
            ..desired_macos()
        }
    }

    #[test]
    fn companion_absent_ensures_the_scaffold_first() {
        let facts = HostFacts {
            companion_scaffold: false,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_with_companion()),
            vec![RepairAction::EnsureCompanionScaffold]
        );
    }

    #[test]
    fn companion_half_provisioned_composes_up() {
        let facts = HostFacts {
            companion_scaffold: true,
            local_companion_image_digest: Some("sha256:companion-good".into()),
            companion_containers: CompanionContainers {
                running: 1,
                total: 3,
            },
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_with_companion()),
            vec![RepairAction::ComposeCompanionUp(companion_image())]
        );
    }

    #[test]
    fn companion_ready_needs_no_action() {
        let facts = HostFacts {
            companion_health: CompanionHealth::Reachable,
            companion_scaffold: true,
            local_companion_image_digest: Some("sha256:companion-good".into()),
            companion_containers: CompanionContainers {
                running: 1,
                total: 1,
            },
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_with_companion()),
            Vec::<RepairAction>::new()
        );
    }

    #[test]
    fn companion_not_desired_is_never_touched() {
        // desired_macos() has companion_image: None — even a half-provisioned
        // scaffold must not produce a companion action nobody asked for.
        let facts = HostFacts {
            companion_scaffold: false,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            Vec::<RepairAction>::new()
        );
    }

    // ---- 12. second instance ------------------------------------------------

    #[test]
    fn second_instance_focuses_the_existing_window_over_everything_else() {
        let facts = HostFacts {
            another_instance_running: true,
            runtime_staged: false, // must not matter: focusing wins regardless
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::FocusExistingWindow]
        );
    }

    // ---- 13. state.json absent/corrupt never change the plan ----------------

    #[test]
    fn local_state_never_changes_the_plan() {
        let half_provisioned = HostFacts {
            engine_container: Some(ContainerFact {
                exists: true,
                running: false,
                image_digest: Some("sha256:engine-good".into()),
            }),
            ..converged_macos_facts()
        };
        for state in [
            LocalStateFact::Trusted,
            LocalStateFact::Missing,
            LocalStateFact::Corrupt,
        ] {
            let facts = HostFacts {
                local_state: state,
                ..half_provisioned.clone()
            };
            assert_eq!(
                reconcile(&facts, &desired_macos()),
                vec![RepairAction::StartContainer],
                "local_state={state:?} must not change the plan"
            );
        }
    }

    // ---- 14. userns blocked on Linux -----------------------------------------

    #[test]
    fn userns_blocked_on_linux_installs_the_helper_once() {
        let facts = HostFacts {
            user_ns_allowed: false,
            helper_installed: false,
            ..converged_linux_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_linux()),
            vec![RepairAction::InstallPrivilegedHelper]
        );
    }

    #[test]
    fn userns_blocked_but_helper_already_installed_does_not_reinstall() {
        let facts = HostFacts {
            user_ns_allowed: false,
            helper_installed: true,
            ..converged_linux_facts()
        };
        // Nothing left for THIS gap; whatever else is wrong (if anything) drives the plan.
        assert_eq!(
            reconcile(&facts, &desired_linux()),
            Vec::<RepairAction>::new()
        );
    }

    /// The helper is NEVER planned unconditionally: on the pinned podman
    /// 6.1.1, the whole cage runs rootless (systemd PID1, Landlock,
    /// netns/nftables) and the helper exists ONLY for hosts where the kernel
    /// itself blocks unprivileged user namespaces
    /// (`kernel.unprivileged_userns_clone=0` — Ubuntu's AppArmor userns
    /// restriction). `user_ns_allowed: true` must mean "never install it",
    /// regardless of `helper_installed`.
    #[test]
    fn userns_allowed_never_installs_the_helper_even_if_not_marked_installed() {
        let facts = HostFacts {
            user_ns_allowed: true,
            helper_installed: false,
            ..converged_linux_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_linux()),
            Vec::<RepairAction>::new()
        );
    }

    // ---- runtime hash mismatch (re-stage, never a crash) ----------------------

    #[test]
    fn runtime_hash_mismatch_after_staging_re_stages_instead_of_crashing() {
        // Staged once (runtime_staged: true) but the verifier now disagrees —
        // corrupted on disk, tampered, or a manifest that moved under it.
        // `stage-runtime` re-verifies by sha256 and re-deploys; there is no
        // separate "recreate" action for this in the closed RepairAction
        // vocabulary because staging is already idempotent and self-verifying.
        let facts = HostFacts {
            runtime_staged: true,
            runtime_hash_ok: false,
            ..converged_macos_facts()
        };
        assert_eq!(
            reconcile(&facts, &desired_macos()),
            vec![RepairAction::StageRuntime]
        );
    }

    // ---- preflight ------------------------------------------------------------

    #[test]
    fn preflight_passes_when_converged() {
        assert!(preflight_violation(&converged_macos_facts(), &desired_macos()).is_none());
    }

    #[test]
    fn preflight_flags_insufficient_disk_as_retryable() {
        let facts = HostFacts {
            free_disk_bytes: Bytes(1),
            ..converged_macos_facts()
        };
        let violation = preflight_violation(&facts, &desired_macos()).unwrap();
        assert_eq!(violation.code, FailureCode::InsufficientDisk);
        assert!(violation.retryable);
    }

    #[test]
    fn preflight_flags_insufficient_memory_as_retryable() {
        let facts = HostFacts {
            total_memory_bytes: Bytes(1),
            ..converged_macos_facts()
        };
        let violation = preflight_violation(&facts, &desired_macos()).unwrap();
        assert_eq!(violation.code, FailureCode::InsufficientMemory);
        assert!(violation.retryable);
    }

    #[test]
    fn preflight_flags_unsupported_os_as_unretryable() {
        let facts = HostFacts {
            os: HostOs::Unsupported,
            ..converged_macos_facts()
        };
        let violation = preflight_violation(&facts, &desired_macos()).unwrap();
        assert_eq!(violation.code, FailureCode::UnsupportedOs);
        assert!(!violation.retryable);
    }

    #[test]
    fn preflight_flags_mac_intel_as_unsupported_arch() {
        let facts = HostFacts {
            arch: Arch::Amd64,
            ..converged_macos_facts()
        };
        let violation = preflight_violation(&facts, &desired_macos()).unwrap();
        assert_eq!(violation.code, FailureCode::UnsupportedArch);
        assert!(!violation.retryable);
    }
}
