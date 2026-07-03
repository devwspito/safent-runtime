/**
 * SeguridadView — Security, governance, and HITL approvals.
 *
 * Three sub-areas:
 *   (a) Pending HITL approvals — polled every 3 s, Approve/Deny via MfaModal.
 *   (b) Governance — MFA enrollment + security policy presets + accordion catalog.
 *   (c) Security center — egress permissions, recent scans.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { sileo } from 'sileo'
import { Save, CheckCircle, ShieldCheck, Globe } from 'lucide-react'
import { useT } from '../lib/i18n'
import { isApprovalFresh } from '../hooks/usePendingApprovals'
import {
  listPendingApprovals,
  mfaStatus,
  getPolicies,
  setPolicyPreset,
  setPolicyTools,
  setMfaOnDangers,
  getSecurityScans,
  grantEgressDomain,
  revokeEgressDomain,
  getEgressMode,
  setEgressMode,
  blockEgressDomain,
  unblockEgressDomain,
  recordInstallDecision,
} from '../api/client'
import type { EgressMode, EgressModeResponse } from '../api/types'
import type {
  PendingApproval,
  MfaStatus,
  PoliciesResponse,
  PolicyCatalogEntry,
  SecurityScan,
} from '../api/types'
import ApprovalCard from '../components/ApprovalCard'
import MfaEnroll from '../components/MfaEnroll'
import MfaModal from '../components/MfaModal'
import type { MfaFactors } from '../components/MfaModal'
import { Button } from '../components/ui/Button'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  AnimatedChevron,
  Stagger,
  StaggerItem,
  HoverRow,
  motion,
  SPRING,
  TWEEN,
} from '../components/ui/motion'
import s from './SeguridadView.module.css'

/**
 * Translate a key not yet registered in the central i18n dictionary.
 * Falls back to the given literal until the key is added centrally
 * (see report). `useT()`'s key type is a closed union derived from the
 * dictionary, so new keys need this narrow, intentional cast.
 */
type Translate = ReturnType<typeof useT>
function tNew(t: Translate, key: string, fallback: string): string {
  return t(key as Parameters<Translate>[0], fallback)
}

// ── Approvals section ─────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3000

function ApprovalsSection({ mfaDisabled }: { mfaDisabled: boolean }) {
  const t = useT()
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    const data = await listPendingApprovals()
    const fresh = Array.isArray(data) ? data.filter(a => isApprovalFresh(a.created_at)) : []
    setApprovals(fresh)
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [load])

  return (
    <section className="cv-section">
      <div className={s.sectionLabel}>
        <span>{t('seg.approvals.label')}</span>
        {!loading && approvals.length > 0 && (
          <span className={s.sectionLabelCount}>{approvals.length}</span>
        )}
      </div>
      {loading ? (
        <ApprovalsSkeletonBlock />
      ) : approvals.length === 0 ? (
        <div className={s.approvalsEmptyRow} role="status">
          <CheckCircle size={15} aria-hidden="true" />
          {t('seg.approvals.empty')}
        </div>
      ) : (
        <ul className="cv-list" aria-label="Aprobaciones pendientes">
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
        </ul>
      )}
    </section>
  )
}

function ApprovalsSkeletonBlock() {
  return (
    <div aria-busy="true" aria-label="Cargando aprobaciones…" className="cv-list">
      {[0, 1].map(i => (
        <div
          key={i}
          className="skeleton skeleton--card"
          style={{ animationDelay: `${i * 80}ms`, borderRadius: 'var(--radius-md)' }}
        />
      ))}
    </div>
  )
}

// ── Governance section ────────────────────────────────────────────────────────

