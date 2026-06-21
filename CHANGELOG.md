# Changelog

## [0.4.0] — 2026-05-28 — Agents OS Edition ready-for-production

Cambio mayor: el repositorio hermes-runtime ahora contiene
**Agents OS Edition**, un sistema operativo agéntico inmutable
basado en Fedora bootc Image Mode.

### Added — Spec 003 Agents OS Edition

#### SDD artifacts
- `specs/003-agents-os-edition/`: spec.md (61 FR), research.md (14
  decisiones), data-model.md (14 entidades), threat-model.md (15
  surfaces, STRIDE), plan.md (Constitution Check PASS), tasks.md
  (123 tareas), 10 contracts/, checklists/

#### Application services (15)
- AlwaysOnPolicy + AlwaysOnSupervisor (FR-040..FR-046): invariante
  24/7, suspend_with_authorization sólo con TOTP humano
- ConsentManager (FR-013): macOS-like ONCE/SESSION/PERSISTENT
- OtaOrchestrator (FR-008, FR-050 BLOQUEANTE): state machine 8
  estados + monotonic version + revocation cache TTL 30d fail-closed
- AuditHashChainSigner (FR-049 BLOQUEANTE): HMAC-SHA-256 hash chain
  con anclaje génesis + canonical JSON
- RemoteControlOrchestrator (FR-053..FR-056 BLOQUEANTES): SO-level
  sesiones + AES-GCM-256 con AAD + DTLS fingerprint binding + TTL
  ≤ 60min + HITL local obligatorio
- FirstBootWizard (FR-002, FR-019..FR-023): 11 estados, fail-closed,
  diferencia perfil personal_desktop (consents obligatorios) vs
  server/workspace_only
- NodeEnrollmentService (FR-007): challenge-response HMAC anti-replay
- HealthyTargetWatchdog (FR-008): auto-rollback si target no se
  alcanza en 600s
- WhisperWorker (FR-018): cola FIFO + thread + 5 estados,
  transcripción on-device
- TrainingSessionOrchestrator (FR-024..FR-038): 6 estados, allowlist
  surface_kinds, voice_chunks pending guard
- SkillCompiler (FR-026, FR-031): TrainingSession SIGNED →
  SkillPackage firmado HMAC sobre canonical JSON
- SkillReplayer (FR-027, FR-029): replay ordenado cross-surface,
  fail-closed por defecto, NUNCA replay si signature inválida
- IntentRouter (FR-028): selecciona version más alta NON-deprecated
- TenantBindingService (FR-019, FR-020, FR-032): 4 estados con
  invariante "una sola ACTIVE por nodo"
- TelemetryOptInService (FR-061 BLOQUEANTE): default OFF, flip ON
  requiere TOTP, set cerrado de exporters

#### Infrastructure adapters (13)
- Surface adapters: Browser, Terminal, Filesystem, ApiCall,
  DesktopApp (AT-SPI), Chromium ops
- SystemdSupervisor: systemctl mask + logind drop-in
- BootcUpdater: bootc upgrade/switch/rollback + status JSON parse
- LandlockRulesetBuilder (FR-052): deny-by-default per capability
- AuditTailWriter (FR-049 BLOQUEANTE): cola memoria + spool disco
- SQLite/Postgres NodeInstallation repos
- SQLite/Postgres SkillPackage repos (round-trip safe)
- DBus runtime service (org.hermes.Runtime1)
- ScreenStreamingAdapter (FR-053..FR-056): GStreamer+WebRTC SO-level
- FasterWhisperBackend (FR-018): faster-whisper distil-large-v3 lazy
- LibAtSpiClient (FR-038): pyatspi lazy
- PrometheusExporterAdapter (FR-061): 7 métricas gated por opt-in

#### Apps
- apps/agentic_panel/: GTK4 + gtk4-layer-shell overlay panel para
  personal-desktop

#### Ops (build + systemd + migrations + kickstart + runbook)
- 4 Containerfiles: base (Fedora bootc 41) + workspace-only +
  personal-desktop (GNOME 47+) + server
- 12 systemd units: hermes-runtime + control-plane + whisper +
  audit-tail + consent-manager + remote-control + targets/slice +
  bootc-updater timer
- 11 Postgres migrations (013-023) + 2 SQLite migrations
- 2 Kickstart Anaconda con cosign verify en %pre
- 2 osbuild blueprints (personal-desktop + server)
- build/build-agents-os.sh: podman buildx multi-arch + cosign keyless
  sign + syft SBOM + cosign attest + cosign verify gate
- build/compose-iso.sh: composer-cli ISO + cosign verify previo
- runbook/incident-response.md: 5 playbooks + severity matrix
- README.md: production operations guide
- .github/workflows/agents-os-edition.yml: unit + integration +
  threat-model gate + build-base opcional

### Tests
- 295 unit tests verde (agents_os + cli + apps)
- 1 E2E integration test verde (personal-desktop bootstrap completo)
- 15/15 BLOQUEANTES del threat-model (FR-047..FR-061) implementados

### Changed
- `version` bump 0.2.1 → 0.4.0 (semver MINOR: nueva feature mayor
  Agents OS Edition, sin breaking changes en hermes-runtime existente)

---

## [0.2.1] — previa
- Base hermes-runtime: ReasoningEngine + CapturingToolHost + PII
  tokenization + policy layer + browser stack (BrowserSession +
  Playwright/Stagehand drivers + SignedSelectorRegistry HMAC +
  StepRecorder + anti-bot lognormal)
