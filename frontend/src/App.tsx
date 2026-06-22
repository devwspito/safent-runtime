import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import AgentsView from './views/AgentsView'
import ChatView from './views/ChatView'
import ComingSoonView from './views/ComingSoonView'

// Code-split OfficeView at the route boundary; it imports the canvas engine
// which is non-trivial (~10 kB gzipped) and not needed on other routes.
const OfficeView = lazy(() => import('./views/OfficeView'))

// basename="/app" matches the shell-server mount point and Vite's base: '/app/'
export default function App() {
  return (
    <BrowserRouter basename="/app">
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/chat" replace />} />
          <Route path="chat" element={<ChatView />} />
          <Route path="programadas" element={<ComingSoonView name="Programadas" />} />
          <Route path="agentes" element={<AgentsView />} />
          <Route path="office" element={
            <Suspense fallback={
              <div className="state-container" aria-busy="true">
                <p className="state-label">Cargando Office…</p>
              </div>
            }>
              <OfficeView />
            </Suspense>
          } />
          <Route path="skills" element={<ComingSoonView name="Skills" />} />
          <Route path="integraciones" element={<ComingSoonView name="Integraciones" />} />
          <Route path="mcp" element={<ComingSoonView name="MCP" />} />
          <Route path="proveedores" element={<ComingSoonView name="Proveedores" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
