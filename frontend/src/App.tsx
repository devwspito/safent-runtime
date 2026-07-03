import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Toaster } from 'sileo'
import Layout from './components/Layout'
import ChatView from './views/ChatView'
import { CapacidadesView, SistemaView } from './views/SectionHubs'
import { useActiveProvider } from './hooks/useActiveProvider'
import { useFeatures } from './hooks/useFeatures'

// Code-split OfficeView at the route boundary; it imports the canvas engine
// which is non-trivial (~10 kB gzipped) and not needed on other routes.
// (UsageView/EnVivoView are lazy-loaded inside SectionHubs.)
const OfficeView = lazy(() => import('./views/OfficeView'))


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
          {/* Agentes = the unified team view (swarm + cards + pixel floor). */}
          <Route path="agentes" element={
            <ViewGuard viewId="agentes">
              <Suspense fallback={<OfficeFallback />}>
                <OfficeView />
              </Suspense>
            </ViewGuard>
          } />
          <Route path="office" element={<Navigate to="/agentes" replace />} />
          {/* The two hubs (owner decision): everything that isn't Chat/Agentes
              lives inside them as tabs. Hubs gate their own tabs by features. */}
          <Route path="capacidades" element={<CapacidadesView />} />
          <Route path="sistema" element={<SistemaView />} />
          {/* Back-compat: old standalone paths (deep-links, the agent app-map)
              → the owning hub tab. */}
          <Route path="skills" element={<Navigate to="/capacidades?tab=skills" replace />} />
          <Route path="integraciones" element={<Navigate to="/capacidades?tab=integraciones" replace />} />
          <Route path="mcp" element={<Navigate to="/capacidades?tab=mcp" replace />} />
          <Route path="en-vivo" element={<Navigate to="/capacidades?tab=en-vivo" replace />} />
          <Route path="ensenar" element={<Navigate to="/capacidades?tab=en-vivo" replace />} />
          <Route path="seguridad" element={<Navigate to="/sistema?tab=seguridad" replace />} />
          <Route path="coste" element={<Navigate to="/sistema?tab=coste" replace />} />
          <Route path="proveedores" element={<Navigate to="/sistema?tab=proveedores" replace />} />
          {/* The recurring-tasks calendar lives inside Agentes ("Tareas" tab). */}
          <Route path="programadas" element={<Navigate to="/agentes?tab=tareas" replace />} />
          <Route path="memoria" element={<Navigate to="/sistema?tab=memoria" replace />} />
          <Route path="archivos" element={<Navigate to="/sistema?tab=archivos" replace />} />
          <Route path="ajustes" element={<Navigate to="/sistema" replace />} />
          {/* Unknown paths (incl. the removed /tablero, stale bookmarks) → chat. */}
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
