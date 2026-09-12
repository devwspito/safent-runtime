/** Independent native-app and engine-update status. Requests are acknowledgements,
 * not proof of installation, restart or removal. No unsigned VERSION checks. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, Trash2 } from 'lucide-react'
import { getSystemUpdate, requestSystemUninstall, postInstallRequest, getInstallRequests, type SystemUpdateStatus } from '../api/client'
import type { InstallRequestStatus, SafentUpdateGlobal } from '../api/types'
import { useConfirmDialog } from './ConfirmDialog'
import { useT } from '../lib/i18n'
import { stageLabelKey, formatProgress } from '../lib/installStages'
import css from './SystemUpdateFooter.module.css'

const SYSTEM_UPDATE_POLL_MS = 15 * 60_000
const SYSTEM_UPDATE_ACTIVE_POLL_MS = 20_000

function nativeAppVersion(): string | null {
  const value = (window as unknown as { __safentNativeUpdater?: unknown }).__safentNativeUpdater
  if (!value || typeof value !== 'object') return null
  const data = value as Record<string, unknown>
  return data.status === 'unavailable' && data.reason === 'integration_missing'
    && typeof data.app_version === 'string' && data.app_version.length <= 64
    && /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(data.app_version) ? data.app_version : null
}

function resolveUpdateSignal(status: SystemUpdateStatus | null) {
  const host = (window as unknown as { __safentUpdate?: SafentUpdateGlobal }).__safentUpdate
  return { available: host ? host.available : !!status?.update_available,
    latestVersion: host?.to?.app ?? status?.latest_version ?? '' }
}

export function SystemUpdateFooter() {
  const t = useT()
  const [status, setStatus] = useState<SystemUpdateStatus | null>(null)
  const [liveRequest, setLiveRequest] = useState<InstallRequestStatus | null>(null)
  const [readError, setReadError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [action, setAction] = useState<'update' | 'uninstall' | null>(null)
  const [actionError, setActionError] = useState('')
  const [uninstallRequested, setUninstallRequested] = useState(false)
  const [confirm, confirmDialog] = useConfirmDialog()
  const mounted = useRef(false)
  const generation = useRef(0)
  const reading = useRef(false)
  const submitting = useRef(false)
  const mutating = useRef(false)
  const currentSnapshot = useRef('')
  const updating = liveRequest?.state === 'pending' || liveRequest?.state === 'claimed'

  const poll = useCallback(async () => {
    if (reading.current || mutating.current) return
    reading.current = true
    const epoch = ++generation.current
    setLoading(true)
    const [update, requests] = await Promise.allSettled([getSystemUpdate(), getInstallRequests()])
    if (!mounted.current || epoch !== generation.current) return
    const updateValid = update.status === 'fulfilled' && typeof update.value?.current_version === 'string' && !!update.value.current_version.trim()
    if (updateValid) setStatus(update.value)
    if (requests.status === 'fulfilled' && Array.isArray(requests.value.requests)) {
      setLiveRequest(requests.value.requests.find(request => request.verb === 'update_system') ?? null)
    }
    setReadError(!updateValid || requests.status === 'rejected'
      || requests.status === 'fulfilled' && !Array.isArray(requests.value.requests))
    setLoading(false)
    reading.current = false
  }, [])

  useEffect(() => {
    mounted.current = true
    void poll()
    const timer = setInterval(() => { void poll() }, updating ? SYSTEM_UPDATE_ACTIVE_POLL_MS : SYSTEM_UPDATE_POLL_MS)
    return () => {
      mounted.current = false
      generation.current += 1
      reading.current = false
      clearInterval(timer)
    }
  }, [poll, updating])

  const signal = resolveUpdateSignal(status)
  const failed = !updating && liveRequest?.state === 'failed'
  const expired = !updating && liveRequest?.state === 'expired'
  const available = !updating && signal.available
  const canRetry = expired || failed && liveRequest?.last_failure?.retryable !== false
  const blocked = updating || readError || loading || uninstallRequested
  currentSnapshot.current = JSON.stringify([signal, liveRequest?.state, liveRequest?.expires_at, blocked])

  async function requestAction(kind: 'update' | 'uninstall') {
    if (submitting.current || blocked || kind === 'update' && failed && !canRetry) return
    submitting.current = true
    setAction(kind)
    setActionError('')
    const snapshot = currentSnapshot.current
    try {
      const accepted = await confirm({
        title: t(kind === 'update' ? 'sysupdate.confirm.title' : 'sysuninstall.confirm.title'),
        description: t(kind === 'update' ? 'sysupdate.confirm.body' : 'sysuninstall.confirm.body'),
        confirmLabel: t(kind === 'update' ? 'sysupdate.confirm.ok' : 'sysuninstall.confirm.ok'),
        variant: kind === 'uninstall' ? 'danger' : 'default',
      })
      if (!mounted.current || !accepted) return
      if (snapshot !== currentSnapshot.current) {
        setActionError(t('sysupdate.changed'))
        return
      }
      // Older reads must not erase an acknowledged request with their pre-POST view.
      generation.current += 1
      reading.current = false
      mutating.current = true
      if (kind === 'update') {
        const result = await postInstallRequest('update_system')
        if (!mounted.current) return
        if (!result.accepted || !result.request || result.request.verb !== 'update_system') throw new Error('unconfirmed')
        setLiveRequest(result.request)
      } else {
        const result = await requestSystemUninstall()
        if (!mounted.current) return
        if (!result.ok) throw new Error('unconfirmed')
        setUninstallRequested(true)
      }
    } catch {
      if (mounted.current) {
        setActionError(t(kind === 'update' ? 'sysupdate.err.start' : 'sysuninstall.err.start'))
        // The request may have reached the host. Check before another human retry.
        setReadError(true)
      }
    } finally {
      mutating.current = false
      submitting.current = false
      if (mounted.current) setAction(null)
    }
  }

  const appVersion = nativeAppVersion()
  const label = signal.latestVersion ? `${t('sysupdate.available')} · v${signal.latestVersion}` : t('sysupdate.available')
  return <section className={css.footer} aria-label={t('sysupdate.section')}>
    {appVersion && <p className={css.native}>{t('sysupdate.native.version').replace('{v}', appVersion)}
      <span>{t('sysupdate.native.unavailable')}</span></p>}
    <div className={css.version}>
      {status?.current_version && <span>{t('sysupdate.current').replace('{v}', status.current_version)}</span>}
      {available && !readError && <span className={css.available} title={label}>{signal.latestVersion ? `v${signal.latestVersion}` : t('sysupdate.available')}</span>}
    </div>
    {!status && loading && <p role="status">{t('sysupdate.checking')}</p>}
    {readError && <div className={css.notice}><p role="status">{t('sysupdate.unknown')}</p>
      <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm" disabled={loading} onClick={() => { void poll() }}>{t('sysupdate.check_again')}</button></div>}
    {updating && <p role="status">{t(stageLabelKey(liveRequest?.stage))}
      {formatProgress(liveRequest?.progress) && ` (${formatProgress(liveRequest?.progress)})`}{' — '}{t('sysupdate.relaunch_notice')}</p>}
    {(failed || expired) && <p role="alert" className={css.error}>{failed ? t('sysupdate.err.generic') : t('sysupdate.expired')}</p>}
    {actionError && <p role="alert" className={css.error}>{actionError}</p>}
    {uninstallRequested && <p role="status">{t('sysuninstall.toast.started')}</p>}
    {status?.current_version && <div className={css.actions}>
      {(available && !failed || updating || canRetry) && <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm"
        onClick={() => { void requestAction('update') }} aria-disabled={blocked || action !== null}
        aria-label={available ? `${label} — ${t('sysupdate.action')}` : t('sysupdate.action')}>
        <RefreshCw size={13} aria-hidden="true" />
        {action === 'update' ? t('sysupdate.requesting') : updating ? t('sysupdate.updating') : canRetry ? t('sysupdate.retry') : t('sysupdate.action')}
      </button>}
      <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger" onClick={() => { void requestAction('uninstall') }}
        aria-disabled={blocked || action !== null} title={t('sysuninstall.action')} aria-label={t('sysuninstall.action')}>
        <Trash2 size={13} aria-hidden="true" />
      </button>
    </div>}
    {confirmDialog}
  </section>
}
