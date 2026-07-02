/**
 * AjustesView — settings hub. Everything that isn't Chat/Agentes/Habilidades
 * lives here as a tab: Programadas, Modelo de IA, Integraciones, Herramientas,
 * Archivos, Seguridad, Memoria, Coste, En vivo. Each tab renders the existing
 * view component as-is — this file only owns tab selection.
 */
import { lazy, Suspense, useMemo } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { Tabs, type Tab } from '../components/ui/Tabs'
import { useSettingsNavItems } from '../components/Layout'
import { useFeatures } from '../hooks/useFeatures'
import { useT } from '../lib/i18n'
import CalendarView from './CalendarView'
import ProvidersView from './ProvidersView'
import IntegrationsView from './IntegrationsView'
import McpView from './McpView'
import ArchivosView from './ArchivosView'
import SeguridadView from './SeguridadView'
import MemoriaView from './MemoriaView'
import styles from './AjustesView.module.css'

// Code-split the same two views App.tsx already lazy-loads at their standalone
// routes (recharts / the VNC canvas aren't needed on the other Ajustes tabs).
const UsageView = lazy(() => import('./UsageView'))
const EnVivoView = lazy(() => import('./EnVivoView'))

const EN_VIVO_TAB = 'en-vivo'

function TabFallback() {
  return (
    <div className="view-body" aria-busy="true">
      <div className="skeleton skeleton--card" />
    </div>
  )
}

function ActiveTabView({ tab }: { tab: string }) {
  switch (tab) {
    case 'programadas':   return <CalendarView />
    case 'proveedores':   return <ProvidersView />
    case 'integraciones': return <IntegrationsView />
    case 'mcp':            return <McpView />
    case 'archivos':       return <ArchivosView />
    case 'seguridad':      return <SeguridadView />
    case 'memoria':        return <MemoriaView />
    case 'coste':
      return <Suspense fallback={<TabFallback />}><UsageView /></Suspense>
    case EN_VIVO_TAB:
      return <Suspense fallback={<TabFallback />}><EnVivoView /></Suspense>
    default:
      return null
  }
}

export default function AjustesView() {
  const t = useT()
  const { isLoading: featuresLoading, allowed } = useFeatures()
  const settingsNavItems = useSettingsNavItems()
  const [searchParams, setSearchParams] = useSearchParams()

  const tabs: Tab[] = useMemo(() => [
    ...settingsNavItems.map(({ to, label }) => ({ key: to.replace(/^\//, ''), label })),
    { key: EN_VIVO_TAB, label: t('nav.envivo') },
  ], [settingsNavItems, t])

  const visibleTabs = tabs.filter(tab => allowed(tab.key))
  const requestedTab = searchParams.get('tab')
  const activeTab = requestedTab && visibleTabs.some(tab => tab.key === requestedTab)
    ? requestedTab
    : visibleTabs[0]?.key

  function handleChange(key: string) {
    setSearchParams({ tab: key })
  }

  // Mirror ViewGuard: don't flash a tab before permissions are known.
  if (featuresLoading) return null
  // No settings section is allowed for this tenant — nothing to show here.
  if (!activeTab) return <Navigate to="/chat" replace />

  return (
    <div className={styles.ajustes}>
      <div className={styles.tabBar}>
        <Tabs
          tabs={visibleTabs}
          active={activeTab}
          onChange={handleChange}
          ariaLabel={t('nav.ajustes')}
        />
      </div>
      <ActiveTabView tab={activeTab} />
    </div>
  )
}
