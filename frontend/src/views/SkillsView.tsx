import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { X, Zap, Search as SearchIcon, ChevronRight, Package } from 'lucide-react'
import { useT } from '../lib/i18n'
import {
  listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
  uninstallHubSkill, promoteSkill,
  createTrainingSession, startTrainingRecording, stopTrainingRecording,
  synthesizeSkill, abandonTrainingSession,
  pauseTrainingRecording, resumeTrainingRecording, cancelTrainingRecording,
  getSkillDetails, scanInstall, recordSecurityDecision,
  ApiError,
} from '../api/client'
import type { Skill, HubSkillResult, HubInstallResponse, InstallScanResponse, SkillDetails } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import Badge, { type BadgeVariant } from '../components/Badge'
import InstallScanModal from '../components/InstallScanModal'
import SkillDetailsModal from '../components/SkillDetailsModal'
import type { MfaFactors } from '../components/MfaModal'
import { PageHeader } from '../components/ui/PageHeader'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import {
  AnimatePresence,
  AnimatedListItem,
  AnimatedExpanderContent,
  FadeIn,
  Stagger,
  StaggerItem,
  motion,
  useReducedMotion,
  SPRING,
} from '../components/ui/motion'

// ── Poll helper ───────────────────────────────────────────────────────────────

interface PollHandle {
  cancel(): void
}

function pollHubOp(opId: string, { onDone, onError }: { onDone?: () => void; onError?: (r: string) => void }): PollHandle {
  let tries = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let cancelled = false

  const tick = async () => {
    if (cancelled) return
    if (tries++ > 40) { onError?.('timeout'); return }
    const st = await getHubOpStatus(opId)
    if (cancelled) return
    const s = String(st?.status ?? '').toLowerCase()
    if (s === 'done' || s === 'completed' || s === 'success') { onDone?.(); return }
    if (s === 'error' || s === 'failed') { onError?.(st?.error ?? st?.message ?? 'error'); return }
    if (!cancelled) timer = setTimeout(tick, 2500)
  }

  timer = setTimeout(tick, 1500)

  return {
    cancel() {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    },
  }
}

