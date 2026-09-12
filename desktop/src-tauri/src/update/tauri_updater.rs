//! Adapter primitives consumed by native.rs: reshape Tauri metadata and verify
//! the signed runtime manifest. Fetch/install/relaunch live in native.rs.
//!
//! Two manifests, two different verification paths, by design (contracts/
//! update.md §1-2):
//! - `latest.json` (the app piece): `Updater::check()` fetches release metadata
//!   and compares versions. It does NOT verify a signature of latest.json.
//!   The plugin verifies the downloaded artifact in `Update::download()`.
//!   `tauri_manifest_from_check` only reshapes metadata; it cannot authorize
//!   installation. Native app-only and any future UpdatePorts complete verified
//!   download before backup/apply, never install raw metadata URLs directly.
//! - `runtime-manifest.json` (engine + companion digests): the plugin has no
//!   concept of this file — it is ours, so WE verify it, with the same
//!   minisign public key embedded in `tauri.conf.json` (`plugins.updater.
//!   pubkey`). A signature that does not verify is fail-closed: no
//!   `RuntimeManifest`, so `plan_update` never runs and there is no button
//!   (Constitution Principle IV; contracts/update.md §2 invariant 3).

use super::types::{RuntimeManifest, TauriManifest, TauriManifestPlatform, UpdateFailure};
use base64::Engine;
use minisign_verify::{PublicKey, Signature};
use std::collections::HashMap;

/// The app-platform key `latest.json` is indexed by (`darwin-aarch64`, ...).
/// Re-exported from the plugin instead of reimplemented: it is the ONE place
/// that already encodes Tauri's own `{os}-{arch}` convention correctly
/// (notably `"darwin"`, not `std::env::consts::OS`'s `"macos"`).
pub fn app_platform_key() -> Option<String> {
    tauri_plugin_updater::target()
}

/// The fields of `tauri_plugin_updater::Update` that `tauri_manifest_from_check`
/// needs — named individually (not the whole `Update`) so this conversion
/// stays a pure function callable from a unit test without a live `AppHandle`.
pub struct CheckedAppUpdate {
    pub version: String,
    pub signature: String,
    pub download_url: String,
}

/// Reshapes the plugin's release metadata from `check()` into `plan.rs`'s
/// `TauriManifest` — a single-entry manifest for OUR platform when something
/// is newer, or an empty one (matching "platform absent" in `plan.rs`, which
/// already means "no app piece") when `check()` found nothing.
pub fn tauri_manifest_from_check(
    checked: Option<CheckedAppUpdate>,
    platform_key: &str,
    current_app_version: &semver::Version,
) -> Result<TauriManifest, UpdateFailure> {
    match checked {
        None => Ok(TauriManifest {
            version: current_app_version.clone(),
            platforms: HashMap::new(),
        }),
        Some(update) => {
            let version = semver::Version::parse(&update.version)
                .map_err(|_| UpdateFailure::ManifestUnverified)?;
            let mut platforms = HashMap::new();
            platforms.insert(
                platform_key.to_string(),
                TauriManifestPlatform {
                    signature: update.signature,
                    url: update.download_url,
                },
            );
            Ok(TauriManifest { version, platforms })
        }
    }
}

/// Verifies + parses `runtime-manifest.json` against the SAME public key
/// embedded in `tauri.conf.json` (`plugins.updater.pubkey`). Fail-closed: any
/// verification or parse failure returns `Err`, never a best-effort manifest.
pub struct RuntimeManifestVerifier {
    pubkey: PublicKey,
}

impl RuntimeManifestVerifier {
    /// Tauri's updater embeds base64 of the WHOLE .pub file (comment + key),
    /// not the 42-byte key line accepted by `new`. Never conflate the formats.
    pub fn from_tauri_pubkey(encoded_file: &str) -> Result<Self, UpdateFailure> {
        let text = decode_tauri_text(encoded_file)?;
        let pubkey = PublicKey::decode(&text).map_err(|_| UpdateFailure::ManifestUnverified)?;
        Ok(Self { pubkey })
    }
    /// A raw minisign public-key line. For `plugins.updater.pubkey` use
    /// `from_tauri_pubkey`: Tauri encodes the whole .pub file, not this line.
    pub fn new(pubkey_b64: &str) -> Result<Self, UpdateFailure> {
        let pubkey =
            PublicKey::from_base64(pubkey_b64).map_err(|_| UpdateFailure::ManifestUnverified)?;
        Ok(Self { pubkey })
    }

