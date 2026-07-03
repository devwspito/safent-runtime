/**
 * ContextPanel — collapsible right-side panel in ChatView.
 * Mirrors vanilla context-panel.js: workspace files / skills / connected apps.
 * Three sources are fetched in parallel; each failing independently so a
 * 503 from Composio (not yet configured) never blanks the other sections.
 */

import { useCallback, useEffect, useId, useState } from 'react'
import { useT } from '../lib/i18n'
import {
  listWorkspaceFiles,
  listSkills,
  listComposioConnected,
} from '../api/client'
import type { WorkspaceFile, Skill, ComposioApp } from '../api/types'

// ── Icons ─────────────────────────────────────────────────────────────────────

function ChevronDownIcon({ rotated }: { rotated: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
      style={{
        transition: 'transform 200ms ease',
        transform: rotated ? 'rotate(-90deg)' : 'none',
        flexShrink: 0,
      }}
    >
      <path
        d="M2 4l4 4 4-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <path
        d="M3 3l8 8M11 3l-8 8"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

function FileIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path
        d="M3 1h6l3 3v9a1 1 0 01-1 1H3a1 1 0 01-1-1V2a1 1 0 011-1z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path d="M9 1v3h3" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true" style={{ flexShrink: 0, opacity: 0.5 }}>
      <path
        d="M6 2v6M3 6l3 3 3-3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M2 10h8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

function SkillIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      <polygon
        points="8,1 10,6 15,6 11,9 13,14 8,11 3,14 5,9 1,6 6,6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function GlobeIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.3" />
      <ellipse cx="8" cy="8" rx="2.5" ry="6" stroke="currentColor" strokeWidth="1.3" />
      <path d="M2 8h12" stroke="currentColor" strokeWidth="1.3" />
    </svg>
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
        <ChevronDownIcon rotated={!open} />
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
    return <p className="ctx-empty">{t('ctx.files.empty')}</p>
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
            <FileIcon />
            <span className="ctx-file-row__name">{truncate(f.name, 28)}</span>
            <DownloadIcon />
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
    return <p className="ctx-empty">{t('ctx.skills.empty')}</p>
  }

  return (
    <ul className="ctx-list" role="list" aria-label={t('ctx.skills.aria')}>
      {skills.map((s, i) => {
        const name = s.name ?? s.slug ?? s.skill_name ?? t('ctx.skill_fallback')
        return (
          <li key={s.package_id ?? s.skill_id ?? i} className="ctx-tag-row">
            <SkillIcon />
            <span className="ctx-tag-row__name">{name}</span>
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
          <GlobeIcon />
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
          <CloseIcon />
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
