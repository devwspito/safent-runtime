# 032 — Safent para Claude Code y Codex

Fecha: 14 de septiembre de 2026 (v2, tarde: el vínculo con el panel web y con Enterprise se hace en la instalación, por OAuth; Anuncios alojado en Safent Cloud). Decisión del dueño: «la versión instalable que de verdad funcione perfecto en Claude Code o Codex» es la prioridad. La app propia con Hermes se mantiene y se pule aparte; no se toca en esta especificación.

## Qué es

Safent deja de ser solo «una app con su propio agente» y pasa a ser **la capa de gobierno que se instala dentro de Claude Code y de Codex**: un comando, y el usuario tiene las herramientas de Safent (Anuncios incluido) gobernadas por Safent (políticas por herramienta, freno de gasto, aprobaciones, auditoría), con su cuenta de Anthropic u OpenAI y el harness de ellos.

Community es el puesto de una persona. Enterprise sigue siendo la web en la nube que administra empresas, personas y sus puestos; un puesto pasa a ser «lanzador de Safent + Claude Code o Codex».

## Usuarios y trabajos

- **Persona con Claude Code o Codex** (P1). Quiere instalar Safent con un comando, conectar sus cuentas de anuncios desde el chat y operar campañas con freno de gasto y aprobaciones, sin ver podman, puertos ni credenciales en ficheros.
- **Empresa** (P2). Quiere dar de alta puestos, asignar cuentas y capacidades por persona, encargar trabajo al agente de un puesto y ver auditoría, sin custodiar claves de modelo.
- **Dueño de Safent** (P3). Quiere publicar una versión y que se actualice sola en todos los puestos.

## Historias, por prioridad

### P1 — Instalar y operar Anuncios desde Claude Code (el primer recorrido)

1. En Claude Code, `/plugin install safent@safent-plugins` (o `claude mcp add --transport http safent https://mcp.safent.app/mcp`). En Codex, `safent codex install` (o `[mcp_servers.safent]` con `auth = "oauth"`).
2. `claude mcp login safent` / `codex mcp login safent` abre el navegador contra Safent Cloud; la persona entra con su cuenta y **la instalación queda vinculada** con un identificador único: aparece en el panel web como «Claude Code en el MacBook de Luis». Si la invitó una empresa, queda ligada a su puesto con las cuentas y políticas de la organización. Nada que instalar en la máquina: Anuncios corre en Safent Cloud.
3. El usuario escribe «conecta mi cuenta de Google Ads»; la herramienta devuelve el enlace de consentimiento; tras el OAuth, las cuentas aparecen en el chat y en el panel web.
4. «Guarda dos borradores de campaña para Friendog»: los borradores se guardan con `propose_campaign_draft` a la primera; el esquema completo es visible para el modelo.
5. «Aprueba la propuesta 3 con 20 € al día»: la herramienta está marcada como acción con dinero; Claude Code muestra la tarjeta de confirmación de forma nativa; con la confirmación, el companion aplica la propuesta dentro de sus límites y freno.
6. `/safent:panel` abre el panel web de Safent en el cuadro de mando de Anuncios; desde el panel (web o móvil) se aprueban las acciones que la política de la organización reserve al panel.

**Aceptación P1**: un usuario nuevo, en un Mac limpio y sin runtime local, completa 1→6 sin abrir un terminal aparte ni tocar ficheros de configuración, con Claude Code y con Codex; su instalación aparece en el panel web con nombre y se puede revocar desde allí. Cada acción con dinero deja rastro en la auditoría y respeta los límites del companion.

### P2 — Gobierno de las herramientas nativas del harness

7. El plugin instala hooks: antes de cada herramienta de Claude Code (comandos, ficheros, red) se consulta a Safent, que responde permitir, preguntar o denegar según la política del puesto; después de cada herramienta se escribe en la auditoría.
8. En Codex, el lanzador configura la política de aprobación y el sandbox del puesto, y el modo de aprobación de cada herramienta MCP de Safent (`auto`, `prompt`, `writes`, `approve`).
9. El tráfico de red del harness sale por el proxy de Safent con lista blanca cuando la política del puesto lo exige.

