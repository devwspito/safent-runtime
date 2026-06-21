/**
 * agents.js — Agents capability view (parity with the native SO AgentsApp.qml).
 * The default agent is the "Cerebro" (omnipotent, non-editable, non-deletable).
 * Custom agents: create / edit (persona + autonomy) / activate / delete, plus
 * per-agent capability binding (skills + MCP) and Composio connections.
 */

import {
  listAgents, getActiveAgent, createAgent, updateAgent, deleteAgent, setActiveAgent,
  listAgentCapabilities, bindAgentCapability, unbindAgentCapability,
  listAgentComposio, listComposioConnections, bindAgentComposio, unbindAgentComposio,
  listSkills, listMcpServers,
} from './api.js';
import { Icon } from './icons.js';
import { showToast, confirmDialog } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const AUTONOMY = [
  { value: 'ask_always', label: () => t('agents.autonomyAsk') },
  { value: 'balanced', label: () => t('agents.autonomyBalanced') },
  { value: 'autonomous', label: () => t('agents.autonomyAutonomous') },
];

function renderAgentCard(agent, activeId, onAction) {
  const id = agent.agent_id ?? agent.id ?? '';
  const isActive = id === activeId;
  const isDefault = agent.is_default === true;
  const el = document.createElement('div');
  el.className = `agent-card${isActive ? ' agent-card--active' : ''}`;
  el.dataset.agentId = id;
  const role = agent.role ? esc(agent.role) : '';
  el.innerHTML = `
    <div class="agent-card__header">
      <div class="agent-avatar" aria-hidden="true">${esc((agent.name ?? 'A')[0].toUpperCase())}</div>
      <div class="agent-card__info">
        <div class="agent-card__name">${esc(agent.name ?? id)}
          ${isDefault ? `<span class="agent-brain-chip">${esc(t('agents.brain'))}</span>` : ''}
        </div>
        <div class="agent-card__desc">${isDefault ? esc(t('agents.brainSubtitle')) : (role || '')}</div>
      </div>
      ${isActive ? `<span class="agent-card__active-badge">${esc(t('agents.active'))}</span>` : ''}
    </div>
    <div class="agent-card__actions">
      ${!isActive ? `<button class="btn btn--primary btn--sm" data-action="activate">${esc(t('agents.activate'))}</button>` : ''}
      <button class="btn btn--ghost btn--sm" data-action="caps">${esc(t('agents.manage'))}</button>
      ${!isDefault ? `<button class="btn btn--ghost btn--sm" data-action="edit">${esc(t('agents.edit'))}</button>` : ''}
      ${!isDefault ? `<button class="btn btn--ghost btn--sm btn--danger-ghost" data-action="delete">${Icon.trash}</button>` : ''}
    </div>
    <div class="agent-card__panel" hidden></div>`;

  el.querySelectorAll('.agent-card__actions [data-action]').forEach(btn => {
    btn.addEventListener('click', () => onAction(btn.dataset.action, agent, el));
  });
  return el;
}

// ── Edit form (custom agents) ──────────────────────────────────────────────────
function renderEditForm(agent, panel, onSaved) {
  const id = agent.agent_id ?? agent.id;
  panel.innerHTML = `
    <label class="cv-label">${esc(t('agents.nameLabel'))}</label>
    <input class="cv-input" data-f="name" type="text" value="${esc(agent.name ?? '')}">
    <label class="cv-label">${esc(t('agents.roleLabel'))}</label>
    <input class="cv-input" data-f="role" type="text" value="${esc(agent.role ?? '')}" placeholder="${esc(t('agents.rolePlaceholder'))}">
    <label class="cv-label">${esc(t('agents.missionLabel'))}</label>
    <input class="cv-input" data-f="primary_mission" type="text" value="${esc(agent.primary_mission ?? '')}" placeholder="${esc(t('agents.missionPlaceholder'))}">
    <label class="cv-label">${esc(t('agents.instructionsLabel'))}</label>
    <textarea class="cv-textarea" data-f="instructions" rows="3" placeholder="${esc(t('agents.instructionsPlaceholder'))}">${esc(agent.instructions ?? '')}</textarea>
    <label class="cv-label">${esc(t('agents.autonomyLabel'))}</label>
    <select class="cv-input" data-f="autonomy_level">
      ${AUTONOMY.map(a => `<option value="${a.value}" ${agent.autonomy_level === a.value ? 'selected' : ''}>${esc(a.label())}</option>`).join('')}
    </select>
    <div class="cv-form-actions">
      <button class="btn btn--primary btn--sm" data-save>${esc(t('agents.save'))}</button>
      <button class="btn btn--ghost btn--sm" data-cancel>${esc(t('agents.cancel'))}</button>
    </div>`;
  panel.hidden = false;
  panel.querySelector('[data-cancel]').addEventListener('click', () => { panel.hidden = true; panel.innerHTML = ''; });
  panel.querySelector('[data-save]').addEventListener('click', async () => {
    const draft = {};
    panel.querySelectorAll('[data-f]').forEach(i => { draft[i.dataset.f] = i.value; });
    try {
      await updateAgent(id, draft);
      showToast(t('agents.saved'), 'ok');
      panel.hidden = true; panel.innerHTML = '';
      onSaved();
    } catch (e) { showToast(e.message, 'error'); }
  });
}