function skillDocUrl(item: HubSkillResult): string {
  const raw = String(item.repo ?? item.url ?? item.homepage ?? '').trim()
  if (!raw) return ''
  if (/^https?:\/\//i.test(raw)) return raw
  if (/^[\w.-]+\/[\w.-]+$/.test(raw)) return `https://github.com/${raw}`
  return ''
}

// ── State ─────────────────────────────────────────────────────────────────────

type InstalledState =
  | { status: 'loading' }
  | { status: 'success'; skills: Skill[] }
  | { status: 'error'; message: string }

type InstalledAction =
  | { type: 'LOADING' }
  | { type: 'LOADED'; skills: Skill[] }
  | { type: 'FAILED'; message: string }

function installedReducer(_s: InstalledState, a: InstalledAction): InstalledState {
  switch (a.type) {
    case 'LOADING': return { status: 'loading' }
    case 'LOADED': return { status: 'success', skills: a.skills }
    case 'FAILED': return { status: 'error', message: a.message }
  }
}

type TeachPhase = 'idle' | 'form' | 'recording' | 'paused' | 'synth'

function show(message: string, kind: 'ok' | 'warn' | 'error' = 'ok') {
  if (kind === 'ok') sileo.success({ title: message })
  else if (kind === 'error') sileo.error({ title: message })
  else sileo.warning({ title: message })
}

interface PendingSkillInstall {
  scan: InstallScanResponse
  item: HubSkillResult
  onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void
}

export default function SkillsView() {
  const t = useT()
  const [state, dispatch] = useReducer(installedReducer, { status: 'loading' })
  const [installedHubNames, setInstalledHubNames] = useState<Set<string>>(new Set())
  const [hubResults, setHubResults] = useState<HubSkillResult[]>([])
  const [hubQuery, setHubQuery] = useState('')
  const [hubSearching, setHubSearching] = useState(false)
  const hubInputRef = useRef<HTMLInputElement>(null)
  const [teachPhase, setTeachPhase] = useState<TeachPhase>('idle')
  const teachSessionRef = useRef<string | null>(null)
  const teachNameRef = useRef<HTMLInputElement>(null)
  const teachDescRef = useRef<HTMLTextAreaElement>(null)
  const teachNameValueRef = useRef('')
  const [confirm, ConfirmDialogNode] = useConfirmDialog()
  const [pendingSkillInstall, setPendingSkillInstall] = useState<PendingSkillInstall | null>(null)
  const [skillDetails, setSkillDetails] = useState<SkillDetails | null>(null)
  const [loadingDetailsId, setLoadingDetailsId] = useState<string | null>(null)
  const [teachOpen, setTeachOpen] = useState(false)

  const pollHandlesRef = useRef<PollHandle[]>([])

  useEffect(() => {
    return () => {
      for (const h of pollHandlesRef.current) h.cancel()
      pollHandlesRef.current = []
    }
  }, [])

  function trackPoll(handle: PollHandle) {
    pollHandlesRef.current.push(handle)
  }

  const loadInstalled = useCallback(async () => {
    dispatch({ type: 'LOADING' })
    try {
      const [skills, hub] = await Promise.all([listSkills(), listHubSkills().catch(() => [])])
      const hubArr = Array.isArray(hub) ? hub : []
      const names = new Set(hubArr.flatMap(h => [h.name, h.skill_name, h.identifier].filter(Boolean) as string[]))
      setInstalledHubNames(names)
      // INSTALADAS must show BOTH native skills AND hub-installed packages. listSkills
      // only returns the native/synthesized set; a skill installed from the hub lands
      // in listHubSkills. Without merging, a just-installed hub skill showed "Instalada"
      // in search but never appeared in the INSTALADAS list. Adapt hub items to the
      // Skill shape and dedup against the native ones (same key → native wins).
      const native = Array.isArray(skills) ? skills : []
      const keyOf = (s: { skill_name?: string; name?: string; slug?: string; identifier?: string }) =>
        (s.skill_name ?? s.name ?? s.slug ?? s.identifier ?? '').trim().toLowerCase()
      const nativeKeys = new Set(native.map(keyOf).filter(Boolean))
      const hubOnly: Skill[] = hubArr
        .filter(h => { const k = keyOf(h); return k && !nativeKeys.has(k) })
        .map(h => {
          const id = h.identifier ?? h.slug ?? h.skill_name ?? h.name ?? ''
          return {
            package_id: id,
            skill_id: id,
            skill_name: h.skill_name ?? h.name ?? id,
            name: h.name ?? h.skill_name ?? id,
            slug: h.slug ?? h.identifier,
            state: 'installed',
          }
        })
      dispatch({ type: 'LOADED', skills: [...native, ...hubOnly] })
    } catch (e) {
      dispatch({
        type: 'FAILED',
        message: e instanceof ApiError ? e.message : 'No se pudieron cargar las skills.',
      })
    }
  }, [])

  useEffect(() => { loadInstalled() }, [loadInstalled])

  const HUB_SUGGESTIONS = ['web search', 'email', 'calendar', 'github', 'spreadsheet']

  async function runSearch() {
    const q = hubQuery.trim()
    if (!q) return
    setHubSearching(true)
    try {
      const results = await searchSkillsHub(q)
      const arr = Array.isArray(results) ? results : ((results as { results?: HubSkillResult[] })?.results ?? [])
      setHubResults(arr)
    } finally { setHubSearching(false) }
  }

  async function runSearchFor(q: string) {
    setHubQuery(q)
    setHubSearching(true)
    try {
      const results = await searchSkillsHub(q)
      const arr = Array.isArray(results) ? results : ((results as { results?: HubSkillResult[] })?.results ?? [])
      setHubResults(arr)
    } finally { setHubSearching(false) }
  }

  async function handleInstall(item: HubSkillResult, onBtnUpdate: (state: 'installing' | 'installed' | 'ready') => void) {
    const identifier = item.identifier ?? item.slug ?? item.name ?? ''
    const name = item.name ?? identifier
    onBtnUpdate('installing')

    try {
      const scan = await scanInstall('skill', identifier)
      if (scan.requires_owner_approval || scan.verdict === 'WARN' || scan.verdict === 'FAIL') {
        setPendingSkillInstall({ scan, item, onBtnUpdate })
        return
      }
    } catch {
      // Scan unavailable — fall through to direct install
    }

    await doInstallSkill(identifier, name, onBtnUpdate, false)
  }

  async function handleScanApprove(factors: MfaFactors) {
    if (!pendingSkillInstall) return
    const { scan, item, onBtnUpdate } = pendingSkillInstall
    setPendingSkillInstall(null)
    const identifier = item.identifier ?? item.slug ?? item.name ?? ''
    const name = item.name ?? identifier
    try {
      await recordSecurityDecision({
        scan_id: scan.scan_id,
        decision: 'approve',
        identifier,
        kind: 'skill',
        score: scan.score,
        verdict: scan.verdict,
        risks_json: JSON.stringify(scan.risks),
        totp: factors.totp,
      })
      await doInstallSkill(identifier, name, onBtnUpdate, true)
    } catch (e) {
      show(e instanceof Error ? e.message : 'Error al registrar la decisión', 'error')
      onBtnUpdate('ready')
    }
  }

  async function doInstallSkill(
    identifier: string,
    name: string,
    onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void,
    force: boolean,
  ) {
    try {
      const op: HubInstallResponse = await installSkill(identifier, force)

      if (op && op.blocked) {
        const risksText = (op.risks ?? []).slice(0, 3).join('; ') || 'varios riesgos detectados'
        const ok = await confirm({
          title: `El análisis de seguridad bloqueó "${name}"`,
          description: `Puntuación: ${op.score ?? '?'}/100. Riesgos: ${risksText}.\n\n¿Instalar igualmente bajo tu responsabilidad?`,
          confirmLabel: 'Instalar igualmente',
          variant: 'danger',
        })
        if (ok) {
          await doInstallSkill(identifier, name, onBtnUpdate, true)
        } else {
          onBtnUpdate('ready')
        }
        return
      }

      if (op && (op.ok === false || op.error)) {
        throw new Error(op.error ?? 'No se pudo instalar: security')
      }
      handleInstallOp(op, name, onBtnUpdate)
    } catch (e) {
      show(`No se pudo instalar: ${e instanceof Error ? e.message : 'error'}`, 'error')
      onBtnUpdate('ready')
    }
  }

  function handleInstallOp(op: HubInstallResponse, name: string, onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void) {
    if (op?.op_id) {
      show(`Instalando "${name}"…`, 'ok')
      trackPoll(pollHubOp(op.op_id, {
        onDone: () => { show(`"${name}" instalada — pruébala en el chat`, 'ok'); onBtnUpdate('installed'); loadInstalled() },
        onError: r => { show(`No se pudo instalar: ${r}`, 'error'); onBtnUpdate('ready') },
      }))
    } else {
      show(`"${name}" instalada — pruébala en el chat`, 'ok')
      onBtnUpdate('installed')
      loadInstalled()
    }
  }

  async function handleTeachStart() {
    const name = teachNameRef.current?.value.trim() ?? ''
    const description = teachDescRef.current?.value.trim() ?? ''
    if (!name) { show('Ponle un nombre a la skill', 'warn'); return }
    teachNameValueRef.current = name
    try {
      const s = await createTrainingSession({ skill_name: name, description, surface_kind: 'browser' })
      teachSessionRef.current = s.session_id
      await startTrainingRecording(s.session_id)
      setTeachPhase('recording')
    } catch (e) {
      show(`No se pudo crear la skill: ${e instanceof Error ? e.message : 'error'}`, 'error')
      if (teachSessionRef.current) {
        abandonTrainingSession(teachSessionRef.current)
        teachSessionRef.current = null
      }
    }
  }

  async function handleTeachPause() {
    const sid = teachSessionRef.current
    if (!sid) return
    try {
      await pauseTrainingRecording(sid)
      setTeachPhase('paused')
    } catch (e) {
      show(`No se pudo pausar: ${e instanceof Error ? e.message : 'error'}`, 'error')
    }
  }

  async function handleTeachResume() {
    const sid = teachSessionRef.current
    if (!sid) return
    try {
      await resumeTrainingRecording(sid)
      setTeachPhase('recording')
    } catch (e) {
      show(`No se pudo reanudar: ${e instanceof Error ? e.message : 'error'}`, 'error')
    }
  }

  async function handleTeachStop() {
    const sid = teachSessionRef.current
    if (!sid) { setTeachPhase('idle'); return }
    const name = teachNameValueRef.current
    setTeachPhase('synth')
    try {
      await stopTrainingRecording(sid)
      await synthesizeSkill(sid)
      show(`Skill "${name}" creada`, 'ok')
      setTeachPhase('idle')
      loadInstalled()
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0
      const msg = status === 409
        ? 'Conecta un modelo en Proveedores para crear skills.'
        : `No se pudo crear la skill: ${e instanceof Error ? e.message : 'error'}`
      show(msg, status === 409 ? 'warn' : 'error')
      setTeachPhase('idle')
    } finally {
      teachSessionRef.current = null
    }
  }

  async function handleTeachCancel() {
    const sid = teachSessionRef.current
    if (sid) {
      try {
        await cancelTrainingRecording(sid)
      } catch {
        abandonTrainingSession(sid)
      }
      teachSessionRef.current = null
    }
    setTeachPhase('idle')
  }

  async function handleViewSkillDetails(skill: Skill) {
    const pkgId = skill.package_id ?? skill.skill_id ?? ''
    if (!pkgId) return
    setLoadingDetailsId(pkgId)
    try {
      const details = await getSkillDetails(pkgId)
      setSkillDetails(details)
    } catch (e) {
      show(e instanceof Error ? e.message : 'No se pudieron cargar los detalles', 'error')
    } finally {
      setLoadingDetailsId(null)
    }
  }

  return (
    <>
      {ConfirmDialogNode}
      {pendingSkillInstall && (
        <InstallScanModal
          scan={pendingSkillInstall.scan}
          name={pendingSkillInstall.item.name ?? pendingSkillInstall.item.identifier ?? ''}
          onApprove={handleScanApprove}
          onCancel={() => {
            pendingSkillInstall.onBtnUpdate('ready')
            setPendingSkillInstall(null)
          }}
        />
      )}
      {skillDetails && (
        <SkillDetailsModal
          details={skillDetails}
          onClose={() => setSkillDetails(null)}
        />
      )}
      <PageHeader
        title="Habilidades"
        subtitle="Amplía lo que puede hacer el agente. Busca e instala en segundos."
      />

      <div className="view-body cv-view-body">
        <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-8)' }}>

          {/* ── Hub search ──────────────────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Catálogo de habilidades">
              <h2 className="cv-section-label">Catálogo</h2>

              {!hubSearching && hubResults.length === 0 && (
                <motion.div
                  layout
                  style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sp-2)', marginBottom: 'var(--sp-3)' }}
                  aria-label="Búsquedas sugeridas"
                >
                  <AnimatePresence>
                    {HUB_SUGGESTIONS.map((chip) => (
                      <motion.button
                        key={chip}
                        type="button"
                        className="suggestion-pill"
                        onClick={() => runSearchFor(chip)}
                        layout
                        initial={{ opacity: 0, scale: 0.92 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.88 }}
                        transition={{ ...SPRING, delay: HUB_SUGGESTIONS.indexOf(chip) * 0.04 }}
                      >
                        {chip}
                      </motion.button>
                    ))}
                  </AnimatePresence>
                </motion.div>
              )}

              <div className="cv-search-row">
                <label className="sr-only" htmlFor="hub-search">Buscar en el hub de skills</label>
                <div style={{ position: 'relative', flex: 1 }}>
                  <SearchIcon
                    size={14}
                    aria-hidden="true"
                    style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--ink4)', pointerEvents: 'none' }}
                  />
                  <input
                    id="hub-search"
                    ref={hubInputRef}
                    className="cv-input"
                    type="search"
                    placeholder="Buscar skills…"
                    autoComplete="off"
                    value={hubQuery}
                    onChange={e => setHubQuery(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') runSearch() }}
                    style={{ paddingLeft: 30 }}
                  />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={runSearch}
                  loading={hubSearching}
                >
                  Buscar
                </Button>
              </div>

              {!hubSearching && hubQuery && hubResults.length === 0 && (
                <EmptyState
                  icon={<SearchIcon size={32} />}
                  title={`Sin resultados para "${hubQuery}"`}
                />
              )}

              <AnimatePresence mode="popLayout">
                {hubResults.length > 0 && (
                  <motion.ul
                    className="cv-list"
                    role="list"
                    key="hub-results"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.15 }}
                  >
                    <AnimatePresence initial={false}>
                      {hubResults.map((item, i) => (
                        <AnimatedListItem key={item.identifier ?? item.slug ?? item.name ?? i}>
                          <HubResultRow
                            item={item}
                            installedNames={installedHubNames}
                            onInstall={handleInstall}
                          />
                        </AnimatedListItem>
                      ))}
                    </AnimatePresence>
                  </motion.ul>
                )}
              </AnimatePresence>
            </section>
          </StaggerItem>

          {/* ── Installed skills ─────────────────────────────────────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Skills activas">
              <h2 className="cv-section-label">{t('skills.installed.label')}</h2>

              {state.status === 'loading' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }} aria-busy="true">
                  {[...Array(2)].map((_, i) => <div key={i} className="cv-skeleton" style={{ height: 48 }} />)}
                </div>
              )}

              {state.status === 'error' && (
                <FadeIn>
                  <div role="alert">
                    <p className="state-error">{state.message}</p>
                    <Button variant="secondary" size="sm" onClick={loadInstalled} style={{ marginTop: 8 }}>Reintentar</Button>
                  </div>
                </FadeIn>
              )}

              {state.status === 'success' && (
                state.skills.length === 0
                  ? (
                    <EmptyState
                      icon={<Zap size={36} />}
                      title={t('skills.installed.empty')}
                      action={
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => {
                            hubInputRef.current?.focus()
                            hubInputRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                          }}
                        >
                          Buscar en el catálogo
                        </Button>
                      }
                    />
                  )
                  : (
                    <ul className="cv-list" role="list">
                      <AnimatePresence initial={false}>
                        {state.skills.map(s => (
                          <AnimatedListItem key={s.package_id ?? s.skill_id}>
                            <SkillRow
                              skill={s}
                              loadingDetails={loadingDetailsId === (s.package_id ?? s.skill_id ?? '')}
                              onView={() => handleViewSkillDetails(s)}
                              onPromote={async () => {
                                const pkgId = s.package_id ?? s.skill_id ?? ''
                                try {
                                  await promoteSkill(pkgId)
                                  show('El agente puede usar esta habilidad de forma autónoma', 'ok')
                                  loadInstalled()
                                }
                                catch (e) { show(e instanceof Error ? e.message : 'Error', 'error') }
                              }}
                              onUninstall={async () => {
                                const name = s.skill_name ?? s.name ?? s.package_id ?? ''
                                const ok = await confirm({
                                  title: `¿Desinstalar "${name}"?`,
                                  description: 'El agente dejará de tener esta habilidad.',
                                  confirmLabel: 'Desinstalar',
                                  variant: 'danger',
                                })
                                if (!ok) return
                                try {
                                  const op = await uninstallHubSkill(name)
                                  if (op?.op_id) {
                                    trackPoll(pollHubOp(op.op_id, {
                                      onDone: () => { show(`"${name}" desinstalada`, 'ok'); loadInstalled() },
                                      onError: r => { show(`No se pudo desinstalar: ${r}`, 'error'); loadInstalled() },
                                    }))
                                  } else {
                                    show(`"${name}" desinstalada`, 'ok'); loadInstalled()
                                  }
                                } catch (e) { show(e instanceof Error ? e.message : 'Error', 'error') }
                              }}
                            />
                          </AnimatedListItem>
                        ))}
                      </AnimatePresence>
                    </ul>
                  )
              )}
            </section>
          </StaggerItem>

          {/* ── Teach a skill (animated accordion at the bottom) ────────── */}
          <StaggerItem>
            <section className="cv-section" aria-label="Enseñar una habilidad">
              <TeachSkillExpander
                teachPhase={teachPhase}
                open={teachOpen}
                onToggle={() => {
                  if (!teachOpen) setTeachOpen(true)
                  else if (teachPhase === 'idle') setTeachOpen(false)
                }}
                teachNameRef={teachNameRef}
                teachDescRef={teachDescRef}
                onStart={handleTeachStart}
                onPause={handleTeachPause}
                onResume={handleTeachResume}
                onStop={handleTeachStop}
                onCancel={handleTeachCancel}
                onSetPhase={setTeachPhase}
              />
            </section>
          </StaggerItem>

        </Stagger>
      </div>
    </>
  )
}

