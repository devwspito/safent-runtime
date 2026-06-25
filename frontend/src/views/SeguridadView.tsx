/**
 * SeguridadView — Security, governance, and HITL approvals.
 *
 * Three sub-areas:
 *   (a) Pending HITL approvals — polled every 3 s, Approve/Deny via MfaModal.
 *   (b) Governance — MFA enrollment + security policy presets + accordion catalog.
 *   (c) Security center — egress permissions, audit chain, recent scans.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { sileo } from 'sileo'
import { Save } from 'lucide-react'
import {
  listPendingApprovals,
  mfaStatus,
  getPolicies,
  setPolicyPreset,
  setPolicyTools,
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
import MfaModal from '../components/MfaModal'
import type { MfaFactors } from '../components/MfaModal'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  AnimatedChevron,
  FadeIn,
  Stagger,
  StaggerItem,
  HoverRow,
  motion,
  SPRING,
  TWEEN,
} from '../components/ui/motion'

// ── Approvals section ─────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3000

function ApprovalsSection({ mfaDisabled }: { mfaDisabled: boolean }) {
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
          <AnimatePresence initial={false}>
            {approvals.map(a => (
              <AnimatedListItem key={a.proposal_id}>
                <ApprovalCard
                  approval={a}
                  mfaDisabled={mfaDisabled}
                  onResolved={load}
                />
              </AnimatedListItem>
            ))}
          </AnimatePresence>
        </div>
      )}
    </section>
  )
}

// ── Governance section ────────────────────────────────────────────────────────

const PRESETS: Array<[string, string, string]> = [
  ['equilibrado', 'Equilibrado', 'Herramientas estándar activas; las de mayor riesgo (most_delicate) requieren tu aprobación explícita'],
  ['permisivo', 'Permisivo', 'Todas las herramientas activas — el agente actúa sin restricciones, bajo tu responsabilidad'],
  ['bloqueado', 'Bloqueado', 'Todas las herramientas desactivadas — el agente no puede ejecutar ninguna acción'],
]

/**
 * Mirror of the backend's _preset_default (tool_policy.py:224-229).
 * Returns what `enabled` would be for a given tool under the target preset,
 * before any per-tool overrides.  Used only for client-side preview.
 *
 * EQUILIBRADO: off only for most_delicate tools (those that require explicit
 * owner opt-in). Everything else is on.
 */
function presetPreviewEnabled(entry: PolicyCatalogEntry, preset: string): boolean {
  if (preset === 'permisivo') return true
  if (preset === 'bloqueado') return false
  // equilibrado: disabled only for most_delicate
  return entry.delicacy !== 'most_delicate'
}

const CATEGORY_LABELS: Record<string, string> = {
  apps:          'Apps',
  web:           'Web y navegador',
  communication: 'Comunicación',
  screen:        'Pantalla y control',
  composio:      'Apps conectadas',
  system:        'Sistema',
  orchestration: 'Orquestación',
  terminal:      'Terminal',
  media:         'Medios',
  mcp:           'Herramientas externas (MCP)',
  programming:   'Programación',
  filesystem:    'Ficheros',
  memory:        'Memoria',
  network:       'Red',
  browser:       'Navegador',
  tasks:         'Tareas programadas',
  agents:        'Agentes',
  providers:     'Modelos y proveedores',
  security:      'Seguridad del sistema',
}

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

// ── Accordion category group ──────────────────────────────────────────────────

interface CategoryGroupProps {
  category: string
  entries: PolicyCatalogEntry[]
  /** Overrides for local pending changes (tool name → enabled). */
  pendingChanges: Record<string, boolean>
  busy: boolean
  onToggleTool: (name: string, enabled: boolean) => void
  onToggleAll: (category: string, enabled: boolean, entries: PolicyCatalogEntry[]) => void
}

