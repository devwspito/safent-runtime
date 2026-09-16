import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ExternalLink, Megaphone, RefreshCw, ShieldCheck, ArrowLeft } from 'lucide-react'
import { Button } from '../components/ui/Button'
import { useLocale } from '../lib/i18n'
import {
  adsBindingKey, getManagedMetrics, listManagedCampaigns, prepareManagedReview, proposeManagedChange,
  type AdsBinding, type AdsCampaign, type AdsCampaignPage, type AdsMetrics, type AdsPolicy, type AdsProposal, type AdsReview,
} from '../api/managedAds'
import css from './ManagedAdsView.module.css'

const copy = {
  es: {
    title: 'Anuncios', managed: 'Administrado por Enterprise', refresh: 'Actualizar asignaciones',
    intro: 'Consulta tus cuentas y prepara cambios. La aprobación humana se realiza en Enterprise; aquí no se publican anuncios.',
    account: 'Cuenta asignada', select: 'Selecciona una asignación', empty: 'No hay asignaciones disponibles en la política recibida.',
    scope: 'Ámbito de la asignación', connection: 'Conexión', user: 'Usuario', organization: 'Organización',
    revision: 'Revisión', load: 'Consultar campañas', loading: 'Consultando…',
    choose: 'Elige una cuenta para consultar sus campañas.', registered: 'Asignación recibida. El servicio comprobará el permiso vigente en cada operación.',
    noCampaigns: 'No hay campañas en esta página.', previous: 'Primera página', next: 'Siguiente página',
    campaign: 'Campaña', budget: 'Presupuesto diario', active: 'Activa', paused: 'Pausada', removed: 'Eliminada',
    back: 'Volver a campañas', metrics: 'Consultar métricas · 7 días', metricsNote: 'Datos almacenados de los últimos 7 días. No implican sincronización en tiempo real.',
    noMetrics: 'No hay observaciones para este periodo.', day: 'Fecha', spend: 'Gasto', conversions: 'Conversiones',
    proposal: 'Preparar propuesta', action: 'Cambio', pause: 'Pausar campaña', adjust: 'Cambiar presupuesto diario', amount: 'Nuevo presupuesto diario',
    exact: 'Usa un punto decimal y hasta dos decimales. No se aplica ningún cambio al guardar.',
    reason: 'Motivo del cambio', save: 'Guardar propuesta', saved: 'Propuesta guardada, sin ejecutar.',
    blocked: 'Esta campaña no permite cambios desde esta asignación.', review: 'Preparar revisión en Enterprise',
    open: 'Abrir revisión en Enterprise', expires: 'Revisión disponible hasta', expired: 'El enlace de revisión ha caducado. Puedes preparar una nueva revisión.',
    nextStep: 'Enterprise mostrará el cambio exacto y solicitará la confirmación humana. El enlace no aprueba ni ejecuta.',
    error: 'No se pudo verificar o completar la operación. Se han ocultado los datos anteriores. Actualiza las asignaciones y consulta de nuevo.',
    uncertain: 'No se pudo confirmar el resultado. La propuesta o revisión podría haberse registrado. No se ha reintentado ni aprobado; consulta al agente antes de crear otra.',
  },
  en: {
    title: 'Ads', managed: 'Managed by Enterprise', refresh: 'Refresh assignments',
    intro: 'Inspect assigned accounts and prepare changes. Human approval takes place in Enterprise; ads are not published here.',
    account: 'Assigned account', select: 'Select an assignment', empty: 'There are no available assignments in the received policy.',
    scope: 'Assignment scope', connection: 'Connection', user: 'User', organization: 'Organization', revision: 'Revision',
    load: 'Load campaigns', loading: 'Loading…', choose: 'Select an account to inspect its campaigns.',
    registered: 'Assignment received. The service checks live permission on every operation.',
    noCampaigns: 'No campaigns on this page.', previous: 'First page', next: 'Next page',
    campaign: 'Campaign', budget: 'Daily budget', active: 'Active', paused: 'Paused', removed: 'Removed',
    back: 'Back to campaigns', metrics: 'Load metrics · 7 days', metricsNote: 'Stored observations from the last 7 days, not a guarantee of real-time sync.',
    noMetrics: 'No observations for this period.', day: 'Date', spend: 'Spend', conversions: 'Conversions',
    proposal: 'Prepare proposal', action: 'Change', pause: 'Pause campaign', adjust: 'Change daily budget', amount: 'New daily budget',
    exact: 'Use a decimal point and at most two decimal places. Saving does not apply the change.',
    reason: 'Reason for change', save: 'Save proposal', saved: 'Proposal saved, not executed.',
    blocked: 'This campaign cannot be changed using this assignment.', review: 'Prepare Enterprise review',
    open: 'Open Enterprise review', expires: 'Review available until', expired: 'The review link has expired. You can prepare a new review.',
    nextStep: 'Enterprise displays the exact change and asks for human confirmation. This link neither approves nor executes.',
    error: 'Could not verify or complete the operation. Previous data has been hidden. Refresh assignments and query again.',
    uncertain: 'The result could not be confirmed. A proposal or review may have been recorded. Nothing was retried or approved; ask the agent before creating another.',
  },
}
type Copy = typeof copy.es | typeof copy.en

