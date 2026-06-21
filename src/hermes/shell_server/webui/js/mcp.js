/**
 * mcp.js — MCP servers view.
 * Endpoints: GET /mcp, POST /mcp, DELETE /mcp/{id}
 */

import { listMcpServers, addMcpServer, removeMcpServer, searchMcpRegistry } from './api.js';
import { Icon } from './icons.js';
import { showToast, docLinkHtml, confirmDialog, promptDialog } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Curated starter catalog — parity with the native SO (McpApp.qml). The official
// registry (registry.modelcontextprotocol.io) is the open-ended source below.
// Curated catalog = SOLO servidores verificados que conectan 100% en el SO
// (runner npx). Los de uvx (Python: serena, sqlite) se retiraron temporalmente:
// uvx muere con "Invalid cross-device link" al poblar su caché dentro del jaula
// del mcp-launcher (un choque uv-vs-sandbox pendiente de strace como root). El
// buscador del registry sigue ofreciéndolos; si fallan, muestran el error real
// (no mienten). open-design retirado: exige un daemon Open Design externo.
const MCP_CATALOG = [
  { server_id: 'github', label: 'GitHub', tag: 'Dev', desc: 'MCP oficial de GitHub: repos, issues, PRs, código.',
    argv: ['npx', '-y', '@modelcontextprotocol/server-github'],
    repository: 'https://github.com/github/github-mcp-server' },
  { server_id: 'context7', label: 'Context7', tag: 'Docs', desc: 'Documentación de librerías en vivo, siempre actualizada.',
    argv: ['npx', '-y', '@upstash/context7-mcp'],
    repository: 'https://github.com/upstash/context7' },
  { server_id: 'filesystem', label: 'Filesystem', tag: 'Sistema', desc: 'Lectura/escritura de ficheros locales con HITL.',
    argv: ['npx', '-y', '@modelcontextprotocol/server-filesystem', '/var/lib/hermes/workspace'],
    repository: 'https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem' },
];

// reverse-DNS registry name ("io.github.owner/repo") → server slug [a-z0-9-]
function slugify(name) {
  const s = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return s.slice(0, 60) || 'mcp-server';
}

// Collect BYOK env vars for an entry (envSchema for catalog, env_vars for registry).
// Returns {env} or null if the user cancelled a required field.
async function collectEnv(entry) {
  const schema = entry.envSchema
    ?? (Array.isArray(entry.env_vars) ? entry.env_vars.map(k => (typeof k === 'string' ? { key: k, required: false } : k)) : []);
  const env = {};
  for (const field of schema) {
    const val = await promptDialog({
      message: `${field.label ?? field.key}${field.required ? ' *' : ''}`,
      password: Boolean(field.secret),
    });
    if (val === null) { if (field.required) return null; continue; }
    if (val.trim()) env[field.key] = val.trim();
    else if (field.required) return null;
  }
  return { env };
}

function renderMcpRow(server, onRemove) {
  const el = document.createElement('div');
  el.className = 'mcp-row';
  el.dataset.serverId = server.server_id ?? server.id ?? '';
  const argv = Array.isArray(server.argv) ? server.argv.join(' ') : (server.argv ?? '');
  const healthy = String(server.health ?? '').toLowerCase() === 'healthy';
  const hasHealth = server.health != null && server.health !== '';
  const tools = (server.tool_count != null) ? `${server.tool_count} tools` : '';
  const statusChip = hasHealth
    ? `<span class="mcp-health ${healthy ? 'is-ok' : 'is-down'}">${healthy ? '●' : '○'} ${tools || esc(String(server.health))}</span>`
    : (tools ? `<span class="mcp-health">${tools}</span>` : '');
  el.innerHTML = `
    <div class="mcp-row__info">
      <div class="mcp-row__name">${esc(server.label ?? server.server_id ?? 'MCP Server')} ${statusChip}</div>
      <div class="mcp-row__cmd">${esc(argv)}</div>
    </div>
    <button class="btn btn--ghost btn--sm btn--danger-ghost" aria-label="${esc(t('mcp.deleteAriaLabel', { name: server.label ?? '' }))}">${Icon.trash}</button>`;
  el.querySelector('button').addEventListener('click', () => onRemove(server, el));
  return el;
}