function CategoryGroup({
  category,
  entries,
  pendingChanges,
  busy,
  onToggleTool,
  onToggleAll,
}: CategoryGroupProps) {
  const [expanded, setExpanded] = useState(false)

  // Merge committed state with local pending changes
  const effectiveEntries = entries.map(e =>
    e.name in pendingChanges ? { ...e, enabled: pendingChanges[e.name] } : e,
  )

  const allOn = effectiveEntries.every(e => e.enabled)
  const allOff = effectiveEntries.every(e => !e.enabled)
  const mixed = !allOn && !allOff
  const delicacy = aggregateDelicacy(entries)
  const switchId = `cat-switch-${category}`
  const bodyId = `cat-body-${category}`

  return (
    <div className="seg-pol-group">
      {/* Entire header row is clickable to toggle expand/collapse */}
      <HoverRow
        className="seg-pol-group__header seg-pol-group__header--clickable"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls={bodyId}
        onClick={() => setExpanded(v => !v)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(v => !v) } }}
      >
        <AnimatedChevron open={expanded} size={13} />

        <span className="seg-pol-group__name">{categoryLabel(category)}</span>
        <span className="seg-pol-group__count" aria-label={`${entries.length} herramientas`}>{entries.length}</span>

        <DelicacyBadge level={delicacy} />

        {/* Stop propagation so the toggle switch doesn't also expand/collapse */}
        <div
          onClick={e => e.stopPropagation()}
          onKeyDown={e => e.stopPropagation()}
          style={{ display: 'contents' }}
        >
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
            onChange={v => onToggleAll(category, v, effectiveEntries)}
          />
        </div>
      </HoverRow>

      {/* AnimatedExpanderContent replaces the conditional render — smooth height transition */}
      <AnimatedExpanderContent open={expanded}>
        <ul
          id={bodyId}
          className="seg-pol-tool-list"
          aria-label={`Herramientas de ${categoryLabel(category)}`}
        >
          {effectiveEntries.map(entry => (
            <ToolRow
              key={entry.name}
              entry={entry}
              busy={busy}
              onToggle={onToggleTool}
            />
          ))}
        </ul>
      </AnimatedExpanderContent>
    </div>
  )
}

// ── Tool row ──────────────────────────────────────────────────────────────────

interface ToolRowProps {
  entry: PolicyCatalogEntry
  busy: boolean
  onToggle: (name: string, enabled: boolean) => void
}

function ToolRow({ entry, busy, onToggle }: ToolRowProps) {
  const checkId = `tool-${entry.name}`
  const tipId = `tool-tip-${entry.name}`
  const notVisible = !entry.llm_visible

  return (
    <motion.li
      className={`seg-pol-tool-row ${notVisible ? 'seg-pol-tool-row--muted' : ''}`}
      whileHover={{ x: 2 }}
      transition={SPRING}
    >
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
    </motion.li>
  )
}

// ── Pending MFA action ────────────────────────────────────────────────────────

type PendingAction =
  | { kind: 'preset'; preset: string }
  | { kind: 'batch'; changes: Record<string, boolean> }
  | { kind: 'mfa_dangers'; enabled: boolean }

// ── Governance section ────────────────────────────────────────────────────────

