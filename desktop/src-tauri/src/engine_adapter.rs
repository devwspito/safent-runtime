//! The ONLY implementation of `EngineProbe`/`EngineDriver` that talks to a
//! real `safent` binary (contracts/app-engine.md). Spawns it directly as an
//! argv array (never a shell string — see `spawn`), streams NDJSON off
//! stdout with a stall timeout + a hard timeout + an output cap, and hands
//! the bootstrap ticket to the caller through a dedicated pipe that never
//! touches stdout, argv, the environment, or a log line.
//!
//! T004 (a sibling lane, `RT` repo) is landing `--porcelain`/`facts --json`/
//! `SAFENT_PODMAN` on the CLI concurrently with this file. Until it ships,
//! every real invocation here fails CLOSED: the first line that is not valid
//! NDJSON is reported as `EngineError::UnexpectedOutput`, which
//! `to_failure_cause()` maps to `FailureCode::CliPorcelainUnsupported` —
//! never parsed as human text, never silently ignored, never hung. That is
//! an ordinary, retryable `EngineError` like any other, so `boot.rs`'s
//! degrade-after-no-progress rule applies to it unchanged.

use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::domain::{
    Arch, BootstrapTicket, Bytes, CompanionContainers, CompanionHealth, ContainerFact,
    DaemonHealth, DomainEvent, FailureCause, FailureCode, HostFacts, HostOs, ImageRef,
    LocalStateFact, MachineFact, MachineName, MachineProvider, Port, ProgressUnit, RepairAction,
    SemVer, Stage,
};
use crate::ports::{ApplyOutcome, CancelSignal, EngineDriver, EngineError, EngineProbe, Notifier};

/// Everything the adapter needs to talk to ONE bundled CLI. Built once at
/// boot time from the bundle layout + the runtime manifest (T010, another
/// lane) and handed to `EmbeddedCliDriver::new`.
#[derive(Debug, Clone)]
pub struct EmbeddedCliConfig {
    pub cli_path: PathBuf,
    pub podman_path: PathBuf,
    pub state_home: PathBuf,
    pub engine_image: ImageRef,
    pub companion_image: Option<ImageRef>,
    pub stall_timeout: Duration,
    pub hard_timeout: Duration,
    pub max_output_bytes: usize,
}

impl EmbeddedCliConfig {
    /// Conservative production defaults: 15 s without a single NDJSON line is
    /// well past "stalled" (contract app-engine.md §3.2 requires `progress`
    /// at least every 5 s while a stage is alive); 20 minutes covers the
    /// documented p95 for a first-run pull (spec NFR-001); 8 MiB of stdout is
    /// orders of magnitude more than the busiest real invocation ever needs.
    pub fn with_defaults(
        cli_path: PathBuf,
        podman_path: PathBuf,
        state_home: PathBuf,
        engine_image: ImageRef,
        companion_image: Option<ImageRef>,
    ) -> Self {
        Self {
            cli_path,
            podman_path,
            state_home,
            engine_image,
            companion_image,
            stall_timeout: Duration::from_secs(15),
            hard_timeout: Duration::from_secs(20 * 60),
            max_output_bytes: 8 * 1024 * 1024,
        }
    }
}

pub struct EmbeddedCliDriver {
    config: EmbeddedCliConfig,
}

impl EmbeddedCliDriver {
    pub fn new(config: EmbeddedCliConfig) -> Self {
        Self { config }
    }

    fn spawn(
        &self,
        verb: &str,
        args: &[String],
        secret: Option<&SecretPipe>,
    ) -> Result<Child, EngineError> {
        let mut cmd = Command::new(&self.config.cli_path);
        cmd.arg(verb);
        cmd.args(args);
        cmd.arg("--porcelain");
        cmd.env("PATH", augmented_path());
        cmd.env("SAFENT_PODMAN", &self.config.podman_path);
        cmd.env("SAFENT_NO_BROWSER", "1");
        cmd.env("SAFENT_NO_SELF_UPDATE", "1");
        cmd.env("SAFENT_IMAGE", self.config.engine_image.reference());
        if let Some(companion) = &self.config.companion_image {
            cmd.env("SAFENT_ADS_IMAGE", companion.reference());
        }
        cmd.env("SAFENT_STATE_HOME", &self.config.state_home);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());
        if let Some(secret) = secret {
            secret.install(&mut cmd);
        }
        // MAC2-04 (verificacion-mac-2.md): a real Mac run left TWO orphaned
        // `podman run` processes alive, still downloading 2.7 GB, minutes
        // after the wrapper had already declared the boot failed —
        // `kill_and_timeout`/the cancel path kill only this DIRECT child
        // (`/bin/sh safent`); a grandchild it spawned in its own right
        // (`_ensure_seccomp`'s `podman run`, or `cmd_ensure_images`'s
        // backgrounded `podman pull`) is reparented to init and keeps
        // running. Giving this child its OWN new process group (leader =
        // its own pid) means `kill_child_group` below can signal the WHOLE
        // group at once — SIGKILL is never catchable by a trap either way,
        // so this must happen from OUR side, not rely on the CLI script
        // cleaning up after itself once already dead.
        new_process_group(&mut cmd);
        spawn_with_etxtbsy_retry(&mut cmd).map_err(|e| {
            // MAC-04 (verificacion-mac-1.md): ENOENT here means the bundled
            // CLI does not exist at the resolved path — a packaging/path
            // defect no retry can ever fix, not the generic transient `Io`
            // the daemon-unhealthy/retryable classification below is for.
            if e.kind() == std::io::ErrorKind::NotFound {
                EngineError::CliNotFound {
                    path: self.config.cli_path.clone(),
                    verb: verb.to_string(),
                }
            } else {
                EngineError::Io(format!("no pude ejecutar '{verb}': {e}"))
            }
        })
    }
}

/// `ETXTBSY` ("text file busy") is a well-documented, ordinarily transient
/// condition: the kernel refuses to `execve()` a file that some OTHER
/// process still has open for writing at that exact instant (e.g. another
/// process mid-write to the same path, or — during a real update — the
/// installer replacing this very binary). A short, bounded retry clears it
/// without the caller ever seeing a spurious failure for something that
/// resolves itself a few milliseconds later.
fn spawn_with_etxtbsy_retry(cmd: &mut Command) -> std::io::Result<Child> {
    const ETXTBSY: i32 = 26;
    const MAX_ATTEMPTS: u32 = 5;
    let mut attempt = 0;
    loop {
        match cmd.spawn() {
            Ok(child) => return Ok(child),
            Err(e) if e.raw_os_error() == Some(ETXTBSY) && attempt < MAX_ATTEMPTS => {
                attempt += 1;
                std::thread::sleep(Duration::from_millis(20 * attempt as u64));
            }
            Err(e) => return Err(e),
        }
    }
}

