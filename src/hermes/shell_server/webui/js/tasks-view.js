/**
 * tasks-view.js — Scheduled tasks / queue view.
 * Endpoints: GET /tasks/configured, GET /tasks/recent,
 *            POST /tasks/configured, DELETE /tasks/configured/{id},
 *            PATCH /tasks/configured/{id}
 */

import { listConfiguredTasks, listRecentTasks, createTask, deleteTask, toggleTask, listAgents } from './api.js';
import { Icon } from './icons.js';
import { showToast, confirmDialog } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Cron → weekly-calendar mapping ──────────────────────────────────────────────
// Columns are Mon..Sun (index 0..6). Parses the standard 5-field cron's dow + time.
const DOW_KEYS = ['tasks.dowMon', 'tasks.dowTue', 'tasks.dowWed', 'tasks.dowThu', 'tasks.dowFri', 'tasks.dowSat', 'tasks.dowSun'];

function expandCronField(field, lo, hi) {
  const out = new Set();
  if (field == null || field === '*' || field === '?') { for (let i = lo; i <= hi; i++) out.add(i); return out; }
  for (const part of String(field).split(',')) {
    const stepM = part.match(/^(.+)\/(\d+)$/);
    const range = stepM ? stepM[1] : part;
    const step = stepM ? parseInt(stepM[2], 10) : 1;
    let a, b;
    if (range === '*') { a = lo; b = hi; }
    else {
      const rm = range.match(/^(\d+)-(\d+)$/);
      if (rm) { a = +rm[1]; b = +rm[2]; }
      else if (/^\d+$/.test(range)) { a = b = +range; }
      else continue;
    }
    for (let i = a; i <= b; i += (step || 1)) out.add(i);
  }
  return out;
}

/** @returns {{days:Set<number>, time:string, daily:boolean, valid:boolean}} days are Mon=0..Sun=6 */
function parseCron(cron) {
  if (!cron || typeof cron !== 'string') return { days: new Set(), time: '', daily: true, valid: false };
  const parts = cron.trim().split(/\s+/);
  if (parts.length < 5) return { days: new Set(), time: '', daily: true, valid: false }; // natural language
  const [min, hour, , , dow] = parts;
  let time = '';
  if (/^\d+$/.test(hour)) {
    time = `${String(hour).padStart(2, '0')}:${/^\d+$/.test(min) ? String(min).padStart(2, '0') : '00'}`;
  }
  const dowAny = dow === '*' || dow === '?';
  const days = new Set();
  if (!dowAny) {
    expandCronField(dow, 0, 7).forEach(d => {
      const sun = d === 7 ? 0 : d;        // cron: 0/7=Sun, 1=Mon..6=Sat
      days.add((sun + 6) % 7);            // → Mon=0..Sun=6
    });
  }
  return { days, time, daily: dowAny, valid: true };
}

