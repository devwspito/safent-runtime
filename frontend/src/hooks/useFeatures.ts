/**
 * useFeatures — fetches /api/v1/instance/features once and caches the result
 * at module level so re-renders (and multiple callers) never trigger a second
 * request within the same page session.
 *
 * Fail-open strategy: if the fetch fails for any reason (network error, 401,
 * 5xx, malformed JSON) we treat all views as allowed.  The real enforcement
 * lives in the backend; the frontend gate is UX sugar only.  Blocking the nav
 * on a transient error would be worse than showing a view the backend refuses.
 */

import { useEffect, useState } from 'react'
import { getInstanceFeatures } from '../api/client'
import type { InstanceFeatures } from '../api/client'

export type Edition = InstanceFeatures['edition']

interface FeaturesState {
  edition: Edition
  /** Stable Set — never undefined; always constructed from an array, even on error. */
  views: Set<string>
  isLoading: boolean
}

// Module-level cache: populated after the first fetch (success or failure).
// Null means "not yet fetched".
let _cache: { edition: Edition; views: Set<string> } | null = null
// Pending promise: deduplicate concurrent calls that race before the first
// fetch resolves (e.g. Layout + App both calling useFeatures on first render).
let _inflight: Promise<void> | null = null

function buildSet(views: unknown): Set<string> {
  // Normalise with ?? [] so Set() never receives undefined — tsc can miss this
  // when the runtime shape differs from the declared type.
  return new Set<string>(Array.isArray(views) ? (views as string[]) : [])
}

function isAllowed(views: Set<string>, viewId: string, edition: Edition): boolean {
  // Community edition: no restrictions.
  if (edition === 'community') return true
  // Always-on view: chat (core). Tablero was removed from the product (owner
  // decision: not useful). Keep in sync with shell_server/instance/api.py:_ALL_VIEWS.
  if (viewId === 'chat') return true
  // Empty set signals a failed fetch (fail-open) — allow everything.
  if (views.size === 0) return true
  return views.has(viewId)
}

export interface FeaturesResult extends FeaturesState {
  /** Returns true if the given view identifier is accessible for this user. */
  allowed(viewId: string): boolean
}

export function useFeatures(): FeaturesResult {
  const [state, setState] = useState<FeaturesState>(() => {
    // If another component already resolved the cache, hydrate immediately
    // so there is zero loading flash on subsequent mounts.
    if (_cache !== null) {
      return { edition: _cache.edition, views: _cache.views, isLoading: false }
    }
    return { edition: 'community', views: new Set<string>(), isLoading: true }
  })

  useEffect(() => {
    // Already resolved — sync local state and bail.
    if (_cache !== null) {
      setState({ edition: _cache.edition, views: _cache.views, isLoading: false })
      return
    }

    // Start the fetch only once; subsequent calls attach to the same promise.
    if (_inflight === null) {
      _inflight = getInstanceFeatures()
        .then((data) => {
          const edition: Edition =
            data.edition === 'associate' ? 'associate' : 'community'
          const views = buildSet(data.views ?? [])
          _cache = { edition, views }
        })
        .catch(() => {
          // Fail-open: empty Set triggers the size===0 branch in isAllowed → all allowed.
          _cache = { edition: 'community', views: new Set<string>() }
        })
        .finally(() => {
          _inflight = null
        })
    }

    let alive = true
    void _inflight.then(() => {
      if (alive && _cache !== null) {
        setState({ edition: _cache.edition, views: _cache.views, isLoading: false })
      }
    })

    return () => { alive = false }
  }, [])

  return {
    ...state,
    allowed: (viewId: string) => isAllowed(state.views, viewId, state.edition),
  }
}

/** Resets the module-level cache. For testing only — do not call in production. */
export function _resetFeaturesCache(): void {
  _cache = null
  _inflight = null
}
