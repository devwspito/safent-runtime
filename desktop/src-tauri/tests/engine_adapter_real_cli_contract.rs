//! Field-for-field parity between `engine_adapter.rs`'s wire structs and the
//! REAL `safent` CLI at the repo root — deliberately NOT the hand-rolled
//! fake script `engine_adapter_cli_contract.rs` uses (that one only proves
//! the adapter parses whatever a Rust developer assumed the CLI emits; it
//! stayed green while the real CLI's camelCase `facts` body and missing
//! `localEngineImageDigest`/`localCompanionImageDigest` would have made
//! every real bootstrap loop forever on `ensure-images` without ever
//! reaching `engine_ready` — see the integration report).
//!
//! Only `podman` is faked (same technique + same case branches as
//! `tests/unit/cli/test_safent_porcelain.py`'s `_FAKE_PODMAN`, the CLI
//! suite's own real-script contract test) — no container, no network, no
//! real podman (Constitution Principle V: base tests never touch
//! containers/network).

#[allow(dead_code)]
#[path = "../src/domain.rs"]
mod domain;
#[allow(dead_code)]
#[path = "../src/engine_adapter.rs"]
mod engine_adapter;
#[allow(dead_code)]
#[path = "../src/ports.rs"]
mod ports;

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use domain::{HostOs, ImageRef, Port, RepairAction};
use engine_adapter::{EmbeddedCliConfig, EmbeddedCliDriver};
use ports::fakes::RecordingNotifier;
use ports::{ApplyOutcome, CancelSignal, EngineDriver, EngineProbe};

/// `Command::spawn` (engine_adapter.rs) inherits the CURRENT PROCESS's env
/// and overlays only a fixed set of `SAFENT_*` vars — reaching the fake
/// podman's `FAKE_*` knobs means setting REAL process env vars, which is
/// unsound across concurrent test threads. Every test locks this for its
/// whole body (std::env::set_var itself requires `unsafe` since it is not
/// thread-safe in general — the mutex is what makes it safe HERE).
static ENV_LOCK: Mutex<()> = Mutex::new(());

static COUNTER: AtomicU32 = AtomicU32::new(0);

fn unique_dir(label: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system clock before UNIX_EPOCH")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "safent-real-cli-{}-{nanos}-{n}-{label}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).expect("create unique test dir");
    dir
}

/// The real CLI, `desktop/src-tauri/tests/../../../safent` — asserts it
/// exists rather than skip, so a missing script fails LOUDLY (this test
/// proving nothing silently would defeat its entire purpose).
fn real_safent() -> PathBuf {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("desktop/src-tauri has repo-root two levels up")
        .join("safent");
    assert!(
        path.is_file(),
        "expected the real CLI at {path:?} — engine_adapter_real_cli_contract \
         has nothing to verify against without it"
    );
    path
}

/// Same case branches as `tests/unit/cli/test_safent_porcelain.py`'s
/// `_FAKE_PODMAN`, plus one new `image exists` branch for
/// `localEngineImageDigest`/`localCompanionImageDigest` (this integration
/// pass's own fix — see the safent script's `cmd_facts`).
const FAKE_PODMAN: &str = r#"#!/bin/sh
set -e
echo "$@" >> "$FAKE_PODMAN_LOG"