const AGENT_HUES = [210, 145, 280, 30, 340, 190, 95, 255];
function agentHue(id) {
  let h = 0; const s = String(id ?? '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return AGENT_HUES[h % AGENT_HUES.length];
}

function relativeTime(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return t('tasks.timeNow');
  if (mins < 60) return t('tasks.timeMins', { n: mins });
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return t('tasks.timeHours', { n: hrs });
  return t('tasks.timeDays', { n: Math.floor(hrs / 24) });
}

// Daemon status vocabulary: completed | in_progress | failed (+ pending).
function statusMeta(status = '') {
  const s = String(status).toLowerCase();
  if (s === 'completed' || s === 'done' || s === 'success') return { icon: Icon.statusDone, label: t('tasks.lastDone') };
  if (s === 'in_progress' || s === 'running' || s === 'claimed') return { icon: Icon.statusRunning, label: t('tasks.lastRunning') };
  if (s === 'failed' || s === 'error') return { icon: Icon.statusError, label: t('tasks.lastFailed') };
  return { icon: Icon.statusIdle, label: status || '' };
}

function renderConfiguredTask(task, onAction) {
  const isEnabled = task.enabled !== false;
  const el = document.createElement('div');
  el.className = `task-row${!isEnabled ? ' task-row--disabled' : ''}`;
  const sched = task.recurrence_human || task.recurrence || task.cron || task.schedule;
  const last = task.last_status ? statusMeta(task.last_status) : null;
  const nextRun = task.next_run_at ? t('tasks.nextRun', { when: relativeTime(task.next_run_at) }) : '';
  const oneShotChip = task.one_shot
    ? `<span class="task-meta-chip">${esc(t('tasks.oneShotChip'))}</span>`
    : '';
  el.innerHTML = `
    <div class="task-row__info">
      <div class="task-row__name">${esc(task.label ?? task.title ?? task.name ?? task.task_id)} ${oneShotChip}</div>
      ${sched ? `<div class="task-row__schedule">${Icon.clock} ${esc(sched)}</div>` : ''}
      ${nextRun ? `<div class="task-row__schedule">${esc(nextRun)}</div>` : ''}
    </div>
    <div class="task-row__actions">
      ${last ? `<span class="task-status-chip">${last.icon} ${esc(last.label)}</span>` : ''}
      <button class="btn btn--ghost btn--sm toggle-task-btn" data-enabled="${isEnabled}" aria-label="${esc(isEnabled ? t('tasks.pauseAriaLabel') : t('tasks.activateAriaLabel'))}">
        ${esc(isEnabled ? t('tasks.pauseBtn') : t('tasks.activateBtn'))}
      </button>
      <button class="btn btn--ghost btn--sm btn--danger-ghost" data-action="delete" aria-label="${esc(t('tasks.deleteAriaLabel'))}">${Icon.trash}</button>
    </div>`;

  el.querySelector('.toggle-task-btn')?.addEventListener('click', () => onAction('toggle', task, el));
  el.querySelector('[data-action="delete"]')?.addEventListener('click', () => onAction('delete', task, el));

  return el;
}

function renderRecentTask(task) {
  const el = document.createElement('div');
  el.className = 'recent-task-row';
  const meta = statusMeta(task.status);
  const when = task.claimed_at ?? task.enqueued_at ?? task.started_at;
  el.innerHTML = `
    <div class="recent-task-row__status" aria-label="${esc(meta.label)}">${meta.icon}</div>
    <div class="recent-task-row__info">
      <div class="recent-task-row__name">${esc(task.label ?? task.name ?? task.task_id ?? t('tasks.defaultName'))}</div>
      ${when ? `<div class="recent-task-row__time">${esc(relativeTime(when))}</div>` : ''}
    </div>`;
  return el;
}

export async function renderTasksView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('tasks.title'))}</h2>
        <p class="cv-subtitle">${esc(t('tasks.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="cv-section-head">
          <div class="cv-section-label">${esc(t('tasks.scheduledSection'))}</div>
          <div class="cv-section-head__right">
            <button class="btn btn--primary btn--sm" id="tasks-new-btn">${esc(t('tasks.newBtn'))}</button>
            <div class="seg-toggle" role="tablist" aria-label="${esc(t('tasks.scheduledSection'))}">
              <button class="seg-toggle__btn is-active" id="tasks-view-board" role="tab">${esc(t('tasks.viewBoard'))}</button>
              <button class="seg-toggle__btn" id="tasks-view-list" role="tab">${esc(t('tasks.viewList'))}</button>
            </div>
          </div>
        </div>
        <div id="tasks-board"><div class="cv-skeleton"></div></div>
        <div class="cv-list" id="tasks-configured-list" hidden><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('tasks.recentSection'))}</div>
        <div class="cv-list" id="tasks-recent-list"><div class="cv-skeleton"></div></div>
      </div>
    </div>

    <!-- Friendly create modal: click a day → pick time + days + details, no cron -->
    <div id="task-modal" class="modal-overlay" hidden>
      <div class="modal-card" role="dialog" aria-modal="true" aria-label="${esc(t('tasks.modalTitle'))}">
        <div class="modal-card__head">
          <h3 class="modal-card__title">${esc(t('tasks.modalTitle'))}</h3>
          <button class="icon-btn" id="task-modal-close" aria-label="${esc(t('common.close'))}">${Icon.close}</button>
        </div>
        <div class="modal-card__body">
          <label class="cv-label">${esc(t('tasks.nameLabel'))}</label>
          <input id="tm-name" class="cv-input" type="text" placeholder="${esc(t('tasks.namePlaceholder'))}" autocomplete="off">
          <label class="cv-label">${esc(t('tasks.promptLabel'))}</label>
          <textarea id="tm-prompt" class="cv-textarea" rows="3" placeholder="${esc(t('tasks.promptPlaceholder'))}"></textarea>
          <label class="cv-label" for="tm-mode">${esc(t('tasks.modeLabel'))}</label>
          <select id="tm-mode" class="cv-input">
            <option value="recurrent">${esc(t('tasks.modeRecurrent'))}</option>
            <option value="once">${esc(t('tasks.modeOnce'))}</option>
          </select>
          <div id="tm-days-wrap">
            <label class="cv-label">${esc(t('tasks.daysLabel'))}</label>
            <div class="day-chips" id="tm-days">
              ${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((_, i) =>
                `<button type="button" class="day-chip" data-day="${i}">${esc(t(`tasks.dow${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][i]}`))}</button>`).join('')}
              <button type="button" class="day-chip day-chip--all" id="tm-everyday">${esc(t('tasks.everyDay'))}</button>
            </div>
          </div>
          <div id="tm-date-wrap" hidden>
            <label class="cv-label" for="tm-date">${esc(t('tasks.dateLabel'))}</label>
            <input id="tm-date" class="cv-input" type="date">
          </div>
          <div class="task-form-grid">
            <div>
              <label class="cv-label" for="tm-time">${esc(t('tasks.timeLabel'))}</label>
              <input id="tm-time" class="cv-input" type="time" value="09:00">
            </div>
            <div>
              <label class="cv-label" for="tm-time-end">${esc(t('tasks.timeEndLabel'))}</label>
              <input id="tm-time-end" class="cv-input" type="time" placeholder="--:--">
            </div>
            <div>
              <label class="cv-label" for="tm-agent">${esc(t('tasks.targetAgentLabel'))}</label>
              <select id="tm-agent" class="cv-input"><option value="">${esc(t('tasks.anyAgent'))}</option></select>
            </div>
            <div>
              <label class="cv-label" for="tm-risk">${esc(t('tasks.riskLabel'))}</label>
              <select id="tm-risk" class="cv-input">
                <option value="low">${esc(t('tasks.riskLow'))}</option>
                <option value="high">${esc(t('tasks.riskHigh'))}</option>
              </select>
            </div>
          </div>
        </div>
        <div class="modal-card__actions">
          <button class="btn btn--ghost btn--sm" id="tm-cancel">${esc(t('common.cancel'))}</button>
          <button class="btn btn--primary btn--sm" id="tm-create">${esc(t('tasks.createBtn'))}</button>
        </div>
      </div>
    </div>`;

  // Resolve target_agent_id → display name (Brain for the default agent).
  let _agentsById = {};
  async function loadAgents() {
    const agents = await listAgents().catch(() => []);
    const arr = Array.isArray(agents) ? agents : [];
    _agentsById = {};
    arr.forEach(a => { _agentsById[a.agent_id ?? a.id] = a; });
    // Populate the modal agent picker (custom agents; default = "Cerebro").
    const sel = container.querySelector('#tm-agent');
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = `<option value="">${esc(t('tasks.anyAgent'))}</option>` +
        arr.filter(a => !a.is_default).map(a =>
          `<option value="${esc(a.agent_id ?? a.id)}">${esc(a.name ?? a.alias ?? a.agent_id)}</option>`).join('');
      sel.value = cur;
    }
  }
  function agentLabel(task) {
    const id = task.target_agent_id ?? task.agent_id;
    if (!id) return t('tasks.allAgents');
    const a = _agentsById[id];
    if (a?.is_default) return t('tasks.brain');
    return a?.name ?? a?.alias ?? t('tasks.brain');
  }

  function agentChip(task) {
    const id = task.target_agent_id ?? task.agent_id ?? 'default';
    const hue = agentHue(id);
    return `<span class="task-chip__agent" style="background:hsl(${hue} 70% 50% / .18);color:hsl(${hue} 70% 72%)">${esc(agentLabel(task))}</span>`;
  }

  function taskCron(task) { return task.recurrence ?? task.cron ?? task.schedule ?? task.trigger?.cron ?? ''; }

  async function loadBoard() {
    const board = document.getElementById('tasks-board');
    if (!board) return;
    const [result] = await Promise.all([listConfiguredTasks(), loadAgents()]);
    const tasks = (result?.tasks ?? []).filter(tk => tk.enabled !== false || true);
    if (tasks.length === 0) {
      board.innerHTML = `<div class="cv-empty">${esc(t('tasks.boardEmpty'))}</div>`;
      return;
    }
    // Bucket tasks per weekday (Mon..Sun) + a daily lane.
    const cols = Array.from({ length: 7 }, () => []);
    const daily = [];
    for (const tk of tasks) {
      const { days, time, daily: isDaily } = parseCron(taskCron(tk));
      const chip = { task: tk, time };
      if (isDaily || days.size === 0) daily.push(chip);
      else days.forEach(d => cols[d].push(chip));
    }
    const chipHtml = (c) => `<div class="task-chip${c.task.enabled === false ? ' task-chip--off' : ''}">
        ${c.time ? `<span class="task-chip__time">${esc(c.time)}</span>` : ''}
        <span class="task-chip__name" title="${esc(c.task.label ?? c.task.name ?? '')}">${esc(c.task.label ?? c.task.name ?? c.task.task_id ?? t('tasks.defaultName'))}</span>
        ${agentChip(c.task)}
      </div>`;
    const dailyHtml = daily.length
      ? `<div class="task-board__daily"><span class="task-board__daily-label">${esc(t('tasks.boardDaily'))}</span>
          <div class="task-board__daily-chips">${daily.map(chipHtml).join('')}</div></div>`
      : '';
    const gridHtml = `<div class="task-board__grid">${
      cols.map((chips, i) => `<div class="task-board__col" data-day="${i}" role="button" tabindex="0" title="${esc(t('tasks.addOnDay'))}">
        <div class="task-board__dow">${esc(t(DOW_KEYS[i]))}</div>
        <div class="task-board__cells">${chips.length ? chips.map(chipHtml).join('') : ''}<div class="task-board__add">+</div></div>
      </div>`).join('')
    }</div>`;
    board.innerHTML = dailyHtml + gridHtml;
    // Click a day → open the friendly create modal with that day preselected.
    board.querySelectorAll('.task-board__col[data-day]').forEach(col => {
      const open = (e) => {
        // ignore clicks on an existing task chip (those are for future detail/edit)
        if (e.target.closest('.task-chip')) return;
        openTaskModal([Number(col.dataset.day)]);
      };
      col.addEventListener('click', open);
      col.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(e); } });
    });
  }

  async function loadConfigured() {
    const result = await listConfiguredTasks();
    const list = document.getElementById('tasks-configured-list');
    if (!list) return;
    list.innerHTML = '';
    const arr = (result?.tasks ?? []);
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(t('tasks.empty'))}</div>`;
    } else {
      arr.forEach(task => list.appendChild(renderConfiguredTask(task, async (action, tsk) => {
        const id = tsk.trigger_id ?? tsk.task_id ?? tsk.id;
        if (action === 'toggle') {
          try {
            await toggleTask(id, tsk.enabled === false);
            showToast(tsk.enabled !== false ? t('tasks.paused') : t('tasks.activated'), 'ok');
            loadConfigured();
          } catch (e) { showToast(e.message, 'error'); }
        } else if (action === 'delete') {
          const name = tsk.name ?? id;
          if (!(await confirmDialog(t('tasks.confirmDelete', { name })))) return;
          try {
            await deleteTask(id);
            showToast(t('tasks.deleted'), 'ok');
            loadConfigured();
          } catch (e) { showToast(e.message, 'error'); }
        }
      })));
    }
  }

  async function loadRecent() {
    const result = await listRecentTasks(20);
    const list = document.getElementById('tasks-recent-list');
    if (!list) return;
    list.innerHTML = '';
    const arr = result?.tasks ?? [];
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(t('tasks.noRecent'))}</div>`;
    } else {
      arr.forEach(task => list.appendChild(renderRecentTask(task)));
    }
  }

  // ── Friendly create modal (day chips + time picker → cron built internally) ──
  const modal = container.querySelector('#task-modal');
  const dayChips = () => [...container.querySelectorAll('#tm-days .day-chip[data-day]')];
  function selectedDays() {
    return dayChips().filter(c => c.classList.contains('is-on')).map(c => Number(c.dataset.day));
  }
  function syncEveryday() {
    const all = selectedDays().length === 7;
    container.querySelector('#tm-everyday')?.classList.toggle('is-on', all);
  }
  // Recurrent: Mon=0..Sun=6 → cron dow (0=Sun..6=Sat); all 7 → "*".
  // One-off: a specific date → cron "min hour DD MM *" (+ one_shot).
  function buildCron({ mode, days, date, time }) {
    const [hh, mm] = (time || '09:00').split(':');
    const min = parseInt(mm, 10) || 0, hour = parseInt(hh, 10) || 0;
    if (mode === 'once') {
      const [, mo, dd] = (date || '').split('-');   // YYYY-MM-DD
      return `${min} ${hour} ${parseInt(dd, 10)} ${parseInt(mo, 10)} *`;
    }
    if (days.length === 0 || days.length === 7) return `${min} ${hour} * * *`;
    const dow = days.map(d => (d === 6 ? 0 : d + 1)).sort((a, b) => a - b).join(',');
    return `${min} ${hour} * * ${dow}`;
  }
  // Recurrent → day chips; one-off → date picker.
  function syncMode() {
    const once = container.querySelector('#tm-mode')?.value === 'once';
    container.querySelector('#tm-days-wrap').hidden = once;
    container.querySelector('#tm-date-wrap').hidden = !once;
  }
  function openTaskModal(presetDays = []) {
    if (!modal) return;
    container.querySelector('#tm-name').value = '';
    container.querySelector('#tm-prompt').value = '';
    container.querySelector('#tm-time').value = '09:00';
    container.querySelector('#tm-time-end').value = '';
    container.querySelector('#tm-date').value = '';
    container.querySelector('#tm-mode').value = 'recurrent';
    container.querySelector('#tm-risk').value = 'low';
    dayChips().forEach(c => c.classList.toggle('is-on', presetDays.includes(Number(c.dataset.day))));
    syncEveryday();
    syncMode();
    modal.hidden = false;
    setTimeout(() => container.querySelector('#tm-name')?.focus(), 30);
  }
  function closeTaskModal() { if (modal) modal.hidden = true; }

  dayChips().forEach(c => c.addEventListener('click', () => { c.classList.toggle('is-on'); syncEveryday(); }));
  container.querySelector('#tm-everyday')?.addEventListener('click', () => {
    const turnOn = selectedDays().length !== 7;
    dayChips().forEach(c => c.classList.toggle('is-on', turnOn));
    syncEveryday();
  });
  container.querySelector('#tasks-new-btn')?.addEventListener('click', () => openTaskModal([]));
  container.querySelector('#task-modal-close')?.addEventListener('click', closeTaskModal);
  container.querySelector('#tm-cancel')?.addEventListener('click', closeTaskModal);
  container.querySelector('#tm-mode')?.addEventListener('change', syncMode);
  modal?.addEventListener('click', e => { if (e.target === modal) closeTaskModal(); });

  container.querySelector('#tm-create')?.addEventListener('click', async () => {
    const name = container.querySelector('#tm-name')?.value.trim();
    let prompt = container.querySelector('#tm-prompt')?.value.trim();
    if (!name || !prompt) { showToast(t('tasks.nameAndPromptRequired'), 'warn'); return; }
    const once = container.querySelector('#tm-mode')?.value === 'once';
    const days = selectedDays();
    const date = container.querySelector('#tm-date')?.value;
    if (once && !date) { showToast(t('tasks.dateRequired'), 'warn'); return; }
    if (!once && days.length === 0) { showToast(t('tasks.daysRequired'), 'warn'); return; }
    const time = container.querySelector('#tm-time')?.value || '09:00';
    const timeEnd = container.querySelector('#tm-time-end')?.value;
    // "Franja" for long tasks: the scheduler fires at the start; pass the window
    // to the agent as guidance (the cron models a start, not a duration).
    if (timeEnd) prompt += `\n\n(Ventana de trabajo: ${time}–${timeEnd}.)`;
    const cron = buildCron({ mode: once ? 'once' : 'recurrent', days, date, time });
    const btn = container.querySelector('#tm-create');
    if (btn) btn.disabled = true;
    try {
      await createTask({
        label: name,
        cron,
        instruction: prompt,
        target_agent_id: container.querySelector('#tm-agent')?.value || undefined,
        risk_ceiling: container.querySelector('#tm-risk')?.value || 'low',
        one_shot: once,
      });
      showToast(t('tasks.created'), 'ok');
      closeTaskModal();
      loadConfigured();
      loadBoard();
    } catch (e) { showToast(e.message, 'error'); }
    finally { if (btn) btn.disabled = false; }
  });

  // List ↔ Calendar toggle
  const boardEl = container.querySelector('#tasks-board');
  const listEl = container.querySelector('#tasks-configured-list');
  const btnBoard = container.querySelector('#tasks-view-board');
  const btnList = container.querySelector('#tasks-view-list');
  function setMode(mode) {
    const board = mode === 'board';
    if (boardEl) boardEl.hidden = !board;
    if (listEl) listEl.hidden = board;
    btnBoard?.classList.toggle('is-active', board);
    btnList?.classList.toggle('is-active', !board);
    if (board) loadBoard();
  }
  btnBoard?.addEventListener('click', () => setMode('board'));
  btnList?.addEventListener('click', () => setMode('list'));

  loadBoard();
  loadConfigured();
  loadRecent();
}
