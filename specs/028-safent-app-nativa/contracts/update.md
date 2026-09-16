# Contract — `update`: qué hay publicado y cómo se aplica entero

**Fuente de verdad de la forma.** Dos manifiestos firmados y una orquestación de
una sola pulsación. Nada de esto se decide en tiempo de ejecución a partir de
etiquetas móviles.

## 1. Manifiesto del envoltorio — `latest.json` (actualizador de Tauri v2)

Publicado por el pipeline en cada release. Firmado con minisign; la **clave
pública** viaja literal en `desktop/src-tauri/tauri.conf.json`
(`plugins.updater.pubkey`) y el pipeline usa el secreto `TAURI_SIGNING_PRIVATE_KEY`.

```json
{
  "version": "0.2.0",
  "notes": "…",
  "pub_date": "2026-09-10T18:00:00Z",
  "platforms": {
    "darwin-aarch64":  { "signature": "<minisign>", "url": "https://…/Safent_0.2.0_aarch64.app.tar.gz" },
    "linux-x86_64":    { "signature": "<minisign>", "url": "https://…/safent_0.2.0_amd64.AppImage" },
    "linux-aarch64":   { "signature": "<minisign>", "url": "https://…/safent_0.2.0_arm64.AppImage" }
  }
}
```

`darwin-x86_64` **ausente a propósito**: Mac Intel no está servido y una entrada
ausente es la forma honesta de decirlo (028, Out of Scope). `windows-x86_64` se
añadirá cuando Windows entre.

## 2. Manifiesto del motor y del compañero — `runtime-manifest.json`

Vive junto a `latest.json`. Es lo que convierte «hay versión nueva» en un hecho
comprobable en lugar de una comparación de cadenas.

> **Reconciliación entre carriles (T005→T006, backend-engineer).** T005
> implementó una desviación temporal (Ed25519 crudo hex, clave propia); el
> carril de escritorio (T014, RT-DESK) ya verificaba `runtime-manifest.json`
> con **minisign** usando **la misma clave** que el actualizador de Tauri
> (`TAURI_SIGNING_PRIVATE_KEY` en `agents-autonomy`) — decisión del dueño:
> **una sola clave para los dos ficheros**. T006 corrige el lado del daemon
> para volver a coincidir con el texto original de este documento:
> - Firmado con **minisign, modo prehashed «ED»** (BLAKE2b-512 + Ed25519) —
>   el mismo par que `latest.json`, nunca un par propio.
>   `runtime-manifest.json.minisig` es el sidecar separado y estándar
>   (`minisign -S -s <clave> -m runtime-manifest.json`); el JSON en sí **ya
>   no lleva un campo de firma embebido**.
> - Verificador: `hermes.shell_server.runtime_manifest` — implementación
>   Python pura sobre `cryptography` (Ed25519) + `hashlib.blake2b` de la
>   librería estándar (**sin dependencia nueva**); el formato se validó
>   contra el binario real `minisign` 0.11, no solo de memoria.
> - Herramienta de firma: `ops/container/sign_runtime_manifest.py sign
>   --minisign-secret-key <clave>` — invoca el binario `minisign` real (no
>   reimplementa el manejo de la clave secreta, deliberadamente: eso es
>   justo la parte que no conviene reescribir a mano). Sin esa opción,
>   escribe solo el JSON sin firmar para que el pipeline firme aparte.
> - Clave pública **comprometida en el repo**:
>   `ops/keys/runtime-manifest.pub` (formato de fichero público de
>   minisign), horneada en la imagen en
>   `/usr/share/hermes/keys/runtime-manifest.pub` (Containerfile) y cargada
>   **por defecto**. `SAFENT_RUNTIME_MANIFEST_PUBKEY` solo la sustituye
>   **en tests**.
> - **Hoy `ops/keys/runtime-manifest.pub` es un placeholder** (clave
>   Ed25519 toda a cero, comentario explícito) — el carril del pipeline
>   (`agents-autonomy`, T023, `minisign -G`) aún no ha generado el par real
>   ni comprometido la mitad pública. Mientras tanto,
>   `is_placeholder_pubkey()` lo detecta y `fetch_verified_manifest()`
>   devuelve `None` (fail-closed: ningún manifiesto verifica con la clave
>   de relleno). `tests/unit/ops/test_runtime_manifest_pubkey_release_gate.py`
>   falla a propósito con `SAFENT_RELEASE=1` mientras el placeholder siga
>   ahí — es el gate que bloquea una build de release sin la clave real.

```jsonc
{
  "schema_version": 1,
  "version": "0.2.0",                     // la versión que el dueño lee
  "engine": {
    "linux/arm64": "sha256:1121ff…",
    "linux/amd64": "sha256:767605…"
  },
  "companion": {
    "safent-ads": {
      "linux/arm64": "sha256:e51097…",
      "linux/amd64": "sha256:6b85c8…"
    }
  },
  "runtime_bundle": { "podman": "6.1.1", "machine_os": "6.1" },
  "min_app_version": "0.2.0"              // por debajo de esto, actualizar el envoltorio primero
}
```

**Invariantes**

1. **Todo digest, ningún tag.** `:latest` no aparece en ninguna parte de la
   cadena de actualización. Es lo que evita el fallo CLI-10 (mezclar
   `:latest` con `safent-ads:local` y romper las migraciones).
2. `min_app_version` obliga a un orden: si el envoltorio instalado es anterior, el
   plan **empieza** por el envoltorio y relanza antes de tocar el motor.
3. Un manifiesto cuya firma no verifica **no se usa**: la app se queda con lo que
   tiene y no ofrece botón. Fail-closed (Principio IV de la constitución).

