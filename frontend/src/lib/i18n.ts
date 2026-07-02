/**
 * Lightweight i18n foundation — no external library.
 *
 * Pattern:
 *   import { useT } from '../lib/i18n'
 *   const t = useT()
 *   t('nav.chat')  // → "Chat" | "Chat"
 *
 * Extend the `translations` object to cover more of the app incrementally.
 * Agent language is NOT controlled here — Hermes responds in the user's
 * language naturally; this only controls the platform UI strings.
 */

import { createContext, useContext, useState, createElement, type ReactNode } from 'react'

export type Locale = 'es' | 'en'

const STORAGE_KEY = 'lumen_ui_locale'
const DEFAULT_LOCALE: Locale = 'es'

// ── Translation dictionary ────────────────────────────────────────────────────

const translations = {
  es: {
    // Navigation
    'nav.chat':           'Chat',
    'nav.programadas':    'Programadas',
    'nav.agentes':        'Agentes',
    'nav.skills':         'Habilidades',
    'nav.integraciones':  'Integraciones',
    'nav.mcp':            'Herramientas',
    'nav.archivos':       'Archivos',
    'nav.proveedores':    'Modelo de IA',
    'nav.seguridad':      'Seguridad',
    'nav.memoria':        'Memoria',
    'nav.coste':          'Coste',
    'nav.envivo':         'En vivo',
    'nav.ajustes':        'Ajustes',
    'nav.ajustes.pending_aria': '{count} aprobaciones pendientes',

    // Language selector
    'settings.language':  'Idioma',
    'settings.lang.es':   'Español',
    'settings.lang.en':   'English',

    // Approval card — titles by kind
    'approval.title.write_file':       'El agente quiere guardar un archivo',
    'approval.title.skill_manage':     'El agente quiere añadir una nueva capacidad',
    'approval.title.install_skill':    'El agente quiere añadir una nueva capacidad',
    'approval.title.install_mcp':      'El agente quiere conectar una herramienta externa',
    'approval.title.set_policy':       'El agente quiere cambiar sus propios permisos',
    'approval.title.disable_mfa':      'El agente quiere cambiar sus propios permisos',
    'approval.title.execute_code':     'El agente quiere ejecutar un comando',
    'approval.title.run_command':      'El agente quiere ejecutar un comando',
    'approval.title.send_message':     'El agente quiere enviar un mensaje',
    'approval.title.browser_navigate': 'El agente quiere abrir una página web',
    'approval.title.delegate_task':    'El agente quiere pedir ayuda a otro agente',
    'approval.title.cronjob':          'El agente quiere programar una tarea automática',

    // Approval card — badge labels
    'approval.badge.manual':     'Requiere aprobación manual',
    'approval.badge.attention':  'Requiere atención',
    'approval.badge.destructive':'Requiere aprobación manual',

    // Approval card — buttons
    'approval.btn.allow':        'Sí, permitir',
    'approval.btn.allow_mfa':    'Sí, permitir (necesita código)',
    'approval.btn.deny':         'No, rechazar',
    'approval.btn.allowing':     'Permitiendo…',
    'approval.btn.denying':      'Rechazando…',

    // Approval card — states / toasts
    'approval.toast.allowed':    'Acción permitida. El agente continúa.',
    'approval.toast.denied':     'Acción rechazada. El agente se ha detenido.',
    'approval.toast.err_allow':  'No se pudo aprobar. Inténtalo de nuevo.',
    'approval.toast.err_deny':   'No se pudo rechazar la acción. Inténtalo de nuevo.',
    'approval.details.toggle':   'Ver detalles técnicos',
    'approval.processing':       'Procesando…',
    'approval.expired':          'Esta solicitud caducó.',
    'approval.expired.close':    'Cerrar',
    'approval.err.retry':        'Reintentar',
    'approval.err.cancel':       'Cancelar',
    'approval.err.allow':        'No se pudo permitir la acción.',
    'approval.err.deny':         'No se pudo rechazar la acción.',

    // MFA enrollment nudge (inside card when mfa not enrolled)
    'approval.enroll.prompt':    'Esta acción necesita verificación en dos pasos. Actívala para autorizarla.',
    'approval.enroll.cta':       'Activar verificación en dos pasos',
    'approval.enroll.later':     'Ahora no',

    // MFA modal
    'mfa.title.code':        'Código de verificación',
    'mfa.btn.confirm':       'Confirmar con código',
    'mfa.btn.cancel':        'Cancelar',
    'mfa.err.empty':         'Introduce el código de 6 dígitos de tu app de verificación.',
    'mfa.err.invalid':       'Código incorrecto. Inténtalo de nuevo.',
    'mfa.placeholder':       '6 dígitos',

    // View titles (used in page <h1>)
    'view.chat':          'Chat',
    'view.agentes':       'Agentes',
    'view.skills':        'Habilidades',
    'view.integraciones': 'Integraciones',
    'view.mcp':           'Herramientas externas',
    'view.archivos':      'Archivos',
    'view.proveedores':   'Modelo de IA',
    'view.seguridad':     'Seguridad',
    'view.memoria':       'Memoria',
    'view.programadas':   'Tareas programadas',

    // Chat — errors and states
    'chat.err.connection':  'No se pudo conectar con el agente. Espera un momento y reinténtalo.',
    'chat.err.stream':      'Hubo un problema al recibir la respuesta. Inténtalo de nuevo.',
    'chat.err.timeout':     'El agente tardó demasiado en responder. Inténtalo de nuevo.',
    'chat.err.generic':     'Algo salió mal. El agente no pudo procesar tu mensaje. Inténtalo de nuevo.',
    'chat.err.provider':    'Tu modelo no respondió (error del proveedor). Revisa Modelo de IA: que el endpoint y la API key sean correctos y haya un modelo activo.',
    'chat.err.not_sent':    'No se envió. Inténtalo de nuevo.',
    'chat.err.attach':      'No se pudo adjuntar «{name}». Comprueba que no supera el límite de tamaño.',
    'chat.nomodel.text':    'El agente no tiene modelo de IA conectado. Conecta uno para empezar a chatear.',
    'chat.nomodel.cta':     'Conectar modelo',
    'chat.reconnecting':    'Reconectando…',

    // Seguridad view
    'seg.approvals.label':        'Acciones pendientes de aprobación',
    'seg.approvals.empty':        'Ninguna acción pendiente de tu aprobación.',
    'seg.mfa.label':              'Verificación de dos pasos',
    'seg.mfa.enrolled':           'Verificación de dos pasos activa. Aprobar acciones sensibles y cambiar permisos requiere tu código.',
    'seg.mfa.not_enrolled':       'Sin verificación de dos pasos no puedes autorizar acciones sensibles del agente. Actívala en menos de un minuto con tu app de códigos.',
    'seg.policies.label':         'Permisos del agente — qué puede hacer',
    'seg.policies.intro':         'Para cambiar los permisos necesitarás tu código de verificación. Esto evita que el agente modifique sus propios límites.',
    'seg.policies.dangers.label': 'Pedirme código de verificación antes de acciones sensibles',
    'seg.policies.dangers.off':   'Desactivado — el agente ejecuta acciones sensibles sin pedirte confirmación.',
    'seg.policies.dangers.on':    'Si lo desactivas, el agente ejecuta acciones sensibles en autónomo sin pedírtelo. Recomendado mantenerlo activo.',
    'seg.policies.preset.hint':   'Vista previa del preset «{preset}» — las capacidades de abajo ya reflejan el cambio. Guarda para aplicarlo.',
    'seg.preset.equilibrado.desc':'Capacidades estándar activas; las más sensibles requieren tu aprobación antes de ejecutarse.',
    'seg.preset.permisivo.desc':  'El agente puede hacer todo sin pedirte confirmación. Úsalo solo si confías plenamente en su configuración.',
    'seg.preset.bloqueado.desc':  'El agente solo puede conversar. No ejecuta ninguna acción ni accede a sistemas externos.',
    'seg.badge.attention':        'Requiere atención',
    'seg.badge.approval':         'Requiere aprobación manual',
    'seg.tool.native.tip':        'Esta capacidad está integrada en el sistema; el agente la usa directamente.',
    'seg.tool.native.label':      'integrado',
    'seg.network.label':          'Acceso a internet',
    'seg.network.mode.label':     'El agente puede acceder a internet',
    'seg.network.mode.hint':      'Cambiar el modo requiere tu código de verificación.',
    'seg.network.allow':          'Acceso libre',
    'seg.network.deny':           'Solo sitios autorizados',
    'seg.network.allow.intro':    'El agente puede acceder a internet. El sistema bloquea automáticamente sitios maliciosos conocidos, y tú puedes bloquear sitios concretos.',
    'seg.network.deny.intro':     'Por defecto el agente no puede acceder a ningún sitio web. Autoriza dominios concretos (p.ej. tu ERP, tu CRM, servicios de tu empresa). Aplica al navegador y al terminal del agente.',
    'seg.network.none_blocked':   'Ningún dominio bloqueado manualmente.',
    'seg.network.none_allowed':   'Ningún dominio autorizado — el agente no accede a la red.',
    'seg.network.revoke':         'Quitar acceso',
    'seg.scans.label':            'Análisis de seguridad recientes',
    'seg.scans.empty':            'No hay análisis de seguridad recientes.',
    'seg.scan.allow':             'Instalar bajo mi responsabilidad',
    'seg.scan.allowed':           'Autorizado',
    'seg.mfa_modal.preset':       'Aplicar configuración «{preset}»',
    'seg.mfa_modal.dangers_off':  'Desactivar verificación en acciones sensibles',
    'seg.mfa_modal.tools':        'Guardar cambios de permisos',
    'seg.save.ok':                'Cambios guardados',
    'seg.save.err':               'No se pudo guardar: {err}',
    'seg.preset.ok':              'Configuración «{preset}» aplicada',
    'seg.preset.err':             'No se pudo aplicar: {err}',
    'seg.dangers.on.ok':          'Verificación en acciones sensibles: activa',
    'seg.dangers.off.ok':         'Verificación en acciones sensibles: desactivada',
    'seg.allow_mode.ok':          'Modo acceso libre activado',
    'seg.deny_mode.ok':           'Modo solo sitios autorizados activado',

    // MFA enroll
    'mfa_enroll.activate':      'Activar verificación de dos pasos',
    'mfa_enroll.activating':    'Activando…',
    'mfa_enroll.err':           'No se pudo activar la verificación: {err}',
    'mfa_enroll.done':          'Listo, ya está configurado',

    // Skills view
    'skills.subtitle':          'Amplía las capacidades del agente. Busca, instala o enséñale desde una demostración.',
    'skills.state.autonomous':  'Sin supervisión',
    'skills.state.deprecated':  'Desactualizada',
    'skills.state.validated':   'Verificada',
    'skills.promote':           'Permitir al agente usarla sin preguntar',
    'skills.installed.label':   'Activas',
    'skills.installed.empty':   'Sin habilidades activas',
    'skills.teach.header':      'Crear una habilidad a medida',
    'skills.teach.synth':       'Procesando la demostración…',
    'skills.teach.stop':        'Terminar y guardar habilidad',
    'skills.teach.stop_paused': 'Terminar y guardar habilidad',
    'skills.teach.open':        'Enseñar habilidad',
    'skills.view':              'Ver',
    'skills.view.aria':         'Ver instrucciones de {name}',
    'skills.uninstall.aria':    'Desinstalar {name}',
    'skills.verify':            'Verificar',
    'skills.verify.aria':       'Verificar {name}',
    'skills.verify.tip':        'Pruébala y mira cómo trabaja',
    'skills.verify.msg':        'Usa la habilidad "{name}" y muéstrame el resultado.',
    'skills.live.badge':        'en vivo',
    'skills.live.tip':          'Puedes verla en directo cuando la usas',
    'skills.section.live':      'Enseñadas en vivo',
    'skills.section.rest':      'Habilidades',
    'skills.catalog.label':     'Catálogo',

    // MCP view
    'mcp.env.label':            'Claves de acceso (una por línea, formato CLAVE=VALOR)',
    'mcp.subtitle':             'Conecta servicios externos para que el agente acceda a más funciones.',

    // Integrations view
    'int.subtitle':             'Conecta el agente a tus apps. Más de 250 servicios disponibles.',

    // Calendar view
    'cal.status.failed':        'No se completó',

    // Agents / Office view — header & tabs
    'agents.subtitle.ready':      'Tu equipo de {count} agente',
    'agents.subtitle.ready_pl':   'Tu equipo de {count} agentes',
    'agents.subtitle.loading':    'Tu equipo de IA',
    'agents.subtitle.suffix':     '',
    'agents.tab.cards':           'Tarjetas',
    'agents.tab.live':            'En vivo',
    'agents.tab.premium':         'Premium',
    'agents.tab.aria':            'Vista de la oficina',
    'agents.create.btn':          '+ Crear agente',
    'agents.create.aria':         'Crear nuevo agente',

    // Agents / Office view — loading / empty states
    'agents.loading':             'Cargando la oficina…',
    'agents.map.loading':         'Cargando mapa…',
    'agents.empty.text':          'Aún no tienes agentes.',
    'agents.empty.cta':           'Crear tu primer agente',
    'agents.error.title':         'No se pudo cargar el equipo',
    'agents.error.retry':         'Reintentar',

    // Agents / Office view — Premium floor
    'agents.premium.today_action':    '{count} acción hoy',
    'agents.premium.today_action_pl': '{count} acciones hoy',

    // Agents / Office view — fullscreen
    'agents.fullscreen':          'Pantalla completa',

    // Agents / Office view — department section chrome
    'agents.dept.cerebro.desc':   'Orquestador principal — coordina todos los agentes.',
    'agents.dept.factory.desc':   'Agentes especializados del sistema — solo lectura.',
    'agents.dept.factory.tag':    'Sistema',
    'agents.dept.mine.title':     'Mis agentes',
    'agents.dept.mine.empty':     'No tienes agentes personalizados aún.',
    'agents.dept.swarm.title':    'Agentes del sistema',
    'agents.dept.swarm.desc':     'Agentes del sistema conectados — disponibles para el agente en tiempo real.',
    'agents.dept.swarm.active':   'Activo',

    // Agents / Office view — agent card & status
    'agents.card.aria':           '{name}, trabajando. Click para ver detalle.',
    'agents.card.aria_idle':      '{name}. Click para ver detalle.',
    'agents.status.working':      'Trabajando',
    'agents.status.online':       'En línea',
    'agents.badge.default':       'CEO',
    'agents.badge.factory':       'Del sistema',

    // Agents / Office view — create card
    'agents.card.create.aria':    'Crear nuevo agente',
    'agents.card.create.label':   'Crear agente',

    // Agents / Office view — agent drawer
    'agents.drawer.close':        'Cerrar',
    'agents.drawer.chat':         'Chatear',
    'agents.drawer.chat.title':   'Iniciar un chat con este agente',
    'agents.drawer.clone':        'Clonar y personalizar',
    'agents.drawer.schedule':     'Programar tarea',
    'agents.drawer.edit':         'Editar',
    'agents.drawer.delete':       'Borrar',
    'agents.drawer.readonly.default': 'No editable (puedes clonarlo para crear tu propia versión).',
    'agents.drawer.readonly.factory': 'Agente del sistema — clónalo para personalizar.',
    'agents.drawer.confirm.title':    '¿Eliminar "{name}"?',
    'agents.drawer.confirm.desc':     'El agente se eliminará permanentemente. Esta acción no se puede deshacer.',
    'agents.drawer.confirm.confirm':  'Eliminar',
    'agents.drawer.toast.deleted':    '{name} eliminado',
    'agents.drawer.toast.delete_err': 'No se pudo eliminar el agente.',
    'agents.clone.name_suffix':       ' (copia)',
    'agents.clone.default_dept':      'Mis agentes',

    // Agents / Office view — form modal
    'agents.form.title.create':   'Nuevo agente',
    'agents.form.title.clone':    'Clonar agente',
    'agents.form.title.edit':     'Editar agente',
    'agents.form.close':          'Cerrar',
    'agents.form.clone.hint':     'Copia personalizable de un agente. Puedes modificarla libremente.',
    'agents.form.name.label':     'Nombre *',
    'agents.form.name.placeholder': 'Ej: Asistente ventas',
    'agents.form.desc.label':     'Descripción',
    'agents.form.desc.placeholder': 'Describe la tarea principal del agente…',
    'agents.form.dept.label':     'Departamento',
    'agents.form.err.name':       'El nombre es obligatorio.',
    'agents.form.err.create':     'Error al crear el agente.',
    'agents.form.err.edit':       'Error al guardar el agente.',
    'agents.form.submit.create':  'Crear agente',
    'agents.form.submit.creating':'Creando…',
    'agents.form.submit.clone':   'Crear copia',
    'agents.form.submit.cloning': 'Clonando…',
    'agents.form.submit.edit':    'Guardar cambios',
    'agents.form.submit.saving':  'Guardando…',
    'agents.form.cancel':         'Cancelar',

    // Agents / Office view — dept selector
    'agents.dept.none':           'Sin departamento',
    'agents.dept.new':            'Nuevo departamento…',
    'agents.dept.new.placeholder':'Nombre del nuevo departamento',
    'agents.dept.clear.aria':     'Borrar búsqueda',

    // Agents / Office view — default roster toggle
    'agents.roster.toggle.label':        'Equipo por defecto',
    'agents.roster.toggle.tooltip':      'Apágalo si quieres usar solo tu propio equipo; el CEO y tus agentes se mantienen.',
    'agents.roster.toggle.on':           'Activado',
    'agents.roster.toggle.off':          'Apagado',
    'agents.roster.toggle.err':          'No se pudo cambiar el equipo por defecto.',

    // Agents / Office view — live canvas
    'agents.canvas.aria':                'Vista isométrica de la oficina con los agentes',
    'agents.canvas.hint.detail':         'ver detalle',
    'agents.canvas.furniture.bookshelf': 'Conocimiento',
    'agents.canvas.furniture.whiteboard':'Reglas',
    'agents.canvas.furniture.tv':        'Workflows',
    'agents.canvas.furniture.printer':   'Auditoría',
    'agents.canvas.furniture.router':    'Gateway',
    'agents.canvas.furniture.toolbox':   'Herramientas',
    'agents.canvas.furniture.emptydesk': 'Nuevo Agente',
  },

  en: {
    // Navigation
    'nav.chat':           'Chat',
    'nav.programadas':    'Scheduled',
    'nav.agentes':        'Agents',
    'nav.skills':         'Skills',
    'nav.integraciones':  'Integrations',
    'nav.mcp':            'Tools',
    'nav.archivos':       'Files',
    'nav.proveedores':    'AI Model',
    'nav.seguridad':      'Security',
    'nav.memoria':        'Memory',
    'nav.coste':          'Cost',
    'nav.envivo':         'Live',
    'nav.ajustes':        'Settings',
    'nav.ajustes.pending_aria': '{count} pending approvals',

    // Language selector
    'settings.language':  'Language',
    'settings.lang.es':   'Español',
    'settings.lang.en':   'English',

    // Approval card — titles by kind
    'approval.title.write_file':       'The agent wants to save a file',
    'approval.title.skill_manage':     'The agent wants to add a new capability',
    'approval.title.install_skill':    'The agent wants to add a new capability',
    'approval.title.install_mcp':      'The agent wants to connect an external tool',
    'approval.title.set_policy':       'The agent wants to change its own permissions',
    'approval.title.disable_mfa':      'The agent wants to change its own permissions',
    'approval.title.execute_code':     'The agent wants to run a command',
    'approval.title.run_command':      'The agent wants to run a command',
    'approval.title.send_message':     'The agent wants to send a message',
    'approval.title.browser_navigate': 'The agent wants to open a webpage',
    'approval.title.delegate_task':    'The agent wants to ask another agent for help',
    'approval.title.cronjob':          'The agent wants to schedule an automated task',

    // Approval card — badge labels
    'approval.badge.manual':      'Requires manual approval',
    'approval.badge.attention':   'Requires attention',
    'approval.badge.destructive': 'Requires manual approval',

    // Approval card — buttons
    'approval.btn.allow':        'Yes, allow',
    'approval.btn.allow_mfa':    'Yes, allow (needs code)',
    'approval.btn.deny':         'No, deny',
    'approval.btn.allowing':     'Allowing…',
    'approval.btn.denying':      'Denying…',

    // Approval card — states / toasts
    'approval.toast.allowed':    'Action allowed. The agent is continuing.',
    'approval.toast.denied':     'Action denied. The agent has stopped.',
    'approval.toast.err_allow':  'Could not approve. Please try again.',
    'approval.toast.err_deny':   'Could not deny the action. Please try again.',
    'approval.details.toggle':   'View technical details',
    'approval.processing':       'Processing…',
    'approval.expired':          'This request has expired.',
    'approval.expired.close':    'Close',
    'approval.err.retry':        'Retry',
    'approval.err.cancel':       'Cancel',
    'approval.err.allow':        'Could not allow the action.',
    'approval.err.deny':         'Could not deny the action.',

    // MFA enrollment nudge
    'approval.enroll.prompt':    'This action requires two-step verification. Enable it to authorize.',
    'approval.enroll.cta':       'Enable two-step verification',
    'approval.enroll.later':     'Not now',

    // MFA modal
    'mfa.title.code':        'Verification code',
    'mfa.btn.confirm':       'Confirm with code',
    'mfa.btn.cancel':        'Cancel',
    'mfa.err.empty':         'Enter the 6-digit code from your authenticator app.',
    'mfa.err.invalid':       'Incorrect code. Please try again.',
    'mfa.placeholder':       '6 digits',

    // View titles
    'view.chat':          'Chat',
    'view.agentes':       'Agents',
    'view.skills':        'Skills',
    'view.integraciones': 'Integrations',
    'view.mcp':           'External Tools',
    'view.archivos':      'Files',
    'view.proveedores':   'AI Model',
    'view.seguridad':     'Security',
    'view.memoria':       'Memory',
    'view.programadas':   'Scheduled Tasks',

    // Chat — errors and states
    'chat.err.connection':  'Could not connect to the agent. Wait a moment and try again.',
    'chat.err.stream':      'There was a problem receiving the response. Please try again.',
    'chat.err.timeout':     'The agent took too long to respond. Please try again.',
    'chat.err.generic':     'Something went wrong. The agent could not process your message. Please try again.',
    'chat.err.provider':    'Your model did not respond (provider error). Check AI Model: that the endpoint and API key are correct and a model is active.',
    'chat.err.not_sent':    'Not sent. Please try again.',
    'chat.err.attach':      'Could not attach «{name}». Check it doesn\'t exceed the size limit.',
    'chat.nomodel.text':    'The agent has no AI model connected. Connect one to start chatting.',
    'chat.nomodel.cta':     'Connect model',
    'chat.reconnecting':    'Reconnecting…',

    // Seguridad view
    'seg.approvals.label':        'Actions awaiting your approval',
    'seg.approvals.empty':        'No actions pending your approval.',
    'seg.mfa.label':              'Two-step verification',
    'seg.mfa.enrolled':           'Two-step verification active. Authorizing sensitive actions and changing permissions requires your code.',
    'seg.mfa.not_enrolled':       'Without two-step verification you cannot authorize sensitive agent actions. Set it up in under a minute with your authenticator app.',
    'seg.policies.label':         'Agent permissions — what it can do',
    'seg.policies.intro':         'Changing permissions requires your verification code. This prevents the agent from modifying its own limits.',
    'seg.policies.dangers.label': 'Ask me for a verification code before sensitive actions',
    'seg.policies.dangers.off':   'Disabled — the agent runs sensitive actions without asking you.',
    'seg.policies.dangers.on':    'If disabled, the agent runs sensitive actions autonomously without asking. Recommended to keep enabled.',
    'seg.policies.preset.hint':   'Preview of «{preset}» — the capabilities below already reflect the change. Save to apply.',
    'seg.preset.equilibrado.desc':'Standard capabilities active; the most sensitive ones require your approval before running.',
    'seg.preset.permisivo.desc':  'The agent can do everything without asking for confirmation. Use only if you fully trust its setup.',
    'seg.preset.bloqueado.desc':  'The agent can only chat. It runs no actions and accesses no external systems.',
    'seg.badge.attention':        'Requires attention',
    'seg.badge.approval':         'Requires manual approval',
    'seg.tool.native.tip':        'This capability is built into the system; the agent uses it directly.',
    'seg.tool.native.label':      'built-in',
    'seg.network.label':          'Internet access',
    'seg.network.mode.label':     'The agent can access the internet',
    'seg.network.mode.hint':      'Changing the mode requires your verification code.',
    'seg.network.allow':          'Open access',
    'seg.network.deny':           'Approved sites only',
    'seg.network.allow.intro':    'The agent can access the internet. The system automatically blocks known malicious sites, and you can block specific sites.',
    'seg.network.deny.intro':     'By default the agent cannot access any website. Authorize specific domains (e.g. your ERP, your CRM, your company services). Applies to the agent\'s browser and terminal.',
    'seg.network.none_blocked':   'No domains blocked manually.',
    'seg.network.none_allowed':   'No domains authorized — the agent has no internet access.',
    'seg.network.revoke':         'Remove access',
    'seg.scans.label':            'Recent security scans',
    'seg.scans.empty':            'No recent security scans.',
    'seg.scan.allow':             'Install at my own risk',
    'seg.scan.allowed':           'Authorized',
    'seg.mfa_modal.preset':       'Apply configuration «{preset}»',
    'seg.mfa_modal.dangers_off':  'Disable verification on sensitive actions',
    'seg.mfa_modal.tools':        'Save permission changes',
    'seg.save.ok':                'Changes saved',
    'seg.save.err':               'Could not save: {err}',
    'seg.preset.ok':              'Configuration «{preset}» applied',
    'seg.preset.err':             'Could not apply: {err}',
    'seg.dangers.on.ok':          'Verification on sensitive actions: active',
    'seg.dangers.off.ok':         'Verification on sensitive actions: disabled',
    'seg.allow_mode.ok':          'Open access mode enabled',
    'seg.deny_mode.ok':           'Approved sites only mode enabled',

    // MFA enroll
    'mfa_enroll.activate':      'Enable two-step verification',
    'mfa_enroll.activating':    'Enabling…',
    'mfa_enroll.err':           'Could not enable verification: {err}',
    'mfa_enroll.done':          'Done, it\'s all set up',

    // Skills view
    'skills.subtitle':          'Extend the agent\'s capabilities. Search, install, or teach it from a demonstration.',
    'skills.state.autonomous':  'Unsupervised',
    'skills.state.deprecated':  'Outdated',
    'skills.state.validated':   'Verified',
    'skills.promote':           'Let the agent use it without asking',
    'skills.installed.label':   'Active',
    'skills.installed.empty':   'No active skills',
    'skills.teach.header':      'Create a custom skill',
    'skills.teach.synth':       'Processing the demonstration…',
    'skills.teach.stop':        'Finish and save skill',
    'skills.teach.stop_paused': 'Finish and save skill',
    'skills.teach.open':        'Teach skill',
    'skills.view':              'View',
    'skills.view.aria':         'View instructions for {name}',
    'skills.uninstall.aria':    'Uninstall {name}',
    'skills.verify':            'Verify',
    'skills.verify.aria':       'Verify {name}',
    'skills.verify.tip':        'Try it and watch it work',
    'skills.verify.msg':        'Use the "{name}" skill and show me the result.',
    'skills.live.badge':        'live',
    'skills.live.tip':          'You can watch it live as it runs',
    'skills.section.live':      'Taught live',
    'skills.section.rest':      'Skills',
    'skills.catalog.label':     'Catalog',

    // MCP view
    'mcp.env.label':            'Access keys (one per line, format KEY=VALUE)',
    'mcp.subtitle':             'Connect external services to give the agent access to more features.',

    // Integrations view
    'int.subtitle':             'Connect the agent to your apps. Over 250 services available.',

    // Calendar view
    'cal.status.failed':        'Did not complete',

    // Agents / Office view — header & tabs
    'agents.subtitle.ready':      'Your team of {count} agent',
    'agents.subtitle.ready_pl':   'Your team of {count} agents',
    'agents.subtitle.loading':    'Your AI team',
    'agents.subtitle.suffix':     '',
    'agents.tab.cards':           'Cards',
    'agents.tab.live':            'Live',
    'agents.tab.premium':         'Premium',
    'agents.tab.aria':            'Office view',
    'agents.create.btn':          '+ New agent',
    'agents.create.aria':         'Create new agent',

    // Agents / Office view — loading / empty states
    'agents.loading':             'Loading the office…',
    'agents.map.loading':         'Loading map…',
    'agents.empty.text':          'You have no agents yet.',
    'agents.empty.cta':           'Create your first agent',
    'agents.error.title':         'We could not load your team',
    'agents.error.retry':         'Retry',

    // Agents / Office view — Premium floor
    'agents.premium.today_action':    '{count} action today',
    'agents.premium.today_action_pl': '{count} actions today',

    // Agents / Office view — fullscreen
    'agents.fullscreen':          'Full screen',

    // Agents / Office view — department section chrome
    'agents.dept.cerebro.desc':   'Main orchestrator — coordinates all agents.',
    'agents.dept.factory.desc':   'Specialized system agents — read-only.',
    'agents.dept.factory.tag':    'System',
    'agents.dept.mine.title':     'My agents',
    'agents.dept.mine.empty':     'You have no custom agents yet.',
    'agents.dept.swarm.title':    'System agents',
    'agents.dept.swarm.desc':     'System agents connected — available to the agent in real time.',
    'agents.dept.swarm.active':   'Active',

    // Agents / Office view — agent card & status
    'agents.card.aria':           '{name}, working. Click to view details.',
    'agents.card.aria_idle':      '{name}. Click to view details.',
    'agents.status.working':      'Working',
    'agents.status.online':       'Online',
    'agents.badge.default':       'CEO',
    'agents.badge.factory':       'System',

    // Agents / Office view — create card
    'agents.card.create.aria':    'Create new agent',
    'agents.card.create.label':   'New agent',

    // Agents / Office view — agent drawer
    'agents.drawer.close':        'Close',
    'agents.drawer.chat':         'Chat',
    'agents.drawer.chat.title':   'Start a chat with this agent',
    'agents.drawer.clone':        'Clone and customize',
    'agents.drawer.schedule':     'Schedule task',
    'agents.drawer.edit':         'Edit',
    'agents.drawer.delete':       'Delete',
    'agents.drawer.readonly.default': 'Not editable (you can clone it to create your own version).',
    'agents.drawer.readonly.factory': 'System agent — clone it to customize.',
    'agents.drawer.confirm.title':    'Delete "{name}"?',
    'agents.drawer.confirm.desc':     'The agent will be permanently deleted. This action cannot be undone.',
    'agents.drawer.confirm.confirm':  'Delete',
    'agents.drawer.toast.deleted':    '{name} deleted',
    'agents.drawer.toast.delete_err': 'Could not delete the agent.',
    'agents.clone.name_suffix':       ' (copy)',
    'agents.clone.default_dept':      'My agents',

    // Agents / Office view — form modal
    'agents.form.title.create':   'New agent',
    'agents.form.title.clone':    'Clone agent',
    'agents.form.title.edit':     'Edit agent',
    'agents.form.close':          'Close',
    'agents.form.clone.hint':     'A customizable copy of an agent. You can modify it freely.',
    'agents.form.name.label':     'Name *',
    'agents.form.name.placeholder': 'E.g. Sales assistant',
    'agents.form.desc.label':     'Description',
    'agents.form.desc.placeholder': 'Describe the agent\'s main task…',
    'agents.form.dept.label':     'Department',
    'agents.form.err.name':       'Name is required.',
    'agents.form.err.create':     'Error creating the agent.',
    'agents.form.err.edit':       'Error saving the agent.',
    'agents.form.submit.create':  'Create agent',
    'agents.form.submit.creating':'Creating…',
    'agents.form.submit.clone':   'Create copy',
    'agents.form.submit.cloning': 'Cloning…',
    'agents.form.submit.edit':    'Save changes',
    'agents.form.submit.saving':  'Saving…',
    'agents.form.cancel':         'Cancel',

    // Agents / Office view — dept selector
    'agents.dept.none':           'No department',
    'agents.dept.new':            'New department…',
    'agents.dept.new.placeholder':'New department name',
    'agents.dept.clear.aria':     'Clear',

    // Agents / Office view — default roster toggle
    'agents.roster.toggle.label':        'Default team',
    'agents.roster.toggle.tooltip':      'Turn it off to use only your own team; the CEO and your agents remain.',
    'agents.roster.toggle.on':           'On',
    'agents.roster.toggle.off':          'Off',
    'agents.roster.toggle.err':          'Could not change the default team setting.',

    // Agents / Office view — live canvas
    'agents.canvas.aria':                'Isometric office view with agents',
    'agents.canvas.hint.detail':         'view details',
    'agents.canvas.furniture.bookshelf': 'Knowledge',
    'agents.canvas.furniture.whiteboard':'Rules',
    'agents.canvas.furniture.tv':        'Workflows',
    'agents.canvas.furniture.printer':   'Audit',
    'agents.canvas.furniture.router':    'Gateway',
    'agents.canvas.furniture.toolbox':   'Tools',
    'agents.canvas.furniture.emptydesk': 'New Agent',
  },
} as const

