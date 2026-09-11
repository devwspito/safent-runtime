# Actualizador nativo: auditoría y estado honesto

## Resultado y alcance

**El actualizador de la app nativa aún no está operativo.** Este corte elimina
una detección engañosa y muestra esa limitación explícitamente. No instala,
publica, modifica el motor del usuario ni sustituye el protocolo firmado por
un `pull` de una etiqueta. La disponibilidad del actualizador del daemon sigue
siendo independiente; este corte no certifica su instalador.

Skill aplicada: `emil-design-eng`, lectura completa. Se conserva la jerarquía
compacta y la recuperación existente; información secundaria legible, sin
spinner, entrada animada ni botón que prometa una acción inexistente. No hay
movimiento nuevo ni cambio de orden de foco.

| Before | After | Why |
| --- | --- | --- |
| Un hilo ejecutaba `curl main/VERSION` y exponía una cadena sin firma como versión nueva. Los fallos se ocultaban. | Eliminados fetch, hilo periódico, llamada de arranque y aceptación del global legacy en el footer. | Una rama de desarrollo no acredita una release instalable ni su autenticidad. |
| El loader no mostraba que faltaba el actualizador nativo. | Metadata fija `unavailable/integration_missing` y versión real de Cargo, inyectada antes de scripts en cada navegación. | Disponibilidad de una capacidad no equivale a haber comprobado versiones. |
| App nativa y versión del motor podían confundirse. | Aviso «App nativa …» independiente, también si aún no responde el daemon. Se conserva una actualización del motor disponible. | No borrar estado válido de otro ámbito ni usar la versión del motor como versión del wrapper. |
| Comentarios afirmaban que `check()` verificaba la firma de `latest.json`. | Documentado: check lee metadata; download verifica la firma del artefacto. | No elevar metadata no verificada a autorización de instalación. |

## Evidencia del contrato sin implementar

- `src-tauri/src/main.rs`: no registro del plugin updater ni comandos check/install.
- `src-tauri/src/update/orchestrator.rs`: `UpdatePorts` existe, pero su única
  implementación es `RecordingPorts` de pruebas. No hay puertos reales de
  quiesce, descarga, backup, apply, rollback o relanzamiento.
- `src-tauri/tauri.conf.json`: clave `__TAURI_UPDATER_PUBKEY__` pendiente de
  sustitución por el pipeline. No se probó ninguna clave ni release live.
- `src-tauri/src/update/tauri_updater.rs`: verificación minisign real del
  runtime manifest conservada, con fixtures firmadas, más conversión de
  metadata de Tauri. No realiza una actualización por sí sola.
- Fuente exacta inspeccionada en Cargo DGX:
  `tauri-plugin-updater-2.11.0/src/updater.rs`, `Updater::check` línea432,
  `Update::download` línea680 y `verify_signature` línea740. La firma se
  verifica sobre el buffer descargado antes de devolverlo, no en check.
- `../safent:cmd_update` hace self-update/pull/run; no es una implementación
  equivalente de la transacción por digest descrita en
  `../specs/028-safent-app-nativa/contracts/update.md`.

No se añadió otro instalador, un permiso IPC nuevo, ejecución de scripts de
actualización ni acceso remoto a los comandos privilegiados locales.

## Estado público mínimo

`window.__safentNativeUpdater` contiene sólo:

```json
{"status":"unavailable","reason":"integration_missing","app_version":"0.9.0"}
```

El host obtiene la versión de Cargo y define el objeto como congelado, no
escribible/no configurable. No contiene secretos, timestamps de comprobación,
URLs ni disponibilidad de una release. Renderer y footer validan forma/versión,
usan texto y no aceptan estados desconocidos como éxito. No sustituye
`__safentUpdate`, `getSystemUpdate` ni `update_system`. En una web sin metadata
nativa no se inventa la existencia de un wrapper.

## Verificación

- Renderer desktop: **104 PASS / 8 archivos**; typecheck y build PASS.
- Frontend Community: **260 PASS / 44 archivos**; typecheck/build PASS.
  Incluye 9 tests del footer: ignorar VERSION sin firma, app nativa no
  disponible con daemon pendiente y actualización del motor aún visible.
- Rust en scratch DGX `/tmp/safent-native-diagnostics.RlX4xP/desktop/src-tauri`:
  **215 PASS** (128 unit +48+39 integración), `cargo fmt -- --check` y
  `cargo build --locked` y `cargo clippy --locked --all-targets -- -D warnings`
  PASS. Primera comprobación de formato detectó un
  assert largo: corregido antes de la pasada final; no se cuenta como verde.
- Smoke **Tauri/WebKitGTK real Linux aarch64**, no HTML fixture:
  perfil XDG aislado `/tmp/safent-native-updater.LUYfeM`, runtime y CLI
  deliberadamente ausentes. Binario y assets normales del producto.
  Captura `/tmp/community-native-updater.png` inspeccionada: recuperación
  «Esta copia de Safent está incompleta», exportación de diagnóstico
  conservada, aviso nativo/version0.9.0 legible y sin acción ficticia.
  El proceso terminó por timeout20 previsto; el arnés acepta ese124, no lo
  presenta como un test de instalación. Avisos de portal/GVFS de Xvfb no
  equivalen a validación de macOS, Windows o escritorio real del usuario.
- `git diff --check` PASS. No instalación, descarga de release ni publicación.

## Matriz de estados y siguiente integración real

| Condición | Estado comprobado en este corte | Pendiente antes de habilitar |
| --- | --- | --- |
| Sin updater integrado / sin manifiesto validado | No disponible explícito; sin checking/up-to-date falsos | Registrar plugin y servicio host con clave de release real y comprobación acotada de los dos manifests |
| Offline / red falla | La capacidad sigue no disponible; ya no hay fetch silencioso | Estados distintos checking, offline, verificación fallida, sin nueva release; timeout y reintento singleflight reales |
| Release nueva | El aviso nativo no fabrica disponibilidad; estado daemon independiente | Plan por digest y versión actual real; URL/artifact descargado por plugin y firma verificada antes de aplicar |
| Descarga / falta disco / firma inválida | Primitivas puras conservadas, no progreso simulado | Puertos reales, rechazo antes del punto irreversible y pruebas con transporte/almacén aislados |
| Cancelar / retry de actualización | No se muestra acción porque no hay operación | Cancelación cooperativa sólo antes de backup, exclusión de intentos, estados persistidos |
| Apply / reinicio / rollback | No ejecutado ni afirmado | Quiesce real, backup/restore, engine+companion por digest, continuación durable tras relanzar la app, healthcheck y rollback verificables |

La orquestación actual llama `apply_app_and_relaunch` antes de terminar la
transacción, mientras sus tests retornan desde ese puerto. Es imprescindible
definir continuación durable tras reinicio: un proceso relanzado no conserva
la pila ni el backup handle en memoria. No debe habilitarse pegando solamente
el plugin a un botón. Las suites fake de puertos no prueban este lifecycle.

Archivos de producto cambiados: `src-tauri/src/{main,boot,window_policy}.rs`,
`src-tauri/src/update/{availability,mod,tauri_updater,types,plan}.rs`,
`src/{main.ts,index.html,styles.css,native-updater.ts,native-updater.test.ts}`,
assets generados `ui/`; integración mínima autorizada en
`../frontend/src/components/SystemUpdateFooter.tsx` y su test, dos claves
ES/EN en `../frontend/src/lib/i18n.ts`. Ningún backend ni manifiesto publicado.
