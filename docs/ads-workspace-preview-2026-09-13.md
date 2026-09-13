# Ads: comprobación visual aislada — 13/09/2026

Esta prueba no es aceptación del binario nativo ni validación de OAuth.

## Fuentes y aislamiento

- Runtime: interfaz `eeac35f`, snapshot DGX
  `/tmp/safent-sidebar-23c44ef.NNKIYs/frontend/dist`.
- Ads: interfaz `654daf3`, build
  `/tmp/safent-ads-oauth-copy.YIqBdA/panel/dist`.
- Servidor aislado: `/tmp/safent-isolated-preview.5WoP5C`, loopback 5197/5198,
  acceso mediante túnel SSH. No proxy de producción.
- Banner explícito de datos ficticios, empresa de prueba sin cuentas OAuth.
  Escrituras rechazadas. Handshake local de presentación simulado.

## Comprobaciones visuales realizadas por el coordinador

1. Abrir Community, entrar mediante el enlace real Anuncios (`/app/anuncios`).
2. Confirmar en captura y árbol accesible: desaparece el sidebar de Community;
   sólo queda la navegación del panel Ads. Botón Volver a Safent enfocado.
3. Navegar dentro del iframe a Conexiones y comprobar tarjetas Google/Meta
   deshabilitadas con explicación; ninguna cuenta ni autorización ficticia.
4. Pulsar Volver a Safent después de navegar dentro del iframe.
5. Confirmar URL `/app/chat`, sidebar Community visible y foco de vuelta en el
   enlace Anuncios. No se usa el historial del iframe como retorno.

## Revisión Emil

| Antes | Después | Motivo |
| --- | --- | --- |
| Dos barras laterales simultáneas | Una navegación de Ads al entrar | Mantener espacio y jerarquía claras |
| Retorno ambiguo después de navegar en el iframe | Volver a Safent restaura ruta y foco | Navegación predecible y accesible |

No se añadieron animaciones a esta navegación frecuente.

## Límites y defecto adicional detectado

La primera fixture de onboarding devolvía una lista vacía y ocultaba la lista
técnica de pasos. Se corrigió a los cuatro estados reales de una instalación sin
configurar. La revisión pidió mover esos pasos al detalle de administración,
para que la acción Conectar quede primero también con datos reales. Esa revisión
posterior de Ads y la aceptación de su nuevo build se registran por separado.

Los errores de freno/datos del cockpit visibles en esta preview son endpoints
no simulados, respondidos explícitamente como no disponibles. No prueban un
fallo nuevo de producción ni justifican presentar esos módulos como verificados.

Pendiente: aceptación del instalador nuevo, proveedor autorizado, regreso real
desde navegador externo y cuentas publicitarias reales. No se modificó la app
firmada instalada, su motor, su base de datos ni sus conexiones.
