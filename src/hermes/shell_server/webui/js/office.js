/**
 * office.js — "Office" view: draws the agent team as a live floor and shows how
 * they're working (active agent + runtime state, polled). Click a desk → drawer
 * with actions adapted to Lumen's own views (activate+chat, manage). Vanilla JS,
 * Sereno theme. Endpoints: GET /agents, /agents/active, /runtime/status; POST
 * /agents/{id}/activate.
 */

import { listAgents, getActiveAgent, getRuntimeStatus, setActiveAgent } from './api.js';
import { switchView, showToast } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const _PALETTE = ['#6366f1', '#0A84FF', '#34D399', '#F5B945', '#FF6B6B', '#5BC8E0', '#C084FC', '#FB923C'];
function _colorFor(agent, idx) {
  if (agent.color && /^#?[0-9a-fA-F]{3,8}$/.test(agent.color)) {
    return agent.color.startsWith('#') ? agent.color : `#${agent.color}`;
  }
  return _PALETTE[idx % _PALETTE.length];
}

let _pollTimer = null;
function _stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

/** Build one agent "desk". Live state (active/working) is applied separately. */
function _renderDesk(agent, idx) {
  const id = agent.agent_id ?? agent.id ?? '';
  const name = agent.name ?? id;
  const role = agent.role || agent.primary_mission || '';
  const isDefault = agent.is_default === true;
  const color = _colorFor(agent, idx);
  const initial = (name || 'A')[0].toUpperCase();

  const el = document.createElement('button');
  el.className = 'office-desk';
  el.type = 'button';
  el.dataset.agentId = id;
  el.setAttribute('aria-label', `${name}${role ? ' — ' + role : ''}`);
  el.style.cssText = [
    'position:relative', 'display:flex', 'flex-direction:column', 'align-items:center',
    'gap:8px', 'padding:18px 14px 14px', 'background:var(--card)',
    'border:1px solid var(--line)', 'border-radius:var(--r-lg, 16px)', 'cursor:pointer',
    'text-align:center', 'transition:transform .12s ease, border-color .12s ease, box-shadow .12s ease',
    'min-height:150px', 'justify-content:flex-start',
  ].join(';');

  el.innerHTML = `
    <span class="office-desk__status" aria-hidden="true" style="position:absolute;top:10px;right:10px;width:10px;height:10px;border-radius:50%;background:var(--ink4);transition:background .2s"></span>
    <span class="office-desk__avatar" aria-hidden="true" style="width:52px;height:52px;border-radius:50%;display:grid;place-items:center;font-size:20px;font-weight:700;color:#fff;background:${color};box-shadow:0 0 0 0 ${color}00;transition:box-shadow .3s">${esc(initial)}</span>
    <span class="office-desk__name" style="font-size:var(--text-body,14px);font-weight:600;color:var(--ink);line-height:1.2">${esc(name)}${isDefault ? ' <span style="font-size:9px;font-weight:700;color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:1px 4px;vertical-align:middle">CEREBRO</span>' : ''}</span>
    <span class="office-desk__role" style="font-size:var(--text-caption,12px);color:var(--ink3);line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(role)}</span>
    <span class="office-desk__activity" style="margin-top:auto;font-size:11px;color:var(--ink4);min-height:14px"></span>`;

  el.addEventListener('mouseenter', () => { el.style.transform = 'translateY(-2px)'; el.style.borderColor = color; });
  el.addEventListener('mouseleave', () => { el.style.transform = ''; el.style.borderColor = 'var(--line)'; });
  el.addEventListener('click', () => _openDrawer(agent, color));
  return el;
}

/** Apply live state to the rendered desks: which agent is active + busy/idle. */
function _applyLiveState(activeId, runtime) {
  const working = runtime && (runtime.state === 'working' || runtime.state === 'busy' || (runtime.active_task_count || 0) > 0);
  document.querySelectorAll('.office-desk').forEach((el) => {
    const isActive = el.dataset.agentId === activeId;
    const dot = el.querySelector('.office-desk__status');
    const avatar = el.querySelector('.office-desk__avatar');
    const act = el.querySelector('.office-desk__activity');
    const color = avatar ? (avatar.style.background || 'var(--accent)') : 'var(--accent)';
    if (isActive && working) {
      dot.style.background = 'var(--ok)';
      avatar.style.boxShadow = `0 0 0 4px ${color}33`;
      avatar.style.animation = 'office-pulse 1.4s ease-in-out infinite';
      act.textContent = t('office.working');
      act.style.color = 'var(--ok)';
      el.style.borderColor = color;
    } else if (isActive) {
      dot.style.background = 'var(--accent)';
      avatar.style.boxShadow = `0 0 0 3px ${color}22`;
      avatar.style.animation = '';
      act.textContent = t('office.activeIdle');
      act.style.color = 'var(--ink3)';
    } else {
      dot.style.background = 'var(--ink4)';
      avatar.style.boxShadow = 'none';
      avatar.style.animation = '';
      act.textContent = '';
      el.style.borderColor = 'var(--line)';
    }
  });
}