impl EngineProbe for EmbeddedCliDriver {
    fn observe(&self) -> Result<HostFacts, EngineError> {
        let mut facts: Option<HostFacts> = None;
        // A pure observation is never cancelled — there is nothing to
        // interrupt (contract: "facts --json ... No modifica nada").
        let never_cancelled = CancelSignal::new();
        let outcome = self.run_porcelain(
            "facts",
            &["--json".to_string()],
            false,
            &never_cancelled,
            |event| {
                if let WireEvent::Facts { facts: wire } = event {
                    facts = Some(map_host_facts(wire)?);
                }
                Ok(())
            },
        )?;
        // A successful observation wins even over a nonzero trailing exit
        // (facts --json is documented pure — "no modifica nada"); only when
        // NO facts ever arrived does the exit code decide which error to
        // surface.
        match (facts, outcome.exit_ok) {
            (Some(facts), _) => Ok(facts),
            (None, false) => Err(EngineError::ProcessExited {
                code: outcome.exit_code,
                stderr_tail: outcome.stderr_tail,
            }),
            (None, true) => Err(EngineError::Protocol(
                "facts --json exited without emitting `facts`".to_string(),
            )),
        }
    }
}

impl EmbeddedCliDriver {
    /// One closed marker-consumer tick. Claim/lease/status handling stays in
    /// the shared CLI consumer; no arbitrary verb, image, URL or args enter.
    pub(crate) fn consume_companion_requests(
        &self,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> Result<ApplyOutcome, EngineError> {
        if cancel.is_set() {
            return Err(EngineError::Cancelled);
        }
        if self.config.companion_image.is_none() {
            return Err(EngineError::Protocol(
                "bundled companion digest required".into(),
            ));
        }
        // Like boot's companion apply_gated, a claimed operation may already
        // be migrating before its stage reaches this reader. Shutdown drains
        // it under the existing stall/hard caps; it does not kill a migration
        // on a window gesture or claim to have rolled it back.
        self.apply_command(
            "companion",
            vec!["requests".into()],
            notifier,
            &CancelSignal::new(),
        )
    }

    fn apply_command(
        &self,
        verb: &str,
        mut args: Vec<String>,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> Result<ApplyOutcome, EngineError> {
        // `up` always provisions the companion SCAFFOLD unconditionally
        // (T015: "el andamiaje existe siempre") unless told not to — but
        // when THIS boot's own DesiredState wants no companion at all
        // (companion_image: None, e.g. every selftest / --no-companion
        // caller), that provisioning attempt is pure unwanted work: real
        // network fetches + cert/network setup with zero progress events
        // for the whole `container` stage's duration, verified live to
        // exceed this adapter's own 15s stall timeout (packaging review
        // item 4, verificacion-paquete-linux.md §6). `--no-companion` is
        // now recognized from ANY position in argv (safent's own filter
        // loop, same fix) — this is the only place a Rust caller can ask.
        if verb == "up" && self.config.companion_image.is_none() {
            args.push("--no-companion".to_string());
        }
        let want_secret = verb == "up";
        let mut failure: Option<FailureCause> = None;
        let mut ready = false;

        let outcome = self.run_porcelain(verb, &args, want_secret, cancel, |event| {
            match event {
                WireEvent::Stage {
                    id,
                    label,
                    total_bytes,
                } => {
                    let stage = map_stage(&id)?;
                    notifier.notify(&DomainEvent::StageEntered {
                        stage,
                        label,
                        total_bytes,
                    });
                }
                WireEvent::Progress {
                    id,
                    done,
                    total,
                    unit,
                } => {
                    let stage = map_stage(&id)?;
                    let unit = map_progress_unit(&unit)?;
                    notifier.notify(&DomainEvent::StageProgressed {
                        stage,
                        done,
                        total,
                        unit,
                    });
                }
                WireEvent::Done { id, ms } => {
                    let stage = map_stage(&id)?;
                    notifier.notify(&DomainEvent::StageCompleted {
                        stage,
                        duration_ms: ms,
                    });
                }
                WireEvent::Failed {
                    code,
                    detail,
                    retryable,
                } => {
                    let code = map_failure_code(&code)?;
                    failure = Some(FailureCause {
                        code,
                        message: detail,
                        retryable,
                    });
                }
                WireEvent::Ready { .. } => ready = true,
                WireEvent::Facts { .. } => {
                    return Err(EngineError::Protocol(
                        "unexpected `facts` event from a non-probe verb".to_string(),
                    ));
                }
            }
            Ok(())
        })?;

        // A `failed` event is authoritative over the raw exit code (contract
        // app-engine.md §2: 10..39 IS that event, reported previously) — it
        // must win even though the process also exited non-zero.
        if let Some(cause) = failure {
            return Err(EngineError::Reported(reclassify_from_stderr(
                cause,
                &outcome.stderr_tail,
            )));
        }
        if ready {
            let line = outcome.secret_line.ok_or(EngineError::ReadyWithoutTicket)?;
            return Ok(ApplyOutcome::Ready(BootstrapTicket::new(line)));
        }
        if !outcome.exit_ok {
            // A shell running with `set -e` can exit before it has emitted a
            // structured `failed` event (podman's own seccomp/open errors are
            // a real example).  Do not discard the classifier merely because
            // the failure arrived through the raw process-exit path.
            let generic = FailureCause {
                code: FailureCode::DaemonUnhealthy,
                message: outcome.stderr_tail.clone(),
                retryable: true,
            };
            return Err(EngineError::Reported(reclassify_from_stderr(
                generic,
                &outcome.stderr_tail,
            )));
        }
        Ok(ApplyOutcome::Progressed)
    }
}

impl EngineDriver for EmbeddedCliDriver {
    fn apply(
        &self,
        action: &RepairAction,
        notifier: &dyn Notifier,
        cancel: &CancelSignal,
    ) -> Result<ApplyOutcome, EngineError> {
        let (verb, args) = cli_invocation_for(action)?;
        self.apply_command(verb, args, notifier, cancel)
    }

    fn stop(&self) -> Result<(), EngineError> {
        // `stop` is a plain, fast, non-progress-bearing operation — it is not
        // part of the porcelain contract's verb table (app-engine.md §4), so
        // this bypasses run_porcelain entirely: no NDJSON expected, just a
        // bounded wait on the exit code.
        let mut cmd = Command::new(&self.config.cli_path);
        cmd.arg("stop");
        cmd.env("PATH", augmented_path());
        cmd.env("SAFENT_PODMAN", &self.config.podman_path);
        cmd.env("SAFENT_STATE_HOME", &self.config.state_home);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::piped());
        new_process_group(&mut cmd);
        let mut child = spawn_with_etxtbsy_retry(&mut cmd)
            .map_err(|e| EngineError::Io(format!("no pude ejecutar 'stop': {e}")))?;
        let stderr = child.stderr.take();
        wait_bounded(&mut child, self.config.hard_timeout, stderr)
    }
}

/// Waits for `child` to exit, killing it (and failing) if `timeout` elapses
/// first. Drains `stderr` on a side thread so a chatty exit never blocks the
/// wait itself.
fn wait_bounded(
    child: &mut Child,
    timeout: Duration,
    stderr: Option<impl Read + Send + 'static>,
) -> Result<(), EngineError> {
    let tail = stderr.map(|s| {
        std::thread::spawn(move || {
            let mut buf = String::new();
            let _ = BufReader::new(s).take(4096).read_to_string(&mut buf);
            buf
        })
    });
    let deadline = Instant::now() + timeout;
    loop {
        match child
            .try_wait()
            .map_err(|e| EngineError::Io(e.to_string()))?
        {
            Some(status) if status.success() => return Ok(()),
            Some(status) => {
                let stderr_tail = tail.and_then(|h| h.join().ok()).unwrap_or_default();
                return Err(EngineError::ProcessExited {
                    code: status.code(),
                    stderr_tail,
                });
            }
            None if Instant::now() >= deadline => {
                kill_child_group(child);
                let _ = child.wait();
                return Err(EngineError::Timeout { after: timeout });
            }
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    }
}

/// The macOS app carries its engine and helpers, addressed by verified paths.
/// Its shell utilities must come from macOS too, never from a user's PATH or
/// package manager. Keep the existing non-macOS resolution contract unchanged.
pub(crate) fn native_command_path(macos: bool, inherited: Option<&str>) -> String {
    if macos {
        return "/usr/bin:/bin:/usr/sbin:/sbin".into();
    }
    let mut parts = Vec::new();
    if let Some(p) = inherited {
        if !p.is_empty() {
            parts.push(p.to_string());
        }
    }
    for extra in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"] {
        parts.push(extra.to_string());
    }
    parts.join(":")
}

fn augmented_path() -> String {
    native_command_path(
        cfg!(target_os = "macos"),
        std::env::var("PATH").ok().as_deref(),
    )
}

#[cfg(test)]
mod augmented_path_tests {
    use super::*;

