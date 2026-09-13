import { useEffect, useId, useRef, useState } from 'react'
import { Button } from './ui/Button'
import { useLocale } from '../lib/i18n'
import { capsError, getNativeCaps, listCapsAccounts, listCapsBusinesses, parseMinor, sameGuardrail, saveCaps, TWO_DECIMAL_CURRENCIES, type Business, type CapsAccount, type Limits, type Snapshot } from '../lib/adsCaps'
import css from './AdsCapsSetup.module.css'

const FIELDS = [
  ['daily_cap_minor', 'Tope diario', 'Daily limit'],
  ['monthly_cap_minor', 'Tope mensual', 'Monthly limit'],
  ['floor_minor', 'Presupuesto mínimo por campaña', 'Minimum campaign budget'],
  ['ceiling_minor', 'Presupuesto máximo por campaña', 'Maximum campaign budget'],
  ['max_step_pct', 'Cambio máximo (%)', 'Maximum change (%)'],
  ['max_changes_per_day', 'Cambios máximos por día', 'Maximum changes per day'],
] as const
type Values = Record<typeof FIELDS[number][0], string>
const EMPTY: Values = { daily_cap_minor: '', monthly_cap_minor: '', floor_minor: '', ceiling_minor: '', max_step_pct: '', max_changes_per_day: '' }
const ERRORS: Record<string, [string, string]> = {
  ads_caps_unavailable: ['No se pudieron verificar los límites. Vuelve a intentarlo.', 'The limits could not be verified. Try again.'],
  ads_caps_invalid: ['Revisa los importes y la moneda. No se han aplicado límites nuevos.', 'Check the amounts and currency. No new limits were applied.'],
  ads_caps_changed: ['Los límites cambiaron mientras editabas. Carga los valores actuales antes de guardar.', 'The limits changed while you were editing. Reload the current values before saving.'],
  ads_caps_busy: ['Safent está realizando otra operación. Espera y vuelve a intentarlo.', 'Safent is performing another operation. Wait and try again.'],
  ads_caps_session: ['Vuelve a abrir Anuncios para recuperar tu sesión.', 'Reopen Ads to restore your session.'],
  ads_caps_forbidden: ['Tu sesión no permite modificar estos límites.', 'Your session cannot change these limits.'],
  ads_caps_restored: ['No se aplicó el cambio. Se han restaurado y verificado los límites anteriores.', 'The change was not applied. The previous limits were restored and verified.'],
  ads_caps_rollback_unknown: ['No se pudo verificar la recuperación. No apruebes campañas hasta volver a comprobar los límites.', 'Recovery could not be verified. Do not approve campaigns until the limits are checked again.'],
  ads_caps_sql_pending: ['Los límites duros se confirmaron, pero falta verificar las reglas de la cuenta. Vuelve a cargar y guardar antes de aprobar campañas.', 'The hard limits were confirmed, but the account rules still need verification. Reload and save before approving campaigns.'],
}
function valuesOf(limits: Limits | undefined): Values {
  if (!limits) return { ...EMPTY }
  return Object.fromEntries(FIELDS.map(([key]) => [key, key.endsWith('_minor') ? String(limits[key] / 100) : String(limits[key])])) as Values
}
function validated(values: Values): Limits | null {
  const result = {} as Limits
  for (const [key] of FIELDS) {
    const value = key.endsWith('_minor') ? parseMinor(values[key]) : /^\d+(?:[.,]\d+)?$/.test(values[key]) ? Number(values[key].replace(',', '.')) : null
    if (value === null || !Number.isFinite(value)) return null
    result[key] = value
  }
  if (result.daily_cap_minor <= 0 || result.monthly_cap_minor <= 0 || result.ceiling_minor <= 0 || result.floor_minor > result.ceiling_minor || result.max_step_pct <= 0 || result.max_step_pct > 100 || !Number.isInteger(result.max_changes_per_day) || result.max_changes_per_day < 1 || result.max_changes_per_day > 10000) return null
  return result
}

