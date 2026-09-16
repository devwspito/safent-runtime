import { useCallback, useEffect, useRef, useState } from 'react'

/** Independent, latest-request-wins source. null means unknown, not empty. */
export function useContextSource<T>(load: () => Promise<T[]>, valid: (row: T) => boolean) {
  const [state, setState] = useState<{ data: T[] | null; loading: boolean; error: boolean }>({ data: null, loading: true, error: false })
  const alive = useRef(false)
  const revision = useRef(0)
  const inFlight = useRef(false)
  const refresh = useCallback(async (skipIfBusy = false) => {
    if (skipIfBusy && inFlight.current) return
    const request = ++revision.current
    inFlight.current = true
    setState(previous => ({ ...previous, loading: true }))
    try {
      const data = await load()
      if (!Array.isArray(data) || !data.every(valid)) throw new Error('Invalid context source')
      if (alive.current && revision.current === request) setState({ data, loading: false, error: false })
    } catch {
      if (alive.current && revision.current === request) setState(previous => ({ ...previous, loading: false, error: true }))
    } finally {
      if (revision.current === request) inFlight.current = false
    }
  }, [load, valid])
  useEffect(() => {
    alive.current = true
    void refresh()
    return () => { alive.current = false; revision.current++; inFlight.current = false }
  }, [refresh])
  return { ...state, refresh }
}