    #[test]
    fn macos_ignores_user_path_even_when_it_contains_foreign_engines_and_tools() {
        let path = native_command_path(true, Some("/tmp/decoy:/opt/homebrew/bin:/usr/local/bin:/opt/podman/bin:.:/Users/test/.local/bin"));
        assert_eq!(path, "/usr/bin:/bin:/usr/sbin:/sbin");
    }

    #[test]
    fn macos_needs_no_inherited_path() {
        assert_eq!(
            native_command_path(true, None),
            "/usr/bin:/bin:/usr/sbin:/sbin"
        );
        assert_eq!(
            native_command_path(true, Some("")),
            native_command_path(true, None)
        );
    }

    #[test]
    fn non_macos_retains_its_existing_resolution_contract() {
        assert_eq!(
            native_command_path(false, Some("/test/bin:/usr/bin")),
            "/test/bin:/usr/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        );
        assert_eq!(
            native_command_path(false, None),
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        );
    }
}

/// Maps the closed `RepairAction` vocabulary onto the CLI's verb table
/// (contract app-engine.md §4). Several distinct actions collapse onto the
/// SAME idempotent verb on purpose — the CLI decides adopt-vs-create-vs-start
/// internally too (defense in depth); the Rust-side distinction exists so
/// `reconcile`'s tests can prove the RIGHT SITUATION was recognized, not to
/// steer the CLI with flags the contract does not define.
fn cli_invocation_for(action: &RepairAction) -> Result<(&'static str, Vec<String>), EngineError> {
    match action {
        RepairAction::StageRuntime => Ok(("stage-runtime", vec![])),
        RepairAction::AdoptMachine(_)
        | RepairAction::CreateMachine
        | RepairAction::StartMachine(_)
        | RepairAction::InstallPrivilegedHelper => Ok(("ensure-machine", vec![])),
        RepairAction::PullEngine(_) | RepairAction::PullCompanion(_) => {
            Ok(("ensure-images", vec![]))
        }
        RepairAction::ChoosePort
        | RepairAction::CreateContainer
        | RepairAction::StartContainer
        | RepairAction::RecreateEngine => Ok(("up", vec![])),
        RepairAction::EnsureCompanionScaffold => Ok(("companion", vec!["repair".to_string()])),
        RepairAction::ComposeCompanionUp(_) => Ok(("companion", vec!["install".to_string()])),
        // Not part of this adapter's contract at all: ReloadCompanionPresence is a
        // daemon dbus verb (T017, a different bounded context); FocusExistingWindow
        // is a pure window action boot.rs must special-case and never hand here.
        RepairAction::ReloadCompanionPresence | RepairAction::FocusExistingWindow => {
            Err(EngineError::UnsupportedByAdapter {
                action: action.clone(),
            })
        }
    }
}

/// A `failed` event's own `code` can be a poor diagnosis of what actually
/// happened: the CLI maps ANY `podman pull` failure to `registry_unreachable`
/// (`cmd_ensure_images`), regardless of WHY podman actually failed — verified
/// live (packaging review item 3, verificacion-paquete-linux.md §"Pasada 1"):
/// a bundled/host podman storage-lock collision ("failed to open 2048 locks
/// in /libpod_rootless_lock_1000: numerical result out of range") surfaced
/// to the owner as "no pude conectarme para descargar", pointing at network
/// connectivity for a purely local, already-captured-but-unused stderr
/// detail. This is the one place both the reported cause AND the process's
/// full stderr (`stderr_tail`, already captured for every apply()) are both
/// in hand — reclassifies to a precise code when stderr's own text is a
/// clearer diagnosis than the CLI's generic one; the message becomes
/// podman's own stderr instead of the CLI's generic detail, so a real cause
/// is never hidden. A CLI that itself starts reporting the more precise code
/// one day makes this a no-op (the `match` below simply never fires).
fn reclassify_from_stderr(cause: FailureCause, stderr_tail: &str) -> FailureCause {
    let lower = stderr_tail.to_ascii_lowercase();
    let is_local_storage_conflict = (lower.contains("numerical result out of range")
        || lower.contains("erange"))
        && (lower.contains("lock") || lower.contains("libpod_rootless_lock"));
    if is_local_storage_conflict {
        return FailureCause {
            code: FailureCode::LocalStorageConflict,
            message: stderr_tail.to_string(),
            retryable: false,
        };
    }
    // MAC3-03 (verificacion-mac-3.md): "seccomp profile" appears BOTH in
    // podman's own raw error ("opening seccomp profile failed: open
    // <path>: no such file or directory" — reached because `safent`'s
    // `_run` runs under `set -e` with no dedicated failure branch, so this
    // aborted before any `_die_porcelain` call) and in the CLI's own honest
    // last-resort message ("Could not obtain the seccomp profile...") —
    // one phrase, one precise code, regardless of which of the two
    // produced it. Retryable: a transient network hiccup fetching the
    // fallback (or a not-yet-finished stage-runtime) can resolve on retry.
    if lower.contains("seccomp profile") {
        return FailureCause {
            code: FailureCode::SeccompProfileMissing,
            message: stderr_tail.to_string(),
            retryable: true,
        };
    }
    // MAC3-07 (verificacion-mac-3.md, MAC-07/MAC2-13 repeated unfixed):
    // `cmd_ensure_machine`'s own honest detection (`_foreign_engine_helper`)
    // reports this generically as `machine_start_failed` — the closed
    // 20-code CLI vocabulary has no dedicated code for "the machine is
    // running fine but its gvproxy/vfkit is not the bundled one". Not
    // retryable: retrying `machine start` alone never changes which helper
    // binary wins — the environment (containers.conf/PATH) needs to change.
    if lower.contains("foreign helper binary") {
        return FailureCause {
            code: FailureCode::ForeignEngineHelper,
            message: stderr_tail.to_string(),
            retryable: false,
        };
    }
    cause
}

/// MAC3-03 (verificacion-mac-3.md): `reclassify_from_stderr` must recognize
/// the seccomp-profile failure from EITHER of its two real origins (raw
/// podman stderr, or the CLI's own last-resort message) and must NOT
/// over-fire on an unrelated `daemon_unhealthy` that happens to share
/// neither phrase.
#[cfg(test)]
mod reclassify_from_stderr_tests {
    use super::*;

