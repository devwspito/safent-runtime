import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { sileo } from 'sileo'
import {
  listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
  uninstallHubSkill, promoteSkill,
  createTrainingSession, startTrainingRecording, stopTrainingRecording,
  synthesizeSkill, abandonTrainingSession,
  ApiError,
} from '../api/client'
import type { Skill, HubSkillResult, HubInstallResponse } from '../api/types'
import { useConfirmDialog } from '../components/ConfirmDialog'

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

// Teach-skill phase: discriminated union of the 4 states
type TeachPhase = 'idle' | 'form' | 'recording' | 'synth'

function show(message: string, kind: 'ok' | 'warn' | 'error' = 'ok') {
  if (kind === 'ok') sileo.success({ title: message })
  else if (kind === 'error') sileo.error({ title: message })
  else sileo.warning({ title: message })
}

export default function SkillsView() {
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
  // ── Skill name for teach form (needed inside stop handler)
  const teachNameValueRef = useRef('')
  const [confirm, ConfirmDialogNode] = useConfirmDialog()

  const pollHandlesRef = useRef<PollHandle[]>([])

  // Cancel all active polls on unmount
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
      dispatch({ type: 'LOADED', skills: Array.isArray(skills) ? skills : [] })
    } catch (e) {
      dispatch({
        type: 'FAILED',
        message: e instanceof ApiError ? e.message : 'No se pudieron cargar las skills.',
      })
    }
  }, [])

  useEffect(() => { loadInstalled() }, [loadInstalled])

  // ── Hub search ────────────────────────────────────────────────────────────

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

  // ── Install from hub ──────────────────────────────────────────────────────

  async function handleInstall(item: HubSkillResult, onBtnUpdate: (state: 'installing' | 'installed' | 'ready') => void) {
    const identifier = item.identifier ?? item.slug ?? item.name ?? ''
    const name = item.name ?? identifier
    onBtnUpdate('installing')
    try {
      const op: HubInstallResponse = await installSkill(identifier)

      // Security Center BLOCK: show score + risks, offer owner override
      if (op && op.blocked) {
        const risksText = (op.risks ?? []).slice(0, 3).join('; ') || 'varios riesgos detectados'
        const ok = await confirm({
          title: `El análisis de seguridad bloqueó "${name}"`,
          description: `Puntuación: ${op.score ?? '?'}/100. Riesgos: ${risksText}.\n\n¿Instalar igualmente bajo tu responsabilidad?`,
          confirmLabel: 'Instalar igualmente',
          variant: 'danger',
        })
        if (ok) {
          await doInstall(identifier, name, onBtnUpdate, true)
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

  async function doInstall(identifier: string, name: string, onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void, force: boolean) {
    try {
      const op2 = await installSkill(identifier, force)
      if (op2 && (op2.ok === false || op2.blocked || op2.error)) {
        throw new Error(op2.error ?? 'No se pudo instalar: security')
      }
      handleInstallOp(op2, name, onBtnUpdate)
    } catch (e) {
      show(`No se pudo instalar: ${e instanceof Error ? e.message : 'error'}`, 'error')
      onBtnUpdate('ready')
    }
  }

  // ── Teach skill ───────────────────────────────────────────────────────────

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

  function handleTeachCancel() {
    if (teachSessionRef.current) {
      abandonTrainingSession(teachSessionRef.current)
      teachSessionRef.current = null
    }
    setTeachPhase('idle')
  }

  return (
    <>
      {ConfirmDialogNode}
      <header className="view-header">
        <h1 className="view-title">Habilidades</h1>
        <p className="view-subtitle">Amplía lo que puede hacer el agente. Busca e instala en segundos.</p>
      </header>

      <div className="view-body cv-view-body">
        {/* ── Hub search (first — easiest path to value) ────────────────── */}
        <section className="cv-section" aria-label="Catálogo de habilidades">
          <h2 className="cv-section-label">Catálogo</h2>

          {/* Suggestion chips — visible before any search so the hub is useful immediately */}
          {!hubSearching && hubResults.length === 0 && (
            <div
              style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sp-2)', marginBottom: 'var(--sp-3)' }}
              aria-label="Búsquedas sugeridas"
            >
              {HUB_SUGGESTIONS.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  className="suggestion-pill"
                  onClick={() => runSearchFor(chip)}
                >
                  {chip}
                </button>
              ))}
            </div>
          )}

          <div className="cv-search-row">
            <label className="sr-only" htmlFor="hub-search">Buscar en el hub de skills</label>
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
            />
            <button
              className="cv-btn cv-btn--secondary cv-btn--sm"
              onClick={runSearch}
              disabled={hubSearching}
            >
              {hubSearching ? 'Buscando…' : 'Buscar'}
            </button>
          </div>
          {!hubSearching && hubQuery && hubResults.length === 0 && (
            <p className="cv-empty">Sin resultados para "{hubQuery}"</p>
          )}
          {hubResults.length > 0 && (
            <ul className="cv-list" role="list">
              {hubResults.map((item, i) => (
                <li key={item.identifier ?? item.slug ?? item.name ?? i}>
                  <HubResultRow
                    item={item}
                    installedNames={installedHubNames}
                    onInstall={handleInstall}
                  />
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ── Installed skills ──────────────────────────────────────────── */}
        <section className="cv-section" aria-label="Skills instaladas">
          <h2 className="cv-section-label">Instaladas</h2>
          {state.status === 'loading' && <div className="cv-skeleton" aria-busy="true" />}
          {state.status === 'error' && (
            <div role="alert">
              <p className="state-error">{state.message}</p>
              <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={loadInstalled} style={{ marginTop: 8 }}>Reintentar</button>
            </div>
          )}
          {state.status === 'success' && (
            state.skills.length === 0
              ? (
                <div className="cv-empty" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 'var(--sp-3)' }}>
                  <span>Sin skills instaladas.</span>
                  <button
                    type="button"
                    className="cv-btn cv-btn--secondary cv-btn--sm"
                    onClick={() => {
                      hubInputRef.current?.focus()
                      hubInputRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                    }}
                  >
                    Buscar en el catálogo
                  </button>
                </div>
              )
              : (
                <ul className="cv-list" role="list">
                  {state.skills.map((s, i) => (
                    <li key={s.package_id ?? s.skill_id ?? i}>
                      <SkillRow
                        skill={s}
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
                    </li>
                  ))}
                </ul>
              )
          )}
        </section>

        {/* ── Teach a skill (advanced — collapsable at the bottom) ──────── */}
        <section className="cv-section" aria-label="Enseñar una habilidad">
          <details>
            <summary
              style={{
                cursor: 'pointer',
                fontSize: 'var(--text-label)',
                fontWeight: 600,
                color: 'var(--ink2)',
                listStyle: 'none',
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--sp-2)',
                userSelect: 'none',
                paddingBottom: 'var(--sp-2)',
              }}
            >
              <span aria-hidden="true" style={{ fontSize: 10 }}>▶</span>
              Enseñar una habilidad (avanzado)
            </summary>

            <div className="cv-teach-card" style={{ marginTop: 'var(--sp-3)' }}>
              <p className="cv-teach-intro">
                Enséñale a Lumen a operar en el navegador grabando una demostración. Aprende a usar plataformas y a operar por ti.
              </p>
              <p className="cv-hint" style={{ marginBottom: 8 }}>
                La demostración ocurre en un navegador aislado dentro de Lumen. No interrumpe otras tareas.
              </p>

              {teachPhase === 'idle' && (
                <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={() => setTeachPhase('form')}>
                  + Enseñar skill
                </button>
              )}

              {teachPhase === 'form' && (
                <div className="cv-form-stack">
                  <label className="sr-only" htmlFor="teach-name">Nombre de la skill</label>
                  <input
                    id="teach-name"
                    ref={teachNameRef}
                    className="cv-input"
                    type="text"
                    placeholder='Nombre de la skill (p. ej. "Publicar en LinkedIn")'
                    autoComplete="off"
                  />
                  <label className="sr-only" htmlFor="teach-desc">Descripción de la skill</label>
                  <textarea
                    id="teach-desc"
                    ref={teachDescRef}
                    className="cv-textarea"
                    rows={4}
                    placeholder="Describe qué hace y los pasos — el agente aprende la skill de aquí"
                  />
                  <div className="cv-form-actions">
                    <button className="cv-btn cv-btn--primary cv-btn--sm" onClick={handleTeachStart}>Empezar</button>
                    <button className="cv-btn cv-btn--ghost cv-btn--sm" onClick={handleTeachCancel}>Cancelar</button>
                  </div>
                </div>
              )}

              {teachPhase === 'recording' && (
                <div className="cv-form-stack">
                  <p className="teach-recording-label" role="status">
                    ● Grabando la demostración. Cuando termines, pulsa "Crear skill" y el agente la sintetiza.
                  </p>
                  <button className="cv-btn cv-btn--secondary cv-btn--sm" onClick={handleTeachStop}>
                    Crear skill
                  </button>
                </div>
              )}

              {teachPhase === 'synth' && (
                <p className="state-label" aria-live="polite" aria-busy="true">
                  Creando la skill con IA…
                </p>
              )}
            </div>
          </details>
        </section>
      </div>
    </>
  )
}

// ── Installed skill row ───────────────────────────────────────────────────────

function stateMeta(state: string) {
  const s = state.toLowerCase()
  if (s.includes('autonom')) return { label: 'Autónoma (sin tu permiso)', cls: 'is-autonomous' }
  if (s.includes('deprec')) return { label: 'Obsoleta', cls: 'is-deprecated' }
  if (s.includes('valid')) return { label: 'Validada', cls: 'is-validated' }
  return { label: state, cls: '' }
}

interface SkillRowProps {
  skill: Skill
  onPromote: () => void
  onUninstall: () => void
}

function SkillRow({ skill, onPromote, onUninstall }: SkillRowProps) {
  const name = skill.skill_name ?? skill.name ?? skill.slug ?? ''
  const meta = stateMeta(skill.state ?? '')
  const version = skill.version ? `v${skill.version}` : ''
  const surfaces = Array.isArray(skill.surface_kinds)
    ? skill.surface_kinds.join(' · ')
    : (skill.surface_kinds ?? '')
  const sub = [version, surfaces].filter(Boolean).join(' · ')
  const isValidated = (skill.state ?? '').toLowerCase().includes('valid')

  return (
    <div className="skill-row">
      <div className="skill-row__info">
        <div className="skill-row__name">{name}</div>
        {sub && <div className="skill-row__desc">{sub}</div>}
      </div>
      <div className="skill-row__actions">
        {meta.label && (
          <span className={`skill-state-chip ${meta.cls}`}>{meta.label}</span>
        )}
        {isValidated && (
          <button
            className="cv-btn cv-btn--primary cv-btn--sm"
            onClick={onPromote}
            aria-label="Activar modo autónomo para esta habilidad"
          >
            Activar modo autónomo
          </button>
        )}
        <button
          className="cv-btn cv-btn--ghost cv-btn--sm cv-btn--danger"
          onClick={onUninstall}
          aria-label={`Desinstalar ${name}`}
        >
          ✕
        </button>
      </div>
    </div>
  )
}

// ── Hub result row ────────────────────────────────────────────────────────────

const TRUST_TONE: Record<string, string> = { official: 'ok', verified: 'ok', community: 'warn', unknown: '' }

interface HubResultRowProps {
  item: HubSkillResult
  installedNames: Set<string>
  onInstall: (item: HubSkillResult, onBtnUpdate: (s: 'installing' | 'installed' | 'ready') => void) => void
}

function HubResultRow({ item, installedNames, onInstall }: HubResultRowProps) {
  const [btnState, setBtnState] = useState<'ready' | 'installing' | 'installed'>('ready')
  const name = item.name ?? item.identifier ?? item.slug ?? ''
  const already = installedNames.has(name) || installedNames.has(item.identifier ?? '')
  const docUrl = skillDocUrl(item)
  const trust = item.trust_level ?? ''
  const tone = TRUST_TONE[trust.toLowerCase()] ?? ''

  return (
    <div className="skill-hub-result">
      <div className="skill-hub-result__info">
        <div className="skill-hub-result__name">
          {name}
          {trust && (
            <span className={`hub-badge${tone ? ` hub-badge--${tone}` : ''}`}>{trust}</span>
          )}
          {item.source && <span className="hub-badge">{item.source}</span>}
        </div>
        {item.description && (
          <div className="skill-hub-result__desc">{item.description}</div>
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
    </div>
  )
}

// ── Toast list ────────────────────────────────────────────────────────────────
