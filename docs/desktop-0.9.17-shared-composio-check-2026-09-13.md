# Community 0.9.17 — comprobación de Composio compartido

## Alcance

Guardar Composio una vez en Integraciones; Anuncios recibe esa configuración
mediante una concesión cifrada, firmada, ligada a esta instalación y renovable.
La autorización específica de cada cuenta publicitaria sigue siendo necesaria.

## Entradas inmutables

- Runtime `v0.9.17`: `8ebccecd08919304ca5519607e2849be52396d3f`.
- Ads `v0.2.12`: `8b04d6e0566134a4fc867162d5cc73d113c80dff`.
- Pipeline: rama `safent-desktop-pipeline-028`, `1f87bf34a580e9138f5574a7816d5f20cffc017e`.
- Candidato de pruebas; no se promueve el canal estable.
- Runtime arm64: `sha256:95a3bd5b278b11c97a1340ac637953bc479d2ee6dcaf547f4a5fe03fd9f88a0f`.
- Ads arm64: `sha256:628b0fd9a231345a034f5a6e2399a811db089ac31066f9992b419631526e8718`.
- Workflow nativo: [34762239517](https://github.com/devwspito/agents-autonomy/actions/runs/34762239517).

## Estado previo comprobado

- Mac Apple Silicon con Safent 0.9.16 y Ads 0.2.11 activos.
- Un único sidebar en Anuncios, con vuelta a Safent.
- Integraciones indica Composio activo, pero no representa correctamente las
  conexiones por el contrato `toolkit_slug`/`slug`; corregido en este candidato.
- Inventario anterior de Composio: Gmail ACTIVE; ninguna cuenta Ads autorizada.
- Google disponible mediante configuración antigua de broker; Meta pendiente.

## Respaldo

Antes de instalar se han obtenido copias consistentes de SQLite y PostgreSQL.
Integridad SQLite `ok`; formato PostgreSQL validado mediante `pg_restore --list`.
Los archivos quedan privados en el directorio local de esta comprobación. No se
borran datos, volúmenes ni la máquina virtual. La app anterior se conservará.

Se han retirado únicamente las dos entradas de Composio que se habían añadido
manualmente a `broker.env`. El archivo original tiene copia privada recuperable,
y el archivo activo conserva modo 0600. La clave cifrada de Integraciones no se
ha tocado. Esto permite comprobar que la nueva versión no usa el atajo anterior.

## Pruebas de código

- UI Community: 406 pruebas y compilación TypeScript/Vite.
- Backend Ads: 3570 pruebas; dos avisos preexistentes.
- Panel Ads: 339 pruebas en 47 archivos; TypeScript y build Vite.
- Provisionado y base de datos fría: 98 pruebas.
- Identidad pública fijada: 170 pruebas de runtime y 34 de broker/CLI.
- Imágenes publicadas descargadas por digest en el motor privado del Mac.
  El CLI de Ads produce su identidad pública en aislamiento real sin red ni
  escritura; los tres módulos nuevos del runtime se importan correctamente en
  la imagen publicada, también sin red. No se montaron datos del usuario.

## Aceptación instalada — superada

- DMG público de 1.016.800.750 bytes; SHA-256
  `4ef426abedf22ffd02314ce691553e2ee94105c190ce0e7daf579025107482f0`,
  igual al checksum publicado. `hdiutil verify` válido; firma profunda/estricta
  válida; Gatekeeper devuelve `accepted`, `Notarized Developer ID`.
- `/Applications/Safent.app` muestra versión 0.9.17. La copia instalada conserva
  firma válida. El manifiesto incluido fija ambos digests arm64 indicados arriba.
- La app anterior 0.9.16 se ha movido a una copia recuperable. Se ha desmontado el
  DMG. Sin limpieza de datos ni volúmenes. La app ha llegado al chat normalmente.
- El arranque ha actualizado automáticamente runtime y Ads a sus imágenes
  esperadas. `hermes-runtime.service` y `hermes-shell-server.service`: `active`.
- Registro, SSO e identidad pública: válidos, legibles y de confianza en el
  contenedor real con usuario `hermes`. Composio sigue activo con clave cifrada.
- Google se ha preparado automáticamente desde Integraciones:
  `googleads_config_present=true`; Meta sigue sin configuración.
- Broker real: `companion_mode=true`, `legacy_env_present=false`,
  `legacy_env_ignored_without_lease=true`. No existe la copia antigua en su entorno.
- API real por socket del broker: `google_managed_ready=true`.
- Durante una observación de 45 s sin forzar publicación:
  `automatic_lease_accepted=true`, `lease_metadata_fresh=true`.
- Integraciones muestra Gmail como conectado y ya no muestra el error de carga.
  Explica que Composio se configura allí y enlaza a Anuncios.
- Anuncios tiene una sola barra lateral y botón «Volver a Safent»; se ha revisado
  también mediante captura real, no sólo tests.
- El botón Google inicia un enlace OAuth real de Composio usando la configuración
  compartida. El panel explica «Customer ID» como el número publicitario de diez
  dígitos, no como una clave. La sesión queda pendiente de datos/consentimiento.

## Pendiente ajeno a la configuración compartida

No se han creado ni activado campañas. No se ha ejecutado gasto publicitario.
Google requiere el identificador de cuenta y consentimiento. Meta necesita una
configuración OAuth propia antes del consentimiento: Composio no suministra una
app Meta gestionada según su documentación actual.

El chat informa que no hay un modelo de IA conectado; esta comprobación no lo ha
configurado. No se afirma operatividad integral de campañas ni del agente.

Todos los workflows de imágenes y app terminaron correctamente. La publicación
es una prerelease, no un cambio del canal estable (`latest` conserva v0.9.5).