export function ManagedAdsView({ policy, refresh, refreshing }: { policy: AdsPolicy; refresh: () => void; refreshing: boolean }) {
  const { locale } = useLocale()
  const t = copy[locale]
  const [selectedId, select] = useState('')
  const selected = policy.bindings.find(item => item.grant_id === selectedId)
  return <section className={css.workspace} aria-label={t.title}>
    <header className={css.toolbar}>
      <h1><Megaphone size={16} aria-hidden />{t.title}</h1>
      <span className={css.badge}><ShieldCheck size={13} aria-hidden />{t.managed}</span>
      <Button variant="ghost" size="sm" loading={refreshing} onClick={() => { select(''); refresh() }}><RefreshCw size={13} aria-hidden />{t.refresh}</Button>
    </header>
    <div className={css.content}>
      <p className={css.note}>{t.intro}</p>
      <label className={css.field}>{t.account}
        <select value={selectedId} onChange={event => select(event.target.value)} disabled={!policy.bindings.length}>
          <option value="">{t.select}</option>
          {policy.bindings.map(item => <option key={item.grant_id} value={item.grant_id}>
            {item.platform === 'google' ? 'Google Ads' : 'Meta Ads'} · {item.external_account_id} · {t.connection} {item.connection_id}
          </option>)}
        </select>
      </label>
      {!policy.bindings.length ? <p role="status" className={css.empty}>{t.empty}</p>
        : !selected ? <p className={css.empty}>{t.choose}</p>
          : <AccountWorkspace key={adsBindingKey(selected)} policy={policy} selected={selected} t={t} locale={locale} />}
    </div>
  </section>
}

