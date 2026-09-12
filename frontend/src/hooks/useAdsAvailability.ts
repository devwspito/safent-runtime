/**
 * useAdsAvailability — Ads is a first-level sidebar entry, ALWAYS visible
 * (spec 026 FR-001/Assumption 7: "sin cuentas conectadas, Ads sigue
 * visible"), never hidden behind a connection check like the legacy
 * useAdsPanelOrigin it replaces. This hook only drives the disabled/enabled
 * *presentation* of that entry and pre-warms the same-origin session bridge
 * (contracts/sso.md) so the FIRST click into Ads never shows a login step
 * (SC-002: zero-second logins across 20 opens).
 *
 * Verifies signed routing via GET /api/v1/ads/managed first. Only a free or
 * never-associated policy may warm the local bridge session. Managed has a
 * separate state, and any policy error denies rather than falling back.
 *
 * Also consumed by CompanionInstallAction/useCompanionInstall (029 FR-001):
 * `reason` selects "Instalar" vs "Reparar", and `status === 'ready'` is the
 * one signal allowed to end an install flow — an install-request reaching
 * `applied` is NOT enough on its own (contracts/install-request.md §5).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { mintAdsBridgeSession } from '../api/client'
import { getAdsPolicy, type AdsPolicy } from '../api/managedAds'
import type { AdsAvailabilityReason } from '../api/types'

const ADS_AVAILABILITY_POLL_MS = 15_000

export interface AdsAvailability {
  /** "loading" only before the first response ever arrives. */
  status: 'loading' | 'ready' | 'managed' | 'unavailable'
  reason: AdsAvailabilityReason | null
  policy?: AdsPolicy | null
  /** Re-checks immediately, outside the poll interval (e.g. a "Retry" CTA). */
  refresh: () => void
  refreshing?: boolean
}

export function useAdsAvailability(pollMs = ADS_AVAILABILITY_POLL_MS): AdsAvailability {
  const [state, setState] = useState<Omit<AdsAvailability, 'refresh'>>(
    { status: 'loading', reason: null },
  )
  const [refreshing, setRefreshing] = useState(false)
  const scope = useRef({ active: false, pending: false, controller: new AbortController() })

  const poll = useCallback(() => {
    const current = scope.current
    if (!current.active || current.pending) return
    current.pending = true
    setRefreshing(true)
    void getAdsPolicy(current.controller.signal).then(async policy => {
      if (!current.active) return
      if (policy?.mode === 'managed') {
        setState({ status: 'managed', reason: null, policy })
        return
      }
      const res = await mintAdsBridgeSession()
      if (current.active) setState({ status: res.status, reason: res.reason, policy })
    }).catch(() => {
      if (current.active) setState({ status: 'unavailable', reason: 'unreachable' })
    }).finally(() => {
      current.pending = false
      if (current.active) setRefreshing(false)
    })
  }, [])

  useEffect(() => {
    const current = { active: true, pending: false, controller: new AbortController() }
    scope.current = current
    poll()
    const id = setInterval(poll, pollMs)
    return () => { current.active = false; current.controller.abort(); clearInterval(id) }
  }, [poll, pollMs])

  return { ...state, refreshing, refresh: poll }
}