function GovernanceSection() {
  const [mfa, setMfa] = useState<MfaStatus | null>(null)
  const [pol, setPol] = useState<PoliciesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  // Preset preview: which preset is pending save (not yet applied)
  const [pendingPreset, setPendingPreset] = useState<string | null>(null)

  // MFA modal state: what action is waiting for factors
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)

  // Local tool overrides accumulate until "Guardar cambios" is clicked
  const [toolPending, setToolPending] = useState<Record<string, boolean>>({})
  const hasPendingTools = Object.keys(toolPending).length > 0

  const mfaDisabled = pol?.mfa_on_dangers === false

  const load = useCallback(async () => {
    const [m, p] = await Promise.all([mfaStatus(), getPolicies()])
    setMfa(m)
    setPol(p)
    setLoading(false)
    // Clear any local pending state on reload so we don't show stale overrides
    setToolPending({})
  }, [])

  useEffect(() => { load() }, [load])

  const { capabilityGroups, defenseGroups } = useMemo(() => {
    const rawCatalog = pol?.catalog ?? []
    // When a preset is pending (user clicked a preset button but hasn't saved yet),
    // project the catalog's `enabled` fields through the preset preview so the accordion
    // shows what will happen after save — not the stale committed state.
    const previewPreset = pendingPreset && pendingPreset !== pol?.preset ? pendingPreset : null
    const catalog = previewPreset
      ? rawCatalog.map(e => ({ ...e, enabled: presetPreviewEnabled(e, previewPreset) }))
      : rawCatalog

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
  }, [pol?.catalog, pol?.preset, pendingPreset])

  const legacyToolNames = useMemo(() => {
    if ((pol?.catalog?.length ?? 0) > 0) return []
    return Object.keys(pol?.tools ?? {}).sort()
  }, [pol?.catalog, pol?.tools])

  // Persist a batch of tool changes directly (no MFA modal needed).
  async function persistBatchDirect(changes: Record<string, boolean>) {
    setBusy(true)
    // Optimistic local update
    setPol(prev => {
      if (!prev) return prev
      const updatedTools = { ...prev.tools, ...changes }
      const updatedCatalog = prev.catalog?.map(e =>
        e.name in changes ? { ...e, enabled: changes[e.name] } : e,
      )
      return { ...prev, tools: updatedTools, catalog: updatedCatalog }
    })
    setToolPending({})
    try {
      await setPolicyTools(changes, '')
      sileo.success({ title: 'Cambios guardados' })
      await load()
    } catch (err) {
      await load()
      sileo.error({ title: `No se pudo guardar: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  // Handle MFA sign callback from the modal
  async function handleSign(factors: MfaFactors) {
    if (!pendingAction) return
    setBusy(true)
    try {
      if (pendingAction.kind === 'preset') {
        await setPolicyPreset(pendingAction.preset, factors.totp)
        sileo.success({ title: `Preset «${pendingAction.preset}» aplicado` })
        setPendingPreset(null)
        setPendingAction(null)
        await load()

      } else if (pendingAction.kind === 'batch') {
        // Optimistic: apply locally first
        setPol(prev => {
          if (!prev) return prev
          const updatedTools = { ...prev.tools, ...pendingAction.changes }
          const updatedCatalog = prev.catalog?.map(e =>
            e.name in pendingAction.changes ? { ...e, enabled: pendingAction.changes[e.name] } : e,
          )
          return { ...prev, tools: updatedTools, catalog: updatedCatalog }
        })
        setToolPending({})
        try {
          await setPolicyTools(pendingAction.changes, factors.totp)
          sileo.success({ title: 'Cambios guardados' })
          setPendingAction(null)
          await load()
        } catch (err) {
          // Revert optimistic update
          await load()
          sileo.error({ title: `No se pudo guardar: ${err instanceof Error ? err.message : err}` })
          return
        }

      } else if (pendingAction.kind === 'mfa_dangers') {
        await setMfaOnDangers(pendingAction.enabled, factors.totp)
        sileo.success({
          title: pendingAction.enabled
            ? 'Verificación en peligrosos: activa'
            : 'Verificación en peligrosos: desactivada',
        })
        setPol(prev => prev ? { ...prev, mfa_on_dangers: pendingAction.enabled } : prev)
        setPendingAction(null)
      }
    } catch (err) {
      sileo.error({ title: `No se pudo aplicar: ${err instanceof Error ? err.message : err}` })
      return
    } finally {
      setBusy(false)
    }
  }

  // ── Handler wrappers ─────────────────────────────────────────────────────────

  function requestPresetSave() {
    if (!pendingPreset) return
    if (mfaDisabled) {
      // No MFA required: persist directly without opening the modal
      setBusy(true)
      void setPolicyPreset(pendingPreset, '')
        .then(() => {
          sileo.success({ title: `Preset «${pendingPreset}» aplicado` })
          setPendingPreset(null)
          return load()
        })
        .catch(err => {
          sileo.error({ title: `No se pudo aplicar: ${err instanceof Error ? err.message : err}` })
        })
        .finally(() => setBusy(false))
    } else {
      setPendingAction({ kind: 'preset', preset: pendingPreset })
    }
  }

  // Individual tool toggle: update local pending only (no immediate API call)
  function requestToolToggle(tool: string, enabled: boolean) {
    setToolPending(prev => ({ ...prev, [tool]: enabled }))
  }

  // Category toggle: batch-update all tools in the category locally
  function requestSectionToggle(_category: string, enabled: boolean, entries: PolicyCatalogEntry[]) {
    const updates: Record<string, boolean> = {}
    for (const entry of entries) {
      if (entry.enabled !== enabled) {
        updates[entry.name] = enabled
      }
    }
    if (Object.keys(updates).length > 0) {
      setToolPending(prev => ({ ...prev, ...updates }))
    }
  }

  // Commit the batch of pending tool changes.
  // MFA enabled → open one MfaModal which calls handleSign on confirm.
  // MFA disabled → persist directly, no modal.
  function handleSaveToolChanges() {
    if (!hasPendingTools) return
    if (mfaDisabled) {
      void persistBatchDirect({ ...toolPending })
    } else {
      setPendingAction({ kind: 'batch', changes: { ...toolPending } })
    }
  }

  function requestMfaDangersToggle(checked: boolean) {
    if (checked) {
      void (async () => {
        setBusy(true)
        try {
          await setMfaOnDangers(true, '')
          sileo.success({ title: 'Verificación en peligrosos: activa' })
          setPol(prev => prev ? { ...prev, mfa_on_dangers: true } : prev)
        } catch (err) {
          sileo.error({ title: `No se pudo activar: ${err instanceof Error ? err.message : err}` })
        } finally {
          setBusy(false)
        }
      })()
    } else {
      setPendingAction({ kind: 'mfa_dangers', enabled: false })
    }
  }

  // Legacy tool toggle (flat tools map, no catalog)
  function requestLegacyToolToggle(toolName: string, enabled: boolean) {
    setToolPending(prev => ({ ...prev, [toolName]: enabled }))
  }

  if (loading) return <div className="cv-skeleton" aria-busy="true" aria-label="Cargando gobernanza…" />
  if (!mfa || !pol) return null

  const hasCatalog = (pol.catalog?.length ?? 0) > 0
  const currentPreset = pendingPreset ?? pol.preset
  const hasPendingPreset = pendingPreset !== null && pendingPreset !== pol.preset

  // When batch modal is open, use the already-captured changes snapshot
  const batchChanges = pendingAction?.kind === 'batch' ? pendingAction.changes : toolPending

  return (
    <>
      {/* ── MFA Modal — kept outside Stagger so it renders above all sections */}
      {pendingAction && (
        <MfaModal
          title={
            pendingAction.kind === 'preset'
              ? `Aplicar preset «${pendingAction.preset}»`
              : pendingAction.kind === 'mfa_dangers'
              ? 'Desactivar verificación en peligrosos'
              : 'Guardar cambios de capacidades'
          }
          onSign={handleSign}
          onCancel={() => {
            setPendingAction(null)
            // Restore toolPending from batch snapshot so user can adjust before retrying
            if (pendingAction?.kind === 'batch') {
              setToolPending(batchChanges)
            }
          }}
        />
      )}

      {/* ── MFA enrollment ── */}
      <section className="cv-section">
        <div className="cv-section-label">Tu verificación (MFA)</div>
        <div className="seg-card">
          <p className="seg-card__intro">
            {mfa.enrolled
              ? 'MFA activo. Aprobar acciones peligrosas y cambiar políticas requiere tu código.'
              : 'Sin MFA no puedes aprobar acciones peligrosas. Actívalo con tu app de autenticación.'}
          </p>

          {!mfa.enrolled && <MfaEnroll onEnrolled={load} />}
        </div>
      </section>

      {/* ── Policies ── */}
      <section className="cv-section">
        <div className="cv-section-label">Políticas de seguridad — qué puede hacer el agente</div>
        <div className="seg-card">
          <p className="seg-card__intro">
            Cambiar cualquier política requiere tu código MFA (así el agente nunca abre su propia jaula).
          </p>

          {/* MFA on dangers global toggle */}
          <div className="seg-pol-danger-row">
            <div className="seg-pol-danger-row__info">
              <span className="seg-pol-danger-row__label">
                Pedir mi MFA para los comandos peligrosos
              </span>
              <span className="seg-pol-danger-row__hint">
                {mfaDisabled
                  ? 'Desactivado — el agente ejecuta acciones peligrosas sin pedirte confirmación.'
                  : 'Si lo desactivas, el agente ejecuta acciones peligrosas en autónomo sin pedírtelo. Recomendado mantenerlo activo.'}
              </span>
            </div>
            <ToggleSwitch
              id="toggle-mfa-dangers"
              aria-label="Pedir MFA para comandos peligrosos"
              checked={pol.mfa_on_dangers ?? true}
              disabled={busy}
              onChange={requestMfaDangersToggle}
            />
          </div>

          {/* Preset quick-access: preview + save */}
          <div>
            <div className="seg-pol-sub-label">Preset rápido</div>
            <div className="seg-presets">
              {PRESETS.map(([id, label, desc]) => (
                <button
                  key={id}
                  className={`cv-btn cv-btn--sm ${currentPreset === id ? 'cv-btn--primary' : 'cv-btn--secondary'}`}
                  title={desc}
                  onClick={() => setPendingPreset(id)}
                  type="button"
                  disabled={busy}
                  aria-pressed={currentPreset === id}
                >
                  {label}
                </button>
              ))}
            </div>

            {/* Animated preset-save bar — slides in when a preset is pending */}
            <AnimatePresence initial={false}>
              {hasPendingPreset && (
                <motion.div
                  className="seg-pol-preset-save-row"
                  aria-live="polite"
                  initial={{ opacity: 0, y: -6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -6 }}
                  transition={TWEEN}
                >
                  <span className="seg-pol-preset-hint">
                    Vista previa del preset «{pendingPreset}» — las capacidades de abajo ya reflejan el cambio. Guarda para aplicarlo.
                  </span>
                  <button
                    type="button"
                    className="cv-btn cv-btn--primary cv-btn--sm"
                    onClick={requestPresetSave}
                    disabled={busy}
                  >
                    Guardar
                  </button>
                  <button
                    type="button"
                    className="cv-btn cv-btn--ghost cv-btn--sm"
                    onClick={() => setPendingPreset(null)}
                    disabled={busy}
                  >
                    Cancelar
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* Capability accordion groups — staggered entrance */}
          {hasCatalog && capabilityGroups.size > 0 && (
            <div>
              <div className="seg-pol-sub-label">Capacidades del agente</div>
              <Stagger className="seg-pol-catalog">
                {[...capabilityGroups.entries()].map(([cat, entries]) => (
                  <StaggerItem key={cat}>
                    <CategoryGroup
                      category={cat}
                      entries={entries}
                      pendingChanges={toolPending}
                      busy={busy}
                      onToggleTool={requestToolToggle}
                      onToggleAll={requestSectionToggle}
                    />
                  </StaggerItem>
                ))}
              </Stagger>

              {/* Animated "Guardar cambios" bar — slides in when tools are pending */}
              <AnimatePresence initial={false}>
                {hasPendingTools && (
                  <motion.div
                    className="seg-pol-preset-save-row"
                    aria-live="polite"
                    initial={{ opacity: 0, y: -6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -6 }}
                    transition={TWEEN}
                  >
                    <span className="seg-pol-preset-hint">
                      {Object.keys(toolPending).length} cambio{Object.keys(toolPending).length !== 1 ? 's' : ''} pendiente{Object.keys(toolPending).length !== 1 ? 's' : ''}.
                    </span>
                    <button
                      type="button"
                      className="cv-btn cv-btn--primary cv-btn--sm"
                      onClick={handleSaveToolChanges}
                      disabled={busy}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--sp-1)' }}
                    >
                      <Save size={14} aria-hidden="true" />
                      Guardar cambios
                    </button>
                    <button
                      type="button"
                      className="cv-btn cv-btn--ghost cv-btn--sm"
                      onClick={() => setToolPending({})}
                      disabled={busy}
                    >
                      Descartar
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* Defense tools accordion groups */}
          {hasCatalog && defenseGroups.size > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="seg-pol-sub-label">Defensas del sistema</div>
              <p className="seg-card__intro" style={{ marginTop: 0, marginBottom: 8 }}>
                Estas herramientas protegen el sistema. No son capacidades del agente — no las invoca directamente.
              </p>
              <Stagger className="seg-pol-catalog">
                {[...defenseGroups.entries()].map(([cat, entries]) => (
                  <StaggerItem key={cat}>
                    <CategoryGroup
                      category={cat}
                      entries={entries}
                      pendingChanges={toolPending}
                      busy={busy}
                      onToggleTool={requestToolToggle}
                      onToggleAll={requestSectionToggle}
                    />
                  </StaggerItem>
                ))}
              </Stagger>
            </div>
          )}

          {/* Legacy flat list — shown only when catalog is absent */}
          {!hasCatalog && legacyToolNames.length > 0 && (
            <>
              <details className="seg-details" style={{ marginTop: 12 }}>
                <summary>Comandos uno a uno ({legacyToolNames.length})</summary>
                <div className="seg-tool-list">
                  {legacyToolNames.map(name => {
                    const effective = name in toolPending ? toolPending[name] : (pol.tools?.[name] ?? false)
                    return (
                      <label key={name} className="seg-tool-row">
                        <input
                          type="checkbox"
                          checked={effective}
                          onChange={e => requestLegacyToolToggle(name, e.target.checked)}
                          aria-label={`Permiso para ${name}`}
                        />
                        <span>{name}</span>
                      </label>
                    )
                  })}
                </div>
              </details>

              {/* Animated "Guardar cambios" bar for legacy flat list */}
              <AnimatePresence initial={false}>
                {hasPendingTools && (
                  <motion.div
                    className="seg-pol-preset-save-row"
                    aria-live="polite"
                    initial={{ opacity: 0, y: -6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -6 }}
                    transition={TWEEN}
                  >
                    <span className="seg-pol-preset-hint">
                      {Object.keys(toolPending).length} cambio{Object.keys(toolPending).length !== 1 ? 's' : ''} pendiente{Object.keys(toolPending).length !== 1 ? 's' : ''}.
                    </span>
                    <button
                      type="button"
                      className="cv-btn cv-btn--primary cv-btn--sm"
                      onClick={handleSaveToolChanges}
                      disabled={busy}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--sp-1)' }}
                    >
                      <Save size={14} aria-hidden="true" />
                      Guardar cambios
                    </button>
                    <button
                      type="button"
                      className="cv-btn cv-btn--ghost cv-btn--sm"
                      onClick={() => setToolPending({})}
                      disabled={busy}
                    >
                      Descartar
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </>
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

  const loadDomains = useCallback(async () => {
    const res = await listEgressDomains()
    setDomains(res.domains ?? [])
    setLoading(false)
  }, [])

  useEffect(() => { loadDomains() }, [loadDomains])

  async function handleGrant() {
    const d = input.trim().toLowerCase()
    if (!d) return
    try {
      await grantEgressDomain(d)
      sileo.success({ title: `${d} autorizado` })
      setInput('')
      await loadDomains()
    } catch (err) {
      sileo.error({ title: `No se pudo autorizar: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleRevoke(d: string) {
    try {
      await revokeEgressDomain(d)
      sileo.success({ title: `${d} revocado` })
      await loadDomains()
    } catch (err) {
      sileo.error({ title: `No se pudo revocar: ${err instanceof Error ? err.message : err}` })
    }
  }

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
        {loading ? (
          <div className="cv-skeleton" aria-busy="true" />
        ) : domains.length === 0 ? (
          <p className="cv-empty">Ningún dominio autorizado — el agente no accede a la red.</p>
        ) : (
          <ul className="cv-list" aria-label="Dominios autorizados">
            <AnimatePresence initial={false}>
              {domains.map(d => (
                <AnimatedListItem key={d} className="seg-egress-row">
                  <code className="seg-egress-row__domain">{d}</code>
                  <button
                    className="cv-btn cv-btn--ghost cv-btn--sm"
                    onClick={() => handleRevoke(d)}
                    type="button"
                    aria-label={`Revocar dominio ${d}`}
                  >
                    Revocar
                  </button>
                </AnimatedListItem>
              ))}
            </AnimatePresence>
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

function ScanRow({ scan }: { scan: SecurityScan }) {
  const [showModal, setShowModal] = useState(false)
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

  async function handleAllow(factors: MfaFactors) {
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
        totp: factors.totp.trim(),
      })
      sileo.success({ title: 'Instalación permitida (decisión soberana, auditada). Reinténtala.' })
      setAllowed(true)
      setShowModal(false)
    } catch (err) {
      sileo.error({ title: `No se pudo permitir: ${err instanceof Error ? err.message : err}` })
    } finally {
      setBusy(false)
    }
  }

  return (
    <HoverRow className="seg-scan-row">
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
            onClick={() => setShowModal(true)}
            type="button"
            disabled={busy}
          >
            Permitir igualmente
          </button>
        )}
      </div>

      {showModal && (
        <MfaModal
          title="Permitir instalación"
          onSign={handleAllow}
          onCancel={() => setShowModal(false)}
        />
      )}
    </HoverRow>
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
            <FadeIn>
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
            </FadeIn>
          )}
        </div>
      </section>

      <section className="cv-section">
        <div className="cv-section-label">Escaneos recientes</div>
        {!scans || scans.length === 0 ? (
          <p className="cv-empty">Sin escaneos recientes.</p>
        ) : (
          <div className="cv-list">
            <AnimatePresence initial={false}>
              {scans.map((s, i) => (
                <AnimatedListItem key={s.scan_id ?? s.id ?? i}>
                  <ScanRow scan={s} />
                </AnimatedListItem>
              ))}
            </AnimatePresence>
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
  const [mfaDisabled, setMfaDisabled] = useState(false)

  useEffect(() => {
    getPolicies().then(p => setMfaDisabled(p.mfa_on_dangers === false)).catch(() => {})
  }, [])

  return (
    <div className="cv-view-body">
      <div className="view-header" style={{ padding: 0, border: 'none' }}>
        <h1 className="view-title">Seguridad y gobernanza</h1>
        <p className="view-subtitle">
          Aprobaciones, políticas del agente, escaneos y cadena de auditoría.
        </p>
      </div>

      <ApprovalsSection mfaDisabled={mfaDisabled} />
      <GovernanceSection />
      <EgressSection />
      <SecurityCenterSection />
    </div>
  )
}