case "$1" in
  inspect)
    shift
    # BKP-01: the CLI pins `inspect --type container`; accept the flag pair.
    if [ "$1" = "--type" ]; then shift 2; fi
    if [ "$1" = "-f" ]; then
      case "$2" in
        '{{.State.Running}}')
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          echo "$FAKE_CONTAINER_RUNNING"; exit 0 ;;
        '{{.ImageDigest}}')
          # MAC3-02 (verificacion-mac-3.md): the REAL per-container digest
          # field (confirmed against real podman 6.1.1) — the OLD fake had
          # `{{.Image}}` (the local image ID field) answer with a
          # sha256-shaped value, exactly the wrong assumption that let the
          # pre-fix bug (safent used `{{.Image}}` for this) pass every
          # test while failing on a real Mac.
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          echo "sha256:$FAKE_IMAGE_DIGEST"; exit 0 ;;
        '{{.Image}}')
          # The REAL local image ID — bare hex, never "sha256:"-prefixed.
          [ "$FAKE_CONTAINER_EXISTS" = "true" ] || exit 1
          echo "${FAKE_CONTAINER_IMAGE_ID:-fakelocalimageid0000000000000000000000000000000000000000000}"; exit 0 ;;
      esac
      exit 0
    fi
    [ "$FAKE_CONTAINER_EXISTS" = "true" ] && exit 0 || exit 1
    ;;
  image)
    [ "$2" = "exists" ] || exit 0
    [ "$FAKE_IMAGE_LOCAL" = "true" ] && exit 0 || exit 1
    ;;
  volume)
    case "$2" in
      exists) [ "$FAKE_VOLUME_EXISTS" = "true" ] && exit 0 || exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
  port)
    echo "0.0.0.0:$FAKE_PORT"
    exit 0
    ;;
  exec)
    shift 2
    case "$1" in
      systemctl)
        [ "$FAKE_HEALTH_ACTIVE" = "true" ] && echo active || echo failed
        exit 0 ;;
      cat)
        [ "$FAKE_HEALTH_ACTIVE" = "true" ] && printf '%s' "$FAKE_SECRET"
        exit 0 ;;
      python3)
        printf '%s' "$FAKE_APP_VERSION"; exit 0 ;;
      *) exit 0 ;;
    esac
    ;;
  run)
    case " $* " in
      *" --entrypoint cat "*) printf '#!/bin/sh\nexit 0\n' ;;
    esac
    exit 0
    ;;
  pull)
    [ "${FAKE_PULL_FAILS:-false}" = "true" ] && exit 1
    exit 0
    ;;
  rm|start|stop)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"#;

fn write_fake_podman(dir: &Path) -> PathBuf {
    let path = dir.join("podman");
    std::fs::write(&path, FAKE_PODMAN).expect("write fake podman");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    write_private_build_marker(&path);
    path
}

fn write_private_build_marker(path: &Path) {
    let output = std::process::Command::new("sh")
        .args(["-c", "if command -v sha256sum >/dev/null; then sha256sum \"$1\"; else shasum -a 256 \"$1\"; fi", "sha"])
        .arg(path).output().unwrap();
    assert!(output.status.success());
    let digest = String::from_utf8(output.stdout).unwrap();
    let marker = serde_json::json!({
        "schema_version": 1, "capability": "safent-private-machine-v1",
        "source_commit": "8303f2e25b675ea7f82099d615c60969aec15870",
        "patch_sha256": "a".repeat(64),
        "binary_sha256": digest.split_whitespace().next().unwrap()
    });
    std::fs::write(
        path.parent().unwrap().join("podman-private-build.json"),
        serde_json::to_string_pretty(&marker).unwrap(),
    )
    .unwrap();
}

/// MAC-01: the real CLI's `cmd_facts` derives `os_id` from its OWN `uname -s`
/// call (`safent:126,1648`), not from anything the adapter controls — the
/// only way to prove the adapter parses a REAL macOS observation (not a
/// hand-rolled assumption) on this Linux CI host is to intercept `uname`
/// itself ahead of the real ones on `PATH`. Answers both call shapes the
/// script makes (`-s` for the OS, `-m` for the arch, `safent:1649`).
fn write_fake_uname(dir: &Path) -> PathBuf {
    let path = dir.join("uname");
    std::fs::write(
        &path,
        "#!/bin/sh\ncase \"$1\" in\n  -s) echo Darwin ;;\n  -m) echo arm64 ;;\n  *) echo Darwin ;;\nesac\n",
    )
    .expect("write fake uname");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    path
}