    fn generic_daemon_unhealthy() -> FailureCause {
        FailureCause {
            code: FailureCode::DaemonUnhealthy,
            message: "El servicio de Safent no arranco".to_string(),
            retryable: true,
        }
    }

    #[test]
    fn podmans_own_raw_seccomp_error_is_reclassified() {
        let stderr = "Error: opening seccomp profile failed: open /tmp/safent-mac-test3/state/safent-seccomp.json: no such file or directory";
        let reclassified = reclassify_from_stderr(generic_daemon_unhealthy(), stderr);
        assert_eq!(reclassified.code, FailureCode::SeccompProfileMissing);
        assert!(reclassified.retryable);
        assert_eq!(reclassified.message, stderr);
    }

    #[test]
    fn the_clis_own_last_resort_seccomp_message_is_also_reclassified() {
        let stderr = "[x] Could not obtain the seccomp profile (bundle, image and https://example/safent.json all failed, no cache)";
        let reclassified = reclassify_from_stderr(generic_daemon_unhealthy(), stderr);
        assert_eq!(reclassified.code, FailureCode::SeccompProfileMissing);
    }

    #[test]
    fn an_unrelated_daemon_unhealthy_stderr_is_left_alone() {
        let cause = generic_daemon_unhealthy();
        let reclassified =
            reclassify_from_stderr(cause.clone(), "systemd unit hermes-runtime.service failed");
        assert_eq!(reclassified, cause);
    }

