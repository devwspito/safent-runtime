# UI-NATIVE: diagnóstico de arranque y recuperación del estado inicial

Bloque implementado el2026-09-11 sobre la integración `db8ce57`. Sin cambios
Python, frontend Community/Enterprise, instalaciones del usuario ni imágenes
publicadas. No equivale a finalizar toda la aplicación nativa.

## Contrato real y alcance

Se inspeccionó el dispatcher y `usage()` de `safent`: **no existe** el verbo
`diagnostics --out`, aunque `contracts/app-engine.md §4` lo describe y la versión
anterior de UI-STATES lo daba por hecho. No se ejecuta un comando inexistente ni
se improvisa un recolector de logs. La decisión autorizada es un JSON mínimo
de arranque con allowlist, rotulado como tal.

`DiagnosticsState` recibe `DomainEvent` del mismo `TauriNotifier` usado durante
bootstrap. Proyecta campos tipados **antes de almacenarlos**. Descarta `label`,
`message/detail`, digests libres, argumentos de reparación, URL y paths. No lee
archivos, variables, keychain, chats ni stdout/stderr. El buffer se limita a128
eventos e informa cuántos ha descartado. OS/arch y versión son datos públicos
de compilación/plataforma; no se envía nombre de usuario o máquina.

Se usa el [plugin oficial de diálogos de Tauri](https://v2.tauri.app/plugin/dialog/)
desde Rust, enlazado a la ventana `main`. El renderer no aporta ni destino ni
contenido. Cancelación es `{status:'cancelled'}`, éxito `{status:'saved'}` sólo
después de persistir; error es un código fijo, nunca un error con paths. Single
flight independiente en Rust y UI. Temporal hermano, sincronización y reemplazo
atómico; Unix0600 desde creación, no chmod tardío. Se rechazan rutas relativas,
directorios y symlinks del destino. No se abre ni comparte el resultado.

## Diseño y estados (skill emil-design-eng)

Skill releída completa; feedback inmediato y accesible sin animación adicional
en esta acción de sistema. La plataforma es quien presenta su selector.

| Before | After | Why |
| --- | --- | --- |
| Exportar deshabilitado, sin comando Rust. | Exportar diagnóstico de arranque con selector nativo y estados reales. | Resolver la función sin fingir un bundle completo. |
| No distinción entre cancelación, acuse IPC y archivo guardado. | Pendiente, cancelado, guardado y error separados; `role=status/alert`. | El feedback refleja el resultado real. |
| Doble invocación posible al conectar un botón de forma directa. | Guard síncrono UI y guard RAII Rust. | Nunca abrir dos selectores simultáneos. |
| Fallo temprano se emitía antes de subscribir el renderer; quedaba Iniciando. | Suscripción primero, getter después, secuencia monotónica compartida. | El fallo inicial sigue visible y un snapshot antiguo no pisa un reintento. |
| Código real `engine_digest_missing` caía en copy que recomendaba reintentar, pero retry no estaba permitido. | «Esta copia de Safent está incompleta» y descarga/diagnóstico. | No sugerir una acción que la pantalla no permite. |

## Replay mínimo, no otro motor

`safent://bootstrap-state` y `get_bootstrap_state` comparten
`{sequence,event,last_stage,point_of_no_return}`. Conservan sólo el último evento
tipado y su última etapa, sin texto libre. El renderer reutiliza `reduceLifecycle`
mediante `reduceBootstrapSnapshot`; etiquetas estáticas de etapas, contadores
reales y el gate de cancelación nativo. Se ignoran secuencias viejas/duplicadas.
El getter falla si el estado nativo está corrupto, no devuelve vacío saludable.

Los dos comandos nuevos se registran en `build.rs` y `generate_handler!`, y sólo
se conceden al capability local. `remote-ui` no gana exportación, getter ni
permisos generales de filesystem/diálogo. El plugin transita `tauri-plugin-fs`,
pero no se habilitan permisos JS para él. Los canales legacy siguen presentes;
el loader ahora consume exclusivamente el snapshot seguro.

## Comparación del WIP recuperado de Claude

Se hizo fetch y lectura de `origin/lane/desk-native`, commit
`1cfb39744e0555413551ea5a4dac42716cd8fbb7`, **sin cherry-pick ni sobrescribirlo**.

| Before (WIP1cfb397) | After (este bloque) | Why |
| --- | --- | --- |
| Plugin oficial y buffer200 de `EngineEventPayload` completo. | Misma elección nativa/buffer acotado, pero proyección estricta de DomainEvent. | Los payloads antiguos conservan `label/detail` libres. |
| `host_error=error.to_string()`/`cause.message`; `MachineProvider::Other(raw)`. | No se recolecta `facts`, ni errores/string externos. | Ese WIP no demuestra la promesa «sin secretos» y amplía el alcance autorizado. |
| `std::fs::write(path)` y chmod posterior cuyo error se ignora. | Temporal0600, validación de destino y persistencia atómica. | Evitar truncado por fallo y seguimiento de symlinks. |
| Exportación incompleta: sin conexión del comando/capability/UI. | Contrato completo, feedback probado y smoke nativo real. | No asumir que un método suelto es una función entregada. |
| Cambios adicionales en puertos/retry/cancel del arranque. | No importados en este bloque. | Son lógica de lifecycle separada; requieren pruebas específicas. |

La lectura detectó dos problemas previos reales para seguimiento: retry crea
un `CancelSignal` nuevo que no es el gestionado por `cancel_bootstrap`, y los
retornos `EngineError::Cancelled` del bucle no notifican siempre el estado
failed esperado por UI. El WIP intenta abordarlos; **no se dan por arreglados
aquí**. Este bloque sí corrige la carrera de suscripción inicial.

## Evidencia ejecutada

Renderer Mac, `NODE_OPTIONS=--no-experimental-webstorage npm test`,
`npm run typecheck`, `npm run build`: **95/95 tests PASS**, TypeScript y build
PASS. Diez tests nuevos cubren exportación, cancelación, error/redacción,
single-flight, preview sin Tauri, resultado IPC inválido, fallo temprano,
snapshot tardío durante etapa nueva, error del getter, progreso/gate y retry.
Los assets `desktop/ui/` se regeneraron con build, no a mano.

Rust en copia scratch DGX
`/tmp/safent-native-diagnostics.RlX4xP/desktop/src-tauri`, toolchain existente
`/home/luiscorrea-dev/.cargo/bin/cargo` (1.95), sin instalar nada en host:

```sh
cargo fmt -- --check
cargo clippy --all-targets -- -D warnings
cargo test -q
cargo build -q
```

Resultado final: **207 PASS (120 unit +48 adaptador +39 contrato CLI real)**,
fmt/clippy/build PASS, exit0. Incluye diez tests Rust nuevos: secretos en campos
libres jamás exportados, buffer/truncación, guard, cancelación, permisos0600,
destinos inválidos/symlink, capability local, replay temprano y secuencia.
Se corrigieron antes del cierre un nombre de API de FilePath incorrecto y una
sugerencia `?` de clippy; sus ejecuciones fallidas no se contaron como verdes.

### Binario real, no maqueta de navegador

Linux aarch64, Xvfb aislado: binario compilado real y assets normales de producto,
GTK/WebKit reales. `SAFENT_RUNTIME_DIR` apuntó a una ruta inexistente de scratch
deliberadamente; no se inició motor ni contenedor. Estado/config/data/cache de
la prueba se situaron en scratch. No se tocó la instalación del usuario.

1. Antes del replay se reprodujo «Iniciando...» permanente ante el fallo inmediato.
2. Con replay, la misma ausencia real produce pantalla failed y botón activo.
3. Clic abre selector GTK real; cancelar muestra mensaje normal y no crea archivo.
4. Nuevo intento guarda en el destino elegido dentro del scratch.
5. `stat` confirmó modo0600,264bytes; JSON comprobado:
   schema1/startup_only, versión0.9.0, linux/aarch64, dropped0,
   evento failed/engine_digest_missing/retryablefalse. Nada de paths ni errores libres.

Capturas inspeccionadas: `/tmp/community-native-diagnostics-start.png` (rojo),
`...-replay.png`, `...-dialog.png`, `...-cancel.png`, `...-saved.png` (prefijo
`/tmp/community-native-diagnostics`). Las capturas exitosas preceden únicamente
al pulido final de copy para engine_digest_missing; la lógica probada no cambió.

La sesión se cerró por `timeout240` previsto (exit124 de la sesión, **no** exit0
de una suite). Xvfb emitió avisos EGL/portal/GVFS propios del entorno virtual;
no impidieron selector/guardado, pero no se confunden con una certificación de
integración del escritorio del usuario. Una fixture HTML auxiliar fue preparada
en scratch para explorar; **no se usó como evidencia de cierre ni se integra**.

## Límites

- Selector/guardado reales verificados en Linux; NSSavePanel macOS, Windows,
  accesibilidad con lector de pantalla y paquetes firmados siguen pendientes.
- Autoridad local/remota verificada por registro/capabilities y tests; falta
  prueba de invocación remota maliciosa sobre binario end-to-end.
- Exportación de logs/facts/runtime, cancelación efectiva tras retry y el flujo
  completo de actualización quedan fuera de este bloque.
- No se publica imagen final de Community ni se afirma toda la UI terminada.
