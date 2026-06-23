/**
 * SeguridadView — Security, governance, and HITL approvals.
 *
 * Three sub-areas mirroring vanilla security.js / governance.js / approvals.js:
 *   (a) Pending HITL approvals — polled every 3 s, Approve/Deny with MFA form.
 *   (b) Governance — MFA enrollment + security policy presets + catalog-based toggles.
 *   (c) Security center — egress permissions, audit chain, recent scans, policy JSON.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { sileo } from 'sileo'
import {
  listPendingApprovals,
  mfaStatus,
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
  PolicyCatalogEntry,
  SecurityScan,
  AuditHead,
} from '../api/types'
import ApprovalCard from '../components/ApprovalCard'
import MfaEnroll from '../components/MfaEnroll'
import { useConfirmDialog } from '../components/ConfirmDialog'

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
  ['equilibrado', 'Equilibrado', 'Todo activo salvo las acciones de mayor riesgo (recomendado)'],
  ['permisivo', 'Permisivo', 'Todo activo — el agente actúa sin restricciones, bajo tu responsabilidad'],
  ['bloqueado', 'Bloqueado', 'Todo desactivado — el agente no puede ejecutar ninguna acción'],
]

// Human-readable names for known category slugs.
const CATEGORY_LABELS: Record<string, string> = {
  filesystem:   'Sistema de archivos',
  network:      'Red',
  terminal:     'Terminal',
  browser:      'Navegador',
  memory:       'Memoria',
  tasks:        'Tareas programadas',
  agents:       'Agentes',
  providers:    'Modelos y proveedores',
  security:     'Seguridad del sistema',
  mcp:          'Herramientas externas (MCP)',
  composio:     'Apps conectadas (Composio)',
}

// Categories that are system-defense tools, not LLM capabilities.
// These get separated into the "Defensas del sistema" block.
const DEFENSE_CATEGORIES = new Set(['security'])

function categoryLabel(cat: string): string {
  return CATEGORY_LABELS[cat] ?? cat.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase())
}

type DelicacyLevel = 'normal' | 'delicate' | 'most_delicate'

function aggregateDelicacy(entries: PolicyCatalogEntry[]): DelicacyLevel {
  if (entries.some(e => e.delicacy === 'most_delicate')) return 'most_delicate'
  if (entries.some(e => e.delicacy === 'delicate')) return 'delicate'
  return 'normal'
}

// ── Delicacy badge ────────────────────────────────────────────────────────────

function DelicacyBadge({ level, size = 'normal' }: { level: DelicacyLevel; size?: 'normal' | 'sm' }) {
  if (level === 'normal') return null
  const label = level === 'most_delicate' ? 'Muy delicado' : 'Delicado'
  const color = level === 'most_delicate' ? 'var(--danger)' : 'var(--warn)'
  return (
    <span
      className={`seg-pol-badge ${size === 'sm' ? 'seg-pol-badge--sm' : ''}`}
      style={{ color, background: `${color}22` }}
      aria-label={`Nivel de delicadeza: ${label}`}
    >
      {label}
    </span>
  )
}

// ── Mini toggle switch ────────────────────────────────────────────────────────

interface ToggleSwitchProps {
  id: string
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  indeterminate?: boolean
  'aria-label': string
}

function ToggleSwitch({ id, checked, onChange, disabled, indeterminate, 'aria-label': ariaLabel }: ToggleSwitchProps) {
  return (
    <button
      id={id}
      role="switch"
      type="button"
      aria-checked={indeterminate ? 'mixed' : checked}
      aria-label={ariaLabel}
      disabled={disabled}
      className={`seg-pol-switch ${checked && !indeterminate ? 'seg-pol-switch--on' : ''} ${indeterminate ? 'seg-pol-switch--mixed' : ''}`}
      onClick={() => onChange(!checked)}
    />
  )
}

// ── Category group ────────────────────────────────────────────────────────────

interface CategoryGroupProps {
  category: string
  entries: PolicyCatalogEntry[]
  polTotp: string
  polRiddle: string
  busy: boolean
  onToggleTool: (name: string, enabled: boolean) => Promise<void>
  onToggleAll: (category: string, enabled: boolean, entries: PolicyCatalogEntry[]) => Promise<void>
}

function CategoryGroup({
  category,
  entries,
  polTotp,
  polRiddle,
  busy,
  onToggleTool,
  onToggleAll,
}: CategoryGroupProps) {
  const [expanded, setExpanded] = useState(false)

  const allOn = entries.every(e => e.enabled)
  const allOff = entries.every(e => !e.enabled)
  const mixed = !allOn && !allOff
  const delicacy = aggregateDelicacy(entries)
  const switchId = `cat-switch-${category}`
  const bodyId = `cat-body-${category}`

  return (
    <div className="seg-pol-group">
      <div className="seg-pol-group__header">
        <button
          type="button"
          className="seg-pol-group__expand"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded(v => !v)}
          title={expanded ? 'Contraer' : 'Expandir herramientas'}
        >
          <span className={`seg-pol-chevron ${expanded ? 'seg-pol-chevron--open' : ''}`} aria-hidden="true">▸</span>
        </button>

        <span className="seg-pol-group__name">{categoryLabel(category)}</span>
        <span className="seg-pol-group__count" aria-label={`${entries.length} herramientas`}>{entries.length}</span>

        <DelicacyBadge level={delicacy} />

        <label className="seg-pol-group__toggle-label" htmlFor={switchId}>
          <span className="sr-only">
            {allOn ? 'Todo activo' : allOff ? 'Todo desactivado' : 'Parcialmente activo'} — activar o desactivar toda la categoría
          </span>
        </label>
        <ToggleSwitch
          id={switchId}
          aria-label={`Activar o desactivar todas las herramientas de ${categoryLabel(category)}`}
          checked={allOn}
          indeterminate={mixed}
          disabled={busy}
          onChange={v => onToggleAll(category, v, entries)}
        />
      </div>

      {expanded && (
        <ul
          id={bodyId}
          className="seg-pol-tool-list"
          aria-label={`Herramientas de ${categoryLabel(category)}`}
        >
          {entries.map(entry => (
            <ToolRow
              key={entry.name}
              entry={entry}
              busy={busy}
              polTotp={polTotp}
              polRiddle={polRiddle}
              onToggle={onToggleTool}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

// ── Tool row ──────────────────────────────────────────────────────────────────

interface ToolRowProps {
  entry: PolicyCatalogEntry
  busy: boolean
  polTotp: string
  polRiddle: string
  onToggle: (name: string, enabled: boolean) => Promise<void>
}

function ToolRow({ entry, busy, onToggle }: ToolRowProps) {
  const checkId = `tool-${entry.name}`
  const tipId = `tool-tip-${entry.name}`
  const notVisible = !entry.llm_visible

  return (
    <li className={`seg-pol-tool-row ${notVisible ? 'seg-pol-tool-row--muted' : ''}`}>
      <input
        type="checkbox"
        id={checkId}
        aria-describedby={notVisible ? tipId : undefined}
        checked={entry.enabled}
        disabled={busy}
        onChange={e => onToggle(entry.name, e.target.checked)}
        className="seg-pol-tool-check"
        aria-label={`${entry.label}: ${entry.enabled ? 'activo' : 'inactivo'}`}
      />
      <label htmlFor={checkId} className="seg-pol-tool-label">
        {entry.label}
      </label>
      <DelicacyBadge level={entry.delicacy} size="sm" />
      {notVisible && (
        <span
          id={tipId}
          className="seg-pol-tool-native"
          title="El agente usa el equivalente nativo; esta herramienta no aparece en el catálogo del LLM"
          aria-label="Usa equivalente nativo"
        >
          nativo
        </span>
      )}
    </li>
  )
}

// ── Governance section ────────────────────────────────────────────────────────

function GovernanceSection() {
  const [mfa, setMfa] = useState<MfaStatus | null>(null)
  const [pol, setPol] = useState<PoliciesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [riddleQ, setRiddleQ] = useState('')
  const [riddleA, setRiddleA] = useState('')
  const [riddleTotp, setRiddleTotp] = useState('')
  const [polTotp, setPolTotp] = useState('')
  const [polRiddle, setPolRiddle] = useState('')
  const [mfaError, setMfaError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  const load = useCallback(async () => {
    const [m, p] = await Promise.all([mfaStatus(), getPolicies()])
    setMfa(m)
    setPol(p)
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  // Group catalog entries: separate defense-category tools from capability tools.
  const { capabilityGroups, defenseGroups } = useMemo(() => {
    const catalog = pol?.catalog ?? []
    const grouped = new Map<string, PolicyCatalogEntry[]>()
    for (const entry of catalog) {
      const list = grouped.get(entry.category) ?? []
      list.push(entry)
      grouped.set(entry.category, list)
    }
    const capability: Map<string, PolicyCatalogEntry[]> = new Map()
    const defense: Map<string, PolicyCatalogEntry[]> = new Map()
    for (const [cat, entries] of grouped) {
      if (DEFENSE_CATEGORIES.has(cat)) {
        defense.set(cat, entries)
      } else {
        capability.set(cat, entries)
      }
    }
    return { capabilityGroups: capability, defenseGroups: defense }
  }, [pol?.catalog])

  // Fallback: when catalog is absent, fall back to the flat tools map
  // (pre-catalog backend compatibility).
  const legacyToolNames = useMemo(() => {
    if ((pol?.catalog?.length ?? 0) > 0) return []
    return Object.keys(pol?.tools ?? {}).sort()
  }, [pol?.catalog, pol?.tools])

  function validateMfa(): boolean {
    if (!polTotp.trim()) {
      setMfaError('Introduce tu código MFA antes de cambiar una política.')
      return false
    }
    setMfaError('')
    return true
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
    if (!validateMfa()) return
    setBusy(true)
    try {
      await setPolicyPreset(preset, polTotp, polRiddle || null)
      sileo.success({ title: `Preset «${preset}» aplicado` })
      await load()
    } catch (err) {
      sileo.error({ title: `No se pudo aplicar: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  // Single-tool toggle (used by checkbox rows and master-toggle batches).
  async function handleToolToggle(toolName: string, enabled: boolean) {
    if (!validateMfa()) return
    try {
      await setPolicyTool(toolName, enabled, polTotp, polRiddle || null)
      // Optimistic update: keep local state in sync
      setPol(prev => {
        if (!prev) return prev
        const updatedCatalog = prev.catalog?.map(e =>
          e.name === toolName ? { ...e, enabled } : e
        )
        return {
          ...prev,
          tools: { ...prev.tools, [toolName]: enabled },
          catalog: updatedCatalog,
        }
      })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      sileo.error({ title: `No se pudo cambiar ${toolName}: ${msg}` })
      // Revert optimistic update
      setPol(prev => {
        if (!prev) return prev
        const revertedCatalog = prev.catalog?.map(e =>
          e.name === toolName ? { ...e, enabled: !enabled } : e
        )
        return {
          ...prev,
          tools: { ...prev.tools, [toolName]: !enabled },
          catalog: revertedCatalog,
        }
      })
    }
  }

  // Master-toggle: fires one setPolicyTool per tool in the category sequentially.
  async function handleToggleAll(
    _category: string,
    targetEnabled: boolean,
    entries: PolicyCatalogEntry[],
  ) {
    if (!validateMfa()) return
    setBusy(true)
    const toChange = entries.filter(e => e.enabled !== targetEnabled)
    let firstError: string | null = null
    for (const entry of toChange) {
      try {
        await setPolicyTool(entry.name, targetEnabled, polTotp, polRiddle || null)
        // Optimistic update each tool as it succeeds
        setPol(prev => {
          if (!prev) return prev
          const updatedCatalog = prev.catalog?.map(e =>
            e.name === entry.name ? { ...e, enabled: targetEnabled } : e
          )
          return {
            ...prev,
            tools: { ...prev.tools, [entry.name]: targetEnabled },
            catalog: updatedCatalog,
          }
        })
      } catch (err) {
        firstError = err instanceof Error ? err.message : String(err)
        break
      }
    }
    setBusy(false)
    if (firstError) {
      sileo.error({ title: `Error al cambiar la categoría: ${firstError}` })
      await load() // re-sync from server after partial failure
    } else if (toChange.length > 0) {
      sileo.success({ title: `Categoría ${targetEnabled ? 'activada' : 'desactivada'}` })
    }
  }

  async function handleMfaDangers(checked: boolean) {
    if (!validateMfa()) return
    if (!checked) {
      const ok = await confirm({
        title: 'Desactivar verificación en acciones peligrosas',
        description:
          'El agente podrá ejecutar acciones de alto riesgo en modo autónomo sin pedirte confirmación. ' +
          'La protección de la jaula sigue activa, pero tú asumes la responsabilidad. ¿Continuar?',
        confirmLabel: 'Desactivar',
        variant: 'danger',
      })
      if (!ok) return
    }
    setBusy(true)
    try {
      await setMfaOnDangers(checked, polTotp, polRiddle || null)
      sileo.success({ title: checked ? 'Verificación en peligrosos: activa' : 'Verificación en peligrosos: desactivada' })
      setPol(prev => prev ? { ...prev, mfa_on_dangers: checked } : prev)
    } catch (err) {
      sileo.error({ title: `No se pudo cambiar: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  // Legacy single-tool toggle (when catalog is absent).
  async function handleLegacyToolToggle(toolName: string, enabled: boolean) {
    if (!validateMfa()) return
    try {
      await setPolicyTool(toolName, enabled, polTotp, polRiddle || null)
      sileo.success({ title: `${toolName}: ${enabled ? 'activado' : 'desactivado'}` })
      setPol(prev => prev ? { ...prev, tools: { ...prev.tools, [toolName]: enabled } } : prev)
    } catch (err) {
      sileo.error({ title: `No se pudo cambiar: ${err instanceof Error ? err.message : err}` })
      setPol(prev => prev ? { ...prev, tools: { ...prev.tools, [toolName]: !enabled } } : prev)
    }
  }

  if (loading) return <div className="cv-skeleton" aria-busy="true" aria-label="Cargando gobernanza…" />
  if (!mfa || !pol) return null

  const hasCatalog = (pol.catalog?.length ?? 0) > 0

  return (
    <>
      {ConfirmDialogNode}
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

          {!mfa.enrolled && <MfaEnroll onEnrolled={load} />}

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
            Cambiar cualquier política requiere tu código MFA (así el agente nunca abre su propia jaula).
          </p>

          {/* Shared MFA input — applies to every toggle below */}
          <fieldset className="seg-pol-mfa-bar" aria-label="Autenticación para cambiar políticas">
            <legend className="sr-only">Código MFA para políticas</legend>
            <div className="seg-pol-inputs">
              <div className="seg-pol-input-wrap">
                <label htmlFor="pol-totp" className="cv-label">Código MFA</label>
                <input
                  id="pol-totp"
                  className="cv-input"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="6 dígitos"
                  aria-describedby={mfaError ? 'pol-mfa-error' : undefined}
                  aria-invalid={!!mfaError}
                  value={polTotp}
                  onChange={e => { setPolTotp(e.target.value); setMfaError('') }}
                />
              </div>
              <div className="seg-pol-input-wrap">
                <label htmlFor="pol-riddle" className="cv-label">Acertijo (si aplica)</label>
                <input
                  id="pol-riddle"
                  className="cv-input"
                  placeholder="Respuesta del acertijo"
                  aria-label="Respuesta del acertijo para políticas"
                  value={polRiddle}
                  onChange={e => setPolRiddle(e.target.value)}
                />
              </div>
            </div>
            {mfaError && (
              <p id="pol-mfa-error" className="seg-pol-mfa-error" role="alert">
                {mfaError}
              </p>
            )}
          </fieldset>

          {/* MFA on dangers global toggle */}
          <div className="seg-pol-danger-row">
            <div className="seg-pol-danger-row__info">
              <span className="seg-pol-danger-row__label">
                Pedir mi MFA para los comandos peligrosos
              </span>
              <span className="seg-pol-danger-row__hint">
                Si lo desactivas, el agente ejecuta acciones peligrosas en autónomo sin pedírtelo. Recomendado mantenerlo activo.
              </span>
            </div>
            <ToggleSwitch
              id="toggle-mfa-dangers"
              aria-label="Pedir MFA para comandos peligrosos"
              checked={pol.mfa_on_dangers ?? true}
              disabled={busy}
              onChange={handleMfaDangers}
            />
          </div>

          {/* Preset quick-access */}
          <div>
            <div className="seg-pol-sub-label">Preset rápido</div>
            <div className="seg-presets">
              {PRESETS.map(([id, label, desc]) => (
                <button
                  key={id}
                  className={`cv-btn cv-btn--sm ${pol.preset === id ? 'cv-btn--primary' : 'cv-btn--secondary'}`}
                  title={desc}
                  onClick={() => handlePreset(id)}
                  type="button"
                  disabled={busy}
                  aria-pressed={pol.preset === id}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Catalog-based grouped view */}
          {hasCatalog && capabilityGroups.size > 0 && (
            <div>
              <div className="seg-pol-sub-label">Capacidades del agente</div>
              <div className="seg-pol-catalog">
                {[...capabilityGroups.entries()].map(([cat, entries]) => (
                  <CategoryGroup
                    key={cat}
                    category={cat}
                    entries={entries}
                    polTotp={polTotp}
                    polRiddle={polRiddle}
                    busy={busy}
                    onToggleTool={handleToolToggle}
                    onToggleAll={handleToggleAll}
                  />
                ))}
              </div>
            </div>
          )}

          {/* System defenses — separated from agent capabilities */}
          {hasCatalog && defenseGroups.size > 0 && (
            <div>
              <div className="seg-pol-sub-label">Defensas del sistema</div>
              <p className="seg-card__intro" style={{ marginTop: 0, marginBottom: 8 }}>
                Estas herramientas protegen el sistema. No son capacidades del LLM — el agente no las invoca directamente.
              </p>
              <div className="seg-pol-catalog">
                {[...defenseGroups.entries()].map(([cat, entries]) => (
                  <CategoryGroup
                    key={cat}
                    category={cat}
                    entries={entries}
                    polTotp={polTotp}
                    polRiddle={polRiddle}
                    busy={busy}
                    onToggleTool={handleToolToggle}
                    onToggleAll={handleToggleAll}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Legacy flat list — shown only when catalog is absent */}
          {!hasCatalog && legacyToolNames.length > 0 && (
            <details className="seg-details" style={{ marginTop: 12 }}>
              <summary>Comandos uno a uno ({legacyToolNames.length})</summary>
              <div className="seg-tool-list">
                {legacyToolNames.map(name => (
                  <label key={name} className="seg-tool-row">
                    <input
                      type="checkbox"
                      checked={pol.tools?.[name] ?? false}
                      onChange={e => handleLegacyToolToggle(name, e.target.checked)}
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
          Por defecto el agente no puede acceder a ningún sitio web. Añade aquí los dominios
          a los que quieras darle acceso (p.ej. <code>pypi.org</code>, <code>github.com</code>).
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
          Aprobaciones, políticas del agente, escaneos y cadena de auditoría.
        </p>
      </div>

      <ApprovalsSection />
      <GovernanceSection />
      <EgressSection />
      <SecurityCenterSection />
    </div>
  )
}
