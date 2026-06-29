import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Toaster } from 'sileo'
import Layout from './components/Layout'
import ChatView from './views/ChatView'
import ProvidersView from './views/ProvidersView'
import IntegrationsView from './views/IntegrationsView'
import McpView from './views/McpView'
import SkillsView from './views/SkillsView'
import CalendarView from './views/CalendarView'
import SeguridadView from './views/SeguridadView'
import MemoriaView from './views/MemoriaView'
import ArchivosView from './views/ArchivosView'
import { useActiveProvider } from './hooks/useActiveProvider'
import { useFeatures } from './hooks/useFeatures'

// Code-split OfficeView at the route boundary; it imports the canvas engine
// which is non-trivial (~10 kB gzipped) and not needed on other routes.
const OfficeView = lazy(() => import('./views/OfficeView'))

// Code-split UsageView: recharts (~50 kB gzipped) not needed on other routes.
const UsageView = lazy(() => import('./views/UsageView'))


/** Shared route-boundary skeleton: stacked lines that mirror a view header. */
function RouteFallback({ label }: { label: string }) {
  return (
    <div
      className="view-body"
      aria-busy="true"
      aria-label={label}
      style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-4)' }}
    >
      {/* Header area skeleton */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)', paddingBottom: 'var(--space-4)' }}>
        <div className="skeleton skeleton--line" style={{ width: '40%' }} />
        <div className="skeleton skeleton--line-sm" style={{ width: '60%' }} />
      </div>
      {/* Content skeleton cards */}
      {Array.from({ length: 4 }, (_, i) => (
        <div
          key={i}
          className="skeleton skeleton--card"
          style={{ animationDelay: `${i * 60}ms` }}
        />
      ))}
    </div>
  )
}

function OfficeFallback() {
  return <RouteFallback label="Cargando Office…" />
}

function UsageFallback() {
  return <RouteFallback label="Cargando Coste…" />
}

/**
 * ViewGuard — wraps a route's element and redirects to /chat when the view is
 * not in the allowed set for this user.  While features are still loading we
 * render nothing (the Layout skeleton keeps the sidebar visible) to avoid a
 * flash-redirect before the permission set is known.
 *
 * 'chat' is always allowed — useFeatures.allowed() enforces this invariant.
 */
function ViewGuard({ viewId, children }: { viewId: string; children: React.ReactNode }) {
  const { isLoading, allowed } = useFeatures()
  if (isLoading) return null
  if (!allowed(viewId)) return <Navigate to="/chat" replace />
  return <>{children}</>
}

// Shell: renders the Layout. The sidebar "connect a model" nudge was removed
// (redundant + the chat shows its own in-chat no-model alert). We keep
// useActiveProvider only to expose reload() so ProvidersView can refresh after
// connecting a model.
function Shell() {
  const { reload } = useActiveProvider()
  return <Layout activeProviderReload={reload} />
}

// basename="/app" matches the shell-server mount point and Vite's base: '/app/'
export default function App() {
  return (
    <BrowserRouter basename="/app">
      <Toaster position="top-right" />
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Navigate to="/chat" replace />} />
          {/* chat is always allowed — no guard needed */}
          <Route path="chat" element={<ChatView />} />
          {/* tablero removed (owner: "no es útil para nada") — /tablero now falls through to index → /chat */}
          <Route path="programadas" element={
            <ViewGuard viewId="programadas"><CalendarView /></ViewGuard>
          } />
          {/* Agentes = the unified team view (cards + live floor). Office merged in. */}
          <Route path="agentes" element={
            <ViewGuard viewId="agentes">
              <Suspense fallback={<OfficeFallback />}>
                <OfficeView />
              </Suspense>
            </ViewGuard>
          } />
          <Route path="office" element={<Navigate to="/agentes" replace />} />
          <Route path="skills" element={
            <ViewGuard viewId="skills"><SkillsView /></ViewGuard>
          } />
          <Route path="integraciones" element={
            <ViewGuard viewId="integraciones"><IntegrationsView /></ViewGuard>
          } />
          <Route path="mcp" element={
            <ViewGuard viewId="mcp"><McpView /></ViewGuard>
          } />
          <Route path="archivos" element={
            <ViewGuard viewId="archivos"><ArchivosView /></ViewGuard>
          } />
          <Route path="proveedores" element={
            <ViewGuard viewId="proveedores"><ProvidersView /></ViewGuard>
          } />
          <Route path="seguridad" element={
            <ViewGuard viewId="seguridad"><SeguridadView /></ViewGuard>
          } />
          <Route path="memoria" element={
            <ViewGuard viewId="memoria"><MemoriaView /></ViewGuard>
          } />
          <Route path="coste" element={
            <ViewGuard viewId="coste">
              <Suspense fallback={<UsageFallback />}>
                <UsageView />
              </Suspense>
            </ViewGuard>
          } />
          {/* Unknown paths (incl. the removed /tablero, stale bookmarks) → chat. */}
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
