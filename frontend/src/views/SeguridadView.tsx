/**
 * SeguridadView — Security, governance, and HITL approvals.
 *
 * Three sub-areas mirroring vanilla security.js / governance.js / approvals.js:
 *   (a) Pending HITL approvals — polled every 3 s, Approve/Deny with MFA form.
 *   (b) Governance — MFA enrollment + security policy presets + per-tool toggles.
 *   (c) Security center — egress permissions, audit chain, recent scans, policy JSON.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { sileo } from 'sileo'
import {
  listPendingApprovals,
  resolveApproval,
  mfaStatus,
  mfaEnroll,
  mfaSetRiddle,
  getPolicies,
  setPolicyPreset,
  setPolicyTool,
  setMfaOnDangers,
  getSecurityScans,
  getAuditChainHead,
  getSecurityPolicy,
  listEgressDomains,
  grantEgressDomain,
  revokeEgressDomain,
  recordInstallDecision,
} from '../api/client'
import type {
  PendingApproval,
  MfaStatus,
  PoliciesResponse,
  SecurityScan,
  AuditHead,
} from '../api/types'

// ── Approval cards ────────────────────────────────────────────────────────────

interface ApprovalCardProps {
  approval: PendingApproval
  onResolved(): void
}

function ApprovalCard({ approval, onResolved }: ApprovalCardProps) {
  const [showMfa, setShowMfa] = useState(false)
  const [totp, setTotp] = useState('')
  const [riddle, setRiddle] = useState('')
  const [humanity, setHumanity] = useState(false)
  const [busy, setBusy] = useState(false)
  const [mfaError, setMfaError] = useState('')
  const totpRef = useRef<HTMLInputElement>(null)

  const params = approval.parameters
  const paramEntries = params && typeof params === 'object' && !Array.isArray(params)
    ? Object.entries(params).slice(0, 8)
    : []

  async function handleDeny() {
    setBusy(true)
    try {
      await resolveApproval(approval.proposal_id, 'deny')
      sileo.success({ title: 'Denegado' })
      onResolved()
    } catch (err) {
      sileo.error({ title: `No se pudo denegar: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  function openMfa() {
    setShowMfa(true)
    setTimeout(() => totpRef.current?.focus(), 50)
  }

  async function handleApprove(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setMfaError('')
    try {
      await resolveApproval(approval.proposal_id, 'once', {
        totp: totp.trim() || null,
        riddle_answer: riddle.trim() || null,
        humanity: humanity ? 'confirmado' : null,
      })
      sileo.success({ title: 'Aprobado' })
      onResolved()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setMfaError(msg)
      sileo.error({ title: `No se pudo aprobar: ${msg}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="seg-approval-card"
      role="alertdialog"
      aria-label={`Aprobación requerida: ${approval.summary}`}
    >
      <div className="seg-approval-card__body">
        <p className="seg-approval-card__question">{approval.summary}</p>
        {approval.target && (
          <p className="seg-approval-card__target">{approval.target}</p>
        )}
        {paramEntries.length > 0 && (
          <dl className="seg-approval-card__params">
            {paramEntries.map(([k, v]) => (
              <div key={k} className="seg-approval-card__param-row">
                <dt>{k}</dt>
                <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      <div className="seg-approval-card__actions" role="group" aria-label="Acciones de aprobación">
        <button
          className="cv-btn cv-btn--primary cv-btn--sm"
          onClick={openMfa}
          disabled={busy}
          type="button"
          aria-label="Permitir esta acción (requiere tu MFA)"
        >
          Permitir…
        </button>
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm"
          onClick={handleDeny}
          disabled={busy}
          type="button"
          aria-label="Denegar esta acción"
        >
          Denegar
        </button>
      </div>

      {showMfa && (
        <form
          className="seg-approval-card__mfa"
          onSubmit={handleApprove}
          aria-label="Verificación del dueño"
        >
          <input
            ref={totpRef}
            className="cv-input"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={8}
            placeholder="Código MFA (6 dígitos)"
            aria-label="Código MFA"
            value={totp}
            onChange={e => setTotp(e.target.value)}
          />
          <input
            className="cv-input"
            type="text"
            placeholder="Respuesta del acertijo (si se pide)"
            aria-label="Respuesta del acertijo"
            value={riddle}
            onChange={e => setRiddle(e.target.value)}
          />
          <label className="seg-approval-card__humanity">
            <input
              type="checkbox"
              checked={humanity}
              onChange={e => setHumanity(e.target.checked)}
            />
            Confirmo que soy yo
          </label>
          {mfaError && (
            <p className="seg-approval-card__mfa-error" role="alert">{mfaError}</p>
          )}
          <div className="seg-approval-card__mfa-actions">
            <button
              type="submit"
              className="cv-btn cv-btn--primary cv-btn--sm"
              disabled={busy}
            >
              Confirmar
            </button>
            <button
              type="button"
              className="cv-btn cv-btn--ghost cv-btn--sm"
              onClick={() => setShowMfa(false)}
              disabled={busy}
            >
              Cancelar
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ── Approvals section ─────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3000

function ApprovalsSection() {
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    const data = await listPendingApprovals()
    setApprovals(Array.isArray(data) ? data : [])
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [load])

  return (
    <section className="cv-section">
      <div className="cv-section-label">Aprobaciones pendientes</div>
      {loading ? (
        <div className="cv-skeleton" aria-busy="true" aria-label="Cargando aprobaciones…" />
      ) : approvals.length === 0 ? (
        <div className="cv-empty">Sin aprobaciones pendientes.</div>
      ) : (
        <div className="cv-list">
          {approvals.map(a => (
            <ApprovalCard
              key={a.proposal_id}
              approval={a}
              onResolved={load}
            />
          ))}
        </div>
      )}
    </section>
  )
}

// ── Governance section ────────────────────────────────────────────────────────

const PRESETS: Array<[string, string, string]> = [
  ['equilibrado', 'Equilibrado', 'Todo activo salvo lo más delicado (recomendado)'],
  ['permisivo', 'Permisivo', 'Todo activo — tu responsabilidad'],
  ['bloqueado', 'Bloqueado', 'Todo desactivado (máximo bloqueo)'],
]

function GovernanceSection() {
  const [mfa, setMfa] = useState<MfaStatus | null>(null)
  const [pol, setPol] = useState<PoliciesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [mfaUri, setMfaUri] = useState('')
  const [riddleQ, setRiddleQ] = useState('')
  const [riddleA, setRiddleA] = useState('')
  const [riddleTotp, setRiddleTotp] = useState('')
  const [polTotp, setPolTotp] = useState('')
  const [polRiddle, setPolRiddle] = useState('')

  const load = useCallback(async () => {
    const [m, p] = await Promise.all([mfaStatus(), getPolicies()])
    setMfa(m)
    setPol(p)
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  async function handleEnroll() {
    try {
      const r = await mfaEnroll(null)
      setMfaUri(r.otpauth_uri ?? '')
      sileo.success({ title: 'MFA activado — escanea el código' })
      await load()
    } catch (err) {
      sileo.error({ title: `No se pudo activar MFA: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleRiddleSave() {
    if (!riddleQ.trim() || !riddleA.trim() || !riddleTotp.trim()) {
      sileo.error({ title: 'Rellena pregunta, respuesta y código MFA' })
      return
    }
    try {
      await mfaSetRiddle(riddleTotp, riddleQ, riddleA)
      sileo.success({ title: 'Acertijo guardado' })
      setRiddleQ(''); setRiddleA(''); setRiddleTotp('')
    } catch (err) {
      sileo.error({ title: `No se pudo guardar: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handlePreset(preset: string) {
    try {
      await setPolicyPreset(preset, polTotp, polRiddle || null)
      sileo.success({ title: `Preset «${preset}» aplicado` })
      await load()
    } catch (err) {
      sileo.error({ title: `No se pudo aplicar: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleToolToggle(tool: string, enabled: boolean) {
    try {
      await setPolicyTool(tool, enabled, polTotp, polRiddle || null)
      sileo.success({ title: `${tool}: ${enabled ? 'activado' : 'desactivado'}` })
      setPol(prev => prev ? { ...prev, tools: { ...prev.tools, [tool]: enabled } } : prev)
    } catch (err) {
      sileo.error({ title: `No se pudo cambiar: ${err instanceof Error ? err.message : err}` })
      // revert optimistic update
      setPol(prev => prev ? { ...prev, tools: { ...prev.tools, [tool]: !enabled } } : prev)
    }
  }

  async function handleMfaDangers(checked: boolean) {
    if (!checked && !window.confirm(
      'Vas a permitir que el agente ejecute comandos PELIGROSOS en autónomo sin tu aprobación.\n' +
      'La jaula sigue conteniendo el daño, pero TÚ te haces responsable.\n\n¿Continuar?'
    )) {
      return
    }
    try {
      await setMfaOnDangers(checked, polTotp, polRiddle || null)
      sileo.success({ title: checked ? 'MFA en peligrosos: ACTIVO' : 'MFA en peligrosos: desactivado' })
      setPol(prev => prev ? { ...prev, mfa_on_dangers: checked } : prev)
    } catch (err) {
      sileo.error({ title: `No se pudo cambiar: ${err instanceof Error ? err.message : err}` })
    }
  }

  if (loading) return <div className="cv-skeleton" aria-busy="true" aria-label="Cargando gobernanza…" />
  if (!mfa || !pol) return null

  const toolNames = Object.keys(pol.tools ?? {}).sort()

  return (
    <>
      {/* MFA enrollment */}
      <section className="cv-section">
        <div className="cv-section-label">Tu verificación (MFA)</div>
        <div className="seg-card">
          <p className="seg-card__intro">
            {mfa.enrolled
              ? 'MFA activo. Aprobar acciones peligrosas y cambiar políticas requiere tu código.'
              : 'Sin MFA no puedes aprobar acciones peligrosas. Actívalo con tu app de autenticación.'}
            {mfa.enrolled && (mfa.riddle_set
              ? ' Acertijo configurado.'
              : ' Falta tu acertijo (necesario para lo más delicado).')}
          </p>

          {!mfa.enrolled && (
            <>
              <button
                className="cv-btn cv-btn--primary"
                onClick={handleEnroll}
                type="button"
              >
                Activar MFA
              </button>
              {mfaUri && (
                <p className="seg-card__uri">
                  Escanéalo en tu app (Google Authenticator, Aegis…):
                  <br />
                  <code>{mfaUri}</code>
                </p>
              )}
            </>
          )}

          {mfa.enrolled && (
            <details className="seg-details">
              <summary>{mfa.riddle_set ? 'Cambiar' : 'Configurar'} acertijo personal</summary>
              <div className="seg-details__body">
                <input
                  className="cv-input"
                  placeholder="Pregunta (ej. ciudad donde nací)"
                  aria-label="Pregunta del acertijo"
                  value={riddleQ}
                  onChange={e => setRiddleQ(e.target.value)}
                />
                <input
                  className="cv-input"
                  placeholder="Respuesta"
                  aria-label="Respuesta del acertijo"
                  value={riddleA}
                  onChange={e => setRiddleA(e.target.value)}
                />
                <input
                  className="cv-input"
                  inputMode="numeric"
                  placeholder="Tu código MFA actual"
                  aria-label="Código MFA para guardar acertijo"
                  value={riddleTotp}
                  onChange={e => setRiddleTotp(e.target.value)}
                />
                <button className="cv-btn cv-btn--primary" onClick={handleRiddleSave} type="button">
                  Guardar acertijo
                </button>
              </div>
            </details>
          )}
        </div>
      </section>

      {/* Policies */}
      <section className="cv-section">
        <div className="cv-section-label">Políticas de seguridad — qué puede hacer el agente</div>
        <div className="seg-card">
          <p className="seg-card__intro">
            Cambiar cualquier política requiere tu código MFA + acertijo (así el agente nunca abre su propia jaula).
          </p>

          <div className="seg-pol-inputs">
            <input
              className="cv-input"
              inputMode="numeric"
              placeholder="Código MFA"
              aria-label="Código MFA para cambiar políticas"
              value={polTotp}
              onChange={e => setPolTotp(e.target.value)}
            />
            <input
              className="cv-input"
              placeholder="Respuesta del acertijo"
              aria-label="Respuesta del acertijo para cambiar políticas"
              value={polRiddle}
              onChange={e => setPolRiddle(e.target.value)}
            />
          </div>

          <label className="seg-tool-row" style={{ fontWeight: 600, marginBottom: 8 }}>
            <input
              type="checkbox"
              checked={pol.mfa_on_dangers ?? true}
              onChange={e => handleMfaDangers(e.target.checked)}
              aria-label="Pedir MFA para comandos peligrosos"
            />
            Pedir mi MFA para los comandos peligrosos (recomendado)
          </label>
          <p className="seg-card__intro" style={{ marginBottom: 12 }}>
            Si lo desactivas, el agente ejecutará acciones peligrosas en autónomo sin pedírtelo.
            La jaula sigue conteniendo el daño, pero tú te haces responsable.
          </p>

          <div className="cv-section-label" style={{ marginBottom: 6 }}>Preset</div>
          <div className="seg-presets">
            {PRESETS.map(([id, label, desc]) => (
              <button
                key={id}
                className={`cv-btn cv-btn--sm ${pol.preset === id ? 'cv-btn--primary' : 'cv-btn--secondary'}`}
                title={desc}
                onClick={() => handlePreset(id)}
                type="button"
              >
                {label}
              </button>
            ))}
          </div>

          {toolNames.length > 0 && (
            <details className="seg-details" style={{ marginTop: 12 }}>
              <summary>Comandos uno a uno ({toolNames.length})</summary>
              <div className="seg-tool-list">
                {toolNames.map(name => (
                  <label key={name} className="seg-tool-row">
                    <input
                      type="checkbox"
                      checked={pol.tools?.[name] ?? false}
                      onChange={e => handleToolToggle(name, e.target.checked)}
                      aria-label={`Permiso para ${name}`}
                    />
                    <span>{name}</span>
                  </label>
                ))}
              </div>
            </details>
          )}
        </div>
      </section>
    </>
  )
}