type TranslationKey = keyof (typeof translations)['es']

// ── Context ───────────────────────────────────────────────────────────────────

interface I18nContextValue {
  locale: Locale
  setLocale: (l: Locale) => void
}

const I18nContext = createContext<I18nContextValue>({
  locale: DEFAULT_LOCALE,
  setLocale: () => undefined,
})

// ── Provider ──────────────────────────────────────────────────────────────────

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'en' || stored === 'es' ? stored : DEFAULT_LOCALE
  })

  function setLocale(l: Locale) {
    setLocaleState(l)
    localStorage.setItem(STORAGE_KEY, l)
  }

  return createElement(I18nContext.Provider, { value: { locale, setLocale } }, children)
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useLocale(): I18nContextValue {
  return useContext(I18nContext)
}

export function useT(): (key: TranslationKey, fallback?: string) => string {
  const { locale } = useContext(I18nContext)
  return function t(key: TranslationKey, fallback?: string): string {
    const dict = translations[locale] as Record<string, string>
    return dict[key] ?? fallback ?? key
  }
}

/** Derive a human title from the approval's kind field. Falls back to the summary. */
export function approvalTitle(kind: string | undefined, summary: string, locale: Locale): string {
  if (!kind) return summary
  const key = `approval.title.${kind}` as TranslationKey
  const dict = translations[locale] as Record<string, string>
  return dict[key] ?? summary
}

