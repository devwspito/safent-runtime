# Quickstart — humo de extremo a extremo (028 + 029)

Objetivo: demostrar en un recorrido continuo que **un dueño que no sabe qué es un
contenedor** llega de la descarga al producto y a Anuncios funcionando, sin
escribir nada y sin ver un navegador. Cada bloque se apunta como PASA/FALLA con la
evidencia observada, igual que la matriz 025.

**Escenario canónico**: MacBook Air del dueño — macOS 15.5, Apple Silicon, 24 GB.
Tiene **podman 5 en `/opt/podman/bin` con una máquina libkrun** de antes: es el
caso de adopción real, no un equipo limpio. Un segundo pase en un Ubuntu 24.04
limpio cubre Linux. La DGX **no sirve** para esto: no tiene sesión gráfica.

## 0. Antes de empezar (30 s)

```
No se ejecuta ningún comando de Safent.
Se anota: espacio libre, si hay máquina de podman previa y su nombre,
si existe ~/.safent, y si hay un contenedor 'safent' anterior.
```
Esperado: la app tendrá que **convivir** con todo eso sin tocarlo.

## 1. Descarga e instalación (US1 de 028)

1. Descargar el `.dmg` de la release firmada.
2. Arrastrar Safent a Aplicaciones. Abrir desde el Launchpad.

**PASA si**: no aparece **ningún** aviso de origen desconocido (el DMG está
notarizado y grapado) · el icono, el nombre y la entrada del Dock son de Safent ·
**no se abre ninguna ventana de navegador**.

## 2. Primer arranque — preparación honesta

Observar la ventana sin tocar nada.

**PASA si** se ven etapas **nombradas** con avance real (al menos un cambio cada
5 s): comprobar el equipo → preparar la base de ejecución → preparar la máquina →
descargar Safent → arrancar → listo · la ventana **responde** todo el rato · existe
«Cancelar» hasta el punto de no retorno, y ese punto se **declara** antes.

**PASA además si**, en este equipo concreto, la máquina libkrun preexistente se
**adopta** (si sirve) o se deja **intacta** y se crea la propia — y en ninguno de
los dos casos se pregunta nada al dueño.

**FALLA si**: aparece una dirección local, un puerto, un `?k=`, un comando, una
barra de direcciones o una pestaña.

## 3. Producto listo

**PASA si**: la ventana muestra el producto · el chat responde · no hay ni un
elemento de navegador (probar el menú contextual y `Cmd+L`: nada de «abrir en el
navegador») · la barra de menús tiene el elemento de Safent con «Abrir»,
«Reiniciar el motor» y «Salir».

## 4. Reapertura y segunda instancia

1. Cerrar la ventana. **PASA si** el elemento de la barra de menús sigue ahí y el
   motor sigue vivo.
2. Volver a abrir desde el Dock. **PASA si** se llega al producto en **≤ 10 s** y
   no se repite la preparación.
3. Abrir Safent otra vez con la ventana ya abierta. **PASA si** se **enfoca la
   existente** y no aparece una segunda ventana.

## 5. Auto-sanación (el corazón del principio rector)

Provocar, uno a uno, y comprobar que **nadie pregunta nada**:

| Provocación | Esperado |
|---|---|
| Ocupar el puerto que la app eligió y reabrir | Elige otro y sigue. No lo enseña ni lo menciona |
| `podman stop safent` desde fuera y abrir la app | Lo levanta sola con progreso |
| Cortar la red a mitad de la descarga y devolverla | Reanuda; **no** vuelve a bajar las capas ya completas |
| Borrar `~/.safent/app/state.json` y abrir | Reconstruye el estado observando el equipo; no reinstala nada que ya esté |
| Parar el compañero por fuera y abrir «Anuncios» | Se repara solo o declara la causa con **un** «Reintentar» |
| Bloquear el registro (sin red) y abrir | **Una** pantalla honesta, un «Reintentar», **cero** instrucciones de terminal |

## 6. Anuncios de un botón (US1 de 029)

