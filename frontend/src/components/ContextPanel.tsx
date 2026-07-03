/**
 * ContextPanel — collapsible right-side panel in ChatView.
 * Mirrors vanilla context-panel.js: workspace files / skills / connected apps.
 * Three sources are fetched in parallel; each failing independently so a
 * 503 from Composio (not yet configured) never blanks the other sections.
 */

import { useCallback, useEffect, useId, useState } from 'react'
import { ChevronDown, X, File, Download, Zap, Globe } from 'lucide-react'
import { useT } from '../lib/i18n'
import {
  listWorkspaceFiles,
  listSkills,
  listComposioConnected,
} from '../api/client'
import type { WorkspaceFile, Skill, ComposioApp } from '../api/types'
import { EmptyState } from './ui/EmptyState'
import { isLiveSkill } from '../lib/skills'

// ── Icons ─────────────────────────────────────────────────────────────────────
// lucide-react, matching the iconography used across the rest of the app.

function SectionChevron({ open }: { open: boolean }) {
  return (
    <ChevronDown
      size={12}
      aria-hidden="true"
      style={{
        transition: 'transform 200ms ease',
        transform: open ? 'none' : 'rotate(-90deg)',
        flexShrink: 0,
      }}
    />
  )
}

// ── Section component with collapse ──────────────────────────────────────────

interface PanelSectionProps {
  title: string
  children: React.ReactNode
  defaultOpen?: boolean
}

function PanelSection({ title, children, defaultOpen = true }: PanelSectionProps) {
  const [open, setOpen] = useState(defaultOpen)
  const bodyId = useId()

  return (
    <div className="ctx-panel-section">
      <button
        className="ctx-panel-section__toggle"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
        type="button"
      >
        <span className="ctx-panel-section__title">{title}</span>
        <SectionChevron open={open} />
      </button>
      <div id={bodyId} hidden={!open}>
        {children}
      </div>
    </div>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n) + '…' : s
}

function fileDownloadUrl(path: string) {
  // Use the full-path download endpoint (the legacy /workspace/file/{name} only
  // served root-level files → 404 for anything in a subdirectory).
  return `/api/v1/workspace/download?path=${encodeURIComponent(path)}`
}

// ── Sub-section renderers ─────────────────────────────────────────────────────

interface FilesListProps {
  files: WorkspaceFile[]
  loading: boolean
}

function FilesList({ files, loading }: FilesListProps) {
  const t = useT()
  if (loading) {
    return (
      <ul className="ctx-list" aria-label={t('ctx.files.aria')}>
        {[0, 1, 2].map(i => (
          <li key={i} className="ctx-file-row ctx-file-row--skeleton" aria-hidden="true" />
        ))}
      </ul>
    )
  }

  if (files.length === 0) {
    return <EmptyState compact icon={<File size={28} />} title={t('ctx.files.empty')} />
  }

  return (
    <ul className="ctx-list" role="list" aria-label={t('ctx.files.aria')}>
      {files.map(f => (
        <li key={f.name} className="ctx-file-row">
          <a
            href={fileDownloadUrl(f.path)}
            download={f.name}
            title={t('ctx.files.download').replace('{name}', f.name)}
            aria-label={t('ctx.files.download').replace('{name}', f.name)}
            className="ctx-file-row__link"
            rel="noopener"
          >
            <File size={13} aria-hidden="true" style={{ flexShrink: 0 }} />
            <span className="ctx-file-row__name">{truncate(f.name, 28)}</span>
            <Download size={11} aria-hidden="true" style={{ flexShrink: 0, opacity: 0.5 }} />
          </a>
        </li>
      ))}
    </ul>
  )
}

interface SkillsListProps {
  skills: Skill[]
  loading: boolean
}

function SkillsList({ skills, loading }: SkillsListProps) {
  const t = useT()
  if (loading) return <div className="ctx-skeleton" aria-hidden="true" />

  if (skills.length === 0) {
    return <EmptyState compact icon={<Zap size={28} />} title={t('ctx.skills.empty')} />
  }

  return (
    <ul className="ctx-list" role="list" aria-label={t('ctx.skills.aria')}>
      {skills.map((s, i) => {
        const name = s.name ?? s.slug ?? s.skill_name ?? t('ctx.skill_fallback')
        return (
          <li key={s.package_id ?? s.skill_id ?? i} className="ctx-tag-row">
            <Zap size={12} aria-hidden="true" style={{ flexShrink: 0 }} />
            <span className="ctx-tag-row__name">{name}</span>
            {isLiveSkill(s) && (
              <span className="ctx-live-tag" title={t('skills.live.tip')}>
                {t('skills.live.badge')}
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

interface ConnectorsListProps {
  connected: ComposioApp[]
  loading: boolean
}

function ConnectorsList({ connected, loading }: ConnectorsListProps) {
  const t = useT()
  if (loading) return <div className="ctx-skeleton" aria-hidden="true" />

  const builtIn: { name: string; slug: string }[] = [{ name: t('ctx.web_search'), slug: 'web_search' }]
  const all = [...builtIn, ...connected.map(c => ({ name: c.name ?? c.slug, slug: c.slug }))]

  return (
    <ul className="ctx-list" role="list" aria-label={t('ctx.connectors.aria')}>
      {all.map(c => (
        <li key={c.slug} className="ctx-tag-row">
          <Globe size={12} aria-hidden="true" style={{ flexShrink: 0 }} />
          <span className="ctx-tag-row__name">{c.name}</span>
        </li>
      ))}
    </ul>
  )
}

// ── ContextPanel ──────────────────────────────────────────────────────────────

interface ContextPanelProps {
  onClose: () => void
}

export default function ContextPanel({ onClose }: ContextPanelProps) {
  const t = useT()
  const [files, setFiles] = useState<WorkspaceFile[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [connected, setConnected] = useState<ComposioApp[]>([])
  const [filesLoading, setFilesLoading] = useState(true)
  const [skillsLoading, setSkillsLoading] = useState(true)
  const [connectorsLoading, setConnectorsLoading] = useState(true)

  const loadAll = useCallback(() => {
    setFilesLoading(true)
    setSkillsLoading(true)
    setConnectorsLoading(true)

    // Each source is independent — one failing must not blank the others.
    listWorkspaceFiles()
      .then(data => setFiles(Array.isArray(data) ? data : []))
      .finally(() => setFilesLoading(false))

    listSkills()
      .then(data => setSkills(Array.isArray(data) ? data : []))
      .finally(() => setSkillsLoading(false))

    // Composio 503s on a fresh install — catch silently so the panel still renders
    listComposioConnected()
      .catch(() => [])
      .then(data => setConnected(Array.isArray(data) ? data : []))
      .finally(() => setConnectorsLoading(false))
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  return (
    <aside className="ctx-panel" aria-label={t('ctx.panel.aria')}>
      <div className="ctx-panel-header">
        <span className="ctx-panel-title">{t('ctx.panel.title')}</span>
        <button
          className="ctx-panel-close"
          onClick={onClose}
          aria-label={t('ctx.panel.close.aria')}
          type="button"
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>

      <div className="ctx-panel-body">
        <PanelSection title={t('ctx.section.workspace')}>
          <FilesList files={files} loading={filesLoading} />
        </PanelSection>

        <PanelSection title={t('ctx.section.skills')}>
          <SkillsList skills={skills} loading={skillsLoading} />
        </PanelSection>

        <PanelSection title={t('ctx.section.connectors')}>
          <ConnectorsList connected={connected} loading={connectorsLoading} />
        </PanelSection>
      </div>
    </aside>
  )
}