/// # Safety
/// Caller must hold `ENV_LOCK` for the duration of every effect this env
/// setup drives (spawning `safent`, reading its result) — see `ENV_LOCK`'s
/// own doc comment for why.
unsafe fn set_env(key: &str, value: &str) {
    unsafe { std::env::set_var(key, value) };
}

struct Fixture {
    home_dir: PathBuf,
    podman_path: PathBuf,
    podman_log: PathBuf,
}

/// Sets every `FAKE_*` knob to a healthy, fully-converged steady state
/// (container exists+running+correct digest, volume present, companion
/// health unknown/absent, daemon healthy) — the exact facts a REOPENED,
/// already-bootstrapped Safent observes. Individual tests override specific
/// knobs afterward for their own scenario. Must run with `ENV_LOCK` held.
fn healthy_fixture(label: &str, image_digest: &str) -> Fixture {
    let home_dir = unique_dir(&format!("{label}-home"));
    let bin_dir = unique_dir(&format!("{label}-bin"));
    let podman_path = write_fake_podman(&bin_dir);
    let podman_log = unique_dir(&format!("{label}-log")).join("podman.log");

    // SAFETY: every call site holds ENV_LOCK for its whole test body.
    unsafe {
        set_env("FAKE_PODMAN_LOG", podman_log.to_str().unwrap());
        set_env("FAKE_CONTAINER_EXISTS", "true");
        set_env("FAKE_CONTAINER_RUNNING", "true");
        set_env("FAKE_VOLUME_EXISTS", "true");
        set_env("FAKE_HEALTH_ACTIVE", "true");
        set_env("FAKE_PORT", "17517");
        set_env("FAKE_IMAGE_DIGEST", image_digest);
        set_env("FAKE_IMAGE_LOCAL", "true");
        set_env("FAKE_APP_VERSION", "0.8.42");
        set_env("FAKE_SECRET", "s3cr3t-token-do-not-leak");
        set_env("FAKE_PULL_FAILS", "false");
    }

    Fixture {
        home_dir,
        podman_path,
        podman_log,
    }
}

fn config(fx: &Fixture, engine_digest: &str) -> EmbeddedCliConfig {
    let mut cfg = EmbeddedCliConfig::with_defaults(
        real_safent(),
        fx.podman_path.clone(),
        fx.home_dir.join(".safent"),
        ImageRef::new(
            "ghcr.io/devwspito/safent",
            format!("sha256:{engine_digest}"),
        )
        .unwrap(),
        None,
    );
    cfg.stall_timeout = std::time::Duration::from_secs(5);
    cfg.hard_timeout = std::time::Duration::from_secs(30);
    cfg
}

fn podman_calls(fx: &Fixture) -> Vec<String> {
    std::fs::read_to_string(&fx.podman_log)
        .unwrap_or_default()
        .lines()
        .map(str::to_string)
        .collect()
}

#[test]
fn real_facts_of_a_converged_engine_deserializes_field_for_field() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("converged", "engine-good");
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));

    let facts = driver
        .observe()
        .expect("the REAL safent facts --json --porcelain must parse into HostFacts");

    // NOT asserting runtime_staged/runtime_hash_ok here: this dev copy of
    // `safent` (run straight from the repo root, no app bundle next to it)
    // has no runtime-bundle.json, so stage-runtime — and therefore these two
    // fields — never leaves `false`, by design (mirrors the CLI's own
    // `test_without_a_bundle_manifest_is_a_harmless_noop`). That sequence is
    // covered by this file's stage-runtime/ensure-images test below; this
    // one's job is the digest/container fields.
    assert_eq!(facts.os, HostOs::Linux);
    let container = facts
        .engine_container
        .as_ref()
        .expect("engineContainer is ALWAYS an object on the real wire, never absent");
    assert!(container.exists);
    assert!(container.running);
    assert_eq!(
        container.image_digest.as_deref(),
        Some("sha256:engine-good")
    );
    // THE regression this test exists for: before this integration pass,
    // this field either failed to deserialize at all (wrong case) or was
    // always None (no CLI producer) — reconcile::images_gap would have
    // returned PullEngine forever even though the container above is
    // already running the exact desired digest.
    assert_eq!(
        facts.local_engine_image_digest.as_deref(),
        Some("sha256:engine-good"),
        "images_gap() depends on this to ever stop asking for a pull"
    );
    assert_eq!(facts.published_port, Some(Port(17517)));
    assert!(facts.data_volume);
    assert_eq!(facts.app_version.as_str(), "0.8.42");
    assert!(!facts.another_instance_running);
}

