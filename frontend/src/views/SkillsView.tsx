import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { sileo } from 'sileo'
import { X, Zap, Search as SearchIcon, Plus, Play, Package, AlertTriangle } from 'lucide-react'
import { useT } from '../lib/i18n'
import { isLiveSkill } from '../lib/skills'
import type { ChatOutletContext } from '../components/Layout'
import {
  listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
  uninstallHubSkill, promoteSkill,
  getSkillDetails, scanInstall, recordSecurityDecision,
  ApiError,
} from '../api/client'
import type { Skill, HubSkillResult, HubInstallResponse, InstallScanResponse, SkillDetails } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'
import InstallScanModal from '../components/InstallScanModal'
import SkillDetailsModal from '../components/SkillDetailsModal'
import type { MfaFactors } from '../components/MfaModal'
import { PageHeader } from '../components/ui/PageHeader'
import { TeachModal } from '../components/TeachModal'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import {
  AnimatePresence,
  AnimatedListItem,
  FadeIn,
  Stagger,
  StaggerItem,
  motion,
  useReducedMotion,
  SPRING,
} from '../components/ui/motion'
import s from './SkillsView.module.css'

// ── Poll helper ───────────────────────────────────────────────────────────────

interface PollHandle {
  cancel(): void
}

function pollHubOp(opId: string, t: ReturnType<typeof useT>, { onDone, onError }: { onDone?: () => void; onError?: (r: string) => void }): PollHandle {
  let tries = 0
  let unknownStreak = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let cancelled = false

  const tick = async () => {
    if (cancelled) return
    if (tries++ > 40) { onError?.('timeout'); return }
    const st = await getHubOpStatus(opId)
    if (cancelled) return
    const status = String(st?.status ?? '').toLowerCase()
    if (status === 'done' || status === 'completed' || status === 'success') { onDone?.(); return }
    if (status === 'error' || status === 'failed') { onError?.(st?.error ?? st?.message ?? 'error'); return }
    // 'unknown' = op not found / daemon unavailable. Treat as terminal after a short
    // streak (tolerate a registration race) instead of polling ~100s to "timeout".
    if (status === 'unknown') {
      if (++unknownStreak >= 3) {
        onError?.(t('skills.hub.op_lost'))
        return
      }
    } else {
      unknownStreak = 0
    }
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
  const navigate = useNavigate()
  const { startNew, sendMessage } = useOutletContext<ChatOutletContext>()
  const [state, dispatch] = useReducer(installedReducer, { status: 'loading' })
  const [installedHubNames, setInstalledHubNames] = useState<Set<string>>(new Set())
  const [hubResults, setHubResults] = useState<HubSkillResult[]>([])
  const [hubQuery, setHubQuery] = useState('')
  const [hubSearching, setHubSearching] = useState(false)
  const hubInputRef = useRef<HTMLInputElement>(null)
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
      const keyOf = (sk: { skill_name?: string; name?: string; slug?: string; identifier?: string }) =>
        (sk.skill_name ?? sk.name ?? sk.slug ?? sk.identifier ?? '').trim().toLowerCase()
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
        message: e instanceof ApiError ? e.message : t('skills.err.load'),
      })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
      show(e instanceof Error ? e.message : t('skills.err.decision'), 'error')
      onBtnUpdate('ready')
    }
  }

  async function doInstallSkill(
    identifier: string,
    name: string,
    onBtnUpdate: (st: 'installing' | 'installed' | 'ready') => void,
    force: boolean,
  ) {
    try {
      const op: HubInstallResponse = await installSkill(identifier, force)

      if (op && op.blocked) {
        const risksText = (op.risks ?? []).slice(0, 3).join('; ') || t('skills.install.risks_fallback')
        const ok = await confirm({
          title: t('skills.install.blocked.title').replace('{name}', name),
          description: t('skills.install.blocked.desc').replace('{score}', String(op.score ?? '?')).replace('{risks}', risksText),
          confirmLabel: t('skills.install.blocked.confirm'),
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
        throw new Error(op.error ?? t('skills.install.err.security'))
      }
      handleInstallOp(op, name, onBtnUpdate)
    } catch (e) {
      show(t('skills.install.err').replace('{reason}', e instanceof Error ? e.message : t('skills.err.generic')), 'error')
      onBtnUpdate('ready')
    }
  }

  function handleInstallOp(op: HubInstallResponse, name: string, onBtnUpdate: (st: 'installing' | 'installed' | 'ready') => void) {
    if (op?.op_id) {
      show(t('skills.install.installing').replace('{name}', name), 'ok')
      trackPoll(pollHubOp(op.op_id, t, {
        onDone: () => { show(t('skills.install.installed').replace('{name}', name), 'ok'); onBtnUpdate('installed'); loadInstalled() },
        onError: r => { show(t('skills.install.err').replace('{reason}', r), 'error'); onBtnUpdate('ready') },
      }))
    } else {
      show(t('skills.install.installed').replace('{name}', name), 'ok')
      onBtnUpdate('installed')
      loadInstalled()
    }
  }

  async function handleViewSkillDetails(skill: Skill) {
    const pkgId = skill.package_id ?? skill.skill_id ?? ''
    if (!pkgId) return
    setLoadingDetailsId(pkgId)
    try {
      const details = await getSkillDetails(pkgId)
      setSkillDetails(details)
    } catch (e) {
      show(e instanceof Error ? e.message : t('skills.err.details'), 'error')
    } finally {
      setLoadingDetailsId(null)
    }
  }

  const installedCount = state.status === 'success' ? state.skills.length : null

  // Verify a skill = run it in a fresh chat. If the agent uses the browser, the chat's
  // "Ver en vivo" panel appears automatically (useChat liveBrowserActive). No backend.
  function handleVerify(sk: Skill) {
    const name = sk.skill_name ?? sk.name ?? sk.slug ?? ''
    startNew()
    navigate('/chat')
    void sendMessage(t('skills.verify.msg').replace('{name}', name))
  }

  const renderSkill = (sk: Skill) => (
    <AnimatedListItem key={sk.package_id ?? sk.skill_id}>
      <SkillRow
        skill={sk}
        loadingDetails={loadingDetailsId === (sk.package_id ?? sk.skill_id ?? '')}
        onView={() => handleViewSkillDetails(sk)}
        onVerify={() => handleVerify(sk)}
        onPromote={async () => {
          const pkgId = sk.package_id ?? sk.skill_id ?? ''
          try {
            await promoteSkill(pkgId)
            show(t('skills.promote.toast'), 'ok')
            loadInstalled()
          } catch (e) { show(e instanceof Error ? e.message : t('skills.err.generic'), 'error') }
        }}
        onUninstall={async () => {
          const name = sk.skill_name ?? sk.name ?? sk.package_id ?? ''
          const ok = await confirm({
            title: t('skills.uninstall.confirm.title').replace('{name}', name),
            description: t('skills.uninstall.confirm.desc'),
            confirmLabel: t('skills.uninstall.confirm.label'),
            variant: 'danger',
          })
          if (!ok) return
          try {
            const op = await uninstallHubSkill(name)
            if (op?.op_id) {
              trackPoll(pollHubOp(op.op_id, t, {
                onDone: () => { show(t('skills.uninstall.done').replace('{name}', name), 'ok'); loadInstalled() },
                onError: r => { show(t('skills.uninstall.err').replace('{reason}', r), 'error'); loadInstalled() },
              }))
            } else {
              show(t('skills.uninstall.done').replace('{name}', name), 'ok'); loadInstalled()
            }
          } catch (e) { show(e instanceof Error ? e.message : t('skills.err.generic'), 'error') }
        }}
      />
    </AnimatedListItem>
  )

  return (
    <>
      {ConfirmDialogNode}

      <TeachModal
        open={teachOpen}
        onClose={() => setTeachOpen(false)}
        onSaved={() => { setTeachOpen(false); loadInstalled() }}
      />

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
        title={t('view.skills')}
        subtitle={t('skills.subtitle')}
        actions={
          <Button variant="primary" size="sm" onClick={() => setTeachOpen(true)}>
            <Plus size={14} aria-hidden="true" />
            {t('skills.teach.open')}
          </Button>
        }
      />

      <div className={s.viewBody}>
        <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>

          {/* ── Installed skills ─────────────────────────────────────────── */}
          <StaggerItem>
            <section className={s.section} aria-label={t('skills.installed.aria')}>
              <div className={s.sectionHead}>
                <span className={s.sectionLabel}>{t('skills.installed.label')}</span>
                {installedCount !== null && installedCount > 0 && (
                  <span className={s.sectionCount} aria-label={t('skills.count.aria').replace('{n}', String(installedCount))}>
                    {installedCount}
                  </span>
                )}
              </div>

              {/* Loading skeletons */}
              {state.status === 'loading' && (
                <ul className={s.list} aria-busy="true" aria-label={t('skills.loading_aria')}>
                  {[0, 1, 2].map(i => (
                    <li key={i}>
                      <SkillRowSkeleton delay={i * 60} />
                    </li>
                  ))}
                </ul>
              )}

              {/* Error */}
              {state.status === 'error' && (
                <FadeIn>
                  <div role="alert" className={s.errorInline}>
                    <span className={s.errorIcon} aria-hidden="true">
                      <AlertTriangle size={16} />
                    </span>
                    <div className={s.errorBody}>
                      <p className={s.errorTitle}>{t('skills.err.load')}</p>
                      <p className={s.errorDesc}>{state.message}</p>
                      <div className={s.errorActions}>
                        <Button variant="secondary" size="sm" onClick={loadInstalled}>
                          {t('skills.retry')}
                        </Button>
                      </div>
                    </div>
                  </div>
                </FadeIn>
              )}

              {/* Success */}
              {state.status === 'success' && (
                state.skills.length === 0
                  ? (
                    <FadeIn>
                      <EmptyState
                        compact
                        icon={<Zap size={28} />}
                        title={t('skills.installed.empty')}
                        description={t('skills.installed.empty.desc')}
                        action={
                          <Button
                            variant="secondary"
                            size="sm"
                            onClick={() => {
                              hubInputRef.current?.focus()
                              hubInputRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                            }}
                          >
                            {t('skills.explore_catalog')}
                          </Button>
                        }
                      />
                    </FadeIn>
                  )
                  : (() => {
                    // Split installed skills: the ones demonstrated live (teaching_origin
                    // === 'teaching_live') get their own section above the rest.
                    const live = state.skills.filter(sk => sk.teaching_origin === 'teaching_live')
                    const rest = state.skills.filter(sk => sk.teaching_origin !== 'teaching_live')
                    return (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>
                        {live.length > 0 && (
                          <div>
                            <p className={s.subsectionLabel}>{t('skills.section.live')}</p>
                            <ul className={s.list} role="list">
                              <AnimatePresence initial={false}>
                                {live.map(renderSkill)}
                              </AnimatePresence>
                            </ul>
                          </div>
                        )}
                        <div>
                          {live.length > 0 && <p className={s.subsectionLabel}>{t('skills.section.rest')}</p>}
                          <ul className={s.list} role="list">
                            <AnimatePresence initial={false}>
                              {rest.map(renderSkill)}
                            </AnimatePresence>
                          </ul>
                        </div>
                      </div>
                    )
                  })()
              )}
            </section>
          </StaggerItem>

          {/* ── Hub search ──────────────────────────────────────────────── */}
          <StaggerItem>
            <section className={s.section} aria-label={t('skills.catalog.label')}>
              <div className={s.sectionHead}>
                <span className={s.sectionLabel}>{t('skills.catalog.label')}</span>
              </div>

              {/* Suggestion pills — only visible when no search results yet */}
              <AnimatePresence>
                {!hubSearching && hubResults.length === 0 && (
                  <motion.div
                    className={s.pillsRow}
                    aria-label={t('skills.suggestions.aria')}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    transition={{ ...SPRING, delay: 0.06 }}
                  >
                    {HUB_SUGGESTIONS.map((chip, i) => (
                      <motion.button
                        key={chip}
                        type="button"
                        className={s.pill}
                        onClick={() => runSearchFor(chip)}
                        initial={{ opacity: 0, scale: 0.9 }}
                        animate={{ opacity: 1, scale: 1 }}
                        transition={{ ...SPRING, delay: i * 0.04 }}
                        aria-label={t('skills.search_for.aria').replace('{term}', chip)}
                      >
                        {chip}
                      </motion.button>
                    ))}
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Search row */}
              <div className={s.searchRow}>
                <label className="sr-only" htmlFor="hub-search">{t('skills.hub_search.label')}</label>
                <div className={s.searchWrap}>
                  <span className={s.searchIcon} aria-hidden="true">
                    <SearchIcon size={14} />
                  </span>
                  <input
                    id="hub-search"
                    ref={hubInputRef}
                    className={s.searchInput}
                    type="search"
                    placeholder={t('skills.hub_search.placeholder')}
                    autoComplete="off"
                    value={hubQuery}
                    onChange={e => setHubQuery(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') runSearch() }}
                  />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={runSearch}
                  loading={hubSearching}
                >
                  {t('skills.search')}
                </Button>
              </div>

              {/* Empty search results */}
              {!hubSearching && hubQuery && hubResults.length === 0 && (
                <FadeIn>
                  <EmptyState
                    icon={<SearchIcon size={28} />}
                    title={t('skills.search.empty.title').replace('{query}', hubQuery)}
                    description={t('skills.search.empty.desc')}
                  />
                </FadeIn>
              )}

              {/* Results list */}
              <AnimatePresence mode="popLayout">
                {hubResults.length > 0 && (
                  <motion.ul
                    className={s.list}
                    role="list"
                    key="hub-results"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    aria-label={t('skills.results.aria').replace('{n}', String(hubResults.length))}
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

        </Stagger>
      </div>
    </>
  )
}

// ── Skeleton row (mirrors installed skill row layout) ─────────────────────────

function SkillRowSkeleton({ delay }: { delay: number }) {
  return (
    <div
      className={s.skeletonRow}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className={`skeleton ${s.skeletonIcon}`} />
      <div className={s.skeletonBody}>
        <div className={`skeleton skeleton--line ${s.skeletonTitle}`} />
        <div className={`skeleton skeleton--line-sm ${s.skeletonMeta}`} />
      </div>
      <div className={s.skeletonActions}>
        <div className={`skeleton ${s.skeletonBtn}`} />
        <div className={`skeleton ${s.skeletonBtn}`} style={{ width: 28 }} />
      </div>
    </div>
  )
}

// ── Installed skill row ───────────────────────────────────────────────────────

type StateMeta = { label: string; badgeCls: string }

function useStateMeta(rawState: string): StateMeta {
  const t = useT()
  const lower = rawState.toLowerCase()
  if (lower.includes('autonom')) return { label: t('skills.state.autonomous'), badgeCls: s.stateBadge + ' ' + s['stateBadge--ok'] }
  if (lower.includes('deprec'))  return { label: t('skills.state.deprecated'), badgeCls: s.stateBadge + ' ' + s['stateBadge--neutral'] }
  if (lower.includes('valid'))   return { label: t('skills.state.validated'),  badgeCls: s.stateBadge + ' ' + s['stateBadge--accent'] }
  return { label: rawState, badgeCls: s.stateBadge + ' ' + s['stateBadge--neutral'] }
}

interface SkillRowProps {
  skill: Skill
  loadingDetails: boolean
  onView: () => void
  onVerify: () => void
  onPromote: () => void
  onUninstall: () => void
}

function SkillRow({ skill, loadingDetails, onView, onVerify, onPromote, onUninstall }: SkillRowProps) {
  const t = useT()
  const reduced = useReducedMotion()
  const name = skill.skill_name ?? skill.name ?? skill.slug ?? ''
  const meta = useStateMeta(skill.state ?? '')
  const version = skill.version ? `v${skill.version}` : ''
  const surfaceList = Array.isArray(skill.surface_kinds)
    ? skill.surface_kinds
    : (skill.surface_kinds ? [skill.surface_kinds] : [])
  const surfaces = surfaceList.join(' · ')
  const sub = [version, surfaces].filter(Boolean).join(' · ')
  const isValidated = (skill.state ?? '').toLowerCase().includes('valid')
  const isAutonomous = (skill.state ?? '').toLowerCase().includes('autonom')
  const isLive = isLiveSkill(skill)

  return (
    <motion.div
      className={`${s.skillRow}${isAutonomous ? ' ' + s['skillRow--autonomous'] : ''}`}
      whileHover={reduced ? undefined : { y: -2 }}
      transition={SPRING}
      layout
    >
      <span
        className={`${s.skillIcon}${isAutonomous ? ' ' + s['skillIcon--autonomous'] : ''}`}
        aria-hidden="true"
      >
        <Zap size={14} />
      </span>

      <div className={s.skillInfo}>
        <div className={s.skillName}>
          {name}
          {isLive && (
            <span className={s.liveBadge} title={t('skills.live.tip')}>
              <span className={s.liveDot} aria-hidden="true" />
              {t('skills.live.badge')}
            </span>
          )}
        </div>
        {sub && <div className={s.skillMeta}>{sub}</div>}
      </div>

      <div className={s.skillActions}>
        {meta.label && (
          <span className={meta.badgeCls}>{meta.label}</span>
        )}
        <Button
          variant="secondary"
          size="sm"
          onClick={onVerify}
          title={t('skills.verify.tip')}
          aria-label={t('skills.verify.aria').replace('{name}', name)}
        >
          <Play size={13} aria-hidden="true" />
          {t('skills.verify')}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={onView}
          loading={loadingDetails}
          aria-label={t('skills.view.aria').replace('{name}', name)}
        >
          {t('skills.view')}
        </Button>
        {isValidated && (
          <Button variant="primary" size="sm" onClick={onPromote} aria-label={t('skills.promote')}>
            {t('skills.promote')}
          </Button>
        )}
        <Button
          variant="danger"
          size="sm"
          onClick={onUninstall}
          aria-label={t('skills.uninstall.aria').replace('{name}', name)}
        >
          <X size={13} aria-hidden="true" />
        </Button>
      </div>
    </motion.div>
  )
}

// ── Hub result row ────────────────────────────────────────────────────────────

type TrustLevel = 'official' | 'verified' | 'community' | string

function trustBadgeClass(trust: TrustLevel): string {
  const t = trust.toLowerCase()
  if (t === 'official' || t === 'verified') return s.trustBadge + ' ' + s['trustBadge--official']
  if (t === 'community') return s.trustBadge + ' ' + s['trustBadge--community']
  return s.trustBadge + ' ' + s['trustBadge--neutral']
}

interface HubResultRowProps {
  item: HubSkillResult
  installedNames: Set<string>
  onInstall: (item: HubSkillResult, onBtnUpdate: (st: 'installing' | 'installed' | 'ready') => void) => void
}

function HubResultRow({ item, installedNames, onInstall }: HubResultRowProps) {
  const t = useT()
  const reduced = useReducedMotion()
  const [btnState, setBtnState] = useState<'ready' | 'installing' | 'installed'>('ready')
  const name = item.name ?? item.identifier ?? item.slug ?? ''
  const already = installedNames.has(name) || installedNames.has(item.identifier ?? '')
  const docUrl = skillDocUrl(item)
  const trust = item.trust_level ?? ''

  const installLabel =
    already || btnState === 'installed' ? t('skills.installed_badge') :
    btnState === 'installing' ? t('skills.installing') :
    t('skills.install')

  return (
    <motion.div
      className={s.hubRow}
      whileHover={reduced ? undefined : { y: -1 }}
      transition={SPRING}
      layout
    >
      <span className={s.hubIcon} aria-hidden="true">
        <Package size={14} />
      </span>

      <div className={s.hubInfo}>
        <div className={s.hubName}>
          {name}
          {trust && (
            <span className={trustBadgeClass(trust)}>{trust}</span>
          )}
          {item.source && (
            <span className={`${s.trustBadge} ${s['trustBadge--neutral']}`}>{item.source}</span>
          )}
        </div>
        {item.description && (
          <div className={s.hubDesc} title={item.description}>
            {item.description}
          </div>
        )}
      </div>

      <div className={s.hubActions}>
        {docUrl && (
          <a
            href={docUrl}
            target="_blank"
            rel="noopener noreferrer"
            className={s.docLink}
            aria-label={t('skills.docs.aria').replace('{name}', name)}
          >
            {t('skills.docs')}
          </a>
        )}
        <button
          className="cv-btn cv-btn--secondary cv-btn--sm"
          disabled={already || btnState !== 'ready'}
          onClick={() => onInstall(item, setBtnState)}
          aria-label={`${installLabel} ${name}`}
        >
          {installLabel}
        </button>
      </div>
    </motion.div>
  )
}

