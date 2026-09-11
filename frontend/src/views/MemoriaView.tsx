/**
 * MemoriaView — agent long-term memory browser.
 *
 * - List: GET /memory (rows with truncated content)
 * - Full content: GET /memory/{entry_id}  (entry_id = "{target}:{entry_index}")
 * - Delete: DELETE /memory/{id}
 * - Search: GET /memory/search?q=
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { sileo } from 'sileo'
import { AlertTriangle, Brain, CalendarClock, ChevronRight, Save, Search, Trash2 } from 'lucide-react'
import { useT } from '../lib/i18n'
import { listMemory, searchMemory, forgetMemoryItem, getMemoryEntry, updateMemoryEntry, ApiError } from '../api/client'
import type { MemoryItem, MemoryEntryDetail } from '../api/types'
import { Drawer } from '../components/ui/Drawer'
import { EmptyState } from '../components/ui/EmptyState'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import {
  AnimatePresence,
  AnimatedListItem,
  FadeIn,
  Stagger,
  StaggerItem,
  HoverRow,
  motion,
  TWEEN,
} from '../components/ui/motion'
import styles from './MemoriaView.module.css'

// ── State machine ─────────────────────────────────────────────────────────────

type MemoryState =
  | { status: 'loading' }
  | { status: 'success'; items: MemoryItem[]; query: string }
  | { status: 'error'; message: string }

// `status` tracks the full-content fetch for the item currently open in the
// drawer. 'error' is distinct from a successful fetch that legitimately has
// no full detail (id-less items keep the truncated preview): a failed fetch
// must never leave the truncated list text silently editable/saveable, or a
// save would overwrite the stored full entry with a truncated copy.
type DrawerState =
  | { open: false }
  | { open: true; item: MemoryItem; status: 'loading' }
  | { open: true; item: MemoryItem; status: 'error' }
  | { open: true; item: MemoryItem; status: 'ready'; detail: MemoryEntryDetail | null }

// ── Helpers ───────────────────────────────────────────────────────────────────

function memoryContent(item: MemoryItem): string {
  return String(item.content_truncated ?? item.content ?? item.text ?? '').trim()
}

function formatDate(iso?: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString('es', {
    day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

function entryId(item: MemoryItem): string {
  if (item.id) return item.id
  const target = item.target ?? ''
  const idx = item.entry_index ?? 0
  return target ? `${target}:${idx}` : ''
}

// ── Skeleton rows (mirrors final item layout) ─────────────────────────────────

function MemorySkeletonRows() {
  const t = useT()
  return (
    <div className={styles.skeletonList} aria-busy="true" aria-label={t('memoria.loading_aria')}>
      {[80, 65, 72, 55].map((w, i) => (
        <div
          key={i}
          className={styles.skeletonRow}
          style={{ animationDelay: `${i * 40}ms` }}
        >
          <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
            <div className={`skeleton ${styles.skeletonIcon}`} />
            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div
                className="skeleton skeleton--line"
                style={{ width: `${w}%` }}
              />
              <div
                className="skeleton skeleton--line-sm"
                style={{ width: '40%' }}
              />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Memory row ────────────────────────────────────────────────────────────────

interface MemoryRowProps {
  item: MemoryItem
  index: number
  onClick: () => void
}

function MemoryRow({ item, index, onClick }: MemoryRowProps) {
  const t = useT()
  const content = memoryContent(item)
  const time = formatDate(item.created_at)
  const rowLabel = t('memoria.row.aria').replace('{n}', String(index + 1))

  return (
    <HoverRow
      className={`memory-item ${styles.memItem}`}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } }}
      aria-label={`${rowLabel}${item.target ? ` — ${item.target}` : ''}`}
    >
      {/* Left icon chip */}
      <span className={styles.memIcon} aria-hidden="true">
        <Brain size={13} />
      </span>

      {/* Body */}
      <div className={styles.memBody}>
        <p className={styles.memContent}>{content || t('memoria.content.empty')}</p>
        {(item.target || time) && (
          <div className={styles.memMeta}>
            {item.target && (
              <span className={styles.memTarget}>{item.target}</span>
            )}
            {time && (
              <span className={styles.memTime}>{time}</span>
            )}
          </div>
        )}
      </div>

      {/* Chevron affordance */}
      <ChevronRight
        size={13}
        className={styles.memChevron}
        aria-hidden="true"
      />
    </HoverRow>
  )
}