/// MAC3-02 (verificacion-mac-3.md): `inspect -f '{{.Image}}'` (what
/// `cmd_facts` used to read) returns podman's LOCAL IMAGE ID, never a
/// digest — confirmed against real podman 6.1.1, not assumed — so
/// `reconcile::images_gap` compared an ID against `desired.engine_image.
/// digest` (always `sha256:...`) and could NEVER match: a perfectly
/// healthy, correctly-digested container was destroyed and recreated on
/// EVERY single observation. The real-CLI contract test this replaces
/// (`real_facts_of_a_converged_engine_deserializes_field_for_field`) could
/// not have caught this on its own: its fake podman ALSO answered
/// `{{.Image}}` with a sha256-shaped value (the exact wrong assumption),
/// so it stayed green while production failed — this test sets a
/// DISTINCT, clearly-not-a-digest FAKE_CONTAINER_IMAGE_ID specifically to
/// prove the adapter never receives it as `image_digest`.
#[test]
fn real_facts_reports_the_container_image_digest_never_the_local_image_id() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("digest-not-id", "engine-good");
    // SAFETY: ENV_LOCK held for the whole test body (see healthy_fixture's
    // own doc comment) — a value that would never pass for a digest.
    unsafe {
        set_env(
            "FAKE_CONTAINER_IMAGE_ID",
            "totallydifferentlocalimageid00000000000000000000000000000000",
        );
    }
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));

    let facts = driver
        .observe()
        .expect("the REAL safent facts --json --porcelain must parse into HostFacts");

    let container = facts
        .engine_container
        .as_ref()
        .expect("engineContainer is ALWAYS an object on the real wire");
    assert_eq!(
        container.image_digest.as_deref(),
        Some("sha256:engine-good"),
        "must be the real digest — never the local image id, regardless of what {{{{.Image}}}} reports"
    );
}

/// MAC-01 (verificacion-mac-1.md): the real `safent facts --json --porcelain`
/// on an actual Mac reports `"os":"darwin"` (`cmd_facts`'s own `Darwin) os_id=
/// darwin` branch) — before this pass the adapter's `map_os` only accepted
/// `"macos"`, a string the CLI never emits, so `HostOs::Unsupported` reached
/// `reconcile::preflight_violation` and the engine never started on the
/// owner's MacBook Air. Faking `uname` (not the adapter, not the CLI's
/// output) is what makes this a REAL-CLI proof rather than a restatement of
/// the fix: the script itself decides `os_id` from `uname -s`, exactly as it
/// would on the real hardware.
#[test]
fn real_facts_report_macos_when_uname_reports_darwin() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("darwin-facts", "engine-good");
    let uname_dir = unique_dir("darwin-facts-uname");
    write_fake_uname(&uname_dir);
    let original_path = std::env::var("PATH").unwrap_or_default();
    // SAFETY: ENV_LOCK held (see healthy_fixture's own comment) — restored
    // below before releasing the lock so no other test observes a Darwin
    // `uname` on this Linux host.
    unsafe {
        set_env("PATH", &format!("{}:{original_path}", uname_dir.display()));
    }

    let result = EmbeddedCliDriver::new(config(&fx, "engine-good")).observe();

    // SAFETY: ENV_LOCK still held.
    unsafe {
        set_env("PATH", &original_path);
    }

    let facts = result.expect("a fake-macOS facts observation must still parse");
    assert_eq!(facts.os, HostOs::MacOs);
}