// ── Installed skill row ───────────────────────────────────────────────────────

type StateMeta = { label: string; variant: BadgeVariant }

function useStateMeta(state: string): StateMeta {
  const t = useT()
  const s = state.toLowerCase()
  if (s.includes('autonom')) return { label: t('skills.state.autonomous'), variant: 'ok' }
  if (s.includes('deprec'))  return { label: t('skills.state.deprecated'), variant: 'neutral' }
  if (s.includes('valid'))   return { label: t('skills.state.validated'), variant: 'accent' }
  return { label: state, variant: 'neutral' }
}

interface SkillRowProps {
  skill: Skill
  loadingDetails: boolean
  onView: () => void
  onPromote: () => void
  onUninstall: () => void
}

function SkillRow({ skill, loadingDetails, onView, onPromote, onUninstall }: SkillRowProps) {
  const t = useT()
  const reduced = useReducedMotion()
  const name = skill.skill_name ?? skill.name ?? skill.slug ?? ''
  const meta = useStateMeta(skill.state ?? '')
  const version = skill.version ? `v${skill.version}` : ''
  const surfaces = Array.isArray(skill.surface_kinds)
    ? skill.surface_kinds.join(' · ')
    : (skill.surface_kinds ?? '')
  const sub = [version, surfaces].filter(Boolean).join(' · ')
  const isValidated = (skill.state ?? '').toLowerCase().includes('valid')

  return (
    <motion.div
      className="skill-row"
      whileHover={reduced ? undefined : { y: -2 }}
      transition={SPRING}
      layout
    >
      <span className="ds-icon-chip" aria-hidden="true"><Zap size={14} /></span>
      <div className="skill-row__info">
        <div className="skill-row__name">{name}</div>
        {sub && <div className="skill-row__desc">{sub}</div>}
      </div>
      <div className="skill-row__actions">
        {meta.label && (
          <Badge variant={meta.variant}>{meta.label}</Badge>
        )}
        <button
          className="cv-btn cv-btn--secondary cv-btn--sm"
          onClick={onView}
          disabled={loadingDetails}
          aria-label={`Ver instrucciones de ${name}`}
        >
          {loadingDetails ? '…' : 'Ver'}
        </button>
        {isValidated && (
          <button
            className="cv-btn cv-btn--primary cv-btn--sm"
            onClick={onPromote}
            aria-label={t('skills.promote')}
          >
            {t('skills.promote')}
          </button>
        )}
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          onClick={onUninstall}
          aria-label={`Desinstalar ${name}`}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
    </motion.div>
  )
}