    /// `body`: the raw bytes of `runtime-manifest.json` as downloaded.
    /// `signature_text`: the full contents of the sibling `.minisig` file.
    pub fn verify_and_parse(
        &self,
        body: &[u8],
        signature_text: &str,
    ) -> Result<RuntimeManifest, UpdateFailure> {
        let signature =
            Signature::decode(signature_text).map_err(|_| UpdateFailure::ManifestUnverified)?;
        self.pubkey
            .verify(body, &signature, false)
            .map_err(|_| UpdateFailure::ManifestUnverified)?;
        serde_json::from_slice(body).map_err(|_| UpdateFailure::ManifestUnverified)
    }
}

fn decode_tauri_text(value: &str) -> Result<String, UpdateFailure> {
    if value.len() > 4096 {
        return Err(UpdateFailure::ManifestUnverified);
    }
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(value)
        .map_err(|_| UpdateFailure::ManifestUnverified)?;
    String::from_utf8(bytes).map_err(|_| UpdateFailure::ManifestUnverified)
}

pub fn tauri_signature_well_formed(value: &str) -> bool {
    decode_tauri_text(value)
        .ok()
        .is_some_and(|text| Signature::decode(&text).is_ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Generated once with `minisign -G` + `minisign -S` (see T014 dossier) —
    // a REAL keypair and a REAL signature over the payload below, not a
    // hand-typed fixture standing in for one.
    const TEST_PUBKEY: &str = "RWT/DhbH+Js0LLsDbBHk04vOWy8yegcD+AYxhHFhiD3HlNqFuI5hpd7N";
    // Trailing `\n` matters: it is part of the exact bytes that were signed
    // (the fixture file `echo`-ed into existence, which appends one).
    const TEST_PAYLOAD: &[u8] =
        b"{\"schema_version\":1,\"version\":\"0.2.0\",\"engine\":{\"linux/arm64\":\"sha256:deadbeef\"},\"companion\":{},\"min_app_version\":\"0.2.0\"}\n";
    const TEST_SIGNATURE: &str = "untrusted comment: test fixture\nRUT/DhbH+Js0LOvwmxeLZ7me+l8X12aZ+vtJ9Bz65Fjs/qpyQs+FRh0SlzT+Un7YmwyJBCoYoXrI2bgFf4EtADl61DeUhogCwAw=\ntrusted comment: test fixture\nfbZbRTRYq/L6plES8gHMfp6sCaYW0MTkUPC4e69PiJWWsT8d+3C4uM8S+WVMWBva03o8jZfrL1mxNbA5pWOBCw==\n";

    #[test]
    fn tauri_encoded_file_key_verifies_same_exact_runtime_bytes() {
        let encoded = base64::engine::general_purpose::STANDARD
            .encode(format!("untrusted comment: test key\n{TEST_PUBKEY}\n"));
        let verifier = RuntimeManifestVerifier::from_tauri_pubkey(&encoded).unwrap();
        assert!(verifier
            .verify_and_parse(TEST_PAYLOAD, TEST_SIGNATURE)
            .is_ok());
        assert!(RuntimeManifestVerifier::from_tauri_pubkey(TEST_PUBKEY).is_err());
        assert!(tauri_signature_well_formed(
            &base64::engine::general_purpose::STANDARD.encode(TEST_SIGNATURE)
        ));
        assert!(!tauri_signature_well_formed(TEST_SIGNATURE));
        assert!(!tauri_signature_well_formed("garbage"));
    }

    /// Real plugin fetch+signature verification, with only a loopback fixture
    /// server and fake AppHandle. It never calls install or changes the app.
    #[test]
    fn plugin_download_authenticates_fixture_bytes_and_rejects_tampering() {
        use std::io::{Read, Write};
        use tauri_plugin_updater::UpdaterExt;
        let pubkey = base64::engine::general_purpose::STANDARD
            .encode(format!("untrusted comment: test key\n{TEST_PUBKEY}\n"));
        let signature = base64::engine::general_purpose::STANDARD.encode(TEST_SIGNATURE);
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context
            .config_mut()
            .plugins
            .0
            .insert("updater".into(), serde_json::json!({"pubkey": pubkey}));
        let app = tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .unwrap();
        for tamper in [false, true] {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let signature = signature.clone();
            let server = std::thread::spawn(move || {
                for request in 0..2 {
                    let (mut stream, _) = listener.accept().unwrap();
                    stream
                        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
                        .unwrap();
                    let mut buffer = [0; 4096];
                    let _ = stream.read(&mut buffer).unwrap();
                    let body = if request == 0 {
                        serde_json::to_vec(
                            &serde_json::json!({"version":"99.0.0", "signature":signature,
                            "url":format!("http://{address}/artifact")}),
                        )
                        .unwrap()
                    } else if tamper {
                        b"tampered signed archive".to_vec()
                    } else {
                        TEST_PAYLOAD.to_vec()
                    };
                    write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    )
                    .unwrap();
                    stream.write_all(&body).unwrap();
                }
            });
            let result = tauri::async_runtime::block_on(async {
                let update = app
                    .updater_builder()
                    .endpoints(vec![format!("http://{address}/latest.json")
                        .parse()
                        .unwrap()])
                    .unwrap()
                    .timeout(std::time::Duration::from_secs(5))
                    .no_proxy()
                    .build()
                    .unwrap()
                    .check()
                    .await
                    .unwrap()
                    .unwrap();
                update.download(|_, _| {}, || {}).await
            });
            server.join().unwrap();
            if tamper {
                assert!(result.is_err());
            } else {
                assert_eq!(result.unwrap(), TEST_PAYLOAD);
            }
        }
    }

    #[test]
    fn valid_signature_over_the_exact_bytes_verifies_and_parses() {
        let verifier = RuntimeManifestVerifier::new(TEST_PUBKEY).unwrap();

        let manifest = verifier
            .verify_and_parse(TEST_PAYLOAD, TEST_SIGNATURE)
            .expect("a genuinely valid signature must verify");

        assert_eq!(manifest.version, semver::Version::parse("0.2.0").unwrap());
        assert_eq!(
            manifest.engine.get("linux/arm64").map(String::as_str),
            Some("sha256:deadbeef")
        );
    }

    #[test]
    fn tampered_payload_fails_closed() {
        let verifier = RuntimeManifestVerifier::new(TEST_PUBKEY).unwrap();
        let mut tampered = TEST_PAYLOAD.to_vec();
        // Flip one byte inside the digest — same length, same JSON shape, still invalid.
        let i = tampered.iter().position(|&b| b == b'd').unwrap();
        tampered[i] = b'D';

        let result = verifier.verify_and_parse(&tampered, TEST_SIGNATURE);

        assert_eq!(result.unwrap_err(), UpdateFailure::ManifestUnverified);
    }

    #[test]
    fn signature_from_a_different_key_fails_closed() {
        // A second REAL minisign keypair (generated the same way as the one
        // above, `minisign -G`) — a genuinely different, valid public key,
        // just not the one that produced TEST_SIGNATURE. Must be rejected.
        const OTHER_PUBKEY: &str = "RWRaawgiwtVXbjK5DeskncmzJKfQ4F1YGBZ5wqtVPUxGlSQBZyslUiAj";
        let other = RuntimeManifestVerifier::new(OTHER_PUBKEY).unwrap();

        let result = other.verify_and_parse(TEST_PAYLOAD, TEST_SIGNATURE);

        assert_eq!(result.unwrap_err(), UpdateFailure::ManifestUnverified);
    }

    #[test]
    fn garbage_signature_text_fails_closed_instead_of_panicking() {
        let verifier = RuntimeManifestVerifier::new(TEST_PUBKEY).unwrap();

        let result = verifier.verify_and_parse(TEST_PAYLOAD, "not a minisign signature");

        assert_eq!(result.unwrap_err(), UpdateFailure::ManifestUnverified);
    }

    #[test]
    fn check_none_becomes_an_empty_platforms_manifest_so_plan_update_sees_no_app_piece() {
        let current = semver::Version::parse("0.2.0").unwrap();

        let manifest = tauri_manifest_from_check(None, "linux-aarch64", &current).unwrap();

        assert_eq!(manifest.version, current);
        assert!(manifest.platforms.is_empty());
    }

    #[test]
    fn check_some_becomes_a_single_platform_entry() {
        let current = semver::Version::parse("0.1.0").unwrap();
        let checked = CheckedAppUpdate {
            version: "0.2.0".to_string(),
            signature: "sig".to_string(),
            download_url: "https://example/app.tar.gz".to_string(),
        };

        let manifest = tauri_manifest_from_check(Some(checked), "linux-aarch64", &current).unwrap();

        assert_eq!(manifest.version, semver::Version::parse("0.2.0").unwrap());
        assert_eq!(
            manifest
                .platforms
                .get("linux-aarch64")
                .map(|p| p.url.as_str()),
            Some("https://example/app.tar.gz")
        );
    }
}
