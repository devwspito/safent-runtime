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

type NativeCheckResult = { status: 'available'; version: string } | { status: 'up_to_date'; version: null }

function parseVersion(value: unknown): { core: string[]; prerelease: string[] } | null {
  if (typeof value !== 'string' || value.length > 64) return null
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/.exec(value)
  if (!match) return null
  const prerelease = match[4]?.split('.') ?? []
  if (prerelease.some(part => /^\d+$/.test(part) && part.length > 1 && part.startsWith('0'))) return null
  return { core: match.slice(1, 4), prerelease }
}

function compareNumeric(left: string, right: string): number {
  return left.length !== right.length ? left.length - right.length : left === right ? 0 : left > right ? 1 : -1
}

function newerVersion(candidate: unknown, current: string): candidate is string {
  const next = parseVersion(candidate)
  const installed = parseVersion(current)
  if (!next || !installed) return false
  for (let index = 0; index < 3; index++) {
    const order = compareNumeric(next.core[index], installed.core[index])
    if (order) return order > 0
  }
  if (!next.prerelease.length || !installed.prerelease.length) return !next.prerelease.length && !!installed.prerelease.length
  for (let index = 0; index < Math.max(next.prerelease.length, installed.prerelease.length); index++) {
    const left = next.prerelease[index]
    const right = installed.prerelease[index]
    if (left === undefined || right === undefined) return right === undefined
    if (left === right) continue
    const leftNumeric = /^\d+$/.test(left)
    const rightNumeric = /^\d+$/.test(right)
    return leftNumeric && rightNumeric ? compareNumeric(left, right) > 0 : leftNumeric !== rightNumeric ? !leftNumeric : left > right
  }
  return false
}

function nativeCheckResult(value: unknown, appVersion: string): NativeCheckResult | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const data = value as Record<string, unknown>
  if (Object.keys(data).some(key => !['status', 'app_version', 'version'].includes(key))
    || data.app_version !== appVersion || !parseVersion(data.app_version)) return null
  if (data.status === 'up_to_date' && data.version === null) return { status: 'up_to_date', version: null }
  if (data.status === 'available' && newerVersion(data.version, appVersion)) return { status: 'available', version: data.version }
  return null
}

function invokeNativeUpdate(command: 'get_native_update_status' | 'show_native_updater'): Promise<unknown> {
  const invoke = (window as unknown as {
    __TAURI__?: { core?: { invoke?: (command: string) => Promise<unknown> } };
  }).__TAURI__?.core?.invoke
  if (typeof invoke !== 'function') throw new Error('unavailable')
  return invoke(command)
}

function NativeAppUpdateFooter({ appVersion, available }: { appVersion:string; available:boolean }) {
  const t = useT()
  const [action, setAction] = useState<'checking' | 'opening' | null>(null)
  const [result, setResult] = useState<NativeCheckResult | null>(null)
  const [error, setError] = useState<'check' | 'open' | null>(null)
  const [requested, setRequested] = useState(false)
  const pending = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  async function checkUpdates() {
    if (pending.current || !available) return
    pending.current = true
    setAction('checking')
    setResult(null)
    setError(null)
    setRequested(false)
    try {
      const checked = nativeCheckResult(await invokeNativeUpdate('get_native_update_status'), appVersion)
      if (!checked) throw new Error('unconfirmed')
      if (alive.current) setResult(checked)
    } catch {
      if (alive.current) setError('check')
    } finally {
      pending.current = false
      if (alive.current) setAction(null)
    }
  }

  async function showUpdater() {
    if (pending.current || !available || result?.status !== 'available') return
    pending.current = true
    setAction('opening')
    setError(null)
    setRequested(false)
    try {
      // No target version, check token or artifacts cross this boundary. Native
      // rechecks its signed source and asks for confirmation before installing.
      await invokeNativeUpdate('show_native_updater')
      if (alive.current) {
        setResult(null)
        setRequested(true)
      }
    } catch {
      if (alive.current) setError('open')
    } finally {
      pending.current = false
      if (alive.current) setAction(null)
    }
  }

  return <section className={css.footer} aria-label={t('sysupdate.section')}>
    <p className={css.native}>{t('sysupdate.native.version').replace('{v}', appVersion)}</p>
    {available ? <>
      <button type="button" className="cv-btn cv-btn--ghost cv-btn--sm" disabled={action !== null}
        aria-busy={action !== null} onClick={() => { void (result?.status === 'available' ? showUpdater() : checkUpdates()) }}>
        <RefreshCw size={13} aria-hidden="true" />
        {t(action === 'checking' ? 'sysupdate.native.checking' : action === 'opening' ? 'sysupdate.native.opening' : result?.status === 'available' ? 'sysupdate.action' : 'sysupdate.native.check')}
      </button>
      {result?.status === 'available' && <p role="status" className={css.available}>{t('sysupdate.native.available').replace('{v}', result.version)}</p>}
      {result?.status === 'up_to_date' && <p role="status">{t('sysupdate.native.up_to_date')}</p>}
      {requested && <p role="status">{t('sysupdate.native.requested')}</p>}
      {error && <p role="alert" className={css.error}>{t(error === 'check' ? 'sysupdate.native.check_error' : 'sysupdate.native.error')}</p>}
    </> : <p role="status">{t('sysupdate.native.unavailable')}</p>}
  </section>
}

export function SystemUpdateFooter() {
  const value = (window as unknown as { __safentNativeUpdater?: unknown }).__safentNativeUpdater
  if (value && typeof value === 'object') {
    const data = value as Record<string, unknown>
    const available = data.status === 'available' && data.reason === 'app_only'
    const unavailable = data.status === 'unavailable' && data.reason === 'signing_configuration_missing'
    if ((available || unavailable) && typeof data.app_version === 'string'
      && data.app_version.length <= 64 && /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(data.app_version)) {
      // The native signed bundle owns app, engine and companion versions.
      // Never poll or offer the independent legacy engine updater in this mode.
      return <NativeAppUpdateFooter key={`${data.app_version}:${available}`} appVersion={data.app_version} available={available} />
    }
  }
  return <LegacySystemUpdateFooter />
}

function LegacySystemUpdateFooter() {
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
