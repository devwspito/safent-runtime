/**
 * ReconnectScreen — the ONE honest state for a tokenless load, a stale
 * cached bearer, or a refresh that definitively failed (028 FR-012/FR-013,
 * SC-012). Rendered by App.tsx INSTEAD OF the whole routed app shell, so
 * nothing that polls or fetches ever mounts alongside it — no error stack,
 * no retry loop, one screen, one action.
 */
import { useT } from '../lib/i18n'
import { useEffect, useRef } from 'react'
import type { AuthStatus } from '../lib/token'
import css from './ReconnectScreen.module.css'

export interface ReconnectScreenProps {
  reason: Extract<AuthStatus, { kind: 'unauthenticated' }>['reason']
}

export function ReconnectScreen({ reason }: ReconnectScreenProps) {
  const t = useT()
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => { heading.current?.focus() }, [])

  return (
    <div
      role="alert"
      aria-live="assertive"
      className={css.screen}
    >
      <div className={css.content}>
      <div className={css.brand}><span aria-hidden="true">S</span>Safent</div>
      <h1 ref={heading} tabIndex={-1}>
        {t('reconnect.title')}
      </h1>
      <p>
        {t(`reconnect.reason.${reason}`)}
      </p>
      <p>
        {t('reconnect.hint')}
      </p>
      <button
        type="button"
        className="cv-btn cv-btn--primary"
        onClick={() => window.location.reload()}
      >
        {t('reconnect.action')}
      </button>
      </div>
    </div>
  )
}
