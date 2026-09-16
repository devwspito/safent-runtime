/**
 * AdsNavItem — the sidebar's "Anuncios" entry (026, FR-001/Assumption 7).
 * ALWAYS rendered, ALWAYS a real clickable/focusable link (never `disabled`
 * or `pointer-events:none` — a blocked companion still has an honest
 * empty state to land on, per spec.md Acceptance Scenario 4). Only the
 * PRESENTATION changes: a small decorative dot plus a screen-reader-only
 * reason when the companion isn't ready — never color alone (NFR-004).
 *
 * "no_accounts" is NOT treated as blocked: the companion's own panel
 * guides account connection, so Ads stays fully usable either way.
 *
 * `not_installed` additionally renders the SAME "Instalar" affordance as the
 * Herramientas Ads card (CompanionInstallAction) — one flow, two entry
 * points (029 FR-001).
 */
import { NavLink } from 'react-router-dom'
import { useT } from '../lib/i18n'
import { CompanionInstallAction } from './CompanionInstallAction'
import type { AdsAvailability } from '../hooks/useAdsAvailability'

function AdsIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M2 6.3v3.4a1 1 0 0 0 1 1h1.3L8.5 13V3L4.3 5.3H3a1 1 0 0 0-1 1Z"
        stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M10.6 6c.55.6.55 3.4 0 4M12.4 4.4c1.35 1.5 1.35 5.7 0 7.2"
        stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  )
}

export function isAdsBlocked(availability: AdsAvailability): boolean {
  return availability.status === 'unavailable' && availability.reason !== 'no_accounts'
}

export interface AdsNavItemProps {
  availability: AdsAvailability
}

export function AdsNavItem({ availability }: AdsNavItemProps) {
  const t = useT()
  const blocked = isAdsBlocked(availability)
  const reason = availability.reason ?? 'unreachable'

  return (
    <li>
      <NavLink
        to="/anuncios"
        className={({ isActive }) =>
          ['nav-link', isActive ? 'active' : ''].filter(Boolean).join(' ')
        }
        aria-describedby={blocked ? 'ads-nav-status' : undefined}
      >
        <AdsIcon />
        {t('nav.ads')}
        {blocked && <span className="nav-status-dot" aria-hidden="true" data-testid="ads-nav-dot" />}
      </NavLink>
      {blocked && (
        <span id="ads-nav-status" className="sr-only">
          {t(`ads.state.${reason}.title`)}
        </span>
      )}
      {reason === 'not_installed' && (
        <CompanionInstallAction availability={availability} compact />
      )}
    </li>
  )
}