## 3. Cálculo de «¿hay algo que actualizar?»

```ts
interface VersionSet { app: string; engine: string; companion: string | null }

/** Devuelve null cuando no hay NADA nuevo: sin plan, no hay botón (FR-015). */
function planUpdate(current: VersionSet, manifest: RuntimeManifest, latest: TauriManifest): UpdatePlan | null
```

Reglas:

- `app`: compara semver contra `latest.json` para **esta** plataforma. Sin entrada
  para la plataforma → esa pieza no entra en el plan.
- `engine` / `companion`: compara **digests**, no versiones. Digest igual → nada
  que hacer, aunque la versión legible haya cambiado.
- El compañero sólo entra en el plan **si está instalado**.
- Plan vacío → `null` → la UI **no dibuja «Actualizar»** en ninguna parte.

**Dos fuentes, una verdad**: la app (host, con internet normal) y el daemon
(cuya jaula de salida puede bloquear la consulta) calculan por separado; la UI
toma la que **sí** pudo comprobar. Es el patrón que ya existe hoy con
`window.__safentLatestVersion` en `frontend/src/components/Layout.tsx`, ampliado
de una cadena a un objeto:

```ts
declare global {
  interface Window {
    __safentUpdate?: {
      available: boolean
      current: VersionSet
      to?: VersionSet
      pieces?: { kind: 'app' | 'engine' | 'companion'; size_bytes?: number }[]
      checked_at: string
    }
  }
}
```

`GET /api/v1/system/update` se amplía con los mismos campos, **conservando**
`current_version`, `latest_version` y `update_available`.

## 4. Orquestación de una sola pulsación

```
[Actualizar] ─► una confirmación ─► (el dueño ya no elige nada más)

 1. plan            construir UpdatePlan; si es null, no había botón
 2. quiesce         declarar el trabajo del motor en curso · pausar la cola ·
                    esperar acotado ≤ 10 min · si excede, re-encolar y seguir
 3. download        traer piezas por digest, con reanudación por capa
 4. verify          firma del manifiesto + digest de cada imagen + minisign del envoltorio
 5. backup          ── PUNTO DE NO RETORNO declarado; cancelar deja de estar disponible
 6. apply_engine    recrear el contenedor con el digest nuevo (datos intactos)
 7. apply_companion compose up con el digest nuevo · migraciones
 8. apply_app       el actualizador de Tauri instala el envoltorio nuevo
 9. relaunch        la app se cierra y se vuelve a abrir sola, avisando antes
10. verify_ready    salud del motor + puente del compañero + VersionSet coincidente
11. done            la ventana acaba abierta en el producto, en la versión nueva
```

**Reversión** — cualquier fallo de 6 a 10 dispara `rolling_back`: se restaura la
copia del paso 5, se vuelve a levantar la versión anterior y se comprueba su salud.
Resultado: **o la versión nueva funcionando, o la anterior funcionando**. No hay
tercer resultado (028 FR-020, SC-008).

**Orden obligatorio**: el envoltorio va **después** del motor salvo que
`min_app_version` lo exija antes. Motivo: si el envoltorio nuevo no arranca, con
el motor ya sustituido el dueño se queda sin ventana; al revés, la ventana vieja
sigue sirviendo el motor nuevo.

## 5. Copia y restauración

Reutiliza `safent backup` / `safent restore` tal cual existen (volumen de datos +
estado del compañero + caché de seccomp, con manifiesto y sha256, archivo `0600`).
El plan añade sólo la poda: se conserva **la última copia buena** y se borran las
anteriores **antes** de descargar (motivo medido: cada versión vieja acapara ~7 GB
y el disco lleno es la causa clásica de una actualización a medias).

## 6. Errores del recorrido

| `code` | Qué ve el dueño | Resultado |
|---|---|---|
| `manifest_unverified` | «No pude comprobar la procedencia de esta versión» | Sin botón; nada cambia |
| `insufficient_disk` | Espacio necesario y espacio libre | No empieza |
| `pull_interrupted` | «Se cortó la descarga» + **Reintentar** | Lo bajado se conserva |
| `digest_mismatch` | «El paquete no coincide con lo publicado» | Se descarta y no se aplica |
| `engine_unhealthy_after_apply` | «La versión nueva no arrancó; vuelvo a la anterior» | Reversión automática |
| `companion_migration_failed` | «Anuncios no migró; Safent sigue al día» | Motor nuevo + compañero revertido |
| `relaunch_blocked` | «Ciérrala y ábrela para terminar» | Única instrucción admitida, y es un gesto, no un comando |

Todos son **reintentables** y todos dejan producto. Ninguno pide terminal.

## 7. Lo que el pipeline tiene que publicar

Por release, en el mismo destino:

1. Instaladores firmados y **notarizados** por plataforma (DMG con el ticket
   grapado; .deb y .AppImage).
2. `latest.json` firmado (`includeUpdaterJson: true`).
3. `runtime-manifest.json` + `runtime-manifest.json.minisig`, firmados con
   **la misma clave minisign** que `latest.json` (§2 —
   `ops/container/sign_runtime_manifest.py sign --minisign-secret-key
   <TAURI_SIGNING_PRIVATE_KEY>`, digests que **ya se publicaron** de
   `ghcr.io/devwspito/safent` y `…/safent-ads`, resueltos por la propia
   herramienta de registro del pipeline, no por este script).
4. `VERSION` actualizado (compatibilidad con el chequeo de hoy).

Si falta cualquiera de los cuatro, la release **no se publica**: una app que ve
`latest.json` sin `runtime-manifest.json` ofrecería un envoltorio nuevo con un
motor viejo, que es exactamente la discrepancia que FR-021 prohíbe.