    #[test]
    fn a_foreign_engine_helper_report_is_reclassified_and_never_retryable() {
        let generic_machine_start_failed = FailureCause {
            code: FailureCode::MachineStartFailed,
            message: "No se pudo arrancar la maquina".to_string(),
            retryable: true,
        };
        let stderr = "foreign helper binary in use for safent-engine: /opt/podman/bin/gvproxy";
        let reclassified = reclassify_from_stderr(generic_machine_start_failed, stderr);
        assert_eq!(reclassified.code, FailureCode::ForeignEngineHelper);
        assert!(!reclassified.retryable);
        assert_eq!(reclassified.message, stderr);
    }
}

// ---------------------------------------------------------------------------
// NDJSON streaming: spawn, drain stdout/stderr/secret-fd concurrently, honor
// a stall timeout + a hard timeout + an output cap. Never a shell string —
// `Command::new(cli_path)` execve's the target directly; argv are discrete
// OS-level arguments, never concatenated into anything a shell parses.
// ---------------------------------------------------------------------------

struct RunOutcome {
    secret_line: Option<String>,
    exit_ok: bool,
    exit_code: Option<i32>,
    stderr_tail: String,
}

enum ReaderMsg {
    StdoutLine(String),
    StderrLine(String),
    SecretLine(String),
    ReaderClosed,
    /// Distinct from `ReaderClosed` on purpose: a pipe's kernel buffer (64
    /// KiB+ on Linux) means a flooding child can write far more than
    /// `max_output_bytes` and still exit 0 without ever blocking on a
    /// reader that stopped early — silently treating "stopped reading" as
    /// "the stream ended cleanly" would let that flood report success.
    OutputCapExceeded,
}

impl EmbeddedCliDriver {
    fn run_porcelain(
        &self,
        verb: &str,
        extra_args: &[String],
        want_secret: bool,
        cancel: &CancelSignal,
        mut on_event: impl FnMut(WireEvent) -> Result<(), EngineError>,
    ) -> Result<RunOutcome, EngineError> {
        let mut args = extra_args.to_vec();
        let secret = if want_secret {
            args.push("--secret-fd".to_string());
            args.push("3".to_string());
            Some(SecretPipe::new()?)
        } else {
            None
        };

        let mut child = self.spawn(verb, &args, secret.as_ref())?;
        let stdout = child.stdout.take().expect("stdout was piped");
        let stderr = child.stderr.take().expect("stderr was piped");

        let (tx, rx) = mpsc::channel();
        let mut open_readers = 2u8;
        spawn_stream_reader(
            stdout,
            tx.clone(),
            ReaderMsg::StdoutLine,
            self.config.max_output_bytes,
        );
        spawn_stream_reader(
            stderr,
            tx.clone(),
            ReaderMsg::StderrLine,
            self.config.max_output_bytes,
        );
        if let Some(secret) = secret {
            open_readers += 1;
            spawn_stream_reader(secret.into_reader(), tx, ReaderMsg::SecretLine, 4096);
        }

        let mut stderr_tail = String::new();
        let mut secret_line = None;
        let deadline = Instant::now() + self.config.hard_timeout;
        let mut last_activity = Instant::now();

        // A cancel must be noticed quickly even while the stall-timeout
        // budget is large (production default 15 s) — never wait a whole
        // stall window just to notice the owner clicked Cancel.
        const CANCEL_POLL_INTERVAL: Duration = Duration::from_millis(200);

        while open_readers > 0 {
            if cancel.is_set() {
                kill_child_group(&mut child);
                let _ = child.wait();
                return Err(EngineError::Cancelled);
            }
            let time_left = deadline.saturating_duration_since(Instant::now());
            if time_left.is_zero() {
                return Err(kill_and_timeout(&mut child, self.config.hard_timeout));
            }
            let budget = self
                .config
                .stall_timeout
                .min(time_left)
                .min(CANCEL_POLL_INTERVAL);
            match rx.recv_timeout(budget) {
                Ok(ReaderMsg::StdoutLine(line)) => {
                    last_activity = Instant::now();
                    handle_stdout_line(&mut child, &line, &mut on_event)?;
                }
                Ok(ReaderMsg::StderrLine(line)) => {
                    last_activity = Instant::now();
                    push_capped(&mut stderr_tail, &line);
                }
                Ok(ReaderMsg::SecretLine(line)) => {
                    last_activity = Instant::now();
                    secret_line = Some(line);
                }
                Ok(ReaderMsg::ReaderClosed) => open_readers -= 1,
                Ok(ReaderMsg::OutputCapExceeded) => {
                    kill_child_group(&mut child);
                    let _ = child.wait();
                    return Err(EngineError::Protocol(format!(
                        "salida superó el límite de {} bytes",
                        self.config.max_output_bytes
                    )));
                }
                Err(RecvTimeoutError::Timeout) => {
                    if last_activity.elapsed() >= self.config.stall_timeout {
                        return Err(kill_and_timeout(&mut child, self.config.stall_timeout));
                    }
                }
                Err(RecvTimeoutError::Disconnected) => break,
            }
        }

        // The exit code alone is NOT the verdict here: contract app-engine.md
        // §2 says codes 10..39 correspond to a `failed` event already emitted
        // (the caller's `on_event` already saw it) and only code 1 means
        // "unclassified". Deciding which applies needs what `on_event`
        // learned, which this function does not see — so it hands back the
        // raw exit outcome and lets `observe`/`apply` decide, instead of
        // guessing here and shadowing a real classification.
        let status = child.wait().map_err(|e| EngineError::Io(e.to_string()))?;
        let stderr_tail = if stderr_tail.is_empty() {
            "(sin salida)".to_string()
        } else {
            stderr_tail
        };
        Ok(RunOutcome {
            secret_line,
            exit_ok: status.success(),
            exit_code: status.code(),
            stderr_tail,
        })
    }
}

fn handle_stdout_line(
    child: &mut Child,
    line: &str,
    on_event: &mut impl FnMut(WireEvent) -> Result<(), EngineError>,
) -> Result<(), EngineError> {
    let event = match serde_json::from_str::<WireEvent>(line) {
        Ok(event) => event,
        Err(_) => {
            kill_child_group(child);
            let _ = child.wait();
            return Err(EngineError::UnexpectedOutput {
                line: line.to_string(),
            });
        }
    };
    if let Err(e) = on_event(event) {
        kill_child_group(child);
        let _ = child.wait();
        return Err(e);
    }
    Ok(())
}

fn kill_and_timeout(child: &mut Child, after: Duration) -> EngineError {
    kill_child_group(child);
    let _ = child.wait();
    EngineError::Timeout { after }
}

/// MAC2-04 (verificacion-mac-2.md): puts `cmd`'s eventual child in its OWN
/// new process group (leader = its own pid) — the ONE thing that makes
/// `kill_child_group` below able to reach a grandchild the CLI spawned
/// (`podman run`/`podman pull`) instead of only the direct `/bin/sh safent`
/// process. Every spawn site in this file uses it; a shell-level `trap`
/// alone cannot substitute for this because SIGKILL (what `Child::kill`
/// sends) is never catchable — the killer has to target the right process
/// itself, not hope the target cleans up after being told to die.
#[cfg(unix)]
fn new_process_group(cmd: &mut Command) {
    use std::os::unix::process::CommandExt;
    cmd.process_group(0);
}

#[cfg(not(unix))]
fn new_process_group(_cmd: &mut Command) {}

/// Kills the WHOLE process group `child` leads (see `new_process_group`),
/// not just `child` itself — a plain `child.kill()` leaves any grandchild
/// (a `podman run`/`podman pull` the CLI script spawned) reparented to
/// init and running to completion, orphaned, unbounded (MAC2-04: two such
/// orphans kept downloading 2.7 GB for minutes after the app had already
/// declared the boot failed).
#[cfg(unix)]
fn kill_child_group(child: &mut Child) {
    // SAFETY: `child.id()` is a valid pid for a process this same code
    // spawned with `new_process_group` (pgid == pid) — signaling `-pid`
    // targets that exact group and nothing else. `kill(2)` with signal 0
    // would be a mere existence probe; SIGKILL here matches `Child::kill`'s
    // own (uncatchable) semantics, just widened to the group.
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
    }
    let _ = child.kill(); // belt-and-suspenders if the group signal somehow missed it
}

#[cfg(not(unix))]
fn kill_child_group(child: &mut Child) {
    let _ = child.kill();
}

fn push_capped(buffer: &mut String, line: &str) {
    const CAP: usize = 4096;
    if buffer.len() >= CAP {
        return;
    }
    if !buffer.is_empty() {
        buffer.push('\n');
    }
    buffer.push_str(line);
    buffer.truncate(CAP.min(buffer.len()));
}

fn spawn_stream_reader<R: Read + Send + 'static>(
    reader: R,
    tx: mpsc::Sender<ReaderMsg>,
    wrap: fn(String) -> ReaderMsg,
    max_bytes: usize,
) {
    std::thread::spawn(move || {
        let mut buffered = BufReader::new(reader);
        let mut total = 0usize;
        loop {
            let mut raw = String::new();
            match buffered.read_line(&mut raw) {
                Ok(0) => break,
                Ok(n) => {
                    total += n;
                    if total > max_bytes {
                        let _ = tx.send(ReaderMsg::OutputCapExceeded);
                        return; // terminal — the main loop kills the child; no ReaderClosed follows
                    }
                    let line = raw.trim_end_matches(['\n', '\r']).to_string();
                    if tx.send(wrap(line)).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        let _ = tx.send(ReaderMsg::ReaderClosed);
    });
}

// ---------------------------------------------------------------------------
// Wire contract (contracts/app-engine.md §3). Deliberately NOT `domain`
// types: these derive `serde::Deserialize`, which the domain layer must
// never depend on. Every `map_*` function below is the one place that
// crosses from "what the CLI said" to "what the domain understands".
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
#[serde(tag = "t")]
enum WireEvent {
    #[serde(rename = "stage")]
    Stage {
        id: String,
        label: String,
        total_bytes: Option<u64>,
    },
    #[serde(rename = "progress")]
    Progress {
        id: String,
        done: u64,
        total: Option<u64>,
        unit: String,
    },
    #[serde(rename = "done")]
    Done { id: String, ms: u64 },
    #[serde(rename = "failed")]
    Failed {
        // `id` (which stage failed) is part of the wire contract but this
        // adapter's own classification never needs it — `EngineDegraded`'s
        // payload carries `code`/`detail`/`retryable` only (data-model.md).
        // Deliberately not a struct field: serde ignores unknown JSON keys
        // by default, so there is nothing to silence here.
        code: String,
        detail: String,
        retryable: bool,
    },
    #[serde(rename = "facts")]
    Facts { facts: WireHostFacts },
    #[serde(rename = "ready")]
    // `endpoint_ref` is always the literal "stdout-secret" (contract
    // app-engine.md §5) — nothing to branch on, so not a field either.
    Ready {},
}

// `facts`'s inner body is a SEPARATE sub-protocol from the `t`-tagged event
// envelope above: data-model.md's `HostFacts` value object is specified in
// camelCase (`freeDiskBytes`, `engineContainer`, ...), and `cmd_facts` in the
// real `safent` script emits exactly that — confirmed against the actual
// script, not assumed (see tests/engine_adapter_real_cli_contract.rs). Only
// this struct + its two nested ones need `rename_all`; `WireEvent`'s own
// fields (`id`, `total_bytes`, `retryable`, ...) are already snake_case on
// the wire, per contract app-engine.md §3, and must stay that way.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WireHostFacts {
    os: String,
    arch: String,
    free_disk_bytes: u64,
    total_memory_bytes: u64,
    runtime_staged: bool,
    runtime_hash_ok: bool,
    #[serde(default)]
    machines: Vec<WireMachineFact>,
    engine_container: Option<WireContainerFact>,
    // NOT in data-model.md's documented HostFacts field list — added to
    // cmd_facts (the real CLI) alongside this struct's fix, because
    // `reconcile.rs::images_gap`/`companion_gap` need "is the desired digest
    // present locally" INDEPENDENT of whether a container already runs it
    // (distinct from `engine_container.image_digest`), and nothing else on
    // the wire carries that. Absent/non-digest-pinned image ⇒ null.
    #[serde(default)]
    local_engine_image_digest: Option<String>,
    #[serde(default)]
    local_companion_image_digest: Option<String>,
    published_port: Option<u16>,
    data_volume: bool,
    companion_scaffold: bool,
    #[serde(default)]
    companion_containers: WireCompanionContainers,
    companion_health: String,
    daemon_health: String,
    // The real CLI reports `null` whenever the engine isn't running yet or
    // its version couldn't be read (`cmd_facts`: "app_version=null" is the
    // ordinary fresh-install case) — NOT always present the way every other
    // required field here is.
    app_version: Option<String>,
    user_ns_allowed: bool,
    helper_installed: bool,
    #[serde(default)]
    another_instance_running: bool,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WireCompanionContainers {
    #[serde(default)]
    running: u32,
    #[serde(default)]
    total: u32,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WireMachineFact {
    name: String,
    provider: String,
    rootful: bool,
    running: bool,
    ours: bool,
    // MAC2-01 (verificacion-mac-2.md): the real CLI's `_machines_json` now
    // emits both, straight from `podman machine list --format json`'s own
    // `VMType`/`CPUs`/`Memory` (contract app-engine.md §3). `#[serde(default)]`
    // kept anyway — a Linux `facts` response never has a `machines[]` entry
    // at all (`_machines_json` short-circuits to `[]`), so nothing here ever
    // needs these on that platform, and a malformed/older entry should not
    // fail the whole `facts` parse over two fields the planner already
    // treats conservatively (0 satisfies nothing).
    #[serde(default)]
    cpus: u32,
    #[serde(default)]
    memory_bytes: u64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WireContainerFact {
    exists: bool,
    running: bool,
    image_digest: Option<String>,
}

fn map_host_facts(wire: WireHostFacts) -> Result<HostFacts, EngineError> {
    // The real CLI reports `null` whenever the engine has never run yet
    // (the ordinary fresh-install observation) — that must not fail the
    // WHOLE probe, since `observe()` failing is exactly what would stop
    // preflight/reconcile from ever running in the first place. "0.0.0" is
    // this codebase's existing "not yet known" placeholder (see
    // `boot.rs::app_version`'s own fallback for the same reason).
    let app_version = match wire.app_version.as_deref() {
        Some(v) => SemVer::parse(v)
            .map_err(|_| EngineError::Protocol(format!("invalid app_version: {v}")))?,
        None => SemVer::parse("0.0.0").expect("\"0.0.0\" is a valid SemVer"),
    };
    Ok(HostFacts {
        os: map_os(&wire.os),
        arch: map_arch(&wire.arch),
        free_disk_bytes: Bytes(wire.free_disk_bytes),
        total_memory_bytes: Bytes(wire.total_memory_bytes),
        runtime_staged: wire.runtime_staged,
        runtime_hash_ok: wire.runtime_hash_ok,
        machines: wire.machines.into_iter().map(map_machine_fact).collect(),
        engine_container: wire.engine_container.map(map_container_fact),
        local_engine_image_digest: wire.local_engine_image_digest,
        local_companion_image_digest: wire.local_companion_image_digest,
        published_port: wire.published_port.map(Port),
        data_volume: wire.data_volume,
        companion_scaffold: wire.companion_scaffold,
        companion_containers: CompanionContainers {
            running: wire.companion_containers.running,
            total: wire.companion_containers.total,
        },
        companion_health: map_companion_health(&wire.companion_health),
        daemon_health: map_daemon_health(&wire.daemon_health),
        app_version,
        user_ns_allowed: wire.user_ns_allowed,
        helper_installed: wire.helper_installed,
        // The CLI has no notion of the APP's own state.json — that cache
        // belongs to a layer this adapter does not own (StateStore, out of
        // T009's scope). A live probe is always Trusted by construction:
        // there is nothing cached here to distrust.
        local_state: LocalStateFact::Trusted,
        another_instance_running: wire.another_instance_running,
    })
}

fn map_os(raw: &str) -> HostOs {
    match raw {
        "darwin" => HostOs::MacOs,
        "linux" => HostOs::Linux,
        _ => HostOs::Unsupported,
    }
}

fn map_arch(raw: &str) -> Arch {
    match raw {
        "arm64" | "aarch64" => Arch::Arm64,
        "amd64" | "x86_64" => Arch::Amd64,
        _ => Arch::Unsupported,
    }
}

/// MAC-01 (verificacion-mac-1.md): the CLI's `cmd_facts` (`safent`) has
/// always emitted `os_id=darwin` for a `Darwin` `uname -s` — this adapter
/// was the side out of sync, only ever accepting `"macos"`, a string the
/// CLI never produces. Every Mac observation therefore mapped to
/// `HostOs::Unsupported` and `reconcile::preflight_violation` turned that
/// into a non-retryable `unsupported_os` before the engine ever started.
/// Contract fixed ONE way (contracts/app-engine.md §3): the wire vocabulary
/// for `os` is `uname -s` lower-cased — `"darwin"` / `"linux"` — not a
/// product name. No back-compat alias: `"macos"` was never real CLI output.
#[cfg(test)]
mod os_vocabulary_tests {
    use super::*;

    #[test]
    fn map_os_recognizes_the_full_wire_vocabulary() {
        assert_eq!(map_os("darwin"), HostOs::MacOs);
        assert_eq!(map_os("linux"), HostOs::Linux);
        assert_eq!(map_os("windows"), HostOs::Unsupported);
        assert_eq!(map_os(""), HostOs::Unsupported);
    }
}

/// MAC-04 (verificacion-mac-1.md): a missing CLI binary (the exact shape of
/// `selftest.rs` resolving `Contents/MacOS/runtime` on a real macOS `.app`,
/// where the CLI actually ships at `Contents/Resources/runtime/…`) must
/// never surface as a retryable `daemon_unhealthy` — that is an infinite
/// no-progress loop against a path that can never start existing. This test
/// exercises the REAL `spawn` path (no fake CLI script at all: the whole
/// point is that nothing exists at `cli_path`), not just the classification
/// table in isolation.
#[cfg(test)]
mod missing_cli_tests {
    use super::*;
    use crate::ports::EngineProbe;

    #[test]
    fn observe_against_a_missing_cli_binary_is_non_retryable_cli_porcelain_unsupported() {
        let dir = std::env::temp_dir().join(format!(
            "safent-missing-cli-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let engine_image = ImageRef::new("ghcr.io/devwspito/safent", "sha256:engine-good").unwrap();
        let config = EmbeddedCliConfig::with_defaults(
            dir.join("safent-does-not-exist"),
            dir.join("podman-does-not-exist"),
            dir.join("state"),
            engine_image,
            None,
        );
        let driver = EmbeddedCliDriver::new(config);

        let err = driver
            .observe()
            .expect_err("a missing CLI binary must not observe successfully");
        let cause = err.to_failure_cause();

        assert_eq!(cause.code, FailureCode::CliPorcelainUnsupported);
        assert!(
            !cause.retryable,
            "a missing executable never fixes itself on retry: {cause:?}"
        );
    }
}

/// MAC2-04 (verificacion-mac-2.md): a real Mac run left TWO orphaned
/// `podman run` processes alive — still downloading 2.7 GB — minutes after
/// the wrapper had already declared the boot failed. `Child::kill()` only
/// ever reaches the DIRECT child (`/bin/sh safent`); a grandchild it
/// spawns in its own right is reparented to init and keeps running.
#[cfg(test)]
mod process_group_kill_tests {
    use std::process::{Child, Command, Stdio};
    use std::time::Duration;

    fn alive(pid: i32) -> bool {
        // SAFETY: signal 0 sends nothing — a pure existence/permission
        // probe, exactly libc::kill(2)'s documented purpose for this case.
        unsafe { libc::kill(pid, 0) == 0 }
    }

    fn spawn_with_group(script: &str) -> Child {
        use std::os::unix::process::CommandExt;
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg(script);
        cmd.stdin(Stdio::null());
        cmd.stdout(Stdio::null());
        cmd.stderr(Stdio::null());
        cmd.process_group(0);
        cmd.spawn().expect("spawn sh")
    }

    #[test]
    fn killing_the_group_also_kills_a_backgrounded_grandchild() {
        let pid_file = std::env::temp_dir().join(format!(
            "safent-grandchild-pid-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        // The parent shell backgrounds a long-running grandchild (standing
        // in for `podman run`/`podman pull`), records its pid, then waits
        // on it — exactly the shape `_ensure_seccomp`'s foreground `podman
        // run` and the new heartbeat-driven backgrounded `podman pull`
        // both have relative to the top-level `safent` process this
        // adapter spawns.
        let script = format!("sleep 30 & echo $! > {} ; wait", pid_file.display());
        let mut child = spawn_with_group(&script);
        let parent_pid = child.id() as i32;

        // Give the grandchild time to actually start and write its pid.
        let mut grandchild_pid: Option<i32> = None;
        for _ in 0..50 {
            if let Ok(text) = std::fs::read_to_string(&pid_file) {
                if let Ok(pid) = text.trim().parse::<i32>() {
                    grandchild_pid = Some(pid);
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let grandchild_pid = grandchild_pid.expect("grandchild must have written its own pid");
        let _ = std::fs::remove_file(&pid_file);

        assert!(alive(parent_pid), "parent should be alive before kill");
        assert!(
            alive(grandchild_pid),
            "grandchild should be alive before kill"
        );

        super::kill_child_group(&mut child);
        let _ = child.wait();

        // Group-kill is not necessarily instantaneous from the kernel's
        // perspective across two distinct pids — poll briefly instead of
        // asserting the very next instant.
        let mut grandchild_dead = false;
        for _ in 0..50 {
            if !alive(grandchild_pid) {
                grandchild_dead = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(
            !alive(parent_pid),
            "parent must be dead after kill_child_group"
        );
        assert!(
            grandchild_dead,
            "the backgrounded grandchild must be dead too — this is the whole point of MAC2-04's fix"
        );
    }
}

fn map_companion_health(raw: &str) -> CompanionHealth {
    match raw {
        "reachable" => CompanionHealth::Reachable,
        "unreachable" => CompanionHealth::Unreachable,
        _ => CompanionHealth::Unknown,
    }
}

fn map_daemon_health(raw: &str) -> DaemonHealth {
    match raw {
        "healthy" => DaemonHealth::Healthy,
        "unhealthy" => DaemonHealth::Unhealthy,
        _ => DaemonHealth::Unknown,
    }
}

fn map_machine_provider(raw: &str) -> MachineProvider {
    match raw {
        "applehv" => MachineProvider::AppleHv,
        "qemu" => MachineProvider::Qemu,
        "hyperv" => MachineProvider::HyperV,
        "wsl" => MachineProvider::Wsl,
        other => MachineProvider::Other(other.to_string()),
    }
}

fn map_machine_fact(wire: WireMachineFact) -> MachineFact {
    MachineFact {
        name: MachineName(wire.name),
        provider: map_machine_provider(&wire.provider),
        rootful: wire.rootful,
        running: wire.running,
        ours: wire.ours,
        cpus: wire.cpus,
        memory_bytes: Bytes(wire.memory_bytes),
    }
}

fn map_container_fact(wire: WireContainerFact) -> ContainerFact {
    ContainerFact {
        exists: wire.exists,
        running: wire.running,
        image_digest: wire.image_digest,
    }
}

/// The CLI's `StageId` vocabulary is CLOSED (contract app-engine.md §3) — an
/// id this adapter does not recognize is a protocol violation, not a fact to
/// shrug off with a default.
fn map_stage(raw: &str) -> Result<Stage, EngineError> {
    Ok(match raw {
        "preflight" => Stage::Preflight,
        "runtime_staging" => Stage::RuntimeStaging,
        "machine" => Stage::Machine,
        "pull_engine" => Stage::PullEngine,
        "pull_companion" => Stage::PullCompanion,
        "container" => Stage::Container,
        "health" => Stage::Health,
        "companion_scaffold" => Stage::CompanionScaffold,
        "companion_up" => Stage::CompanionUp,
        "companion_reload" => Stage::CompanionReload,
        "backup" => Stage::Backup,
        "restore" => Stage::Restore,
        "cleanup" => Stage::Cleanup,
        other => return Err(EngineError::Protocol(format!("unknown stage id: {other}"))),
    })
}

fn map_progress_unit(raw: &str) -> Result<ProgressUnit, EngineError> {
    Ok(match raw {
        "bytes" => ProgressUnit::Bytes,
        "layers" => ProgressUnit::Layers,
        "steps" => ProgressUnit::Steps,
        other => {
            return Err(EngineError::Protocol(format!(
                "unknown progress unit: {other}"
            )))
        }
    })
}

/// The CLI's `FailureCode` vocabulary is CLOSED (contract app-engine.md §3,
/// 20 codes) — same fail-closed rule as `map_stage`.
fn map_failure_code(raw: &str) -> Result<FailureCode, EngineError> {
    Ok(match raw {
        "unsupported_os" => FailureCode::UnsupportedOs,
        "unsupported_arch" => FailureCode::UnsupportedArch,
        "insufficient_disk" => FailureCode::InsufficientDisk,
        "insufficient_memory" => FailureCode::InsufficientMemory,
        "runtime_hash_mismatch" => FailureCode::RuntimeHashMismatch,
        "machine_create_failed" => FailureCode::MachineCreateFailed,
        "machine_start_failed" => FailureCode::MachineStartFailed,
        "userns_blocked" => FailureCode::UsernsBlocked,
        "helper_denied" => FailureCode::HelperDenied,
        "registry_unreachable" => FailureCode::RegistryUnreachable,
        "digest_mismatch" => FailureCode::DigestMismatch,
        "pull_interrupted" => FailureCode::PullInterrupted,
        "port_exhausted" => FailureCode::PortExhausted,
        "container_start_failed" => FailureCode::ContainerStartFailed,
        "daemon_unhealthy" => FailureCode::DaemonUnhealthy,
        "companion_network_conflict" => FailureCode::CompanionNetworkConflict,
        "companion_migration_failed" => FailureCode::CompanionMigrationFailed,
        "companion_unreachable" => FailureCode::CompanionUnreachable,
        "backup_failed" => FailureCode::BackupFailed,
        "restore_failed" => FailureCode::RestoreFailed,
        "clock_skew" => FailureCode::ClockSkew,
        other => {
            return Err(EngineError::Protocol(format!(
                "unknown failure code: {other}"
            )))
        }
    })
}

// ---------------------------------------------------------------------------
// Secret-fd delivery (contract app-engine.md §5). Unix only — Windows is out
// of scope for spec 028 (research.md "Decisión: Windows").
// ---------------------------------------------------------------------------

#[cfg(unix)]
mod secret_pipe {
    use std::io::Read;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
    use std::os::unix::process::CommandExt;
    use std::process::Command;

    use crate::ports::EngineError;

    /// A pipe whose write end lands on fd 3 in the child (contract's default
    /// `--secret-fd 3`) and whose read end the PARENT keeps — and ONLY the
    /// parent's own copy of the write end is closed by `into_reader`, which
    /// is what lets `read()` see EOF once the child is done with it.
    pub struct SecretPipe {
        read_fd: OwnedFd,
        write_fd: RawFd,
    }

    /// A pipe with both ends close-on-exec, portably. `pipe2(2)` (atomic —
    /// no window between creating the fds and marking them CLOEXEC) exists
    /// on Linux only; macOS's libc has no `pipe2` at all (confirmed by the
    /// real cross-compile failure this fixed: `E0425: cannot find function
    /// 'pipe2' in crate 'libc'` building for aarch64-apple-darwin). Every
    /// other Unix target this could ever run on (research.md "Decisión:
    /// Windows" scopes this module to Unix; spec 028 itself scopes the
    /// product to macOS + Linux) gets the same POSIX-standard fallback:
    /// `pipe(2)` then `fcntl(F_SETFD, FD_CLOEXEC)` on each fd immediately
    /// after — the same substitute glibc's own `pipe2` uses internally on
    /// platforms that lack the real syscall.
    fn cloexec_pipe() -> Result<[libc::c_int; 2], EngineError> {
        let mut fds: [libc::c_int; 2] = [0; 2];
        #[cfg(target_os = "linux")]
        // SAFETY: `fds` is a valid, correctly-sized out-param for pipe2(2).
        let rc = unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) };
        #[cfg(not(target_os = "linux"))]
        // SAFETY: `fds` is a valid, correctly-sized out-param for pipe(2).
        let rc = unsafe { libc::pipe(fds.as_mut_ptr()) };
        if rc != 0 {
            return Err(EngineError::Io(std::io::Error::last_os_error().to_string()));
        }
        #[cfg(not(target_os = "linux"))]
        for fd in fds {
            // SAFETY: `fd` is this process's own, just opened by the
            // successful pipe(2) above — F_SETFD/FD_CLOEXEC never blocks.
            if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
                let err = std::io::Error::last_os_error();
                // SAFETY: both fds are this process's own, opened above —
                // close whichever succeeded before failing closed.
                unsafe {
                    libc::close(fds[0]);
                    libc::close(fds[1]);
                }
                return Err(EngineError::Io(err.to_string()));
            }
        }
        Ok(fds)
    }

    impl SecretPipe {
        pub fn new() -> Result<Self, EngineError> {
            let fds = cloexec_pipe()?;
            // SAFETY: `fds[0]` was just returned by a successful pipe(2)/
            // pipe2(2) above and is not owned anywhere else yet.
            let read_fd = unsafe { OwnedFd::from_raw_fd(fds[0]) };
            Ok(Self {
                read_fd,
                write_fd: fds[1],
            })
        }

        /// Registers a `pre_exec` hook that lands the write end on fd 3 in the
        /// CHILD only — the parent's own fd table is untouched here.
        pub fn install(&self, cmd: &mut Command) {
            let write_fd = self.write_fd;
            let read_fd = self.read_fd.as_raw_fd();
            // SAFETY: runs in the child after fork(), before exec, single
            // threaded — only async-signal-safe calls (dup2/close/fcntl),
            // exactly what `pre_exec`'s contract requires.
            unsafe {
                cmd.pre_exec(move || {
                    if libc::dup2(write_fd, 3) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    // POSIX: if write_fd ALREADY happened to be 3 (a real,
                    // observed allocation under concurrent test load — fd
                    // numbers are a process-wide resource), dup2(3, 3) is a
                    // documented no-op that does NOT clear FD_CLOEXEC. Since
                    // this pipe was created with O_CLOEXEC (deliberately, to
                    // keep it from leaking into any OTHER child), fd 3 would
                    // then be silently closed by the kernel at THIS child's
                    // own execve() — the ticket would never reach the CLI,
                    // intermittently and only when that specific fd number
                    // was allocated. Clearing FD_CLOEXEC explicitly makes
                    // this correct whether dup2 just did a real copy or
                    // this no-op.
                    if libc::fcntl(3, libc::F_SETFD, 0) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    // Same reasoning for read_fd: if IT happened to be 3, the
                    // dup2 above already closed/replaced that slot as a side
                    // effect (POSIX: dup2 closes newfd before reusing the
                    // number, unless oldfd==newfd) — closing "read_fd" again
                    // here would then hit the FRESH write_fd copy that now
                    // lives at 3, not the original read end.
                    if write_fd != 3 {
                        libc::close(write_fd);
                    }
                    if read_fd != 3 {
                        libc::close(read_fd);
                    }
                    Ok(())
                });
            }
        }

        /// Consumes the pipe: closes the PARENT's own copy of the write end
        /// (never held open here past this point — else `read()` below would
        /// wait for a closure that this very process is preventing) and
        /// returns the read end as a boxed reader for the generic stream loop.
        pub fn into_reader(self) -> Box<dyn Read + Send + 'static> {
            // SAFETY: `self.write_fd` is this process's own valid fd, not
            // used anywhere else after this point.
            unsafe { libc::close(self.write_fd) };
            Box::new(std::fs::File::from(self.read_fd))
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn is_cloexec(fd: libc::c_int) -> bool {
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
            assert!(
                flags >= 0,
                "F_GETFD failed: {}",
                std::io::Error::last_os_error()
            );
            flags & libc::FD_CLOEXEC != 0
        }

        #[test]
        fn cloexec_pipe_creates_two_distinct_fds_both_marked_cloexec() {
            let fds = cloexec_pipe().expect("pipe creation should succeed");
            assert_ne!(fds[0], fds[1]);
            assert!(is_cloexec(fds[0]), "read end must be close-on-exec");
            assert!(is_cloexec(fds[1]), "write end must be close-on-exec");
            unsafe {
                libc::close(fds[0]);
                libc::close(fds[1]);
            }
        }

        #[test]
        fn cloexec_pipe_actually_carries_bytes_from_write_end_to_read_end() {
            let fds = cloexec_pipe().expect("pipe creation should succeed");
            let payload = b"http://127.0.0.1:17517/?k=test-ticket";
            let written = unsafe {
                libc::write(
                    fds[1],
                    payload.as_ptr() as *const libc::c_void,
                    payload.len(),
                )
            };
            assert_eq!(written, payload.len() as isize);
            unsafe { libc::close(fds[1]) };

            let mut buf = vec![0u8; payload.len()];
            let read =
                unsafe { libc::read(fds[0], buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
            assert_eq!(read, payload.len() as isize);
            assert_eq!(&buf, payload);
            unsafe { libc::close(fds[0]) };
        }
    }
}

#[cfg(not(unix))]
mod secret_pipe {
    use std::io::Read;
    use std::process::Command;

    use crate::ports::EngineError;

    /// Not implemented outside Unix — Windows is out of scope for spec 028
    /// (research.md "Decisión: Windows"). `new()` fails closed so a build on
    /// another target can never silently skip ticket delivery.
    pub struct SecretPipe;

    impl SecretPipe {
        pub fn new() -> Result<Self, EngineError> {
            Err(EngineError::Io(
                "secret-fd delivery is only implemented on Unix targets".to_string(),
            ))
        }

        pub fn install(&self, _cmd: &mut Command) {}

        pub fn into_reader(self) -> Box<dyn Read + Send + 'static> {
            Box::new(std::io::empty())
        }
    }
}

use secret_pipe::SecretPipe;
