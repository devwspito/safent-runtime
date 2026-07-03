/**
 * Section hubs — the sidebar stays at four clean entries (Chat, Agentes,
 * Capacidades, Sistema); everything else lives INSIDE the two hubs as tabs
 * (owner decision). Each tab renders the existing view component as-is —
 * this file only owns tab selection.
 *
 *   Capacidades → Habilidades · Integraciones · Herramientas · En vivo
 *   Sistema     → Seguridad · Coste · Modelo de IA · Programadas · Memoria · Archivos
 */
import { lazy, Suspense, useMemo } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { Tabs, type Tab } from '../components/ui/Tabs'
import { useFeatures } from '../hooks/useFeatures'
import { usePendingApprovals } from '../hooks/usePendingApprovals'
import { useT, type TranslationKey } from '../lib/i18n'
import SkillsView from './SkillsView'
import IntegrationsView from './IntegrationsView'
import McpView from './McpView'
import ProvidersView from './ProvidersView'
import SeguridadView from './SeguridadView'
import MemoriaView from './MemoriaView'
import ArchivosView from './ArchivosView'
import styles from './SectionHubs.module.css'

// Same two views App.tsx used to lazy-load at their standalone routes
// (recharts / the VNC canvas aren't needed on the other tabs).
const UsageView = lazy(() => import('./UsageView'))
const EnVivoView = lazy(() => import('./EnVivoView'))

function TabFallback() {
  return (
    <div className="view-body" aria-busy="true">
      <div className="skeleton skeleton--card" />
    </div>
  )
}

interface HubTab {
  key: string
  labelKey: TranslationKey
  render: () => React.ReactNode
}

function Hub({ tabs, ariaLabelKey, tabAlerts }: {
  tabs: HubTab[]
  ariaLabelKey: TranslationKey
  /** Per-tab needs-attention counts (e.g. pending approvals on 'seguridad'). */
  tabAlerts?: Record<string, number>
}) {
  const t = useT()
  const { isLoading: featuresLoading, allowed } = useFeatures()
  const [searchParams, setSearchParams] = useSearchParams()

  const visibleTabs = useMemo(
    () => tabs.filter((tab) => allowed(tab.key)),
    // allowed is stable per features state; tabs is a module-level constant per hub
    [tabs, allowed],
  )
  const uiTabs: Tab[] = visibleTabs.map(({ key, labelKey }) => ({
    key,
    label: t(labelKey),
    alertCount: tabAlerts?.[key],
  }))

  const requestedTab = searchParams.get('tab')
  const activeKey = requestedTab && visibleTabs.some((tab) => tab.key === requestedTab)
    ? requestedTab
    : visibleTabs[0]?.key

  // Mirror ViewGuard: don't flash a tab before permissions are known.
  if (featuresLoading) return null
  // Nothing in this hub is allowed for this tenant.
  if (!activeKey) return <Navigate to="/chat" replace />

  const active = visibleTabs.find((tab) => tab.key === activeKey)

  return (
    <div className={styles.hub}>
      <div className={styles.tabBar}>
        <Tabs
          tabs={uiTabs}
          active={activeKey}
          onChange={(key) => setSearchParams({ tab: key })}
          ariaLabel={t(ariaLabelKey)}
        />
      </div>
      {active?.render()}
    </div>
  )
}

const CAPACIDADES_TABS: HubTab[] = [
  { key: 'skills',        labelKey: 'nav.skills',        render: () => <SkillsView /> },
  { key: 'integraciones', labelKey: 'nav.integraciones', render: () => <IntegrationsView /> },
  { key: 'mcp',           labelKey: 'nav.mcp',           render: () => <McpView /> },
  { key: 'en-vivo',       labelKey: 'nav.envivo',        render: () => (
    <Suspense fallback={<TabFallback />}><EnVivoView /></Suspense>
  ) },
]

// "Programadas" is NOT here: the recurring-tasks calendar belongs to the agents,
// so it lives as the "Tareas" tab inside Agentes (owner decision).
const SISTEMA_TABS: HubTab[] = [
  { key: 'seguridad',   labelKey: 'nav.seguridad',   render: () => <SeguridadView /> },
  { key: 'coste',       labelKey: 'nav.coste',       render: () => (
    <Suspense fallback={<TabFallback />}><UsageView /></Suspense>
  ) },
  { key: 'proveedores', labelKey: 'nav.proveedores', render: () => <ProvidersView /> },
  { key: 'memoria',     labelKey: 'nav.memoria',     render: () => <MemoriaView /> },
  { key: 'archivos',    labelKey: 'nav.archivos',    render: () => <ArchivosView /> },
]

/** View ids each hub aggregates — Layout uses this to gate the nav items. */
export const CAPACIDADES_VIEW_IDS = CAPACIDADES_TABS.map((t) => t.key)
export const SISTEMA_VIEW_IDS = SISTEMA_TABS.map((t) => t.key)

export function CapacidadesView() {
  return <Hub tabs={CAPACIDADES_TABS} ariaLabelKey="nav.section.capabilities" />
}

export function SistemaView() {
  // Same fresh-approvals source as the sidebar badge — the red count on the
  // Seguridad tab always matches what the Seguridad list actually shows.
  const pending = usePendingApprovals().length
  return <Hub tabs={SISTEMA_TABS} ariaLabelKey="nav.section.system" tabAlerts={{ seguridad: pending }} />
}