#[test]
fn real_facts_of_a_fresh_install_reports_null_app_version_and_no_container_without_crashing() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("fresh", "engine-good");
    // SAFETY: ENV_LOCK held (see healthy_fixture's own comment).
    unsafe {
        set_env("FAKE_CONTAINER_EXISTS", "false");
        set_env("FAKE_IMAGE_LOCAL", "false");
    }
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));

    let facts = driver
        .observe()
        .expect("appVersion:null and an absent container must not fail observe()");

    let container = facts.engine_container.as_ref().expect("always an object");
    assert!(!container.exists);
    assert!(!container.running);
    assert_eq!(container.image_digest, None);
    assert_eq!(facts.local_engine_image_digest, None);
    assert_eq!(facts.published_port, None);
    // "0.0.0.0" placeholder for the real wire's `appVersion: null` — proven
    // by NOT erroring above, not by a specific value here.
    assert_eq!(facts.app_version.as_str(), "0.0.0");
}

#[test]
fn real_local_engine_image_digest_is_null_when_podman_image_exists_says_no() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("not-pulled", "engine-good");
    // SAFETY: ENV_LOCK held.
    unsafe {
        set_env("FAKE_CONTAINER_EXISTS", "false");
        set_env("FAKE_IMAGE_LOCAL", "false"); // pulled nothing yet
    }
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));
    let facts = driver.observe().expect("observe should still succeed");
    assert_eq!(
        facts.local_engine_image_digest, None,
        "podman image exists reporting absent must surface as null, not a stale digest"
    );
}

#[test]
fn real_machine_conflict_is_a_terminal_failure_not_bootstrap_progress() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("machine-conflict", "engine-good");
    std::fs::write(&fx.podman_path, r#"#!/bin/sh
echo "$@" >> "$FAKE_PODMAN_LOG"
case "$1 $2" in
  'machine inspect')
    case "$5" in
      '{{.Rootful}}') echo true ;;
      '{{.State}}') echo stopped ;;
    esac
    exit 0 ;;
  'machine start')
    echo 'podman-machine-default already starting or running on the libkrun provider: only one VM can be active at a time' >&2
    exit 1 ;;
esac
# The unrelated default daemon answers info successfully.
exit 0
"#).unwrap();
    write_private_build_marker(&fx.podman_path);
    let uname_dir = unique_dir("machine-conflict-uname");
    write_fake_uname(&uname_dir);
    let original_path = std::env::var("PATH").unwrap_or_default();
    // SAFETY: ENV_LOCK held; restored before inspecting the result.
    unsafe {
        set_env("PATH", &format!("{}:{original_path}", uname_dir.display()));
    }
    let result = EmbeddedCliDriver::new(config(&fx, "engine-good")).apply(
        &RepairAction::CreateMachine,
        &RecordingNotifier::new(),
        &CancelSignal::new(),
    );
    unsafe {
        set_env("PATH", &original_path);
    }
    match result {
        Err(ports::EngineError::Reported(cause)) => {
            assert_eq!(cause.code, domain::FailureCode::MachineStartFailed);
            assert!(!cause.retryable);
            assert!(cause.message.contains("explicitamente"));
        }
        other => panic!("expected a non-retryable machine failure, got {other:?}"),
    }
    let calls = podman_calls(&fx);
    assert_eq!(
        calls
            .iter()
            .filter(|c| c.starts_with("machine start "))
            .count(),
        1
    );
    assert!(!calls
        .iter()
        .any(|c| c == "info" || c.starts_with("machine stop ")));
}

