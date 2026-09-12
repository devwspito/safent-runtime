# Actualizador nativo app-only

El plugin Tauri está registrado y los comandos `check_native_update` e
`install_native_update` están montados. El loader muestra el control cuando
la configuración de firma empaquetada es válida; el menú de bandeja permite
comprobar/actualizar también después de navegar a la UI del producto.

## Autoridad y flujo

- Configuración **merged** de `AppHandle`, incluidos overlays de release;
  no `include_str` del source ni endpoint/clave aportados por JavaScript.
- Endpoint fijo de release oficial, HTTPS, certificados válidos y sin proxy
  del entorno. Artefactos sólo bajo releases/download del mismo repositorio.
- `check()` compara versiones y valida forma de firma/URL; **latest.json no
  tiene firma propia**. No se afirma autenticidad del archivo todavía.
- Snapshot host con ID monotónico y TTL15min. Un nuevo check invalida el
  anterior. Instalación exige ese ID exacto; nunca recibe URL, firma o bytes.
- Un solo check/install simultáneo. Exclusión compartida con bootstrap y
  reparación: no sustituir el bundle mientras se usa su CLI. No cambia el
  attempt_id ni cancela trabajos para obtener la reserva.
- Confirmación nativa explícita con versión y aviso de reinicio. Cancelar,
  caducar o fallar consume el snapshot; no hay reintento automático. El TTL
  también se comprueba al volver del diálogo, antes de descargar.
- El plugin descarga y verifica minisign antes de devolver bytes al único
  `install` host. Después instala y reinicia. Un fallo de instalación no se
  anuncia como rollback: se pide comprobar la versión instalada.
- IPC sólo al loader local, también comprobado por origen y etiqueta del
  webview. No se conceden permisos `updater:*` al JavaScript ni comandos de
  actualización a la página remota. El menú de bandeja llama al mismo host.

Tauri codifica la **totalidad del fichero .pub** en base64. Se añadió
`RuntimeManifestVerifier::from_tauri_pubkey`; el constructor anterior conserva
su formato de línea raw, sin reinterpretar históricos. `base64 0.22.1` ya
existía en Cargo.lock: sólo se añadió la dependencia directa. El feature test
de Tauri se activa exclusivamente como dependencia de desarrollo.

## UI / Emil

Skill `emil-design-eng` leída completa y aplicada.

| Before | After | Why |
| --- | --- | --- |
| Aviso estático de integración ausente | Acción compacta, comprobación manual y estados explícitos | No confundir capacidad con haber comprobado una release |
| Sin recorrido instalar | Versión visible, confirmación nativa, botón bloqueado durante la operación | Evitar duplicados y conservar una intención humana verificable |
| Riesgo de respuesta tardía en UI recreada | Snapshot de renderer; respuestas antiguas ignoradas | No presentar una versión de un contexto anterior |

Reutiliza botones, foco, estados press y reduced-motion existentes. Sin
animación decorativa, porcentaje simulado ni HTML procedente de metadata.

## Verificación

Scratch DGX propio: `/tmp/safent-machine-conflict.ZCYXwt`, sin VM real ni DB.

```sh
cd /tmp/safent-machine-conflict.ZCYXwt/desktop/src-tauri
/home/luiscorrea-dev/.cargo/bin/cargo test --locked -q
/home/luiscorrea-dev/.cargo/bin/cargo clippy --locked --all-targets -- -D warnings
/home/luiscorrea-dev/.cargo/bin/cargo build --locked
```

**225 Rust PASS** (137 unit +48+40 contratos), clippy y build PASS. Incluye
concurrencia singleflight, CAS/TTL/replay, exclusión bootstrap, ACL/orígenes,
configuración TLS y formatos de firma. Una prueba usa el plugin updater real
con MockRuntime y servidor HTTP loopback exclusivamente de fixture: check y
download aceptan el payload minisign exacto y rechazan su alteración. Nunca
llama `install`. Los endpoints de producción siguen siendo HTTPS-only.

En `desktop`: `npm test -- --reporter=dot`, `npm run typecheck`, `npm run build`:
**128 PASS /10 archivos**, typecheck/build PASS; assets generados incluidos.
No se usaron releases live, secretos ni llamadas de instalación/reinicio real.

## Límites explícitos

Este corte **no actualiza motor ni Ads**, no llama `safent update` y no activa
`UpdatePorts` del orquestador conjunto. Éste sigue necesitando continuación
durable, backup/rollback y aplicación por digest propios. No se promete
atomicidad conjunta ni rollback del wrapper tras un fallo del instalador.

El comportamiento de instalación macOS/Windows/Linux pertenece al plugin y
requiere smoke de actualización entre dos builds firmadas en cada plataforma
antes de certificar su distribución. La verificación presente compila en
Linux arm64 y prueba transporte/firma sin instalar una app. El plugin conserva
el archivo descargado en RAM; timeout check30s/download10min, sin un nuevo
descargador propio ni límite de tamaño adicional en este corte.

Referencia primaria consultada el12-sep-2026:
[Tauri Updater](https://v2.tauri.app/plugin/updater/), y código instalado
`tauri-plugin-updater-2.11.0/src/updater.rs` (`check`, `download`, `install`,
`verify_signature`). Nada publicado ni etiquetado.