export function AdsCapsSetup({ getCsrf, onClose }: { getCsrf: () => string | null; onClose: () => void }) {
  const { locale } = useLocale(), en = locale === 'en'
  const [businesses, setBusinesses] = useState<Business[]>([])
  const [business, setBusiness] = useState('')
  const [accounts, setAccounts] = useState<CapsAccount[]>([])
  const [accountId, setAccountId] = useState('')
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [values, setValues] = useState<Values>({ ...EMPTY })
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [revision, setRevision] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null), flight = useRef(false)
  const id = useId()
  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => {
    let cancelled = false
    setLoading(true); setError(''); setNotice('')
    Promise.all([listCapsBusinesses(), getNativeCaps()]).then(([items, caps]) => {
      if (cancelled) return
      setBusinesses(items); setSnapshot(caps); setBusiness(previous => items.some(item => item.business_id === previous) ? previous : items[0]?.business_id ?? '')
      if (!items.length) setLoading(false)
    }).catch(reason => { if (!cancelled) { setError(capsError(reason)); setLoading(false) } })
    return () => { cancelled = true }
  }, [revision])
  useEffect(() => {
    if (!business) return
    let cancelled = false
    setLoading(true); setAccounts([]); setAccountId(''); setValues({ ...EMPTY }); setNotice('')
    listCapsAccounts(business).then(items => { if (!cancelled) { setAccounts(items); setAccountId(items[0]?.account_ref ?? ''); setLoading(false) } }).catch(reason => { if (!cancelled) { setError(capsError(reason)); setLoading(false) } })
    return () => { cancelled = true }
  }, [business, revision])
  const account = accounts.find(item => item.account_ref === accountId)
  useEffect(() => {
    const caps = account && snapshot?.accounts[account.platform_account_id]
    // Fill only saved hard limits. A missing entry stays empty, including zero.
    setValues(valuesOf(caps))
  }, [account, snapshot])
  const limits = validated(values)
  const supportedCurrency = account && TWO_DECIMAL_CURRENCIES.has(account.currency)
  const currentCaps = account && snapshot?.accounts[account.platform_account_id]
  const verified = !!(snapshot && currentCaps && snapshot.revision === snapshot.loaded_digest && account && sameGuardrail(account.guardrail, currentCaps, account.currency))
  const text = (es: string, english: string) => en ? english : es
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (flight.current || !limits || !account || !snapshot || !supportedCurrency) return
    flight.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const saved = await saveCaps(business, account, snapshot, limits, getCsrf())
      if (!saved) setNotice(text('Guardado cancelado. No se cambiaron los límites.', 'Save cancelled. The limits were not changed.'))
      else {
        const updated = await listCapsAccounts(business)
        setSnapshot(saved); setAccounts(updated)
        setNotice(text('Límites aplicados y verificados. Las campañas siguen requiriendo aprobación.', 'Limits applied and verified. Campaigns still require approval.'))
      }
    } catch (reason) { setError(capsError(reason)) }
    finally { flight.current = false; setBusy(false) }
  }
  return <section className={css.panel} aria-busy={loading || busy} aria-labelledby={`${id}-title`}>
    <header className={css.header}><h2 id={`${id}-title`} ref={heading} tabIndex={-1}>{text('Límites de gasto', 'Spending limits')}</h2><Button size="sm" variant="ghost" disabled={busy} onClick={onClose}>{text('Cerrar', 'Close')}</Button></header>
    <p>{text('Elige los topes que autorizas para esta cuenta. Guardar pedirá una confirmación de Safent; no activa campañas ni ejecución automática.', 'Choose the limits you authorize for this account. Saving asks for a Safent confirmation; it does not activate campaigns or automatic execution.')}</p>
    {error && <div role="alert" className={css.error}>{ERRORS[error]?.[en ? 1 : 0] ?? ERRORS.ads_caps_unavailable[en ? 1 : 0]}</div>}
    {notice && <p role="status">{notice}</p>}
    <form onSubmit={submit}>
      <fieldset disabled={loading || busy}>
        <div className={css.selectors}>
          <label htmlFor={`${id}-business`}>{text('Negocio', 'Business')}<select id={`${id}-business`} value={business} onChange={event => { setBusiness(event.target.value); setError('') }}>{businesses.map(item => <option key={item.business_id} value={item.business_id}>{item.name}</option>)}</select></label>
          <label htmlFor={`${id}-account`}>{text('Cuenta publicitaria', 'Ad account')}<select id={`${id}-account`} value={accountId} onChange={event => { setAccountId(event.target.value); setError('') }}>{accounts.map(item => <option key={item.account_ref} value={item.account_ref}>{item.display_name || item.platform_account_id} · {item.platform === 'google' ? 'Google Ads' : 'Meta Ads'} · {item.currency}</option>)}</select></label>
        </div>
        {loading ? <p role="status">{text('Comprobando límites…', 'Checking limits…')}</p> : !accounts.length ? <p>{text('Conecta una cuenta publicitaria para definir sus límites.', 'Connect an ad account to set its limits.')}</p> : <>
          <p className={css.status}>{verified ? text('Límites actuales verificados.', 'Current limits verified.') : text('Esta cuenta todavía no tiene todos sus límites verificados.', 'This account does not yet have all limits verified.')}</p>
          {!supportedCurrency && <p role="alert">{text('Esta versión admite monedas con dos decimales. No se cambiarán los límites de esta cuenta.', 'This version supports two-decimal currencies. The limits for this account will not be changed.')}</p>}
          <div className={css.fields}>{FIELDS.map(([key, es, english]) => <label key={key} htmlFor={`${id}-${key}`}>{en ? english : es}{key.endsWith('_minor') ? ` (${account?.currency})` : ''}<input id={`${id}-${key}`} inputMode={key === 'max_changes_per_day' ? 'numeric' : 'decimal'} autoComplete="off" value={values[key]} onChange={event => setValues(previous => ({ ...previous, [key]: event.target.value }))} /></label>)}</div>
          <p className={css.help}>{text('Completa todos los campos: topes positivos, mínimo de 0 o más, máximo igual o superior al mínimo, porcentaje mayor que 0 y hasta 100, cambios como número entero. Estos topes controlan las operaciones autorizadas, no sustituyen las condiciones de facturación de Google o Meta.', 'Complete every field: positive limits, minimum of 0 or more, maximum at least as high as the minimum, percentage above 0 and up to 100, and a whole number of changes. These limits control authorized operations, not Google or Meta billing terms.')}</p>
        </>}
      </fieldset>
      <div className={css.actions}><Button type="submit" size="sm" disabled={loading || !limits || !supportedCurrency} loading={busy}>{text('Confirmar y aplicar límites', 'Confirm and apply limits')}</Button><Button type="button" size="sm" variant="secondary" disabled={busy || loading} onClick={() => setRevision(value => value + 1)}>{text('Volver a comprobar', 'Check again')}</Button></div>
    </form>
  </section>
}
