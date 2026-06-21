/**
 * skills.js — Skills capability view.
 * Endpoints: GET /skills, GET /skills/hub/search?q=, POST /skills/install,
 *            POST /skills/{id}/promote
 */

import {
  listSkills, searchSkillsHub, listHubSkills, installSkill, getHubOpStatus,
  uninstallHubSkill, promoteSkill,
  createTrainingSession, startTrainingRecording, stopTrainingRecording,
  signTrainingSession, abandonTrainingSession, synthesizeSkill,
} from './api.js';
import { Icon } from './icons.js';
import { showToast, docLinkHtml, confirmDialog } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Skill lifecycle: validated → autonomous → deprecated (daemon DTO `state`).
function stateMeta(state = '') {
  const s = String(state).toLowerCase();
  if (s.includes('autonom')) return { dot: Icon.statusDone, label: t('skills.stateAutonomous'), cls: 'is-autonomous' };
  if (s.includes('deprec')) return { dot: Icon.statusIdle, label: t('skills.stateDeprecated'), cls: 'is-deprecated' };
  if (s.includes('valid')) return { dot: Icon.statusRunning, label: t('skills.stateValidated'), cls: 'is-validated' };
  return { dot: Icon.statusIdle, label: state || '', cls: '' };
}

function renderSkillRow(skill, onAction) {
  const el = document.createElement('div');
  el.className = 'skill-row';
  const name = skill.skill_name ?? skill.name ?? skill.slug ?? '';
  const state = skill.state ?? '';
  const meta = stateMeta(state);
  const version = skill.version ? `v${esc(skill.version)}` : '';
  const surfaces = Array.isArray(skill.surface_kinds) ? skill.surface_kinds.join(' · ') : (skill.surface_kinds ?? '');
  el.dataset.skillId = skill.package_id ?? skill.skill_id ?? '';
  const sub = [version, surfaces].filter(Boolean).join(' · ');
  el.innerHTML = `
    <div class="skill-row__status" aria-label="${esc(meta.label)}">${meta.dot}</div>
    <div class="skill-row__info">
      <div class="skill-row__name">${esc(name)}</div>
      ${sub ? `<div class="skill-row__desc">${esc(sub)}</div>` : ''}
    </div>
    <div class="skill-row__actions">
      ${meta.label ? `<span class="skill-state-chip ${meta.cls}">${esc(meta.label)}</span>` : ''}
      ${state.toLowerCase().includes('valid') ? `<button class="btn btn--primary btn--sm" data-action="promote" aria-label="${esc(t('skills.promoteAriaLabel'))}">${esc(t('skills.promote'))}</button>` : ''}
      <button class="btn btn--ghost btn--sm btn--danger-ghost" data-action="uninstall" aria-label="${esc(t('skills.uninstall'))}">${Icon.trash}</button>
    </div>`;

  el.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => onAction(btn.dataset.action, skill, el));
  });

  return el;
}