function renderAddForm(onAdded) {
  const wrap = document.createElement('div');
  wrap.className = 'cv-form-card';
  wrap.innerHTML = `
    <div class="cv-form-title">${esc(t('mcp.addFormTitle'))}</div>
    <label class="cv-label" for="mcp-label">${esc(t('mcp.labelField'))}</label>
    <input id="mcp-label" class="cv-input" type="text" placeholder="${esc(t('mcp.labelPlaceholder'))}" autocomplete="off">
    <label class="cv-label" for="mcp-argv">${esc(t('mcp.argvField'))}</label>
    <input id="mcp-argv" class="cv-input" type="text" placeholder="${esc(t('mcp.argvPlaceholder'))}" autocomplete="off">
    <label class="cv-label" for="mcp-env">${esc(t('mcp.envField'))}</label>
    <textarea id="mcp-env" class="cv-textarea" rows="3" placeholder="${esc(t('mcp.envPlaceholder'))}"></textarea>
    <div class="cv-form-actions">
      <button class="btn btn--primary btn--sm" id="add-mcp-btn">${esc(t('mcp.addBtn'))}</button>
    </div>`;

  wrap.querySelector('#add-mcp-btn').addEventListener('click', async () => {
    const label = wrap.querySelector('#mcp-label').value.trim();
    const argvRaw = wrap.querySelector('#mcp-argv').value.trim();
    const envRaw = wrap.querySelector('#mcp-env').value.trim();

    if (!label || !argvRaw) { showToast(t('mcp.nameAndCmdRequired'), 'warn'); return; }

    const argv = argvRaw.split(/\s+/).filter(Boolean);
    const env = {};
    envRaw.split('\n').forEach(line => {
      const idx = line.indexOf('=');
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });

    try {
      const res = await addMcpServer({ server_id: label.toLowerCase().replace(/\s+/g, '_'), label, argv, env });
      if (res && res.tool_count === 0) {
        showToast(t('mcp.addedNoTools', { name: label }), 'warn', 7000);
      } else {
        showToast(t('mcp.added'), 'ok');
      }
      wrap.querySelector('#mcp-label').value = '';
      wrap.querySelector('#mcp-argv').value = '';
      wrap.querySelector('#mcp-env').value = '';
      onAdded();
    } catch (e) { showToast(e.message, 'error'); }
  });

  return wrap;
}

// A discover card (catalog entry or registry result) with an Add button.
function renderCatalogCard(entry, installedIds, onInstall) {
  const id = entry.server_id ?? entry.id ?? slugify(entry.name);
  const argv = Array.isArray(entry.argv) ? entry.argv.join(' ') : (entry.argv ?? '');
  const already = installedIds.has(id) || installedIds.has(entry.server_id);
  // Only npx servers are accepted: uvx (Python) currently dies in the launcher
  // sandbox (cross-device link in uv's cache). Gate the runner here so the user
  // can't add something that won't connect. Remote/SSE come back installable:false.
  const runner = (Array.isArray(entry.argv) && entry.argv[0]
    ? String(entry.argv[0])
    : String(entry.runner ?? entry.argv ?? '')).split(/[/\s]+/).filter(Boolean).pop() || '';
  const nonNpx = runner !== '' && runner !== 'npx';
  const unsupported = entry.installable === false || nonNpx;
  const reason = entry.unsupported_reason || (nonNpx ? t('mcp.onlyNpx', { runner }) : '');
  const desc = entry.desc ?? entry.description ?? '';
  const needsEnv = (entry.envSchema?.length || (Array.isArray(entry.env_vars) && entry.env_vars.length));
  const el = document.createElement('div');
  el.className = 'mcp-card';
  let btnLabel = esc(t('mcp.install'));
  if (already) btnLabel = esc(t('mcp.installed', { name: '' }).replace('""', '').trim() || 'OK');
  else if (unsupported) btnLabel = esc(t('mcp.unavailable'));
  el.innerHTML = `
    <div class="mcp-card__info">
      <div class="mcp-card__head">
        <span class="mcp-card__name">${esc(entry.label ?? entry.name ?? id)}</span>
        ${entry.tag ? `<span class="mcp-card__tag">${esc(entry.tag)}</span>` : ''}
        ${needsEnv ? `<span class="mcp-card__tag">BYOK</span>` : ''}
      </div>
      ${desc ? `<div class="mcp-card__desc">${esc(desc)}</div>` : ''}
      ${argv ? `<div class="mcp-card__cmd">${esc(argv)}</div>` : ''}
      ${unsupported && reason ? `<div class="mcp-card__cmd">${esc(reason)}</div>` : ''}
    </div>
    <div class="mcp-card__actions">
      ${docLinkHtml(entry.repository ?? entry.homepage ?? entry.website, t('mcp.docs'))}
      <button class="btn btn--secondary btn--sm" ${already || unsupported ? 'disabled' : ''}>${btnLabel}</button>
    </div>`;
  const btn = el.querySelector('button');
  if (!already && !unsupported) btn.addEventListener('click', () => onInstall(entry, btn));
  return el;
}

