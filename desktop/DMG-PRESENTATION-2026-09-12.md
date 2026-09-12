# DMG nativo: arrastrar a Aplicaciones

Base Runtime `6f8a83c`. No auto-instalación, scripts de copia, asistente adicional,
registro manual, apertura automática, reinicio de Dock ni cambios de Launchpad.
Safent sigue siendo un bundle que Finder copia a Aplicaciones.

## Inspección de la distribución real

DMG publicado v0.9.2 abierto sólo lectura, sin ejecutar Safent: raíz con
`Safent.app` y enlace `Applications → /Applications`; firma Apple deep/strict
validada con identificador `com.safent.desktop` y equipo `JBMBA58A8X`.
Info.plist: `CFBundlePackageType=APPL`, nombre Safent, ejecutable safent-desktop,
icon.icns presente, versiones 0.9.2. `LSUIElement` y `LSBackgroundOnly` ausentes:
no es una app agente oculta ni de segundo plano. El volumen QA fue desmontado;
no se modificó la instalación del usuario.

La estructura no presenta un impedimento demostrado para el registro normal.
Abrir la app directamente en el volumen no equivale a copiarla a Aplicaciones.
La aparición efectiva en Launchpad requiere comprobar la copia realizada por
Finder en el Mac objetivo; esta revisión no simula ni certifica esa instalación.

### LSRequiresCarbon

El paquete contiene `LSRequiresCarbon=true`, insertado por el propio
[bundler de Tauri](https://github.com/tauri-apps/tauri/blob/dev/crates/tauri-bundler/src/bundle/macos/app.rs).
Apple documenta esta clave en el contexto histórico Carbon/Classic, no como
indicador para ocultar una aplicación de la interfaz. No hay evidencia que la
relacione con este síntoma de Launchpad. No se parchea el bundle firmado ni se
añade un fork de Tauri para eliminarla. Las claves pertinentes de aplicación
agente/background permanecen ausentes. Véanse
[Launch Services Concepts](https://developer.apple.com/library/archive/documentation/Carbon/Conceptual/LaunchServicesConcepts/LSCConcepts/LSCConcepts.html)
y [Launch Services Keys](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/LaunchServicesKeys.html).

## Presentación (skill emil-design-eng)

| Before | After | Why |
| --- | --- | --- |
| Fondo blanco, dos iconos sin instrucciones | Título, instrucción de arrastre y flecha discreta | Explica la única acción necesaria |
| Posiciones implícitas del bundler | Ventana 660×400, app y destino alineados en (180,200)/(480,200) | Evita colisiones con el texto y mantiene la densidad |
| No indica dónde abrir después | «Después, ábrelo desde Aplicaciones o Launchpad» | Distingue el volumen temporal de la app copiada |
| Riesgo de arreglos de registro invasivos | Finder y enlace estándar a /Applications | Sin herramientas privadas ni modificación de otras apps |

Fondo PNG generado con AppKit a partir de `scripts/render-dmg-background.swift`;
no fuentes descargadas, imágenes generadas por IA ni dependencia de ejecución.
Los iconos de Safent y Aplicaciones son elementos reales de Finder, no botones
pintados. Sin movimiento: reduced-motion y teclado siguen el comportamiento del SO.

## Gates y límites

`scripts/validate-macos-distribution.py APP --version VERSION [--dmg-root ROOT]`
valida metadatos GUI, ejecutable/icono, versión, ID y firma Apple; con volumen
también valida raíz y enlace de arrastre. No instala, registra o abre nada.
Los tests usan fixtures de metadatos, nunca los presentan como apps firmadas.
El pipeline debe invocar el gate después de construir en macOS aun sin PKG.

Pruebas: 6 casos stdlib (incluidos subcasos de flags/identidad/versiones), esquema
real Tauri CLI 2.11.4, generación nativa del PNG y revisión visual del fondo.
Suite renderer: 156 PASS (10 archivos, 787 ms), typecheck/build PASS.
El layout final de Finder y la firma del nuevo DMG se comprobarán en la próxima
compilación firmada; no se ha publicado una nueva release en este corte.