**Aceptación P2**: con la política «solo lectura», un `rm -rf` o un `curl` a un dominio no permitido se deniegan antes de ejecutarse; con «preguntar», aparece la confirmación nativa; todo queda auditado con la razón.

### P3 — Enterprise con puestos Claude Code o Codex

10. Un puesto se empareja con la empresa con un código (`safent pair`), igual que hoy, y recibe políticas, cuentas asignadas y capacidades.
11. Un administrador encarga trabajo a un puesto desde Equipo; el lanzador ejecuta una sesión sin ventana del harness con el encargo, con las mismas admisiones y aprobaciones del puesto, y devuelve el resultado.
12. La empresa no custodia claves de modelo: cada persona entra con su cuenta de Anthropic u OpenAI o con el plan de equipo de su proveedor.

**Aceptación P3**: dos empresas, dos puestos, un encargo entregado, aprobado localmente, ejecutado y devuelto; el segundo puesto no ve nada del primero; revocar la licencia corta el puesto.

### P4 — Actualización

13. `safent` se actualiza solo (paquete del lanzador por versión, imágenes por digest firmadas en el manifiesto) y el plugin fija la versión mínima del lanzador.

## Reglas

- Un solo servidor MCP visible para el harness (`safent`), remoto en Safent Cloud, que expone las herramientas de la organización y aplica políticas antes de reenviar. Los servidores internos nunca se exponen directamente.
- El vínculo con el panel web y con Enterprise es el login OAuth del harness: cada login crea o reanuda una **instalación** con identificador único, persona, organización, equipo y harness; revocable desde el panel. Los códigos de emparejamiento quedan para el runtime local y los servidores sin navegador (`safent pair`).
- El usuario conversa con Claude Code o Codex; Safent no se interpone en el chat. Safent recibe llamadas de herramientas y devuelve resultados o «pendiente de aprobación»; el panel resuelve lo pendiente por el relé.
- Toda herramienta con efecto sobre dinero o publicación lleva `_meta["anthropic/requiresUserInteraction"]` (Claude Code) y `approval_mode = "approve"` (Codex): la confirmación es nativa y no se puede autoaprobar por reglas.
- El freno de gasto, los límites por cuenta, la firma de propuestas y la reconciliación siguen en el companion; el harness no los toca.
- Sin credenciales en texto claro en ficheros de configuración del harness: el lanzador entrega tokens de sesión cortos por variable de entorno o cabecera generada en el momento.
- La ventana de Safent es el único panel gráfico; en Claude Desktop y ChatGPT se incrusta el mismo panel como MCP App. Claude Code y Codex no admiten paneles de terceros.
- Nada de esto cambia la app propia con Hermes: comparten runtime, companion, políticas y auditoría.

## Fuera de alcance

- Sustituir Hermes en la app propia (se decide aparte, en su momento).
- Windows.
- Paneles dentro de las apps de escritorio de Claude Code y Codex (no existe punto de extensión documentado).
- Un chat propio en el panel web (el chat es el del harness; la app con Hermes cubre el caso «todo en Safent»).

## Criterios de éxito

- SC-1: instalación + primer uso en un Mac limpio en menos de 5 minutos de reloj, sin pasos manuales, en Claude Code y en Codex.
- SC-2: el recorrido P1 completo sale a la primera en 10 de 10 intentos con `gpt-5.6` en Codex y con el modelo por defecto de Claude Code.
- SC-3: ninguna acción con dinero se ejecuta sin confirmación nativa; 100 % de esas acciones en la auditoría con su firma.
- SC-4: cero credenciales de proveedor en ficheros del harness (comprobado por barrido).
- SC-5: un puesto emparejado recibe y ejecuta un encargo de Enterprise de principio a fin.