// ── MemoriaView ───────────────────────────────────────────────────────────────

export default function MemoriaView() {
  const t = useT()
  const [state, setState] = useState<MemoryState>({ status: 'loading' })
  const [searchInput, setSearchInput] = useState('')
  const [drawer, setDrawer] = useState<DrawerState>({ open: false })
  const [deleting, setDeleting] = useState(false)
  const [editValue, setEditValue] = useState('')
  const [saving, setSaving] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // List load/search generation guard: an older in-flight request (e.g. the
  // initial "recent" load) must never overwrite a newer one (a fast search
  // typed right after) when it resolves out of order.
  const loadEpochRef = useRef(0)

  const load = useCallback(async (query = '') => {
    const epoch = ++loadEpochRef.current
    setState({ status: 'loading' })
    try {
      const raw = query ? await searchMemory(query) : await listMemory()
      if (loadEpochRef.current !== epoch) return
      const items = Array.isArray(raw) ? raw : []
      setState({ status: 'success', items, query })
    } catch (e) {
      if (loadEpochRef.current !== epoch) return
      const msg = e instanceof ApiError ? e.message : t('memoria.err.load')
      setState({ status: 'error', message: msg })
      sileo.error({ title: msg })
    }
  }, [])

  useEffect(() => { void load() }, [load])

  function handleSearch() {
    void load(searchInput.trim())
  }

  function handleRetry() {
    setSearchInput('')
    void load('')
  }

  // Drawer selection generation guard: bumped whenever the drawer opens on a
  // (possibly different) item or closes, so a late detail/save response for a
  // no-longer-selected item can never write into whatever is on screen now.
  const drawerEpochRef = useRef(0)

  async function openDrawer(item: MemoryItem) {
    const epoch = ++drawerEpochRef.current
    setEditValue(memoryContent(item))
    const id = entryId(item)
    if (!id) {
      setDrawer({ open: true, item, status: 'ready', detail: null })
      return
    }
    setDrawer({ open: true, item, status: 'loading' })
    try {
      const detail = await getMemoryEntry(id)
      if (drawerEpochRef.current !== epoch) return
      setDrawer({ open: true, item, status: 'ready', detail })
      // Load the FULL content into the editor (list rows are truncated).
      setEditValue(detail.content ?? memoryContent(item))
    } catch {
      if (drawerEpochRef.current !== epoch) return
      setDrawer({ open: true, item, status: 'error' })
    }
  }

  function closeDrawer() {
    drawerEpochRef.current += 1
    setDrawer({ open: false })
    setEditValue('')
  }

  function retryDrawer() {
    if (!drawer.open) return
    void openDrawer(drawer.item)
  }

  async function handleSave() {
    if (!drawer.open || drawer.status !== 'ready') return
    const item = drawer.item
    const id = entryId(item)
    if (!id) { sileo.error({ title: t('memoria.err.no_edit') }); return }
    const next = editValue.trim()
    if (!next) { sileo.warning({ title: t('memoria.err.empty_content') }); return }
    const epoch = drawerEpochRef.current
    setSaving(true)
    try {
      await updateMemoryEntry(id, next)
      sileo.success({ title: t('memoria.toast.saved') })
      // Only reflect the saved value in the drawer if the selection has not
      // moved on to a different item while the request was in flight.
      if (drawerEpochRef.current === epoch) {
        setDrawer(prev => prev.open && prev.status === 'ready'
          ? { ...prev, detail: prev.detail ? { ...prev.detail, content: next } : prev.detail }
          : prev)
      }
      void load(searchInput.trim())
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : t('memoria.err.save') })
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    if (!drawer.open) return
    const item = drawer.item
    const id = entryId(item)
    if (!id) { sileo.error({ title: t('memoria.err.no_delete') }); return }
    setDeleting(true)
    try {
      await forgetMemoryItem(id)
      sileo.success({ title: t('memoria.toast.deleted') })
      closeDrawer()
      void load(searchInput.trim())
    } catch (e) {
      sileo.error({ title: e instanceof Error ? e.message : t('memoria.err.delete') })
    } finally {
      setDeleting(false)
    }
  }

  const isSuccess = state.status === 'success'
  const activeQuery = isSuccess ? state.query : ''
  const itemCount = isSuccess ? state.items.length : 0

  // The list key changes when the query changes so AnimatePresence fires a
  // cross-fade between the old and new result sets.
  const listKey = isSuccess ? `q:${state.query}` : '__loading__'

  const drawerReady = drawer.open && drawer.status === 'ready' ? drawer : null
  const savedContent = drawerReady ? (drawerReady.detail?.content ?? memoryContent(drawerReady.item)) : ''
  const canSave = drawerReady !== null && Boolean(entryId(drawerReady.item))

  return (
    <>
      <PageHeader
        title={t('view.memoria')}
        subtitle={t('memoria.subtitle')}
      />

      <div className="view-body cv-view-body">
        <Stagger style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>

          {/* ── Search ──────────────────────────────────────────────────────── */}
          <StaggerItem>
            <div className={styles.searchPanel} role="search" aria-label={t('memoria.search.aria')}>
              <div className={styles.searchRow}>
                <div className={styles.searchInputWrap}>
                  <Search
                    size={13}
                    aria-hidden="true"
                    className={styles.searchIcon}
                  />
                  <label className="sr-only" htmlFor="memory-search">{t('memoria.search.aria')}</label>
                  <input
                    id="memory-search"
                    ref={inputRef}
                    className={styles.searchInput}
                    type="search"
                    placeholder={t('memoria.search.placeholder')}
                    autoComplete="off"
                    value={searchInput}
                    onChange={e => setSearchInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleSearch() }}
                  />
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleSearch}
                  loading={state.status === 'loading'}
                >
                  {t('memoria.search.btn')}
                </Button>
              </div>
            </div>
          </StaggerItem>

          {/* ── Results ─────────────────────────────────────────────────────── */}
          <StaggerItem>
            <section className={styles.resultsSection} aria-label={t('memoria.results.aria')}>

              {/* Section header */}
              <div className={styles.sectionHead}>
                <h2 className={styles.sectionLabel}>
                  {activeQuery ? t('memoria.results.for').replace('{query}', activeQuery) : t('memoria.results.recent')}
                </h2>
                {isSuccess && itemCount > 0 && (
                  <span className={styles.countChip}>{itemCount}</span>
                )}
              </div>

              {/* Loading skeleton */}
              {state.status === 'loading' && <MemorySkeletonRows />}

              {/* Error state */}
              {state.status === 'error' && (
                <FadeIn>
                  <div role="alert" className={styles.errorState}>
                    <p className={styles.errorMessage}>{state.message}</p>
                    <Button variant="secondary" size="sm" onClick={handleRetry}>
                      {t('memoria.retry')}
                    </Button>
                  </div>
                </FadeIn>
              )}

              {/* AnimatePresence mode="wait" cross-fades between search result sets */}
              <AnimatePresence mode="wait">
                {isSuccess && (
                  <motion.div
                    key={listKey}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={TWEEN}
                  >
                    {state.items.length === 0 && (
                      <EmptyState
                        icon={<Brain size={36} />}
                        title={activeQuery
                          ? t('memoria.empty.noresults').replace('{query}', activeQuery)
                          : t('memoria.empty.title')}
                        description={activeQuery
                          ? undefined
                          : t('memoria.empty.desc')}
                      />
                    )}

                    {state.items.length > 0 && (
                      <ul className="cv-list memory-list" role="list" style={{ gap: 'var(--space-2)' }}>
                        <AnimatePresence initial={false}>
                          {state.items.map((item, i) => (
                            <AnimatedListItem key={item.id ?? i}>
                              <MemoryRow
                                item={item}
                                index={i}
                                onClick={() => void openDrawer(item)}
                              />
                            </AnimatedListItem>
                          ))}
                        </AnimatePresence>
                      </ul>
                    )}
                  </motion.div>
                )}
              </AnimatePresence>

            </section>
          </StaggerItem>

        </Stagger>
      </div>

      {/* ── Full-content drawer ──────────────────────────────────────────────── */}
      <Drawer
        open={drawer.open}
        title={drawer.open && drawer.item.target ? drawer.item.target : t('memoria.drawer.title_default')}
        onClose={closeDrawer}
        footer={
          drawer.open ? (
            <div className={styles.drawerFooter}>
              <Button
                variant="primary"
                size="sm"
                onClick={handleSave}
                loading={saving}
                disabled={
                  !canSave ||
                  deleting ||
                  !editValue.trim() ||
                  editValue.trim() === savedContent.trim()
                }
                aria-label={t('memoria.drawer.save.aria')}
              >
                <Save size={13} aria-hidden="true" />
                {t('memoria.drawer.save')}
              </Button>
              <div className={styles.drawerFooterSpacer} />
              <Button
                variant="danger"
                size="sm"
                onClick={handleDelete}
                loading={deleting}
                disabled={saving}
                aria-label={t('memoria.drawer.delete.aria')}
              >
                <Trash2 size={13} aria-hidden="true" />
                {t('memoria.drawer.delete')}
              </Button>
            </div>
          ) : undefined
        }
      >
        {drawer.open && (
          <div className={styles.drawerContent}>

            {/* Entry metadata */}
            {(drawer.item.target || drawer.item.created_at) && (
              <div className={styles.drawerMeta}>
                {drawer.item.target && (
                  <>
                    <p className={styles.drawerTargetLabel}>{t('memoria.drawer.context')}</p>
                    <p className={styles.drawerTargetValue}>{drawer.item.target}</p>
                  </>
                )}
                {drawer.item.created_at && (
                  <div className={styles.drawerDateRow}>
                    <CalendarClock size={11} aria-hidden="true" />
                    <span>{formatDate(drawer.item.created_at)}</span>
                  </div>
                )}
              </div>
            )}

            {/* Content body — loading / load error / editable */}
            {drawer.status === 'loading' && (
              <div className={styles.drawerLoadingWrap}>
                <Spinner size={14} label={t('memoria.drawer.loading')} />
                <div className="skeleton skeleton--line" style={{ width: '90%' }} />
                <div className="skeleton skeleton--line" style={{ width: '75%' }} />
                <div className="skeleton skeleton--line-sm" style={{ width: '55%' }} />
              </div>
            )}

            {drawer.status === 'error' && (
              <FadeIn>
                <div role="alert" className={styles.errorState}>
                  <AlertTriangle size={16} aria-hidden="true" />
                  <p className={styles.errorMessage}>{t('memoria.drawer.load_error')}</p>
                  <Button variant="secondary" size="sm" onClick={retryDrawer}>
                    {t('memoria.retry')}
                  </Button>
                </div>
              </FadeIn>
            )}

            {drawer.status === 'ready' && (
              <FadeIn>
                <label className="sr-only" htmlFor="memory-edit">{t('memoria.drawer.edit.label')}</label>
                <textarea
                  id="memory-edit"
                  className={styles.editArea}
                  value={editValue}
                  onChange={e => setEditValue(e.target.value)}
                  spellCheck={false}
                  readOnly={!canSave}
                  aria-readonly={!canSave}
                  aria-label={t('memoria.drawer.edit.aria')}
                  placeholder={t('memoria.drawer.edit.placeholder')}
                />
                <p className={styles.editHint}>
                  {canSave
                    ? <>{t('memoria.drawer.hint_pre')} <strong>{t('memoria.drawer.save')}</strong>{t('memoria.drawer.hint_post')}</>
                    : t('memoria.drawer.preview_only')}
                </p>
              </FadeIn>
            )}

          </div>
        )}
      </Drawer>
    </>
  )
}