const TRUST_TONE = { official: 'ok', verified: 'ok', community: 'warn', unknown: '' };
// Hub search results carry a `repo` field: a full URL, or an "owner/repo" slug
// (skills.sh community entries are GitHub-backed). Official builtin skills have
// no external repo, so no doc link is shown for them.
function skillDocUrl(item) {
  const raw = String(item.repo ?? item.url ?? item.homepage ?? '').trim();
  if (!raw) return '';
  if (/^https?:\/\//i.test(raw)) return raw;
  if (/^[\w.-]+\/[\w.-]+$/.test(raw)) return `https://github.com/${raw}`;
  return '';
}

function renderHubResult(item, installed, onInstall) {
  const el = document.createElement('div');
  el.className = 'skill-hub-result';
  const name = item.name ?? item.identifier ?? item.slug ?? '';
  const already = installed.has(name) || installed.has(item.identifier);
  const trust = item.trust_level ? `<span class="hub-badge hub-badge--${TRUST_TONE[String(item.trust_level).toLowerCase()] ?? ''}">${esc(item.trust_level)}</span>` : '';
  const src = item.source ? `<span class="hub-badge">${esc(item.source)}</span>` : '';
  el.innerHTML = `
    <div class="skill-hub-result__info">
      <div class="skill-hub-result__name">${esc(name)} ${trust} ${src}</div>
      ${item.description ? `<div class="skill-hub-result__desc">${esc(item.description)}</div>` : ''}
    </div>
    <div class="skill-hub-result__actions">
      ${docLinkHtml(skillDocUrl(item), t('skills.docs'))}
      <button class="btn btn--secondary btn--sm" ${already ? 'disabled' : ''}>${already ? esc(t('skills.alreadyInstalled')) : esc(t('skills.install'))}</button>
    </div>`;
  const btn = el.querySelector('button');
  if (!already) btn.addEventListener('click', () => onInstall(item, btn));
  return el;
}

// Poll an async hub op until it resolves.
function pollHubOp(opId, { onDone, onError }) {
  let tries = 0;
  const tick = async () => {
    if (tries++ > 40) { onError?.('timeout'); return; }
    const st = await getHubOpStatus(opId);
    const s = String(st?.status ?? '').toLowerCase();
    if (s === 'done' || s === 'completed' || s === 'success') { onDone?.(st); return; }
    if (s === 'error' || s === 'failed') { onError?.(st?.error ?? st?.message ?? 'error'); return; }
    setTimeout(tick, 2500);
  };
  setTimeout(tick, 1500);
}

export async function renderSkillsView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('skills.title'))}</h2>
        <p class="cv-subtitle">${esc(t('skills.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('skills.teachSection'))}</div>
        <div class="teach-card">
          <p class="teach-card__intro">${esc(t('skills.teachIntro'))}</p>
          <div id="teach-idle">
            <button class="btn btn--primary btn--sm" id="teach-toggle">${esc(t('skills.teachBtn'))}</button>
          </div>
          <div id="teach-form" hidden>
            <input id="teach-name" class="cv-input" type="text" placeholder="${esc(t('skills.teachNamePlaceholder'))}" autocomplete="off" aria-label="${esc(t('skills.teachNamePlaceholder'))}">
            <textarea id="teach-desc" class="cv-input cv-textarea" rows="4" placeholder="${esc(t('skills.teachDescPlaceholder'))}" aria-label="${esc(t('skills.teachDescPlaceholder'))}"></textarea>
            <p class="teach-card__privacy">${esc(t('skills.teachPrivacy'))}</p>
            <div class="teach-card__actions">
              <button class="btn btn--primary btn--sm" id="teach-start">${esc(t('skills.teachStart'))}</button>
              <button class="btn btn--ghost btn--sm" id="teach-cancel">${esc(t('skills.teachCancel'))}</button>
            </div>
          </div>
          <div id="teach-recording" hidden>
            <p class="teach-card__recording">${esc(t('skills.teachRecording'))}</p>
            <button class="btn btn--secondary btn--sm" id="teach-stop">${esc(t('skills.teachStop'))}</button>
          </div>
          <div id="teach-synth" hidden>
            <p class="teach-card__synth">${Icon.spinner} <span>${esc(t('skills.teachSaving'))}</span></p>
          </div>
        </div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('skills.installedSection'))}</div>
        <div class="cv-list" id="skills-installed"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('skills.hubSection'))}</div>
        <div class="skills-hub-search">
          <input id="hub-search-input" class="cv-input" type="search" placeholder="${esc(t('skills.searchPlaceholder'))}" autocomplete="off" aria-label="${esc(t('skills.searchAriaLabel'))}">
          <button class="btn btn--secondary btn--sm" id="hub-search-btn">${esc(t('skills.searchBtn'))}</button>
        </div>
        <div class="cv-list" id="skills-hub-results"></div>
      </div>
    </div>`;

  let _installedHub = new Set();   // names of hub-installed skills (for dedup on results)

  async function loadInstalled() {
    const [skills, hub] = await Promise.all([listSkills(), listHubSkills().catch(() => [])]);
    const hubArr = Array.isArray(hub) ? hub : (hub?.results ?? hub?.skills ?? []);
    _installedHub = new Set(hubArr.flatMap(h => [h.name, h.skill_name, h.identifier].filter(Boolean)));
    const list = document.getElementById('skills-installed');
    if (!list) return;
    list.innerHTML = '';
    const arr = Array.isArray(skills) ? skills : [];
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(t('skills.empty'))}</div>`;
    } else {
      arr.forEach(s => list.appendChild(renderSkillRow(s, async (action, skill, rowEl) => {
        const pkgId = skill.package_id ?? skill.skill_id;
        const name = skill.skill_name ?? skill.name ?? pkgId;
        if (action === 'promote') {
          try {
            await promoteSkill(pkgId);
            showToast(t('skills.promoted'), 'ok');
            loadInstalled();
          } catch (e) { showToast(e.message, 'error'); }
        } else if (action === 'uninstall') {
          if (!(await confirmDialog(t('skills.confirmUninstall', { name })))) return;
          const btn = rowEl.querySelector('[data-action="uninstall"]');
          if (btn) btn.disabled = true;
          try {
            const op = await uninstallHubSkill(name);
            const opId = op?.op_id;
            if (opId) {
              pollHubOp(opId, {
                onDone: () => { showToast(t('skills.uninstalled', { name }), 'ok'); loadInstalled(); },
                onError: (r) => { showToast(t('skills.installFailed', { reason: r }), 'error'); loadInstalled(); },
              });
            } else { showToast(t('skills.uninstalled', { name }), 'ok'); loadInstalled(); }
          } catch (e) { showToast(e.message, 'error'); if (btn) btn.disabled = false; }
        }
      })));
    }
  }

  const searchBtn = container.querySelector('#hub-search-btn');
  const searchInput = container.querySelector('#hub-search-input');
  const hubResults = container.querySelector('#skills-hub-results');

  async function runSearch() {
    const q = searchInput?.value.trim() ?? '';
    if (!q) return;
    if (hubResults) hubResults.innerHTML = `<div class="cv-skeleton"></div>`;
    const results = await searchSkillsHub(q);
    if (!hubResults) return;
    hubResults.innerHTML = '';
    // The hub returns { query_id, results:[...] }; tolerate a bare array too.
    const arr = Array.isArray(results) ? results : (results?.results ?? []);
    if (arr.length === 0) {
      hubResults.innerHTML = `<div class="cv-empty">${esc(t('skills.noResults', { q }))}</div>`;
    } else {
      arr.forEach(item => hubResults.appendChild(renderHubResult(item, _installedHub, async (skill, btn) => {
        const identifier = skill.identifier ?? skill.slug ?? skill.name;
        const name = skill.name ?? identifier;
        if (btn) { btn.disabled = true; btn.textContent = t('skills.installing'); }
        try {
          const op = await installSkill(identifier);   // 202 {op_id, status}
          const opId = op?.op_id;
          showToast(t('skills.installQueued', { name }), 'ok');
          if (opId) {
            pollHubOp(opId, {
              onDone: () => { showToast(t('skills.skillInstalled', { name }), 'ok'); loadInstalled(); },
              onError: (r) => {
                showToast(t('skills.installFailed', { reason: r }), 'error');
                if (btn) { btn.disabled = false; btn.textContent = t('skills.install'); }
              },
            });
          } else { loadInstalled(); }
        } catch (e) {
          showToast(t('skills.installFailed', { reason: e.message }), 'error');
          if (btn) { btn.disabled = false; btn.textContent = t('skills.install'); }
        }
      })));
    }
  }

  searchBtn?.addEventListener('click', runSearch);
  searchInput?.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

  // ── Teach skill from a browser demonstration (native SO flow) ──────────────
  const teachIdle = container.querySelector('#teach-idle');
  const teachForm = container.querySelector('#teach-form');
  const teachRec = container.querySelector('#teach-recording');
  let teachSession = null;

  const teachSynth = container.querySelector('#teach-synth');
  function showTeach(phase) {
    if (teachIdle) teachIdle.hidden = phase !== 'idle';
    if (teachForm) teachForm.hidden = phase !== 'form';
    if (teachRec) teachRec.hidden = phase !== 'recording';
    if (teachSynth) teachSynth.hidden = phase !== 'synth';
  }

  container.querySelector('#teach-toggle')?.addEventListener('click', () => showTeach('form'));
  container.querySelector('#teach-cancel')?.addEventListener('click', () => showTeach('idle'));

  container.querySelector('#teach-start')?.addEventListener('click', async () => {
    const name = container.querySelector('#teach-name')?.value.trim() ?? '';
    const description = container.querySelector('#teach-desc')?.value.trim() ?? '';
    if (!name) { showToast(t('skills.teachNameRequired'), 'warn'); return; }
    const startBtn = container.querySelector('#teach-start');
    if (startBtn) { startBtn.disabled = true; startBtn.textContent = t('skills.teachCreating'); }
    try {
      const s = await createTrainingSession({ skill_name: name, description, surface_kind: 'browser' });
      teachSession = s.session_id;
      await startTrainingRecording(teachSession);
      showTeach('recording');
    } catch (e) {
      showToast(t('skills.teachError', { reason: e.message }), 'error');
      if (teachSession) { abandonTrainingSession(teachSession); teachSession = null; }
    } finally {
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = t('skills.teachStart'); }
    }
  });

  container.querySelector('#teach-stop')?.addEventListener('click', async () => {
    if (!teachSession) { showTeach('idle'); return; }
    const name = container.querySelector('#teach-name')?.value.trim() ?? '';
    // Switch out of the recording state immediately so it no longer LOOKS like
    // it's still capturing the screen — show a clean "creating" state.
    showTeach('synth');
    try {
      await stopTrainingRecording(teachSession);
      // Synthesize a real SKILL.md from the demonstration via the active LLM.
      await synthesizeSkill(teachSession);
      showToast(t('skills.teachSaved', { name }), 'ok');
      showTeach('idle');
      loadInstalled();
    } catch (e) {
      const msg = e.status === 409
        ? t('skills.teachNoModel')
        : t('skills.teachError', { reason: e.message });
      showToast(msg, e.status === 409 ? 'warn' : 'error');
      showTeach('idle');
    } finally {
      teachSession = null;
    }
  });

  loadInstalled();
}