function AccountWorkspace({ policy, selected, t, locale }: { policy: AdsPolicy; selected: AdsBinding; t: Copy; locale: string }) {
  const [page, setPage] = useState<AdsCampaignPage | null>(null)
  const [campaign, setCampaign] = useState<AdsCampaign | null>(null)
  const [metrics, setMetrics] = useState<AdsMetrics | null>(null)
  const [proposal, setProposal] = useState<AdsProposal | null>(null)
  const [review, setReview] = useState<AdsReview | null>(null)
  const [error, setError] = useState<'read' | 'write' | null>(null)
  const [busy, setBusy] = useState(false)
  const [clock, tick] = useState(Date.now())
  const scope = useRef({ active: true, pending: false, controller: new AbortController() })
  useEffect(() => {
    const current = { active: true, pending: false, controller: new AbortController() }
    scope.current = current
    const timer = setInterval(() => tick(Date.now()), 1000)
    return () => { current.active = false; current.controller.abort(); clearInterval(timer) }
  }, [])
  const money = (value: AdsCampaign['budget']) => new Intl.NumberFormat(locale, { style: 'currency', currency: value.currency }).format(value.amount)

  async function run<T>(write: boolean, work: (signal: AbortSignal) => Promise<T>, done: (result: T) => void) {
    const current = scope.current
    if (!current.active || current.pending || error) return
    current.pending = true
    setBusy(true)
    try {
      const result = await work(current.controller.signal)
      if (current.active) done(result)
    } catch {
      if (current.active) {
        setPage(null); setCampaign(null); setMetrics(null); setProposal(null); setReview(null)
        setError(write ? 'write' : 'read')
      }
    } finally {
      current.pending = false
      if (current.active) setBusy(false)
    }
  }
  const load = (cursor: string | null) => run(false, signal => listManagedCampaigns(policy, selected, cursor, signal), result => {
    setPage(result); setCampaign(null); setMetrics(null); setProposal(null); setReview(null)
  })
  const expired = !!review && Date.parse(review.expires_at) <= clock
  return <>
    <details className={css.scope}><summary>{t.scope}</summary>
      <dl><dt>{t.organization}</dt><dd>{selected.org_id}</dd><dt>{t.user}</dt><dd>{selected.user_id}</dd>
        <dt>{t.connection}</dt><dd>{selected.connection_id}</dd><dt>{t.revision}</dt><dd>{selected.revision} / {selected.resource_revision}</dd></dl>
    </details>
    <p className={css.note}>{t.registered}</p>
    {error ? <p role="alert" className={css.notice}>{error === 'write' ? t.uncertain : t.error}</p> : <>
      {!campaign && <Button size="sm" loading={busy} onClick={() => void load(null)}>{busy ? t.loading : t.load}</Button>}
      {page && !campaign && <>
        {!page.items.length ? <p className={css.empty}>{t.noCampaigns}</p> : <ul className={css.campaigns} aria-label={t.campaign}>
          {page.items.map(item => <li key={item.entity_ref}>
            <button type="button" className={css.row} disabled={busy} onClick={() => { setCampaign(item); setMetrics(null); setProposal(null); setReview(null) }}>
              <span className={css.name}>{item.name}<small>{t[item.status]}</small></span>
              <span className={css.amount}>{money(item.budget)}<small>{t.budget}</small></span>
            </button>
          </li>)}
        </ul>}
        {page.cursor && <Button size="sm" disabled={busy} onClick={() => void load(page.cursor)}>{t.next}</Button>}
      </>}
      {campaign && <>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setCampaign(null); setMetrics(null); setProposal(null); setReview(null) }}><ArrowLeft size={13} aria-hidden />{t.back}</Button>
        <header className={css.campaignHeading}><h2>{campaign.name}</h2><p>{t[campaign.status]} · {money(campaign.budget)} / {t.budget}</p></header>
        <Button size="sm" loading={busy} onClick={() => void run(false, signal => getManagedMetrics(policy, selected, campaign.entity_ref, signal), setMetrics)}>{t.metrics}</Button>
        {metrics && <div className={css.metrics}>
          <p className={css.note}>{t.metricsNote}</p>
          {!metrics.points.length ? <p>{t.noMetrics}</p> : <table><caption className="sr-only">{t.metrics}</caption>
            <thead><tr><th>{t.day}</th><th>{t.spend}</th><th>{t.conversions}</th></tr></thead>
            <tbody>{metrics.points.map((point, index) => <tr key={`${point.period_start}-${index}`}>
              <td>{new Date(point.period_start).toLocaleDateString(locale)}</td><td>{money(point.spend)}</td><td>{point.conversions}</td>
            </tr>)}</tbody></table>}
        </div>}
        {!campaign.is_controllable ? <p className={css.note}>{t.blocked}</p> : proposal ? <div className={css.notice} role="status">
          <strong>{t.saved}</strong><code>{proposal.proposal_id}</code>
          <p>{t.nextStep}</p>
          {(!review || expired) && <Button size="sm" loading={busy} onClick={() => void run(true, signal => prepareManagedReview(policy, selected, proposal.proposal_id, signal), setReview)}>{t.review}</Button>}
          {review && !expired && <><a className={css.reviewLink} href={review.review_url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{t.open}<ExternalLink size={13} aria-hidden /></a>
            <small>{t.expires} {new Date(review.expires_at).toLocaleTimeString(locale)}</small></>}
          {expired && <p>{t.expired}</p>}
        </div> : <ProposalForm t={t} campaign={campaign} busy={busy} submit={(change, amount, cause) => {
          void run(true, signal => proposeManagedChange(policy, selected, campaign, change, amount, cause, signal), setProposal)
        }} />}
      </>}
    </>}
  </>
}

function ProposalForm({ t, campaign, busy, submit }: { t: Copy; campaign: AdsCampaign; busy: boolean; submit: (change: 'pause' | 'budget', amount: string, cause: string) => void }) {
  const [change, setChange] = useState<'pause' | 'budget'>('budget')
  const [amount, setAmount] = useState('')
  const [cause, setCause] = useState('')
  const valid = cause.trim().length > 0 && cause.length <= 140 && (change === 'pause' || /^\d+(\.\d{1,2})?$/.test(amount))
  function onSubmit(event: FormEvent) { event.preventDefault(); if (valid && !busy) submit(change, amount, cause) }
  return <form className={css.form} onSubmit={onSubmit}>
    <h3>{t.proposal}</h3><fieldset disabled={busy}>
      <label className={css.field}>{t.action}<select value={change} onChange={event => setChange(event.target.value as 'pause' | 'budget')}>
        <option value="budget">{t.adjust}</option><option value="pause">{t.pause}</option>
      </select></label>
      {change === 'budget' && <label className={css.field}>{t.amount} · {campaign.budget.currency}
        <input value={amount} onChange={event => setAmount(event.target.value)} inputMode="decimal" autoComplete="off" maxLength={18} required pattern="[0-9]+(\.[0-9]{1,2})?" aria-describedby="ads-amount-hint" />
        <small id="ads-amount-hint">{t.exact}</small>
      </label>}
      <label className={css.field}>{t.reason}<textarea value={cause} onChange={event => setCause(event.target.value)} maxLength={140} rows={2} required /></label>
      <Button type="submit" size="sm" variant="primary" disabled={!valid} loading={busy}>{t.save}</Button>
    </fieldset>
  </form>
}