// ── Hub result row ────────────────────────────────────────────────────────────

const TRUST_VARIANT: Record<string, BadgeVariant> = {
  official: 'ok', verified: 'ok', community: 'warn',
}

interface HubResultRowProps {
  item: HubSkillResult
  installedNames: Set<string>
  onInstall: (item: HubSkillResult, onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void) => void
}

function HubResultRow({ item, installedNames, onInstall }: HubResultRowProps) {
  const reduced = useReducedMotion()
  const [btnState, setBtnState] = useState<'ready' | 'installing' | 'installed'>('ready')
  const name = item.name ?? item.identifier ?? item.slug ?? ''
  const already = installedNames.has(name) || installedNames.has(item.identifier ?? '')
  const docUrl = skillDocUrl(item)
  const trust = item.trust_level ?? ''
  const trustVariant: BadgeVariant = TRUST_VARIANT[trust.toLowerCase()] ?? 'neutral'

  return (
    <motion.div
      className="skill-hub-result"
      whileHover={reduced ? undefined : { y: -2 }}
      transition={SPRING}
      layout
    >
      <span className="ds-icon-chip ds-icon-chip--neutral" aria-hidden="true"><Package size={14} /></span>
      <div className="skill-hub-result__info">
        <div className="skill-hub-result__name">
          {name}
          {trust && <Badge variant={trustVariant}>{trust}</Badge>}
          {item.source && <Badge variant="neutral">{item.source}</Badge>}
        </div>
        {item.description && (
          <div className="skill-hub-result__desc" title={item.description}>{item.description}</div>
        )}
      </div>
      <div className="skill-hub-result__actions">
        {docUrl && (
          <a href={docUrl} target="_blank" rel="noopener noreferrer" className="cv-link cv-btn--sm">
            Documentación
          </a>
        )}
        <button
          className="cv-btn cv-btn--secondary cv-btn--sm"
          disabled={already || btnState !== 'ready'}
          onClick={() => onInstall(item, setBtnState)}
        >
          {already || btnState === 'installed' ? 'Instalada' : btnState === 'installing' ? 'Instalando…' : 'Instalar'}
        </button>
      </div>
    </motion.div>
  )
}