export async function renderMcpView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('mcp.title'))}</h2>
        <p class="cv-subtitle">${esc(t('mcp.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('mcp.activeSection'))}</div>
        <div class="cv-list" id="mcp-list"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('mcp.suggestedSection'))}</div>
        <div class="cv-list" id="mcp-catalog"></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('mcp.registrySection'))}</div>
        <div class="skills-hub-search">
          <input id="mcp-registry-input" class="cv-input" type="search" placeholder="${esc(t('mcp.registryPlaceholder'))}" autocomplete="off">
          <button class="btn btn--secondary btn--sm" id="mcp-registry-btn">${esc(t('mcp.registrySearchBtn'))}</button>
        </div>
        <p class="teach-card__privacy">${esc(t('mcp.registryHint'))}</p>
        <div class="cv-list" id="mcp-registry-results"></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('mcp.customSection'))}</div>
        <div id="mcp-add-section"></div>
      </div>
    </div>`;

  let installedIds = new Set();

  async function installEntry(entry, btn) {
    // Defensive npx-only gate (the card already disables non-npx, but never trust
    // the click path): reject anything whose runner isn't npx.
    const argvArr = Array.isArray(entry.argv) ? entry.argv : String(entry.argv ?? '').split(/\s+/).filter(Boolean);
    const runner = (argvArr[0] ? String(argvArr[0]) : String(entry.runner ?? '')).split(/[/\s]+/).filter(Boolean).pop() || '';
    if (runner && runner !== 'npx') { showToast(t('mcp.onlyNpx', { runner }), 'warn', 7000); return; }
    // BYOK: collect required env before installing (envSchema or registry env_vars).
    const collected = await collectEnv(entry);
    if (collected === null) return;   // user cancelled a required field
    if (btn) { btn.disabled = true; btn.textContent = t('mcp.installing'); }
    try {
      const res = await addMcpServer({
        server_id: entry.server_id ?? entry.id ?? slugify(entry.name),
        label: entry.label ?? entry.name,
        argv: Array.isArray(entry.argv) ? entry.argv : String(entry.argv ?? '').split(/\s+/).filter(Boolean),
        env: { ...(entry.env ?? {}), ...collected.env },
      });
      const name = entry.label ?? entry.name ?? '';
      if (res && res.tool_count === 0) {
        showToast(t('mcp.addedNoTools', { name }), 'warn', 7000);
      } else {
        showToast(t('mcp.installed', { name }), 'ok');
      }
      await load();
      renderCatalog();
    } catch (e) {
      showToast(e.message, 'error');
      if (btn) { btn.disabled = false; btn.textContent = t('mcp.install'); }
    }
  }

  async function load() {
    const servers = await listMcpServers();
    const list = document.getElementById('mcp-list');
    const arr = Array.isArray(servers) ? servers : [];
    installedIds = new Set(arr.map(s => s.server_id ?? s.id));
    if (!list) return;
    list.innerHTML = '';
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(t('mcp.empty'))}</div>`;
    } else {
      arr.forEach(s => list.appendChild(renderMcpRow(s, async (server) => {
        const name = server.label ?? server.server_id ?? '';
        if (!(await confirmDialog(t('mcp.confirmDelete', { name })))) return;
        try {
          await removeMcpServer(server.server_id ?? server.id);
          showToast(t('mcp.serverDeleted'), 'ok');
          await load();
          renderCatalog();
        } catch (e) { showToast(e.message, 'error'); }
      })));
    }
  }

  function renderCatalog() {
    const cat = document.getElementById('mcp-catalog');
    if (!cat) return;
    cat.innerHTML = '';
    MCP_CATALOG.forEach(e => cat.appendChild(renderCatalogCard(e, installedIds, installEntry)));
  }

  const regInput = container.querySelector('#mcp-registry-input');
  const regResults = container.querySelector('#mcp-registry-results');
  async function searchRegistry() {
    const q = regInput?.value.trim() ?? '';
    if (q.length < 2) return;
    if (regResults) regResults.innerHTML = `<div class="cv-skeleton"></div>`;
    const results = await searchMcpRegistry(q);
    if (!regResults) return;
    regResults.innerHTML = '';
    const arr = Array.isArray(results) ? results : (results?.results ?? []);
    if (arr.length === 0) {
      regResults.innerHTML = `<div class="cv-empty">${esc(t('mcp.registryEmpty'))}</div>`;
    } else {
      arr.forEach(e => regResults.appendChild(renderCatalogCard(e, installedIds, installEntry)));
    }
  }
  container.querySelector('#mcp-registry-btn')?.addEventListener('click', searchRegistry);
  regInput?.addEventListener('keydown', e => { if (e.key === 'Enter') searchRegistry(); });

  const addSection = document.getElementById('mcp-add-section');
  if (addSection) addSection.appendChild(renderAddForm(load));

  await load();
  renderCatalog();
}
