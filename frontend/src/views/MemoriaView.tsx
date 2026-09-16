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
import { Brain, CalendarClock, ChevronRight, Save, Search, Trash2 } from 'lucide-react'
import { useT } from '../lib/i18n'
import { listMemory, searchMemory, forgetMemoryItem, getMemoryEntry, updateMemoryEntry, ApiError } from '../api/client'
import type { MemoryItem, MemoryEntryDetail } from '../api/types'
import { Drawer } from '../components/ui/Drawer'
import { EmptyState } from '../components/ui/EmptyState'
import { PageHeader } from '../components/ui/PageHeader'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import styles from './MemoriaView.module.css'

// ── State machine ─────────────────────────────────────────────────────────────

type MemoryState =
  | { status: 'loading' }
  | { status: 'success'; items: MemoryItem[]; query: string }
  | { status: 'error'; message: string }

type DrawerState =
  | { open: false }
  | { open: true; item: MemoryItem; detail: MemoryEntryDetail | null; loading: boolean; error?: boolean }

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
  if (typeof item.id === 'string' && item.id.trim()) return item.id
  const target = typeof item.target === 'string' ? item.target : ''
  const idx = item.entry_index
  return target && typeof idx === 'number' && Number.isInteger(idx) && idx >= 0 ? `${target}:${idx}` : ''
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
    <div
      className={styles.memItem}
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
    </div>
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
  const listRevision = useRef(0)
  const detailRevision = useRef(0)
  const alive = useRef(true)
  const mutationId = useRef<string | null>(null)
  const mutationSettled = useRef<Promise<unknown> | null>(null)
  const lastQuery = useRef('')

  const load = useCallback(async (query = '') => {
    const revision = ++listRevision.current
    lastQuery.current = query
    setState({ status: 'loading' })
    try {
      const raw = query ? await searchMemory(query) : await listMemory()
      if (revision !== listRevision.current || !alive.current) return
      if (!Array.isArray(raw) || raw.some(item => !item || typeof item !== 'object')) throw new Error('Invalid memory response')
      setState({ status: 'success', items: raw, query })
    } catch (e) {
      if (revision !== listRevision.current || !alive.current) return
      const msg = e instanceof ApiError ? e.message : t('memoria.err.load')
      setState({ status: 'error', message: msg })
      sileo.error({ title: msg })
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    return () => { alive.current = false; listRevision.current++; detailRevision.current++ }
  }, [load])

  function handleSearch() {
    void load(searchInput.trim())
  }

  function handleRetry() {
    void load(lastQuery.current)
  }

  async function openDrawer(item: MemoryItem) {
    const revision = ++detailRevision.current
    setDrawer({ open: true, item, detail: null, loading: true })
    setEditValue('')
    const id = entryId(item)
    if (!id) {
      setDrawer({ open: true, item, detail: null, loading: false, error: true })
      return
    }
    try {
      // Reopening the same entry waits for its already-submitted write before
      // loading authoritative content. Never edit the pre-save snapshot.
      if (mutationId.current === id) await mutationSettled.current?.catch(() => undefined)
      if (revision !== detailRevision.current || !alive.current) return
      const detail = await getMemoryEntry(id)
      if (revision !== detailRevision.current || !alive.current) return
      if (!detail || typeof detail.content !== 'string') throw new Error('Incomplete memory detail')
      setDrawer(prev => prev.open ? { ...prev, detail, loading: false } : prev)
      // Load the FULL content into the editor (list rows are truncated).
      setEditValue(detail.content)
    } catch {
      if (revision !== detailRevision.current || !alive.current) return
      setDrawer(prev => prev.open ? { ...prev, detail: null, loading: false, error: true } : prev)
    }
  }

  function closeDrawer() {
    detailRevision.current++
    setDrawer({ open: false })
    setEditValue('')
  }

  async function handleSave() {
    if (!drawer.open || !drawer.detail || drawer.loading || drawer.error || mutationId.current) return
    const revision = detailRevision.current
    const item = drawer.item
    const id = entryId(item)
    if (!id) { sileo.error({ title: t('memoria.err.no_edit') }); return }
    const next = editValue.trim()
    if (!next) { sileo.warning({ title: t('memoria.err.empty_content') }); return }
    mutationId.current = id
    setSaving(true)
    try {
      const operation = updateMemoryEntry(id, next)
      mutationSettled.current = operation
      const result = await operation
      if (result?.ok !== true || result.updated === false) throw new Error('Memory update was not confirmed')
      if (revision !== detailRevision.current || !alive.current) return
      sileo.success({ title: t('memoria.toast.saved') })
      // Reflect the saved value in the open drawer without a refetch.
      setDrawer(prev => prev.open
        ? { ...prev, detail: prev.detail ? { ...prev.detail, content: next } : prev.detail }
        : prev)
      void load(lastQuery.current)
    } catch (e) {
      if (revision !== detailRevision.current || !alive.current) return
      sileo.error({ title: e instanceof ApiError ? e.message : t('memoria.err.save') })
    } finally {
      mutationId.current = null
      mutationSettled.current = null
      if (alive.current) setSaving(false)
    }
  }

  async function handleDelete() {
    if (!drawer.open || mutationId.current) return
    const revision = detailRevision.current
    const item = drawer.item
    const id = entryId(item)
    if (!id) { sileo.error({ title: t('memoria.err.no_delete') }); return }
    mutationId.current = id
    setDeleting(true)
    try {
      const operation = forgetMemoryItem(id)
      mutationSettled.current = operation
      await operation
      if (revision !== detailRevision.current || !alive.current) return
      sileo.success({ title: t('memoria.toast.deleted') })
      closeDrawer()
      void load(lastQuery.current)
    } catch (e) {
      if (revision !== detailRevision.current || !alive.current) return
      sileo.error({ title: e instanceof Error ? e.message : t('memoria.err.delete') })
    } finally {
      mutationId.current = null
      mutationSettled.current = null
      if (alive.current) setDeleting(false)
    }
  }

  const isSuccess = state.status === 'success'
  const activeQuery = isSuccess ? state.query : ''
  const itemCount = isSuccess ? state.items.length : 0

  // A new query identifies a new result set; keyboard navigation is immediate.
  const listKey = isSuccess ? `q:${state.query}` : '__loading__'

  return (
    <>
      <PageHeader
        title={t('view.memoria')}
        subtitle={t('memoria.subtitle')}
      />

      <div className="view-body cv-view-body">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-6)' }}>

          {/* ── Search ──────────────────────────────────────────────────────── */}
          <div>
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
                    onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) handleSearch() }}
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
          </div>

          {/* ── Results ─────────────────────────────────────────────────────── */}
          <div>
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
                <div>
                  <div role="alert" className={styles.errorState}>
                    <p className={styles.errorMessage}>{state.message}</p>
                    <Button variant="secondary" size="sm" onClick={handleRetry}>
                      {t('memoria.retry')}
                    </Button>
                  </div>
                </div>
              )}

              {/* Results update immediately, including keyboard-driven searches. */}
              <>
                {isSuccess && (
                  <div
                    key={listKey}
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
                        <>
                          {state.items.map((item, i) => (
                            <li key={item.id ?? i}>
                              <MemoryRow
                                item={item}
                                index={i}
                                onClick={() => void openDrawer(item)}
                              />
                            </li>
                          ))}
                        </>
                      </ul>
                    )}
                  </div>
                )}
              </>

            </section>
          </div>

        </div>
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
                loading={saving && mutationId.current === entryId(drawer.item)}
                disabled={
                  drawer.loading ||
                  !drawer.detail || drawer.error || saving ||
                  deleting ||
                  !editValue.trim() ||
                  editValue.trim() === (drawer.detail?.content ?? memoryContent(drawer.item)).trim()
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
                loading={deleting && mutationId.current === entryId(drawer.item)}
                disabled={saving || deleting || !entryId(drawer.item)}
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

            {/* Content body — editable */}
            {drawer.loading ? (
              <div className={styles.drawerLoadingWrap}>
                <Spinner size={14} label={t('memoria.drawer.loading')} />
                <div className="skeleton skeleton--line" style={{ width: '90%' }} />
                <div className="skeleton skeleton--line" style={{ width: '75%' }} />
                <div className="skeleton skeleton--line-sm" style={{ width: '55%' }} />
              </div>
            ) : drawer.error ? (
              <div role="alert" className={styles.errorState}>
                <p>{t('memoria.err.detail')}</p>
                <Button variant="secondary" size="sm" onClick={() => void openDrawer(drawer.item)}>{t('memoria.retry')}</Button>
              </div>
            ) : (
              <div>
                <label className="sr-only" htmlFor="memory-edit">{t('memoria.drawer.edit.label')}</label>
                <textarea
                  id="memory-edit"
                  className={styles.editArea}
                  value={editValue}
                  disabled={saving || deleting}
                  onChange={e => setEditValue(e.target.value)}
                  spellCheck={false}
                  aria-label={t('memoria.drawer.edit.aria')}
                  placeholder={t('memoria.drawer.edit.placeholder')}
                />
                <p className={styles.editHint}>
                  {t('memoria.drawer.hint_pre')} <strong>{t('memoria.drawer.save')}</strong>{t('memoria.drawer.hint_post')}
                </p>
              </div>
            )}

          </div>
        )}
      </Drawer>
    </>
  )
}