function _updateStatusBar(agents, activeId, runtime) {
  const bar = document.getElementById('office-statusbar');
  if (!bar) return;
  const active = agents.find((a) => (a.agent_id ?? a.id) === activeId);
  const working = runtime && (runtime.state === 'working' || runtime.state === 'busy' || (runtime.active_task_count || 0) > 0);
  const tasks = (runtime && runtime.active_task_count) || 0;
  bar.innerHTML = `
    <span style="display:inline-flex;align-items:center;gap:6px">
      <span style="width:8px;height:8px;border-radius:50%;background:${working ? 'var(--ok)' : 'var(--ink4)'}"></span>
      ${working ? esc(t('office.teamWorking')) : esc(t('office.teamIdle'))}
    </span>
    <span style="color:var(--ink3)">·</span>
    <span>${agents.length} ${esc(t('office.agentsLabel'))}</span>
    ${active ? `<span style="color:var(--ink3)">·</span><span>${esc(t('office.activeLabel'))}: <strong style="color:var(--ink)">${esc(active.name || activeId)}</strong></span>` : ''}
    ${tasks > 0 ? `<span style="color:var(--ink3)">·</span><span>${tasks} ${esc(t('office.tasksLabel'))}</span>` : ''}`;
}

function _openDrawer(agent, color) {
  const id = agent.agent_id ?? agent.id ?? '';
  document.getElementById('office-drawer')?.remove();
  const back = document.createElement('div');
  back.id = 'office-drawer';
  back.style.cssText = 'position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.45);display:flex;justify-content:flex-end';
  const panel = document.createElement('div');
  panel.style.cssText = 'width:min(420px,92vw);height:100%;background:var(--bg0);border-left:1px solid var(--line);padding:24px;overflow:auto;box-shadow:-12px 0 40px rgba(0,0,0,.4)';
  const role = agent.role || agent.primary_mission || '';
  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:18px">
      <span style="width:56px;height:56px;border-radius:50%;display:grid;place-items:center;font-size:22px;font-weight:700;color:#fff;background:${color}">${esc((agent.name || 'A')[0].toUpperCase())}</span>
      <div><div style="font-size:18px;font-weight:700;color:var(--ink)">${esc(agent.name || id)}</div>
      <div style="font-size:13px;color:var(--ink3)">${esc(role)}</div></div>
    </div>
    ${agent.primary_mission ? `<div style="font-size:13px;color:var(--ink2);line-height:1.5;margin-bottom:8px"><strong style="color:var(--ink3)">${esc(t('office.mission'))}:</strong> ${esc(agent.primary_mission)}</div>` : ''}
    ${agent.autonomy_level ? `<div style="font-size:12px;color:var(--ink3);margin-bottom:18px">${esc(t('office.autonomy'))}: ${esc(agent.autonomy_level)}</div>` : ''}
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:18px">
      <button class="btn btn--primary" data-act="chat">${esc(t('office.activateChat'))}</button>
      <button class="btn btn--ghost" data-act="manage">${esc(t('office.manage'))}</button>
      <button class="btn btn--ghost" data-act="close">${esc(t('office.close'))}</button>
    </div>`;
  back.appendChild(panel);
  back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
  panel.querySelector('[data-act="close"]').addEventListener('click', () => back.remove());
  panel.querySelector('[data-act="manage"]').addEventListener('click', () => { back.remove(); switchView('agents'); });
  panel.querySelector('[data-act="chat"]').addEventListener('click', async () => {
    try {
      if (!agent.is_default) await setActiveAgent(id);
      back.remove();
      switchView('chat');
      showToast(t('office.nowActive').replace('{name}', agent.name || id), 'ok');
    } catch (e) {
      showToast(`${t('office.activateFail')}: ${e.message || e}`, 'error');
    }
  });
  document.body.appendChild(back);
}

export async function renderOfficeView(container) {
  _stopPolling();
  container.innerHTML = `
    <style>@keyframes office-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.06)}}</style>
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('office.title'))}</h2>
        <p class="cv-subtitle">${esc(t('office.subtitle'))}</p>
      </div>
      <div id="office-statusbar" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;color:var(--ink2);margin:4px 0 18px"></div>
      <div id="office-floor" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px">
        <div class="cv-skeleton"></div><div class="cv-skeleton"></div><div class="cv-skeleton"></div>
      </div>
    </div>`;

  let agents = [];
  try {
    const res = await listAgents();
    agents = Array.isArray(res) ? res : (res.agents || []);
  } catch (e) {
    document.getElementById('office-floor').innerHTML = `<div class="cv-empty">${esc(t('office.loadFail'))}: ${esc(e.message || e)}</div>`;
    return;
  }

  const floor = document.getElementById('office-floor');
  floor.innerHTML = '';
  if (!agents.length) {
    floor.innerHTML = `<div class="cv-empty">${esc(t('office.empty'))}</div>`;
    return;
  }
  agents.forEach((a, i) => floor.appendChild(_renderDesk(a, i)));

  async function refresh() {
    if (!document.getElementById('office-floor')) { _stopPolling(); return; }
    const [act, rt] = await Promise.all([
      getActiveAgent().catch(() => ({ active_agent_id: '' })),
      getRuntimeStatus().catch(() => ({ state: 'unknown', active_task_count: 0 })),
    ]);
    const activeId = act.active_agent_id || act.agent_id || '';
    _applyLiveState(activeId, rt);
    _updateStatusBar(agents, activeId, rt);
  }
  await refresh();
  _pollTimer = setInterval(refresh, 3000);
}