// ── Capabilities panel (skills + MCP + Composio toggles) ───────────────────────
function toggleRow(label, bound, onToggle) {
  const row = document.createElement('label');
  row.className = 'cap-toggle';
  row.innerHTML = `<span class="cap-toggle__name">${esc(label)}</span>
    <input type="checkbox" ${bound ? 'checked' : ''}>`;
  const cb = row.querySelector('input');
  cb.addEventListener('change', async () => {
    cb.disabled = true;
    try { await onToggle(cb.checked); }
    catch (e) { cb.checked = !cb.checked; showToast(t('agents.bindError', { reason: e.message }), 'error'); }
    finally { cb.disabled = false; }
  });
  return row;
}

async function renderCapsPanel(agent, panel) {
  const id = agent.agent_id ?? agent.id;
  if (agent.is_default) {
    panel.innerHTML = `<p class="teach-card__privacy">${esc(t('agents.capsBrainNote'))}</p>`;
    panel.hidden = false;
    return;
  }
  panel.innerHTML = `<div class="cv-skeleton"></div>`;
  panel.hidden = false;
  const [bound, comboBound, skills, mcps, composio] = await Promise.all([
    listAgentCapabilities(id).catch(() => []),
    listAgentComposio(id).catch(() => []),
    listSkills().catch(() => []),
    listMcpServers().catch(() => []),
    listComposioConnections().catch(() => []),
  ]);
  const boundIds = new Set((Array.isArray(bound) ? bound : []).flatMap(b =>
    [b.capability_id, b.id, b.skill_id, b.server_id, b.name].filter(Boolean)));
  const comboIds = new Set((Array.isArray(comboBound) ? comboBound : []).map(c => (typeof c === 'string' ? c : (c.connection_id ?? c.id))));

  panel.innerHTML = '';
  const section = (title, items, render) => {
    if (!items.length) return;
    const h = document.createElement('div'); h.className = 'cap-section-label'; h.textContent = title;
    panel.appendChild(h);
    items.forEach(render);
  };

  section(t('agents.capsSkills'), Array.isArray(skills) ? skills : [], s => {
    const sid = s.package_id ?? s.skill_id ?? s.skill_name ?? s.name;
    panel.appendChild(toggleRow(s.skill_name ?? s.name ?? sid, boundIds.has(sid), async (on) => {
      if (on) await bindAgentCapability(id, { kind: 'skill', capability_id: sid });
      else await unbindAgentCapability(id, sid, 'skill');
    }));
  });
  section(t('agents.capsMcp'), Array.isArray(mcps) ? mcps : [], m => {
    const mid = m.server_id ?? m.id;
    panel.appendChild(toggleRow(m.label ?? mid, boundIds.has(mid), async (on) => {
      if (on) await bindAgentCapability(id, { kind: 'mcp', capability_id: mid });
      else await unbindAgentCapability(id, mid, 'mcp');
    }));
  });
  section(t('agents.capsComposio'), Array.isArray(composio) ? composio : [], c => {
    const cid = c.connection_id ?? c.id ?? c.slug;
    panel.appendChild(toggleRow(c.name ?? c.toolkit_slug ?? cid, comboIds.has(cid), async (on) => {
      if (on) await bindAgentComposio(id, cid, c.toolkit_slug ?? '');
      else await unbindAgentComposio(id, cid);
    }));
  });
  if (!panel.children.length) panel.innerHTML = `<p class="teach-card__privacy">${esc(t('agents.capsEmpty'))}</p>`;
}