const PRESET_IDS: Array<[string, string]> = [
  ['equilibrado', 'Equilibrado'],
  ['permisivo',   'Permisivo'],
  ['bloqueado',   'Bloqueado'],
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
  const t = useT()
  if (level === 'normal') return null
  const label = level === 'most_delicate' ? t('seg.badge.approval') : t('seg.badge.attention')
  const variantClass = level === 'most_delicate' ? s['delicacyBadge--danger'] : s['delicacyBadge--warn']
  const sizeClass = size === 'sm' ? s['delicacyBadge--sm'] : ''
  return (
    <span
      className={`${s.delicacyBadge} ${variantClass} ${sizeClass}`}
      aria-label={label}
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
  const onClass = checked && !indeterminate ? s['toggleSwitch--on'] : ''
  const mixedClass = indeterminate ? s['toggleSwitch--mixed'] : ''
  return (
    <button
      id={id}
      role="switch"
      type="button"
      aria-checked={indeterminate ? 'mixed' : checked}
      aria-label={ariaLabel}
      disabled={disabled}
      className={`${s.toggleSwitch} ${onClass} ${mixedClass}`}
      onClick={() => onChange(!checked)}
    />
  )
}

// ── Accordion category group ──────────────────────────────────────────────────

interface CategoryGroupProps {
  category: string
  entries: PolicyCatalogEntry[]
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
    <div className={s.groupCard}>
      <HoverRow
        className={s.groupHeader}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        aria-controls={bodyId}
        onClick={() => setExpanded(v => !v)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(v => !v) } }}
      >
        <AnimatedChevron open={expanded} size={12} />

        <span className={s.groupName}>{categoryLabel(category)}</span>
        <span className={s.groupCount} aria-label={`${entries.length} herramientas`}>
          {entries.length}
        </span>

        <DelicacyBadge level={delicacy} />

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

      <AnimatedExpanderContent open={expanded}>
        <ul
          id={bodyId}
          className={s.toolList}
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
  const t = useT()
  const checkId = `tool-${entry.name}`
  const tipId = `tool-tip-${entry.name}`
  const notVisible = !entry.llm_visible

  return (
    <motion.li
      className={`${s.toolRow} ${notVisible ? s['toolRow--muted'] : ''}`}
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
        className={s.toolCheck}
        aria-label={`${entry.label}: ${entry.enabled ? 'activo' : 'inactivo'}`}
      />
      <label htmlFor={checkId} className={s.toolLabel}>
        {entry.label}
      </label>
      <DelicacyBadge level={entry.delicacy} size="sm" />
      {notVisible && (
        <span
          id={tipId}
          className={s.toolNativeChip}
          title={t('seg.tool.native.tip')}
          aria-label={t('seg.tool.native.label')}
        >
          {t('seg.tool.native.label')}
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

// ── Pending changes banner (shared by catalog + legacy tool lists) ──────────

interface PendingChangesBannerProps {
  count: number
  busy: boolean
  onSave: () => void
  onDiscard: () => void
}

function PendingChangesBanner({ count, busy, onSave, onDiscard }: PendingChangesBannerProps) {
  const t = useT()
  return (
    <AnimatePresence initial={false}>
      {count > 0 && (
        <motion.div
          className={s.changesBanner}
          aria-live="polite"
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={TWEEN}
        >
          <span className={s.changesBannerText}>
            {count} cambio{count !== 1 ? 's' : ''} pendiente{count !== 1 ? 's' : ''}.
          </span>
          <Button type="button" variant="primary" size="sm" onClick={onSave} disabled={busy}>
            <Save size={13} aria-hidden="true" />
            {tNew(t, 'seg.changes.save', 'Guardar cambios')}
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={onDiscard} disabled={busy}>
            {tNew(t, 'seg.changes.discard', 'Descartar')}
          </Button>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── Governance section ────────────────────────────────────────────────────────

function GovernanceSection() {
  const t = useT()
  const [mfa, setMfa] = useState<MfaStatus | null>(null)
  const [pol, setPol] = useState<PoliciesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const [pendingPreset, setPendingPreset] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [toolPending, setToolPending] = useState<Record<string, boolean>>({})
  const hasPendingTools = Object.keys(toolPending).length > 0

  const mfaDisabled = pol?.mfa_on_dangers === false

  const load = useCallback(async () => {
    const [m, p] = await Promise.all([mfaStatus(), getPolicies()])
    setMfa(m)
    setPol(p)
    setLoading(false)
    setToolPending({})
  }, [])

  useEffect(() => { load() }, [load])

  const { capabilityGroups, defenseGroups } = useMemo(() => {
    const rawCatalog = pol?.catalog ?? []
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

  async function persistBatchDirect(changes: Record<string, boolean>) {
    setBusy(true)
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
      sileo.success({ title: t('seg.save.ok') })
      await load()
    } catch (err) {
      await load()
      sileo.error({ title: t('seg.save.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
    } finally {
      setBusy(false)
    }
  }

  async function handleSign(factors: MfaFactors) {
    if (!pendingAction) return
    setBusy(true)
    try {
      if (pendingAction.kind === 'preset') {
        await setPolicyPreset(pendingAction.preset, factors.totp)
        sileo.success({ title: t('seg.preset.ok').replace('{preset}', pendingAction.preset) })
        setPendingPreset(null)
        setPendingAction(null)
        await load()

      } else if (pendingAction.kind === 'batch') {
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
          sileo.success({ title: t('seg.save.ok') })
          setPendingAction(null)
          await load()
        } catch (err) {
          await load()
          sileo.error({ title: t('seg.save.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
          return
        }

      } else if (pendingAction.kind === 'mfa_dangers') {
        await setMfaOnDangers(pendingAction.enabled, factors.totp)
        sileo.success({
          title: pendingAction.enabled
            ? t('seg.dangers.on.ok')
            : t('seg.dangers.off.ok'),
        })
        setPol(prev => prev ? { ...prev, mfa_on_dangers: pendingAction.enabled } : prev)
        setPendingAction(null)
      }
    } catch (err) {
      sileo.error({ title: t('seg.preset.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
      return
    } finally {
      setBusy(false)
    }
  }

  function requestPresetSave() {
    if (!pendingPreset) return
    if (mfaDisabled) {
      setBusy(true)
      void setPolicyPreset(pendingPreset, '')
        .then(() => {
          sileo.success({ title: t('seg.preset.ok').replace('{preset}', pendingPreset) })
          setPendingPreset(null)
          return load()
        })
        .catch(err => {
          sileo.error({ title: t('seg.preset.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
        })
        .finally(() => setBusy(false))
    } else {
      setPendingAction({ kind: 'preset', preset: pendingPreset })
    }
  }

  function requestToolToggle(tool: string, enabled: boolean) {
    setToolPending(prev => ({ ...prev, [tool]: enabled }))
  }

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

  function handleSaveToolChanges() {
    if (!hasPendingTools) return
    if (mfaDisabled) {
      void persistBatchDirect({ ...toolPending })
    } else {
      setPendingAction({ kind: 'batch', changes: { ...toolPending } })
    }
  }

  function requestMfaDangersToggle(checked: boolean) {
    if (mfaDisabled) {
      setBusy(true)
      void setMfaOnDangers(checked, '')
        .then(() => {
          sileo.success({ title: checked ? t('seg.dangers.on.ok') : t('seg.dangers.off.ok') })
          setPol(prev => prev ? { ...prev, mfa_on_dangers: checked } : prev)
        })
        .catch(err => {
          sileo.error({ title: t('seg.preset.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
        })
        .finally(() => setBusy(false))
    } else {
      setPendingAction({ kind: 'mfa_dangers', enabled: checked })
    }
  }

  function requestLegacyToolToggle(toolName: string, enabled: boolean) {
    setToolPending(prev => ({ ...prev, [toolName]: enabled }))
  }

  if (loading) {
    return <GovernanceSkeletonBlock />
  }
  if (!mfa || !pol) return null

  const hasCatalog = (pol.catalog?.length ?? 0) > 0
  const currentPreset = pendingPreset ?? pol.preset
  const hasPendingPreset = pendingPreset !== null && pendingPreset !== pol.preset

  const batchChanges = pendingAction?.kind === 'batch' ? pendingAction.changes : toolPending

  return (
    <>
      {pendingAction && (
        <MfaModal
          title={
            pendingAction.kind === 'preset'
              ? t('seg.mfa_modal.preset').replace('{preset}', pendingAction.preset)
              : pendingAction.kind === 'mfa_dangers'
              ? pendingAction.enabled
                ? t('seg.policies.dangers.label')
                : t('seg.mfa_modal.dangers_off')
              : t('seg.mfa_modal.tools')
          }
          onSign={handleSign}
          onCancel={() => {
            setPendingAction(null)
            if (pendingAction?.kind === 'batch') {
              setToolPending(batchChanges)
            }
          }}
        />
      )}

      {/* ── Two-step verification ── */}
      <section className="cv-section">
        <div className={s.sectionLabel}>{t('seg.mfa.label')}</div>
        <div className={s.sectionCard}>
          <p className={s['sectionCard__intro']}>
            {mfa.enrolled
              ? t('seg.mfa.enrolled')
              : t('seg.mfa.not_enrolled')}
          </p>
          {!mfa.enrolled && <MfaEnroll onEnrolled={load} />}
        </div>
      </section>

      {/* ── Permissions ── */}
      <section className="cv-section">
        <div className={s.sectionLabel}>{t('seg.policies.label')}</div>
        <div className={s.sectionCard}>
          <p className={s['sectionCard__intro']}>
            {t('seg.policies.intro')}
          </p>

          {/* Verification on sensitive actions */}
          <div className={s.settingsRow}>
            <div className={s.settingsRowInfo}>
              <span className={s.settingsRowLabel}>
                {t('seg.policies.dangers.label')}
              </span>
              <span className={s.settingsRowHint}>
                {mfaDisabled
                  ? t('seg.policies.dangers.off')
                  : t('seg.policies.dangers.on')}
              </span>
            </div>
            <ToggleSwitch
              id="toggle-mfa-dangers"
              aria-label={t('seg.policies.dangers.label')}
              checked={pol.mfa_on_dangers ?? true}
              disabled={busy}
              onChange={requestMfaDangersToggle}
            />
          </div>

          {/* Preset quick-access */}
          <div>
            <div className={s.subLabel}>Preset rápido</div>
            <div className={s.presetsStrip}>
              {PRESET_IDS.map(([id, label]) => {
                const desc = t(
                  id === 'equilibrado' ? 'seg.preset.equilibrado.desc'
                  : id === 'permisivo' ? 'seg.preset.permisivo.desc'
                  : 'seg.preset.bloqueado.desc'
                )
                return (
                  <Button
                    key={id}
                    variant={currentPreset === id ? 'primary' : 'secondary'}
                    size="sm"
                    title={desc}
                    onClick={() => setPendingPreset(id)}
                    type="button"
                    disabled={busy}
                    aria-pressed={currentPreset === id}
                  >
                    {label}
                  </Button>
                )
              })}
            </div>

            {/* Animated preset-save banner */}
            <AnimatePresence initial={false}>
              {hasPendingPreset && (
                <motion.div
                  className={s.presetSaveBanner}
                  aria-live="polite"
                  initial={{ opacity: 0, y: -6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -6 }}
                  transition={TWEEN}
                >
                  <span className={s.presetSaveBannerText}>
                    {t('seg.policies.preset.hint').replace('{preset}', pendingPreset ?? '')}
                  </span>
                  <Button
                    type="button"
                    variant="primary"
                    size="sm"
                    onClick={requestPresetSave}
                    disabled={busy}
                  >
                    {tNew(t, 'seg.preset.save', 'Guardar')}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setPendingPreset(null)}
                    disabled={busy}
                  >
                    {tNew(t, 'seg.preset.cancel', 'Cancelar')}
                  </Button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* Capability accordion groups */}
          {hasCatalog && capabilityGroups.size > 0 && (
            <div>
              <div className={s.subLabel}>Capacidades del agente</div>
              <Stagger className={s.catalog}>
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

              <PendingChangesBanner
                count={Object.keys(toolPending).length}
                busy={busy}
                onSave={handleSaveToolChanges}
                onDiscard={() => setToolPending({})}
              />
            </div>
          )}

          {/* Defense tools accordion groups */}
          {hasCatalog && defenseGroups.size > 0 && (
            <div className={s.defenseSectionWrap}>
              <div className={s.subLabel}>Defensas del sistema</div>
              <p className={s.defenseNote}>
                Estas herramientas protegen el sistema. No son capacidades del agente — no las invoca directamente.
              </p>
              <Stagger className={s.catalog}>
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

          {/* Legacy flat list */}
          {!hasCatalog && legacyToolNames.length > 0 && (
            <>
              <details className={s.legacyDetails}>
                <summary>Comandos uno a uno ({legacyToolNames.length})</summary>
                <div className={s.legacyToolList}>
                  {legacyToolNames.map(name => {
                    const effective = name in toolPending ? toolPending[name] : (pol.tools?.[name] ?? false)
                    return (
                      <label key={name} className={s.legacyToolRow}>
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

              <PendingChangesBanner
                count={Object.keys(toolPending).length}
                busy={busy}
                onSave={handleSaveToolChanges}
                onDiscard={() => setToolPending({})}
              />
            </>
          )}
        </div>
      </section>
    </>
  )
}

function GovernanceSkeletonBlock() {
  return (
    <div aria-busy="true" aria-label="Cargando gobernanza…" className="cv-section" style={{ gap: 'var(--space-6)' }}>
      {/* MFA section skeleton */}
      <div className="cv-section">
        <div className="skeleton skeleton--line-sm" style={{ width: '80px', marginBottom: 'var(--space-3)' }} />
        <div className="skeleton skeleton--card" />
      </div>
      {/* Policies section skeleton */}
      <div className="cv-section">
        <div className="skeleton skeleton--line-sm" style={{ width: '100px', marginBottom: 'var(--space-3)' }} />
        <div className="skeleton skeleton--card" />
        <div className="skeleton skeleton--block" style={{ animationDelay: '60ms' }} />
        <div className="skeleton skeleton--block" style={{ animationDelay: '120ms' }} />
        <div className="skeleton skeleton--block" style={{ animationDelay: '180ms' }} />
      </div>
    </div>
  )
}

// ── Egress section ────────────────────────────────────────────────────────────

interface EgressModeToggleProps {
  mode: EgressMode
  busy: boolean
  onRequest: (next: EgressMode) => void
}

function EgressModeToggle({ mode, busy, onRequest }: EgressModeToggleProps) {
  const t = useT()
  return (
    <div className={s.egressModeToggle} role="group" aria-label={t('seg.network.mode.label')}>
      <button
        type="button"
        className={`${s.egressModeBtn} ${mode === 'allow' ? s['egressModeBtn--active'] : ''}`}
        aria-pressed={mode === 'allow'}
        disabled={busy || mode === 'allow'}
        onClick={() => onRequest('allow')}
      >
        {t('seg.network.allow')}
      </button>
      <button
        type="button"
        className={`${s.egressModeBtn} ${mode === 'deny' ? s['egressModeBtn--denyActive'] : ''}`}
        aria-pressed={mode === 'deny'}
        disabled={busy || mode === 'deny'}
        onClick={() => onRequest('deny')}
      >
        {t('seg.network.deny')}
      </button>
    </div>
  )
}

interface AllowModeProps {
  denyList: string[]
  blocklistCount: number | undefined
  onAdd: (domain: string) => Promise<void>
  onRemove: (domain: string) => Promise<void>
}

function AllowModePanel({ denyList, blocklistCount, onAdd, onRemove }: AllowModeProps) {
  const t = useT()
  const [input, setInput] = useState('')

  async function handleAdd() {
    const d = input.trim().toLowerCase()
    if (!d) return
    await onAdd(d)
    setInput('')
  }

  return (
    <motion.div
      key="allow-panel"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={TWEEN}
    >
      <p className={s['sectionCard__intro']}>
        {t('seg.network.allow.intro')}
      </p>

      {blocklistCount != null && blocklistCount > 0 && (
        <div className={s.blocklistBadge} aria-label={`${blocklistCount} sitios maliciosos bloqueados por el sistema`} style={{ marginTop: 'var(--space-2)' }}>
          <span className={s.blocklistBadgeDot} aria-hidden="true" />
          <span className="num">{blocklistCount}</span> sitios maliciosos bloqueados por el sistema
        </div>
      )}

      <div className={s.subLabel} style={{ marginTop: 'var(--space-4)' }}>
        Sitios bloqueados manualmente
      </div>

      <div className={s.domainInputRow} style={{ marginBottom: 'var(--space-2)' }}>
        <input
          id="egress-block-input"
          className="cv-input"
          type="text"
          placeholder="dominio (ej. ejemplo.com)"
          autoComplete="off"
          spellCheck={false}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { void handleAdd() } }}
          aria-label="Dominio a bloquear"
        />
        <Button
          variant="secondary"
          onClick={() => { void handleAdd() }}
          type="button"
          disabled={!input.trim()}
        >
          {tNew(t, 'seg.network.block', 'Bloquear')}
        </Button>
      </div>

      {denyList.length === 0 ? (
        <EmptyState
          compact
          icon={<Globe size={18} />}
          title={t('seg.network.none_blocked')}
        />
      ) : (
        <ul className="cv-list" aria-label="Dominios bloqueados manualmente">
          <AnimatePresence initial={false}>
            {denyList.map(d => (
              <AnimatedListItem key={d} className={s.egressDomainRow}>
                <code className={s.egressDomainCode}>{d}</code>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => { void onRemove(d) }}
                  type="button"
                  aria-label={tNew(t, 'seg.network.unblock_aria', 'Desbloquear dominio {domain}').replace('{domain}', d)}
                >
                  {tNew(t, 'seg.network.unblock', 'Desbloquear')}
                </Button>
              </AnimatedListItem>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </motion.div>
  )
}

interface DenyModeProps {
  allowList: string[]
  onGrant: (domain: string) => Promise<void>
  onRevoke: (domain: string) => Promise<void>
}

function DenyModePanel({ allowList, onGrant, onRevoke }: DenyModeProps) {
  const t = useT()
  const [input, setInput] = useState('')

  async function handleGrant() {
    const d = input.trim().toLowerCase()
    if (!d) return
    await onGrant(d)
    setInput('')
  }

  return (
    <motion.div
      key="deny-panel"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={TWEEN}
    >
      <p className={s['sectionCard__intro']}>
        {t('seg.network.deny.intro')}
      </p>

      <div className={s.domainInputRow} style={{ marginBottom: 'var(--space-2)' }}>
        <input
          id="egress-grant-input"
          className="cv-input"
          type="text"
          placeholder="dominio (ej. tu-erp.empresa.com)"
          autoComplete="off"
          spellCheck={false}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { void handleGrant() } }}
          aria-label="Dominio a autorizar"
        />
        <Button
          variant="primary"
          onClick={() => { void handleGrant() }}
          type="button"
          disabled={!input.trim()}
        >
          {tNew(t, 'seg.network.authorize', 'Autorizar')}
        </Button>
      </div>

      {allowList.length === 0 ? (
        <EmptyState
          compact
          icon={<Globe size={18} />}
          title={t('seg.network.none_allowed')}
        />
      ) : (
        <ul className="cv-list" aria-label="Dominios autorizados">
          <AnimatePresence initial={false}>
            {allowList.map(d => (
              <AnimatedListItem key={d} className={s.egressDomainRow}>
                <code className={s.egressDomainCode}>{d}</code>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => { void onRevoke(d) }}
                  type="button"
                  aria-label={`${t('seg.network.revoke')} ${d}`}
                >
                  {t('seg.network.revoke')}
                </Button>
              </AnimatedListItem>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </motion.div>
  )
}

function EgressSection() {
  const t = useT()
  const [state, setState] = useState<EgressModeResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [pendingMode, setPendingMode] = useState<EgressMode | null>(null)

  const load = useCallback(async () => {
    const res = await getEgressMode()
    setState(res)
    setLoading(false)
  }, [])

  useEffect(() => { void load() }, [load])

  function requestModeChange(next: EgressMode) {
    setPendingMode(next)
  }

  async function handleModeSign(factors: MfaFactors) {
    if (!pendingMode || !state) return
    const prev = state
    setState(s => s ? { ...s, mode: pendingMode } : s)
    setPendingMode(null)
    setBusy(true)
    try {
      await setEgressMode(pendingMode, factors.totp)
      sileo.success({
        title: pendingMode === 'allow'
          ? t('seg.allow_mode.ok')
          : t('seg.deny_mode.ok'),
      })
      await load()
    } catch (err) {
      setState(prev)
      sileo.error({ title: t('seg.save.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
    } finally {
      setBusy(false)
    }
  }

  async function handleGrant(domain: string) {
    if (!state) return
    const prev = state
    setState(s => s ? { ...s, domains: [...s.domains, domain] } : s)
    try {
      await grantEgressDomain(domain)
      sileo.success({ title: `${domain} autorizado` })
      await load()
    } catch (err) {
      setState(prev)
      sileo.error({ title: `No se pudo autorizar: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleRevoke(domain: string) {
    if (!state) return
    const prev = state
    setState(s => s ? { ...s, domains: s.domains.filter(d => d !== domain) } : s)
    try {
      await revokeEgressDomain(domain)
      sileo.success({ title: `${domain} revocado` })
      await load()
    } catch (err) {
      setState(prev)
      sileo.error({ title: `No se pudo revocar: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleBlock(domain: string) {
    if (!state) return
    const prev = state
    setState(s => s ? { ...s, deny: [...s.deny, domain] } : s)
    try {
      await blockEgressDomain(domain)
      sileo.success({ title: `${domain} bloqueado` })
      await load()
    } catch (err) {
      setState(prev)
      sileo.error({ title: `No se pudo bloquear: ${err instanceof Error ? err.message : err}` })
    }
  }

  async function handleUnblock(domain: string) {
    if (!state) return
    const prev = state
    setState(s => s ? { ...s, deny: s.deny.filter(d => d !== domain) } : s)
    try {
      await unblockEgressDomain(domain)
      sileo.success({ title: `${domain} desbloqueado` })
      await load()
    } catch (err) {
      setState(prev)
      sileo.error({ title: `No se pudo desbloquear: ${err instanceof Error ? err.message : err}` })
    }
  }

  return (
    <section className="cv-section">
      <div className={s.sectionLabel}>{t('seg.network.label')}</div>

      {pendingMode && (
        <MfaModal
          title={pendingMode === 'allow' ? t('seg.allow_mode.ok') : t('seg.deny_mode.ok')}
          onSign={handleModeSign}
          onCancel={() => setPendingMode(null)}
        />
      )}

      <div className={s.sectionCard}>
        {loading ? (
          <div aria-busy="true" aria-label="Cargando…" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            <div className="skeleton skeleton--block" />
            <div className="skeleton skeleton--line" style={{ width: '60%', animationDelay: '60ms' }} />
          </div>
        ) : state != null ? (
          <>
            <div className={s.settingsRow}>
              <div className={s.settingsRowInfo}>
                <span className={s.settingsRowLabel}>{t('seg.network.mode.label')}</span>
                <span className={s.settingsRowHint}>
                  {t('seg.network.mode.hint')}
                </span>
              </div>
              <EgressModeToggle
                mode={state.mode}
                busy={busy}
                onRequest={requestModeChange}
              />
            </div>

            <AnimatePresence mode="wait" initial={false}>
              {state.mode === 'allow' ? (
                <AllowModePanel
                  key="allow"
                  denyList={state.deny ?? []}
                  blocklistCount={state.blocklist_count}
                  onAdd={handleBlock}
                  onRemove={handleUnblock}
                />
              ) : (
                <DenyModePanel
                  key="deny"
                  allowList={state.domains ?? []}
                  onGrant={handleGrant}
                  onRevoke={handleRevoke}
                />
              )}
            </AnimatePresence>
          </>
        ) : null}
      </div>
    </section>
  )
}

// ── Severity badge (token-driven) ─────────────────────────────────────────────

function SeverityBadge({ severity }: { severity: string }) {
  const sev = severity.toLowerCase()
  const classMap: Record<string, string> = {
    critical: s['severityBadge--critical'],
    high:     s['severityBadge--high'],
    medium:   s['severityBadge--medium'],
    low:      s['severityBadge--low'],
    info:     s['severityBadge--info'],
  }
  const labelMap: Record<string, string> = {
    critical: 'CRÍTICO',
    high:     'ALTO',
    medium:   'MEDIO',
    low:      'BAJO',
    info:     'INFO',
  }
  const variantClass = classMap[sev] ?? s['severityBadge--info']
  const label = labelMap[sev] ?? severity.toUpperCase()

  return (
    <span className={`${s.severityBadge} ${variantClass}`}>
      {label}
    </span>
  )
}

// ── Scan row ──────────────────────────────────────────────────────────────────

function ScanRow({ scan }: { scan: SecurityScan }) {
  const t = useT()
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
    <HoverRow className={s.scanRow}>
      <div className={s.scanRowLeft}>
        <div className={s.scanRowName}>{name}</div>
        {target && <div className={s.scanRowTarget}>{target}</div>}
      </div>
      <div className={s.scanRowRight}>
        {scan.severity && <SeverityBadge severity={scan.severity} />}
        {scan.score != null && (
          <span className={s.scoreChip}>{scan.score}</span>
        )}
        {allowed && (
          <span className={`${s.severityBadge} ${s['severityBadge--allowed']}`}>
            {t('seg.scan.allowed')}
          </span>
        )}
        {flagged && !allowed && scanId && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowModal(true)}
            type="button"
            disabled={busy}
          >
            {t('seg.scan.allow')}
          </Button>
        )}
      </div>

      {showModal && (
        <MfaModal
          title={tNew(t, 'seg.scan.allow_modal_title', 'Permitir instalación')}
          onSign={handleAllow}
          onCancel={() => setShowModal(false)}
        />
      )}
    </HoverRow>
  )
}

// ── Security center section ───────────────────────────────────────────────────

function SecurityCenterSection() {
  const t = useT()
  const [scans, setScans] = useState<SecurityScan[] | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getSecurityScans()
      .then(s => { setScans(Array.isArray(s) ? s : []) })
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <section className="cv-section">
        <div className={s.sectionLabel} aria-hidden="true">
          <span style={{ visibility: 'hidden' }}>placeholder</span>
        </div>
        <div aria-busy="true" aria-label="Cargando escaneos…" className="cv-list">
          {[0, 1, 2].map(i => (
            <div
              key={i}
              className="skeleton skeleton--block"
              style={{ animationDelay: `${i * 60}ms`, borderRadius: 'var(--radius-md)' }}
            />
          ))}
        </div>
      </section>
    )
  }

  return (
    <section className="cv-section">
      <div className={s.sectionLabel}>
        <span>{t('seg.scans.label')}</span>
        {scans && scans.length > 0 && (
          <span className={s.sectionLabelCount}>{scans.length}</span>
        )}
      </div>
      {!scans || scans.length === 0 ? (
        <EmptyState
          icon={<ShieldCheck size={28} />}
          title={t('seg.scans.empty')}
          description={tNew(
            t,
            'seg.scans.empty_desc',
            'No se han registrado escaneos de seguridad. Los análisis aparecen aquí cuando el agente intenta instalar software o ejecutar acciones sensibles.',
          )}
        />
      ) : (
        <div className="cv-list">
          <AnimatePresence initial={false}>
            {scans.map((scan, i) => (
              <AnimatedListItem key={scan.scan_id ?? scan.id ?? i}>
                <ScanRow scan={scan} />
              </AnimatedListItem>
            ))}
          </AnimatePresence>
        </div>
      )}
    </section>
  )
}

// ── SeguridadView ─────────────────────────────────────────────────────────────

export default function SeguridadView() {
  const t = useT()
  const [mfaDisabled, setMfaDisabled] = useState(false)

  useEffect(() => {
    getPolicies().then(p => setMfaDisabled(p.mfa_on_dangers === false)).catch(() => {})
  }, [])

  return (
    <>
      <PageHeader
        title={t('view.seguridad')}
        subtitle={tNew(t, 'seg.subtitle', 'Aprobaciones, políticas del agente y escaneos de seguridad.')}
      />

      <div className="view-body cv-view-body">
        <ApprovalsSection mfaDisabled={mfaDisabled} />
        <GovernanceSection />
        <EgressSection />
        <SecurityCenterSection />
      </div>
    </>
  )
}
