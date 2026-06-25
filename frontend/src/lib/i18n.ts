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

