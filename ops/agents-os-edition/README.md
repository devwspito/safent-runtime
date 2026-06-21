# Agents OS Edition — Production Operations Guide

Sistema operativo agéntico inmutable (Fedora bootc Image Mode) que
ejecuta a Hermes como **personal IA 24/7**. Tres perfiles:

| Perfil | Caso de uso | Stack |
|---|---|---|
| **personal-desktop** | Laptop de un humano + agente persistente | GNOME 47+ Wayland + panel agéntico GTK4 + Chromium del agente + SQLite WAL |
| **workspace-only** | Cloud SaaS managed por Hermes | Headless Chromium + control plane + Postgres |
| **server** | Self-hosted on-prem multi-tenant | Control plane + Postgres + nginx + sin DE |

## Estado de producción (2026-05-28)

| Categoría | Estado |
|---|---|
| Tests unit | 295 verde |
| Tests integration | 1 verde (E2E personal-desktop bootstrap) |
| BLOQUEANTES threat-model (FR-047..FR-061) | 15/15 implementados |
| Containerfiles | 4 (base + 3 derivados) |
| systemd units | 12 |
| Migraciones Postgres | 11 (013-023) |
| Migraciones SQLite | 2 |
| Kickstart Anaconda | 2 perfiles |
| osbuild blueprints | 2 perfiles |
| Build pipeline | podman buildx + cosign keyless + composer-cli |
| CI workflow | `.github/workflows/agents-os-edition.yml` |
| Runbook incidentes | 5 playbooks (PB-01..PB-05) |
| Observability | Prometheus exporter gated por telemetry opt-in (FR-061) |

## Flujo de despliegue

### 1. Build de las imágenes OCI (cross-arch)

```bash
export REGISTRY=quay.io/hermes
export VERSION=v0.4.0
podman login quay.io
./ops/agents-os-edition/build/build-agents-os.sh --push --profile all
```

Produce:
- `quay.io/hermes/agents-os-base:v0.4.0` (multi-arch manifest)
- `quay.io/hermes/agents-os-workspace-only:v0.4.0`
- `quay.io/hermes/agents-os-personal-desktop:v0.4.0`
- `quay.io/hermes/agents-os-server:v0.4.0`

Cada imagen firmada keyless cosign + SBOM SPDX-JSON + atestación SLSA
Provenance v1 en Rekor.

### 2. Compose ISO instalable

```bash
sudo VERSION=v0.4.0 ./ops/agents-os-edition/build/compose-iso.sh personal-desktop
sudo VERSION=v0.4.0 ./ops/agents-os-edition/build/compose-iso.sh server
```

Produce ISO Anaconda con kickstart embebido + verificación cosign en
`%pre`. ~30-40 min en hardware moderno.

### 3. Instalación en hardware

- Boot del ISO.
- Anaconda recoge invariantes Fase 1: idioma + teclado + disco +
  cifrado LUKS (obligatorio en server, opcional en personal).
- Reboot.
- Wizard agéntico Fase 2 (`hermes-runtime.service` arranca primero):
  recoge perfil + tenant binding + consentimientos + revisión de
  servicios expuestos.
- Finalize → `NodeInstallation` ACTIVE → primer agente operando.

### 4. OTA updates

Stable + Beta channels via `bootc-updater.timer`. Política:
- `OtaOrchestrator` verifica monotonic versioning + revocation list
  (TTL 30 días, fail-closed).
- `bootc upgrade` aplica al slot inactivo.
- `HealthyTargetWatchdog` 10 min timeout — si `agents-os-healthy.target`
  no se alcanza, auto-rollback.

## Verificación rápida

```bash
# Status del runtime
hermes status

# Estado del invariante 24/7
hermes telemetry --status

# Ver consents activos
hermes consent --list

# Verificar audit chain
hermes audit verify --since "1 day ago"

# OTA status
hermes ota --status
bootc status
```

## Operación de emergencia

```bash
# Pausar el agente (mantiene SO arriba, no procesa nada nuevo)
hermes pause --reason "<motivo>" --confirm-totp

# Suspender el SO (ÚNICA ruta legítima — requiere TOTP)
hermes suspend --yes

# Drain + reboot al slot anterior
bootc rollback --apply
systemctl reboot
```

## Repositorio

- Specs: [specs/003-agents-os-edition/](../../specs/003-agents-os-edition/)
- Threat model: [specs/003-agents-os-edition/threat-model.md](../../specs/003-agents-os-edition/threat-model.md)
- Runbook: [runbook/incident-response.md](runbook/incident-response.md)
- CI: [.github/workflows/agents-os-edition.yml](../../.github/workflows/agents-os-edition.yml)