#[test]
fn real_stage_runtime_ensure_machine_ensure_images_are_contract_shaped_ndjson() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("verbs", "engine-good");
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));

    let notifier = RecordingNotifier::new();
    let cancel = CancelSignal::new();

    // stage-runtime: no bundle manifest next to this dev copy of `safent` ⇒
    // a harmless stage/done pair (contract app-engine.md §4: idempotent).
    driver
        .apply(&RepairAction::StageRuntime, &notifier, &cancel)
        .expect("stage-runtime must parse as Progressed against the real CLI");

    // ensure-machine: Linux ⇒ a no-op stage/done pair, no podman machine call.
    driver
        .apply(&RepairAction::CreateMachine, &notifier, &cancel)
        .expect("ensure-machine must parse against the real CLI");

    // ensure-images: real `podman pull` invocation via the fake.
    let image = ImageRef::new("ghcr.io/devwspito/safent", "sha256:engine-good").unwrap();
    driver
        .apply(&RepairAction::PullEngine(image), &notifier, &cancel)
        .expect("ensure-images must parse against the real CLI");
    assert!(
        podman_calls(&fx).iter().any(|c| c.starts_with("pull ")),
        "ensure-images should have actually called podman pull"
    );

    let events = notifier.events();
    assert!(
        events
            .iter()
            .any(|e| matches!(e, domain::DomainEvent::StageCompleted { .. })),
        "{events:?}"
    );
}

/// MAC2-05 (verificacion-mac-2.md): `cmd_up` now curls `/healthz` on the
/// published port from the HOST before ever declaring ready — a minimal
/// raw TCP listener (no HTTP framework dependency needed for one fixed
/// 200 response) standing in for the real product's own `/healthz`, on
/// the exact port `FAKE_PORT`/`healthy_fixture` already pins (17517).
/// Accepts connections on a background thread until `stop` is signaled,
/// so the CLI's retries (if any) keep succeeding rather than hitting a
/// closed port after the first request.
fn spawn_healthz_listener(
    port: u16,
) -> (
    std::thread::JoinHandle<()>,
    std::sync::Arc<std::sync::atomic::AtomicBool>,
) {
    let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind fake healthz port");
    listener
        .set_nonblocking(true)
        .expect("nonblocking so the accept loop can observe the stop flag");
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let stop_for_thread = stop.clone();
    let handle = std::thread::spawn(move || {
        while !stop_for_thread.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((stream, _)) => handle_healthz_connection(stream),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(_) => break,
            }
        }
    });
    (handle, stop)
}

fn handle_healthz_connection(mut stream: TcpStream) {
    let mut buf = [0u8; 512];
    let _ = stream.read(&mut buf); // drain the request line; contents unchecked, only one route exists here
    let body = b"{\"status\":\"ok\"}";
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.write_all(body);
    let _ = stream.flush();
}

#[test]
fn real_up_delivers_a_working_ticket_over_the_secret_fd() {
    let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let fx = healthy_fixture("up", "engine-good");
    let driver = EmbeddedCliDriver::new(config(&fx, "engine-good"));
    let notifier = RecordingNotifier::new();
    let (healthz_thread, healthz_stop) = spawn_healthz_listener(17517);

    let outcome = driver.apply(
        &RepairAction::CreateContainer,
        &notifier,
        &CancelSignal::new(),
    );
    healthz_stop.store(true, Ordering::SeqCst);
    let _ = healthz_thread.join();
    let outcome = outcome.expect("up --secret-fd 3 --porcelain must succeed against the real CLI");

    match outcome {
        ApplyOutcome::Ready(ticket) => {
            assert_eq!(
                ticket.expose(),
                "http://127.0.0.1:17517/?k=s3cr3t-token-do-not-leak"
            );
        }
        ApplyOutcome::Progressed => panic!("expected Ready — up should have delivered a ticket"),
    }
    for event in notifier.events() {
        assert!(!format!("{event:?}").contains("do-not-leak"));
    }
}
