# Contract — `install-request`: la UI pide, el anfitrión cumple

**Fuente de verdad de la forma.** Extiende el mecanismo de marca que hoy existe
(`.update-requested` / `.uninstall-requested` en `/var/lib/hermes/instance/`,
`src/hermes/shell_server/system_update.py`, `safent agent`). El contenedor
**nunca** crea contenedores hermanos: deja una marca y la cumple el anfitrión.

## 1. Ubicación y forma

Un fichero por verbo, en el directorio propiedad del daemon
`/var/lib/hermes/instance/`, con nombre `request-<verb>.json`, modo `0600`,
escrito de forma atómica (`.tmp` + `rename`).

```json
{
  "schema_version": 1,
  "verb": "install_companion",
  "slug": "safent-ads",
  "created_at": "2026-09-10T18:04:11Z",
  "expires_at": "2026-09-10T18:34:11Z",
  "attempt": 1
}
```

```ts
type HostVerb =
  | 'install_companion' | 'repair_companion' | 'remove_companion'
  | 'update_system'     | 'uninstall_system'

interface InstallRequest {
  schema_version: 1
  verb: HostVerb
  /** Sólo para los verbos de compañero. Enum cerrado, hoy: 'safent-ads'. */
  slug?: 'safent-ads'
  /** Sólo en remove_companion. Por defecto 'keep' (029 CL-003). */
  retention?: 'keep' | 'purge'
  created_at: string   // RFC 3339 UTC
  expires_at: string   // RFC 3339 UTC
  attempt: number      // ≥ 1
}
```

**Invariantes de seguridad (por qué esta forma y no otra)**

1. **Vocabulario cerrado.** No hay campo que lleve una orden, una ruta, una URL,
   una imagen ni un argumento. Un proceso comprometido dentro del contenedor no
   puede convertir la marca en ejecución arbitraria en el host: como mucho pide
   uno de los cinco verbos que ya podía pedir por la UI.
2. **La imagen no viaja en la marca.** El anfitrión resuelve el digest del
   manifiesto firmado (`update.md`), nunca del contenido de la marca.
3. **Consumo único.** El anfitrión **borra la marca antes de actuar**. Un fallo
   deja `last_failure` en el estado observado, no una marca huérfana que se
   reejecute en bucle.
4. **Caducidad.** `expires_at` acota la vida. Vencida, el lector la considera
   inexistente y la borra: la UI vuelve a ofrecer la acción diciendo que **nadie
   la atendió** (edge case «agente del host no corriendo» de 029).
5. **Una viva por verbo.** Crear una segunda con el mismo verbo mientras hay una
   `pending`/`claimed` devuelve la existente. La segunda pulsación no lanza una
   segunda instalación (029 FR-008).

## 2. Ventanas de vida por verbo

| Verbo | `expires_at − created_at` | Por qué |
|---|---|---|
| `install_companion` | **30 min** | Imagen de 0,44 GB comprimida + migraciones, con margen para red lenta |
| `repair_companion` | 15 min | No baja imagen nueva salvo digest cambiado |
| `remove_companion` | 5 min | Compose down + red |
| `update_system` | **45 min** | Motor de 2,50 GB comprimidos, 66 capas |
| `uninstall_system` | 10 min | — |

El `_FLAG_STALE_S = 15*60` de hoy es único para todos los verbos y ya se midió
demasiado corto para una descarga grande (UPD-03). Pasa a ser **por verbo**.

## 3. API que la UI usa

```ts
POST /api/v1/system/requests
  body: { verb: HostVerb, slug?: 'safent-ads', retention?: 'keep' | 'purge' }
  200:  { accepted: true,  request: InstallRequestStatus }
  409:  { accepted: false, request: InstallRequestStatus }   // ya hay una viva
  400:  { accepted: false, code: 'unknown_verb' | 'unknown_slug' }

GET  /api/v1/system/requests
  200:  { requests: InstallRequestStatus[] }

interface InstallRequestStatus {
  verb: HostVerb
  state: 'pending' | 'claimed' | 'applied' | 'expired' | 'failed'
  stage?: StageId          // eco de la etapa viva (app-engine.md §3)
  progress?: { done: number; total?: number; unit: 'bytes'|'layers'|'steps' }
  expires_at: string
  last_failure?: { code: FailureCode; label: string; retryable: boolean }
}
```

`POST /api/v1/system/update` y `POST /api/v1/system/uninstall` **se conservan**
como alias de `{verb:'update_system'}` y `{verb:'uninstall_system'}`: ninguna
instalación existente se rompe (expandir → contraer).

**Autorización**: el bearer de la instalación, igual que el resto de
`/api/v1/*`. El daemon **valida el verbo y el slug contra el enum** antes de
escribir el fichero; un valor desconocido es `400`, nunca un fichero escrito.

## 4. Quién lee la marca

Dos lectores, **misma implementación**:

1. **`safent agent`** (launchd/systemd), como hoy — cubre el caso «la ventana está
   cerrada pero el motor sigue vivo» (FR-030).
2. **La app**, cuando está abierta — el bucle del envoltorio consulta
   `GET /api/v1/system/requests` y, si hay una `pending`, la reclama y la cumple
   con el CLI embebido, con lo que **el progreso se ve en la ventana** en lugar de
   quedarse en un `agent.log` invisible.

**Exclusión mutua**: el que reclama escribe `request-<verb>.claim` (0600) con su
identificador de proceso y una marca de tiempo; el otro lector respeta una
reclamación viva (< 60 s de antigüedad, renovada mientras trabaja). Si la
reclamación caduca sin `applied`, la marca vuelve a `pending`. La app tiene
preferencia cuando está abierta: es la que puede enseñar el progreso.

## 5. Estados que la marca produce en la UI

| Estado observado | Qué ve el dueño |
|---|---|
| sin marca, compañero ausente | Una sola acción: **«Instalar»** |
| `pending` | «Instalando…» y el control **no** dispara otra |
| `claimed` + `stage`/`progress` | Etapas reales con avance vivo (029 FR-004) |
| `applied` + puente `/ads/` respondiendo | «Anuncios» pasa a **`ready`** |
| `expired` | «Nadie atendió la instalación» + **«Reintentar»** |
| `failed` | Causa nombrada + **«Reintentar»**. Nunca «conectado» falso |

**Prohibido**: mostrar `ready` sin que el puente haya respondido de verdad
(029 FR-007). El contador de contenedores no basta: hoy miente (CLI-08).
