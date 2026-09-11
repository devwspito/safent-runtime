/**
 * useAdsAvailability — Ads is a first-level sidebar entry, ALWAYS visible
 * (spec 026 FR-001/Assumption 7: "sin cuentas conectadas, Ads sigue
 * visible"), never hidden behind a connection check like the legacy
 * useAdsPanelOrigin it replaces. This hook only drives the disabled/enabled
 * *presentation* of that entry and pre-warms the same-origin session bridge
 * (contracts/sso.md) so the FIRST click into Ads never shows a login step
 * (SC-002: zero-second logins across 20 opens).
 *
 * Polls POST /api/v1/ads/bridge/session — same call AdsView's first paint
 * would otherwise have to make, so polling it from the sidebar means the
 * bridge cookie is already warm by the time the owner clicks through.
 *
 * Also consumed by CompanionInstallAction/useCompanionInstall (029 FR-001):
 * `reason` selects "Instalar" vs "Reparar", and `status === 'ready'` is the
 * one signal allowed to end an install flow — an install-request reaching
 * `applied` is NOT enough on its own (contracts/install-request.md §5).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { mintAdsBridgeSession } from '../api/client'
import type { AdsAvailabilityReason } from '../api/types'

const ADS_AVAILABILITY_POLL_MS = 15_000

export interface AdsAvailability {
  /** "loading" only before the first response ever arrives. */
  status: 'loading' | 'ready' | 'unavailable'
  reason: AdsAvailabilityReason | null
  /** Re-checks immediately, outside the poll interval (e.g. a "Retry" CTA). */
  refresh: () => void
  refreshing?: boolean
}

export function useAdsAvailability(pollMs = ADS_AVAILABILITY_POLL_MS): AdsAvailability {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'unavailable'; reason: AdsAvailabilityReason | null }>(
    { status: 'loading', reason: null },
  )
  const [refreshing, setRefreshing] = useState(false)
  const scope = useRef({ active: false, pending: false })

  const poll = useCallback(() => {
    const current = scope.current
    if (!current.active || current.pending) return
    current.pending = true
    setRefreshing(true)
    void mintAdsBridgeSession().then((res) => {
      if (current.active) setState({ status: res.status, reason: res.reason })
    }).catch(() => {
      if (current.active) setState({ status: 'unavailable', reason: 'unreachable' })
    }).finally(() => {
      current.pending = false
      if (current.active) setRefreshing(false)
    })
  }, [])

  useEffect(() => {
    const current = { active: true, pending: false }
    scope.current = current
    poll()
    const id = setInterval(poll, pollMs)
    return () => { current.active = false; clearInterval(id) }
  }, [poll, pollMs])

  return { ...state, refreshing, refresh: poll }
}