// ── Egress section ────────────────────────────────────────────────────────────

function EgressSection() {
  const [domains, setDomains] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [input, setInput] = useState('')
  const [status, setStatus] = useState<{ msg: string; kind: '' | 'ok' | 'error' }>({ msg: '', kind: '' })

  const loadDomains = useCallback(async () => {
    const res = await listEgressDomains()
    setDomains(res.domains ?? [])
    setLoading(false)
  }, [])

  useEffect(() => { loadDomains() }, [loadDomains])

  async function handleGrant() {
    const d = input.trim().toLowerCase()
    if (!d) return
    setStatus({ msg: `Autorizando ${d}…`, kind: '' })
    try {
      await grantEgressDomain(d)
      setStatus({ msg: `${d} autorizado.`, kind: 'ok' })
      sileo.success({ title: `${d} autorizado` })
      setInput('')
      await loadDomains()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setStatus({ msg: `No se pudo autorizar: ${msg}`, kind: 'error' })
      sileo.error({ title: `No se pudo autorizar: ${msg}` })
    }
  }

  async function handleRevoke(d: string) {
    setStatus({ msg: `Revocando ${d}…`, kind: '' })
    try {
      await revokeEgressDomain(d)
      setStatus({ msg: `${d} revocado.`, kind: 'ok' })
      sileo.success({ title: `${d} revocado` })
      await loadDomains()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setStatus({ msg: `No se pudo revocar: ${msg}`, kind: 'error' })
      sileo.error({ title: `No se pudo revocar: ${msg}` })
    }
  }

  const statusColor = status.kind === 'error' ? 'var(--danger)'
    : status.kind === 'ok' ? 'var(--ok)' : undefined

  return (
    <section className="cv-section">
      <div className="cv-section-label">Permisos de red — dominios permitidos</div>
      <div className="seg-card">
        <p className="seg-card__intro">
          Por defecto el agente no accede a ninguna web (default-deny). Autoriza aquí los dominios
          que quieras permitir (p.ej. <code>pypi.org</code>, <code>github.com</code>).
          Aplica al navegador y al terminal del agente.
        </p>
        <div className="cv-form-inline">
          <input
            id="egress-domain-input"
            className="cv-input"
            type="text"
            placeholder="dominio (ej. github.com)"
            autoComplete="off"
            spellCheck={false}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleGrant() }}
            aria-label="Dominio a autorizar"
          />
          <button
            className="cv-btn cv-btn--primary"
            onClick={handleGrant}
            type="button"
          >
            Autorizar
          </button>
        </div>
        {status.msg && (
          <p style={{ fontSize: 'var(--text-label)', color: statusColor, marginTop: 4 }}>
            {status.msg}
          </p>
        )}
        {loading ? (
          <div className="cv-skeleton" aria-busy="true" />
        ) : domains.length === 0 ? (
          <p className="cv-empty">Ningún dominio autorizado — el agente no accede a la red.</p>
        ) : (
          <ul className="cv-list" aria-label="Dominios autorizados">
            {domains.map(d => (
              <li key={d} className="seg-egress-row">
                <code className="seg-egress-row__domain">{d}</code>
                <button
                  className="cv-btn cv-btn--ghost cv-btn--sm"
                  onClick={() => handleRevoke(d)}
                  type="button"
                  aria-label={`Revocar dominio ${d}`}
                >
                  Revocar
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}

// ── Severity badge ────────────────────────────────────────────────────────────

function SeverityBadge({ severity }: { severity: string }) {
  const map: Record<string, [string, string]> = {
    critical: ['#FF453A', 'CRÍTICO'],
    high:     ['#FF8C00', 'ALTO'],
    medium:   ['#F5B945', 'MEDIO'],
    low:      ['#34D399', 'BAJO'],
    info:     ['#9A9AA2', 'INFO'],
  }
  const [color, label] = map[severity.toLowerCase()] ?? ['#9A9AA2', severity.toUpperCase()]
  return (
    <span
      className="seg-severity-badge"
      style={{ color, background: `${color}22` }}
    >
      {label}
    </span>
  )
}

// ── Scan row ──────────────────────────────────────────────────────────────────

interface ScanRowProps {
  scan: SecurityScan
}

function ScanRow({ scan }: ScanRowProps) {
  const [showAllow, setShowAllow] = useState(false)
  const [totp, setTotp] = useState('')
  const [riddle, setRiddle] = useState('')
  const [busy, setBusy] = useState(false)
  const [allowed, setAllowed] = useState(
    String(scan.decision ?? '').toUpperCase() === 'ALLOWED'
  )

  const verdict = String(scan.verdict ?? '').toUpperCase()
  const sev = String(scan.severity ?? '').toLowerCase()
  const flagged = verdict === 'FAIL' || verdict === 'WARN' || sev === 'critical' || sev === 'high'
  const scanId = scan.scan_id ?? scan.id

  const name = scan.name ?? scan.identifier ?? scan.scan_id ?? 'Escaneo'
  const target = scan.target ?? scan.identifier

  async function handleAllow(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await recordInstallDecision({
        scan_id: scanId!,
        decision: 'allow',
        identifier: scan.identifier ?? scan.target ?? '',
        kind: scan.kind ?? '',
        score: scan.score ?? -1,
        verdict: verdict || '',
        risks_json: '[]',
        totp: totp.trim(),
        riddle_answer: riddle.trim() || null,
      })
      sileo.success({ title: 'Instalación permitida (decisión soberana, auditada). Reinténtala.' })
      setAllowed(true)
      setShowAllow(false)
    } catch (err) {
      sileo.error({ title: `No se pudo permitir: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="seg-scan-row">
      <div className="seg-scan-row__left">
        <div className="seg-scan-row__name">{name}</div>
        {target && <div className="seg-scan-row__target">{target}</div>}
      </div>
      <div className="seg-scan-row__right">
        {scan.severity && <SeverityBadge severity={scan.severity} />}
        {scan.score != null && (
          <span className="seg-score">{scan.score}</span>
        )}
        {allowed && (
          <span className="seg-severity-badge" style={{ color: '#34D399', background: '#34D39922' }}>
            PERMITIDO
          </span>
        )}
        {flagged && !allowed && scanId && (
          <button
            className="cv-btn cv-btn--ghost cv-btn--sm"
            onClick={() => setShowAllow(v => !v)}
            type="button"
          >
            Permitir igualmente
          </button>
        )}
      </div>
      {showAllow && (
        <form className="seg-allow-form" onSubmit={handleAllow}>
          <input
            className="cv-input"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={8}
            placeholder="Código MFA"
            aria-label="Código MFA para permitir instalación"
            style={{ width: 130 }}
            value={totp}
            onChange={e => setTotp(e.target.value)}
          />
          <input
            className="cv-input"
            type="text"
            placeholder="Respuesta del acertijo"
            aria-label="Respuesta del acertijo"
            style={{ flex: 1 }}
            value={riddle}
            onChange={e => setRiddle(e.target.value)}
          />
          <button type="submit" className="cv-btn cv-btn--primary cv-btn--sm" disabled={busy}>
            Confirmar
          </button>
        </form>
      )}
    </div>
  )
}

// ── Security center section ───────────────────────────────────────────────────

function SecurityCenterSection() {
  const [scans, setScans] = useState<SecurityScan[] | null>(null)
  const [auditHead, setAuditHead] = useState<AuditHead | null | undefined>(undefined)
  const [policy, setPolicy] = useState<unknown>(undefined)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([getSecurityScans(), getAuditChainHead(), getSecurityPolicy()])
      .then(([s, a, p]) => {
        setScans(Array.isArray(s) ? s : [])
        setAuditHead(a)
        setPolicy(p)
      })
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <div className="cv-skeleton" aria-busy="true" aria-label="Cargando centro de seguridad…" />
  }

  return (
    <>
      <section className="cv-section">
        <div className="cv-section-label">Cadena de auditoría</div>
        <div className="seg-card">
          {!auditHead ? (
            <p className="cv-empty">Sin datos de cadena de auditoría.</p>
          ) : (
            <div className="seg-audit-head">
              <code className="seg-audit-head__hash">
                {auditHead.hash ?? auditHead.head ?? '—'}
              </code>
              {auditHead.timestamp && (
                <span className="seg-audit-head__time">
                  {new Date(auditHead.timestamp).toLocaleString('es')}
                </span>
              )}
            </div>
          )}
        </div>
      </section>

      <section className="cv-section">
        <div className="cv-section-label">Escaneos recientes</div>
        {!scans || scans.length === 0 ? (
          <p className="cv-empty">Sin escaneos recientes.</p>
        ) : (
          <div className="cv-list">
            {scans.map((s, i) => (
              <ScanRow key={s.scan_id ?? s.id ?? i} scan={s} />
            ))}
          </div>
        )}
      </section>

      <section className="cv-section">
        <div className="cv-section-label">Política activa</div>
        <div className="seg-card">
          {policy == null ? (
            <p className="cv-empty">Sin política configurada.</p>
          ) : (
            <pre className="seg-policy-pre">
              {JSON.stringify(policy, null, 2)}
            </pre>
          )}
        </div>
      </section>
    </>
  )
}

// ── SeguridadView ─────────────────────────────────────────────────────────────

export default function SeguridadView() {
  return (
    <div className="cv-view-body">
      <div className="view-header" style={{ padding: 0, border: 'none' }}>
        <h1 className="view-title">Seguridad y gobernanza</h1>
        <p className="view-subtitle">
          Aprobaciones HITL, políticas del agente, escaneos y cadena de auditoría.
        </p>
      </div>

      <ApprovalsSection />
      <GovernanceSection />
      <EgressSection />
      <SecurityCenterSection />
    </div>
  )
}