// ── Teach skill expander (replaces <details> with animated accordion) ─────────

interface TeachSkillExpanderProps {
  teachPhase: TeachPhase
  open: boolean
  onToggle: () => void
  teachNameRef: React.RefObject<HTMLInputElement> | React.RefObject<HTMLInputElement | null>
  teachDescRef: React.RefObject<HTMLTextAreaElement> | React.RefObject<HTMLTextAreaElement | null>
  onStart: () => void
  onPause: () => void
  onResume: () => void
  onStop: () => void
  onCancel: () => void
  onSetPhase: (p: TeachPhase) => void
}

function TeachSkillExpander({
  teachPhase, open, onToggle,
  teachNameRef, teachDescRef,
  onStart, onPause, onResume, onStop, onCancel, onSetPhase,
}: TeachSkillExpanderProps) {
  const t = useT()
  const reduced = useReducedMotion()

  return (
    <div>
      {/* Clickable header row — replaces <summary> */}
      <button
        type="button"
        className="cv-teach-expander"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls="teach-skill-body"
      >
        <motion.span
          aria-hidden="true"
          animate={reduced ? undefined : { rotate: open ? 90 : 0 }}
          transition={{ type: 'tween', ease: [0.4, 0, 0.2, 1], duration: 0.18 }}
          style={{ display: 'inline-flex', flexShrink: 0 }}
        >
          <ChevronRight size={13} className="cv-teach-chevron" style={{ transform: 'none' }} />
        </motion.span>
        {t('skills.teach.header')}
      </button>

      {/* Animated body */}
      <AnimatedExpanderContent open={open}>
        <div id="teach-skill-body" className="cv-teach-card" style={{ marginTop: 'var(--sp-3)' }}>
          <p className="cv-teach-intro">
            Enséñale a Lumen a operar en el navegador grabando una demostración. Aprende a usar plataformas y a operar por ti.
          </p>
          <p className="cv-hint" style={{ marginBottom: 8 }}>
            La demostración ocurre en un navegador aislado dentro de Lumen. No interrumpe otras tareas.
          </p>

          <AnimatePresence mode="wait" initial={false}>
            {teachPhase === 'idle' && (
              <motion.div
                key="idle"
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduced ? undefined : { opacity: 0, y: -4 }}
                transition={{ type: 'tween', ease: [0.4, 0, 0.2, 1], duration: 0.15 }}
              >
                <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={() => onSetPhase('form')} style={{ alignSelf: 'flex-start' }}>
                  + Enseñar skill
                </button>
              </motion.div>
            )}

            {teachPhase === 'form' && (
              <motion.div
                key="form"
                className="cv-form-stack"
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduced ? undefined : { opacity: 0, y: -4 }}
                transition={{ type: 'tween', ease: [0.4, 0, 0.2, 1], duration: 0.15 }}
              >
                <label className="sr-only" htmlFor="teach-name">Nombre de la skill</label>
                <input
                  id="teach-name"
                  ref={teachNameRef as React.RefObject<HTMLInputElement>}
                  className="cv-input"
                  type="text"
                  placeholder='Nombre de la skill (p. ej. "Publicar en LinkedIn")'
                  autoComplete="off"
                />
                <label className="sr-only" htmlFor="teach-desc">Descripción de la skill</label>
                <textarea
                  id="teach-desc"
                  ref={teachDescRef as React.RefObject<HTMLTextAreaElement>}
                  className="cv-textarea"
                  rows={4}
                  placeholder="Describe qué hace y los pasos — el agente aprende la skill de aquí"
                />
                <div className="cv-form-actions">
                  <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={onStart}>Empezar</button>
                  <button className="cv-btn cv-btn--ghost cv-btn--sm" onClick={onCancel}>Cancelar</button>
                </div>
              </motion.div>
            )}

            {teachPhase === 'recording' && (
              <motion.div
                key="recording"
                className="cv-form-stack"
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduced ? undefined : { opacity: 0, y: -4 }}
                transition={{ type: 'tween', ease: [0.4, 0, 0.2, 1], duration: 0.15 }}
              >
                <p className="teach-recording-label" role="status" aria-live="polite">
                  Grabando la demostración…
                </p>
                <div className="cv-form-actions">
                  <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={onPause} type="button">Pausar</button>
                  <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={onStop} type="button">{t('skills.teach.stop')}</button>
                  <button className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger" onClick={onCancel} type="button">Cancelar</button>
                </div>
              </motion.div>
            )}

            {teachPhase === 'paused' && (
              <motion.div
                key="paused"
                className="cv-form-stack"
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduced ? undefined : { opacity: 0, y: -4 }}
                transition={{ type: 'tween', ease: [0.4, 0, 0.2, 1], duration: 0.15 }}
              >
                <p className="state-label" role="status" aria-live="polite">Grabación en pausa.</p>
                <div className="cv-form-actions">
                  <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={onResume} type="button">Reanudar</button>
                  <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={onStop} type="button">{t('skills.teach.stop_paused')}</button>
                  <button className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger" onClick={onCancel} type="button">Cancelar</button>
                </div>
              </motion.div>
            )}

            {teachPhase === 'synth' && (
              <motion.p
                key="synth"
                className="state-label"
                aria-live="polite"
                aria-busy="true"
                initial={reduced ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={reduced ? undefined : { opacity: 0 }}
                transition={{ duration: 0.15 }}
              >
                {t('skills.teach.synth')}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </AnimatedExpanderContent>
    </div>
  )
}
