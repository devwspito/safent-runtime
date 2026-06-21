/**
 * context-panel.js — Right sidebar: Progreso checklist, Carpeta de trabajo, Contexto.
 */

import { listWorkspaceFiles, listSkills, listComposioConnected } from './api.js';
import { Icon, fileIcon } from './icons.js';
import { t, onLangChange } from './i18n.js';

function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Workspace files ───────────────────────────────────────────────────────────

function kindToIcon(kind = '', name = '') {
  if (kind === 'xls' || kind === 'xlsx') return Icon.fileXls;
  if (kind === 'doc' || kind === 'docx') return Icon.fileDoc;
  if (kind === 'pdf') return Icon.filePdf;
  if (kind === 'js' || kind === 'ts') return Icon.fileJs;
  if (kind === 'py') return Icon.filePy;
  if (['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(kind)) return Icon.filePng;
  if (kind === 'json') return Icon.fileJson;
  return Icon[fileIcon(name)] ?? Icon.fileGeneric;
}

function renderFileRow(file) {
  const icon = kindToIcon(file.kind, file.name);
  const downloadUrl = `/api/v1/workspace/file/${encodeURIComponent(file.name)}`;
  const el = document.createElement('li');
  el.className = 'file-row';
  el.innerHTML = `
    <a class="file-row__link" href="${downloadUrl}" download="${escapeText(file.name)}" title="Descargar ${escapeText(file.name)}" aria-label="Descargar ${escapeText(file.name)}">
      <span class="file-row__icon" aria-hidden="true">${icon}</span>
      <span class="file-row__name">${escapeText(truncate(file.name, 30))}</span>
      <span class="file-row__dl" aria-hidden="true">${Icon.download}</span>
    </a>`;
  return el;
}

function renderFilesSkeleton() {
  return Array.from({ length: 3 }, () =>
    `<li class="file-row skeleton"><span class="file-row__icon"></span><span class="file-row__name"></span></li>`
  ).join('');
}

// ── Skills ────────────────────────────────────────────────────────────────────

function renderSkill(skill) {
  const name = skill.name ?? skill.slug ?? 'skill';
  return `<li class="context-skill">
    <span class="context-skill__icon">${Icon.skill}</span>
    <span class="context-skill__name">${escapeText(name)}</span>
  </li>`;
}

// ── Connectors ────────────────────────────────────────────────────────────────

function renderConnector(conn) {
  return `<li class="context-connector">
    <span class="context-connector__icon">${Icon.globe}</span>
    <span class="context-connector__name">${escapeText(conn.name ?? conn.slug)}</span>
  </li>`;
}

// ── Section collapse ──────────────────────────────────────────────────────────

function initCollapseButtons() {
  document.querySelectorAll('.panel-section__toggle').forEach(btn => {
    const sectionId = btn.getAttribute('aria-controls');
    const section = document.getElementById(sectionId);
    if (!section) return;
    btn.addEventListener('click', () => {
      const expanded = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', String(!expanded));
      section.hidden = expanded;
      const chevron = btn.querySelector('svg');
      if (chevron) chevron.style.transform = expanded ? 'rotate(-90deg)' : '';
    });
  });
}

// ── Public API ────────────────────────────────────────────────────────────────

function truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }

export async function loadContextPanel() {
  const filesEl = document.getElementById('workspace-files');
  const skillsEl = document.getElementById('context-skills');
  const connectorsEl = document.getElementById('context-connectors');

  if (filesEl) filesEl.innerHTML = renderFilesSkeleton();

  // Each source is independent — one failing (e.g. Composio not yet configured,
  // which 503s on a fresh install) must not blank the rest of the panel.
  const [files, skills, connected] = await Promise.all([
    listWorkspaceFiles().catch(() => []),
    listSkills().catch(() => []),
    listComposioConnected().catch(() => []),
  ]);

  if (filesEl) {
    filesEl.innerHTML = '';
    const arr = Array.isArray(files) ? files : [];
    if (arr.length === 0) {
      filesEl.innerHTML = `<li class="file-row file-row--empty">${escapeText(t('panel.noFiles'))}</li>`;
    } else {
      arr.forEach(f => filesEl.appendChild(renderFileRow(f)));
    }
  }

  if (skillsEl) {
    const arr = Array.isArray(skills) ? skills : [];
    skillsEl.innerHTML = arr.length
      ? arr.map(renderSkill).join('')
      : `<li class="context-skill context-skill--empty">${escapeText(t('panel.noSkills'))}</li>`;
  }

  if (connectorsEl) {
    const builtIn = [{ name: t('panel.webSearch'), slug: 'web_search' }];
    const arr = Array.isArray(connected) ? connected : [];
    const all = [...builtIn, ...arr.map(c => ({ name: c.name ?? c.slug, slug: c.slug }))];
    connectorsEl.innerHTML = all.map(renderConnector).join('');
  }

  initCollapseButtons();
}

// Re-render the panel when the language changes (built-in labels + empty states).
onLangChange(() => loadContextPanel());