1. Ir a «Anuncios» en la barra lateral (o a Herramientas).
2. **PASA si** se ve **una sola** acción, «Instalar», y **ningún** campo de URL.
3. Pulsar «Instalar» **una vez** y no tocar nada más.

**PASA si**: se ven etapas reales (descargar la imagen → red → base de datos y
migraciones → arranque → registrar herramientas) con avance al menos cada 15 s ·
**Safent no se reinicia** (el andamiaje ya estaba) · al terminar, «Anuncios» pasa
a **`listo`** · las herramientas de anuncios del agente aparecen al listarlas.

4. Pulsar «Instalar» otra vez mientras instala. **PASA si** no arranca una segunda
   instalación.
5. Comprobar que el campo de URL heredado **sólo** existe tras abrir «avanzado».

## 7. Onboarding de Ads (US2 de 029)

Dentro del panel, sin salir de la ventana de Safent:

1. Introducir el cliente OAuth de Google Cloud (`client_id`, `client_secret` y,
   solo si se opera mediante una gestora, `login_customer_id`). No se solicita
   developer token. Verificar acceso a cuentas reales en el mismo proyecto Cloud.
2. Introducir la app de Meta (`app_id`, `app_secret`).
3. Conectar **una** cuenta por OAuth de cada plataforma.

**PASA si**: todo ocurre en la UI · el estado pasa de `unauthorized`/`no_accounts`
a `listo` · al releer, los secretos vuelven **enmascarados** · **no** aparece
ninguna credencial en registros, en `argv` ni en variables de entorno visibles.

## 8. Actualizar de una pulsación (US2 de 028)

1. Con todo al día, recorrer la app. **PASA si** «Actualizar» **no existe** en
   ninguna pantalla.
2. Publicar una versión nueva (envoltorio + motor + compañero).
3. **PASA si** aparece la acción diciendo qué trae.
4. Pulsar una vez, confirmar una vez y **no tocar nada más**.

**PASA si**: se ven las etapas · si hay trabajo del motor en curso, se **declara**
y se espera de forma acotada, sin preguntar · la app se cierra y se **vuelve a
abrir sola** · acaba **abierta en el producto** con la versión nueva · envoltorio,
motor y compañero **coinciden** · los datos siguen intactos (chat, memoria,
proveedores, credenciales de Ads).

5. Provocar un fallo (cortar la red durante la descarga de la versión nueva).
   **PASA si** el dueño se queda con la **versión anterior funcionando**, un
   mensaje con la causa y la acción disponible para reintentar.

## 9. Quitar y desinstalar (US3 de 028 · US3 de 029)

1. «Quitar» Anuncios. **PASA si** conserva estado y credenciales, y «Anuncios»
   vuelve a `no instalado` con «Instalar» disponible.
2. Volver a «Instalar». **PASA si** converge al mismo estado sin duplicar red,
   datos ni credenciales, y **sin volver a pedir credenciales**.
3. Desinstalar Safent desde la app, confirmando explícitamente. **PASA si** la
   ventana declara **qué se borra y qué se conserva** · se retira lo que esta
   instalación creó · **no** se toca ningún `safent-agent` ni CLI ajeno · el
   podman **empaquetado** se va con la app.

## 10. Diagnóstico sin secretos

Exportar el diagnóstico de un gesto y **buscar en él el vale de arranque exacto**.

**PASA si**: 0 coincidencias · 0 credenciales · el fichero es útil para soporte.

## Cierre — cuenta final

| Métrica | Objetivo |
|---|---|
| Ventanas o pestañas de navegador en todo el recorrido | **0** |
| Pasos de terminal | **0** |
| Direcciones locales visibles o copiables | **0** |
| Preguntas al dueño que no sean del sistema operativo | **0** |
| Avisos de origen desconocido | **0** |
| Ráfagas de errores de autorización | **0** (como mucho **una** pantalla de reconexión) |
| Relanzamientos manuales tras actualizar | **0** |
