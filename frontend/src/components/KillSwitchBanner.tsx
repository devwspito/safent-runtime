/**
 * KillSwitchBanner — global red banner shown on EVERY view while the
 * emergency brake (025 Top-KILL) is engaged. Lives in Layout so it is
 * visible regardless of which route the owner is on, not just Seguridad.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getKillSwitch } from '../api/client'
import { useT } from '../lib/i18n'

const POLL_MS = 5000

/** Same not-yet-centralized-key fallback trick as SeguridadView's tNew(). */
type Translate = ReturnType<typeof useT>
function tNew(t: Translate, key: string, fallback: string): string {
  return t(key as Parameters<Translate>[0], fallback)
}

export default function KillSwitchBanner() {
  const t = useT()
  const [engaged, setEngaged] = useState<boolean | null>(null)
  const [unverified, setUnverified] = useState(false)

  useEffect(() => {
    let cancelled = false
    let inFlight = false
    const poll = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const status = await getKillSwitch()
        if (!cancelled) { setEngaged(status.engaged); setUnverified(false) }
      } catch {
        // Keep the last known state, but do not present it as current evidence.
        if (!cancelled) setUnverified(true)
      } finally {
        inFlight = false
      }
    }
    poll()
    const id = setInterval(poll, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (!engaged && !unverified) return null

  return (
    <div
      role="alert"
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 'var(--space-3)',
        padding: 'var(--space-2) var(--space-4)',
        background: 'var(--color-danger)', color: '#fff',
        fontSize: 'var(--text-sm)', fontWeight: 600,
      }}
    >
      <span>
        {unverified
          ? engaged
            ? 'No se puede verificar el freno. El último estado confirmado era activado.'
            : 'No se puede verificar el estado del freno de emergencia.'
          : tNew(t, 'killswitch.banner.text', 'Freno de emergencia ACTIVADO — el agente no ejecuta nada ni admite turnos nuevos.')}
      </span>
      <Link to="/sistema?tab=seguridad" style={{ color: '#fff', textDecoration: 'underline', flexShrink: 0 }}>
        {unverified ? 'Revisar estado' : tNew(t, 'killswitch.banner.link', 'Liberar')}
      </Link>
    </div>
  )
}
