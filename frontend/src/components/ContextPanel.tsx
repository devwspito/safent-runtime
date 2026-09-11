import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { AlertCircle, ChevronDown, Download, File, Folder, Globe, RefreshCw, X, Zap } from 'lucide-react'
import { listComposioConnected, listSkills, listWorkspaceFiles, workspaceDownloadUrl } from '../api/client'
import type { ComposioApp, Skill, WorkspaceFile } from '../api/types'
import { useT } from '../lib/i18n'
import { isLiveSkill } from '../lib/skills'
import { useContextSource } from '../hooks/useContextSource'
import styles from './ContextPanel.module.css'

const validFile = (file: WorkspaceFile) => Boolean(file && typeof file.name === 'string' && typeof file.path === 'string' && file.path)
const validSkill = (skill: Skill) => Boolean(skill && [skill.name, skill.slug, skill.skill_name, skill.skill_id, skill.package_id].some(value => typeof value === 'string' && value) && [skill.name, skill.slug, skill.skill_name].every(value => value == null || typeof value === 'string'))
const validConnector = (app: ComposioApp) => Boolean(app && typeof app.slug === 'string' && app.slug && (app.name == null || typeof app.name === 'string'))

function PanelSection({ title, count, loading, error, retry, children }: {
  title: string; count: number | null; loading: boolean; error: boolean; retry: () => void; children: ReactNode
}) {
  const t = useT()
  const [open, setOpen] = useState(true)
  const bodyId = useId()
  return <section className={styles.section}>
    <button type="button" className={styles.toggle} aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(value => !value)}>
      <ChevronDown size={13} aria-hidden="true" style={{ transform: open ? undefined : 'rotate(-90deg)' }} />
      <span className={styles.name}>{title}</span>
      {error && <AlertCircle size={12} aria-label={t('ctx.source.unverified')} />}
      <span className={styles.count}>{count ?? '—'}</span>
    </button>
    <div id={bodyId} hidden={!open} className={styles.sectionBody}>
      {error && <div role="status" className={styles.error}>
        <span>{t(count === null ? 'ctx.source.error' : 'ctx.source.stale')}</span>
        <button type="button" disabled={loading} onClick={retry} aria-label={t('ctx.source.retry').replace('{source}', title)}>{t('seg.policies.retry')}</button>
      </div>}
      {loading && <p role="status" className={styles.hint}>{t(count === null ? 'ctx.source.loading' : 'ctx.source.refreshing')}</p>}
      {children}
    </div>
  </section>
}

/** Read-only instance inventory, not the attachments selected in a conversation. */
export default function ContextPanel({ onClose, busy = false }: { onClose: () => void; busy?: boolean }) {
  const t = useT()
  const files = useContextSource(listWorkspaceFiles, validFile)
  const skills = useContextSource(listSkills, validSkill)
  const connectors = useContextSource(listComposioConnected, validConnector)
  const closeButton = useRef<HTMLButtonElement>(null)
  const wasBusy = useRef(busy)
  useEffect(() => {
    const previous = document.activeElement
    closeButton.current?.focus()
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus() }
  }, [])
  const refreshFiles = files.refresh
  useEffect(() => {
    if (busy) {
      wasBusy.current = true
      const timer = setInterval(() => { void refreshFiles(true) }, 5000)
      return () => clearInterval(timer)
    }
    if (wasBusy.current) { wasBusy.current = false; void refreshFiles() }
  }, [busy, refreshFiles])
  function refreshAll() { void files.refresh(); void skills.refresh(); void connectors.refresh() }
  return <aside className={styles.panel} aria-label={t('ctx.panel.aria')} onKeyDown={event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose() }
  }}>
    <header className={styles.header}>
      <h2>{t('ctx.panel.title')}</h2>
      <button className={styles.iconButton} type="button" aria-label={t('ctx.source.refresh_all')} title={t('ctx.source.refresh_all')} onClick={refreshAll} disabled={files.loading || skills.loading || connectors.loading}><RefreshCw size={14} aria-hidden="true" /></button>
      <button ref={closeButton} className={styles.iconButton} type="button" aria-label={t('ctx.panel.close.aria')} onClick={onClose}><X size={15} aria-hidden="true" /></button>
    </header>
    <div className={styles.body}>
      <p className={styles.scope}>{t('ctx.source.scope')}</p>
      <PanelSection title={t('ctx.section.workspace')} count={files.data?.length ?? null} loading={files.loading} error={files.error} retry={() => { void files.refresh() }}>
        {files.data?.length === 0 && <p className={styles.hint}>{t('ctx.files.empty')}</p>}
        <ul className={styles.list} aria-label={t('ctx.files.aria')}>
          {files.data?.map(file => <li key={file.path}>
            {file.is_dir || file.kind === 'directory' ? <div className={styles.row}><Folder size={14} aria-hidden="true" /><span className={styles.name} title={file.name}>{file.name}</span><span className={styles.meta}>{t('ctx.source.folder')}</span></div>
              : <a className={styles.row} href={workspaceDownloadUrl(file.path)} download={file.name} title={file.name} aria-label={t('ctx.files.download').replace('{name}', file.name)}><File size={14} aria-hidden="true" /><span className={styles.name}>{file.name}</span><Download size={12} aria-hidden="true" /></a>}
          </li>)}
        </ul>
      </PanelSection>
      <PanelSection title={t('ctx.section.skills')} count={skills.data?.length ?? null} loading={skills.loading} error={skills.error} retry={() => { void skills.refresh() }}>
        {skills.data?.length === 0 && <p className={styles.hint}>{t('ctx.skills.empty')}</p>}
        <ul className={styles.list} aria-label={t('ctx.skills.aria')}>
          {skills.data?.map((skill, index) => {
            const name = skill.name ?? skill.slug ?? skill.skill_name ?? skill.skill_id ?? skill.package_id ?? t('ctx.skill_fallback')
            return <li className={styles.row} key={skill.package_id ?? skill.skill_id ?? index}><Zap size={13} aria-hidden="true" /><span className={styles.name} title={name}>{name}</span>{isLiveSkill(skill) && <span className={styles.meta} title={t('skills.live.tip')}>{t('skills.live.badge')}</span>}</li>
          })}
        </ul>
      </PanelSection>
      <PanelSection title={t('ctx.section.connectors')} count={connectors.data?.length ?? null} loading={connectors.loading} error={connectors.error} retry={() => { void connectors.refresh() }}>
        {connectors.data?.length === 0 && <p className={styles.hint}>{t('ctx.source.no_connectors')}</p>}
        <ul className={styles.list} aria-label={t('ctx.connectors.aria')}>
          {connectors.data?.map(app => <li key={app.slug} className={styles.row}><Globe size={13} aria-hidden="true" /><span className={styles.name} title={app.name ?? app.slug}>{app.name ?? app.slug}</span></li>)}
        </ul>
      </PanelSection>
    </div>
  </aside>
}