function renderCreateForm(onCreated) {
  const wrap = document.createElement('div');
  wrap.className = 'cv-form-card';
  wrap.innerHTML = `
    <div class="cv-form-title">${esc(t('agents.newFormTitle'))}</div>
    <label class="cv-label" for="new-agent-name">${esc(t('agents.nameLabel'))}</label>
    <input id="new-agent-name" class="cv-input" type="text" placeholder="${esc(t('agents.namePlaceholder'))}" autocomplete="off">
    <label class="cv-label" for="new-agent-role">${esc(t('agents.roleLabel'))}</label>
    <input id="new-agent-role" class="cv-input" type="text" placeholder="${esc(t('agents.rolePlaceholder'))}">
    <label class="cv-label" for="new-agent-mission">${esc(t('agents.missionLabel'))}</label>
    <input id="new-agent-mission" class="cv-input" type="text" placeholder="${esc(t('agents.missionPlaceholder'))}">
    <label class="cv-label" for="new-agent-instructions">${esc(t('agents.instructionsLabel'))}</label>
    <textarea id="new-agent-instructions" class="cv-textarea" rows="2" placeholder="${esc(t('agents.instructionsPlaceholder'))}"></textarea>
    <label class="cv-label" for="new-agent-autonomy">${esc(t('agents.autonomyLabel'))}</label>
    <select id="new-agent-autonomy" class="cv-input">
      ${AUTONOMY.map(a => `<option value="${a.value}" ${a.value === 'balanced' ? 'selected' : ''}>${esc(a.label())}</option>`).join('')}
    </select>
    <div class="cv-form-actions">
      <button class="btn btn--primary btn--sm" id="create-agent-btn">${esc(t('agents.createBtn'))}</button>
    </div>`;

  wrap.querySelector('#create-agent-btn').addEventListener('click', async () => {
    const name = wrap.querySelector('#new-agent-name').value.trim();
    if (!name) { showToast(t('agents.nameRequired'), 'warn'); return; }
    try {
      await createAgent({
        name,
        role: wrap.querySelector('#new-agent-role').value.trim(),
        primary_mission: wrap.querySelector('#new-agent-mission').value.trim(),
        instructions: wrap.querySelector('#new-agent-instructions').value.trim(),
        autonomy_level: wrap.querySelector('#new-agent-autonomy').value,
      });
      showToast(t('agents.created'), 'ok');
      ['#new-agent-name', '#new-agent-role', '#new-agent-mission', '#new-agent-instructions'].forEach(s => { wrap.querySelector(s).value = ''; });
      onCreated();
    } catch (e) { showToast(e.message, 'error'); }
  });
  return wrap;
}

export async function renderAgentsView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('agents.title'))}</h2>
        <p class="cv-subtitle">${esc(t('agents.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('agents.configuredSection'))}</div>
        <div class="cv-list" id="agents-list"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section" id="create-agent-section"></div>
    </div>`;

  async function load() {
    const [agents, active] = await Promise.all([listAgents(), getActiveAgent()]);
    const activeId = active?.active_agent_id ?? '';
    const list = document.getElementById('agents-list');
    if (!list) return;
    list.innerHTML = '';
    const arr = Array.isArray(agents) ? agents : [];
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(t('agents.empty'))}</div>`;
      return;
    }
    arr.forEach(a => {
      const card = renderAgentCard(a, activeId, async (action, agent, el) => {
        const id = agent.agent_id ?? agent.id ?? '';
        const name = agent.name ?? id;
        const panel = el.querySelector('.agent-card__panel');
        if (action === 'activate') {
          try { await setActiveAgent(id); showToast(t('agents.activated', { name }), 'ok'); load(); }
          catch (e) { showToast(e.message, 'error'); }
        } else if (action === 'delete') {
          if (!(await confirmDialog(t('agents.confirmDelete', { name })))) return;
          try { await deleteAgent(id); showToast(t('agents.deleted'), 'ok'); load(); }
          catch (e) { showToast(e.message, 'error'); }
        } else if (action === 'edit') {
          if (panel.hidden) renderEditForm(agent, panel, load); else { panel.hidden = true; panel.innerHTML = ''; }
        } else if (action === 'caps') {
          if (panel.hidden) renderCapsPanel(agent, panel); else { panel.hidden = true; panel.innerHTML = ''; }
        }
      });
      list.appendChild(card);
    });
  }

  const createSection = document.getElementById('create-agent-section');
  if (createSection) createSection.appendChild(renderCreateForm(load));

  load();
}
