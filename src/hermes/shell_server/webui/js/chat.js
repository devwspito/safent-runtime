/**
 * chat.js — Chat conversation renderer and streaming controller.
 *
 * Manages the center column: message history, live streaming output,
 * tool-call step groups, thinking blocks, HITL approval cards,
 * and the right-panel Progreso live checklist.
 */

import { postChat, getConversation } from './api.js';
import { openTaskStream } from './stream.js';
import { renderMarkdown } from './markdown.js';
import { Icon } from './icons.js';
import { triggerApprovalPoll } from './approvals.js';
import { showToast } from './shell.js';
import { t, onLangChange } from './i18n.js';

const BODY_ID = 'chat-body';
const STATUS_ID = 'chat-status';

let _stream = null;
let _currentConvId = null;

const LAST_CONV_KEY = 'lumen.lastConv';
function persistConv(id) {
  try {
    if (id) localStorage.setItem(LAST_CONV_KEY, id);
    else localStorage.removeItem(LAST_CONV_KEY);
  } catch { /* localStorage unavailable */ }
}
/** The conversation id to restore on reload (null if none / new task). */
export function getPersistedConvId() {
  try { return localStorage.getItem(LAST_CONV_KEY) || null; } catch { return null; }
}

/** RFC4122 v4 — crypto.randomUUID where available, fallback otherwise. */
function genUUID() {
  try { if (globalThis.crypto?.randomUUID) return crypto.randomUUID(); } catch { /* not secure ctx */ }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

// Local clean-render cache for the CURRENT conversation, so a page refresh
// restores the SAME polished view the user saw live (final answers only — never
// the raw inline reasoning the model streams). Bounded to one conversation.
const CONV_CACHE_KEY = 'lumen.convCache';
function cacheGet() {
  try { return JSON.parse(localStorage.getItem(CONV_CACHE_KEY) || 'null'); } catch { return null; }
}
function cachePush(id, role, content) {
  if (!id) return;
  try {
    let c = cacheGet();
    if (!c || c.id !== id) c = { id, messages: [] };
    c.messages.push({ role, content });
    localStorage.setItem(CONV_CACHE_KEY, JSON.stringify(c));
  } catch { /* quota / unavailable — degrade to daemon fetch */ }
}
function cacheFor(id) {
  const c = cacheGet();
  return (c && c.id === id && Array.isArray(c.messages) && c.messages.length) ? c.messages : null;
}

function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Tool label helpers ────────────────────────────────────────────────────────

// Keyed on the backend's technical tool name (see nous_engine._describe_tool_call).
// Values are Lucide SVG strings from the Icon module — no emojis anywhere.
const TOOL_ICON = {
  browser_navigate: Icon.toolNavigate, browser_click: Icon.toolClick,
  browser_type: Icon.toolType, browser_snapshot: Icon.toolCamera,
  browser_back: Icon.toolBack, web_search: Icon.toolSearch,
  web_extract: Icon.toolLink, search_files: Icon.toolFileSearch,
  session_search: Icon.toolFolder, read_file: Icon.toolFileText,
  write_file: Icon.toolFilePen, patch: Icon.toolPatch,
  create_file: Icon.toolFilePlus, delete_file: Icon.toolTrash,
  terminal: Icon.toolTerminal, shell: Icon.toolTerminal,
  execute_code: Icon.toolCode, computer_use: Icon.toolMonitor,
  activate_app: Icon.toolApp, memory: Icon.toolBrain,
  clarify: Icon.toolHelp,
};

// Backend emits the descriptor nested under frame.tool_call = {tool, label, target}.
function toolLabel(frame) {
  const d = frame.tool_call ?? frame;
  const tool = d.tool ?? d.tool_name ?? 'herramienta';
  const icon = TOOL_ICON[tool] ?? Icon.toolWrench;
  const human = d.label ?? String(tool).replace(/_/g, ' ');
  const target = d.target ?? '';
  return { icon, human, target: String(target).slice(0, 80) };
}

// ── Progress checklist ────────────────────────────────────────────────────────

let _checklistSteps = [];
let _checklistEl = null;

export function initProgressChecklist() {
  _checklistSteps = [];
  _checklistEl = document.getElementById('progress-checklist');
  if (_checklistEl) {
    _checklistEl.innerHTML = `<div class="progress-checklist-empty">${escapeText(t('chat.progressIdle'))}</div>`;
  }
}

function addChecklistStep(id, label) {
  const existing = _checklistSteps.find(s => s.id === id);
  if (existing) return;

  const step = { id, label, status: 'running' };
  _checklistSteps.push(step);
  renderChecklist();
}

function completeChecklistStep(id) {
  const step = _checklistSteps.find(s => s.id === id);
  if (step) { step.status = 'done'; renderChecklist(); }
}

function markAllChecklistDone() {
  _checklistSteps.forEach(s => { s.status = 'done'; });
  renderChecklist();
}

function renderChecklist() {
  if (!_checklistEl) return;
  if (_checklistSteps.length === 0) {
    _checklistEl.innerHTML = `<div class="progress-checklist-empty">${escapeText(t('chat.progressEmpty'))}</div>`;
    return;
  }
  _checklistEl.innerHTML = '';
  _checklistSteps.forEach((step, i) => {
    const item = document.createElement('div');
    item.className = `checklist-item checklist-item--${step.status}`;
    item.setAttribute('aria-label', `Paso ${i + 1}: ${step.label} — ${step.status === 'done' ? 'completado' : 'en progreso'}`);
    const icon = step.status === 'done' ? Icon.checkboxDone
      : step.status === 'running' ? Icon.checkboxRunning
      : Icon.checkboxEmpty;
    item.innerHTML = `
      <span class="checklist-item__icon" aria-hidden="true">${icon}</span>
      <span class="checklist-item__label">${escapeText(step.label)}</span>`;
    _checklistEl.appendChild(item);
  });
}

// ── Scroll pinning ────────────────────────────────────────────────────────────

let _pinToBottom = true;
let _userScrolled = false;

function setupScrollPin(bodyEl) {
  bodyEl.addEventListener('scroll', () => {
    const nearBottom = bodyEl.scrollTop + bodyEl.clientHeight >= bodyEl.scrollHeight - 80;
    _pinToBottom = nearBottom;
    _userScrolled = !nearBottom;
  });
}

function scrollToBottom(bodyEl, force = false) {
  if (!_userScrolled || force) {
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }
}

// ── Message elements ──────────────────────────────────────────────────────────

function createUserBubble(text) {
  const el = document.createElement('div');
  el.className = 'message message--user';
  el.setAttribute('role', 'article');
  el.setAttribute('aria-label', 'Tu mensaje');
  const bubble = document.createElement('div');
  bubble.className = 'message__bubble';
  bubble.textContent = text;
  el.appendChild(bubble);
  return el;
}

function createAgentMessage() {
  const el = document.createElement('div');
  el.className = 'message message--agent';
  el.setAttribute('role', 'article');
  el.setAttribute('aria-label', 'Respuesta de Lumen');
  const content = document.createElement('div');
  content.className = 'message__content agent-prose';
  el.appendChild(content);
  return { el, content };
}

function createThinkingBlock() {
  const el = document.createElement('details');
  el.className = 'thinking-block';
  const summary = document.createElement('summary');
  summary.className = 'thinking-block__summary';
  summary.innerHTML = `${Icon.thinking} <span class="thinking-block__label">Proceso de pensamiento</span> <span class="thinking-block__chevron">${Icon.chevronRight}</span>`;
  const body = document.createElement('div');
  body.className = 'thinking-block__body';
  el.appendChild(summary);
  el.appendChild(body);
  return { el, body, summary };
}

function createToolSummaryGroup() {
  const el = document.createElement('details');
  el.className = 'tool-summary-group';
  el.open = false;
  const summary = document.createElement('summary');
  summary.className = 'tool-summary-group__summary';
  const labelSpan = document.createElement('span');
  labelSpan.className = 'tool-summary-group__label';
  labelSpan.textContent = t('chat.toolsUsed', { n: 0, plural: 's' });
  const chevron = document.createElement('span');
  chevron.className = 'tool-summary-group__chevron';
  chevron.innerHTML = Icon.chevronRight;
  summary.appendChild(labelSpan);
  summary.appendChild(chevron);
  const body = document.createElement('div');
  body.className = 'tool-summary-group__body';
  el.appendChild(summary);
  el.appendChild(body);
  return { el, body, labelSpan };
}

// ── Load conversation history ─────────────────────────────────────────────────

export async function loadConversation(convId) {
  _currentConvId = convId;
  persistConv(convId);
  _userScrolled = false;
  _pinToBottom = true;

  const bodyEl = document.getElementById(BODY_ID);
  if (!bodyEl) return;

  // Prefer the local clean cache (same polished view shown live). Falls back to
  // the daemon store for conversations not produced in this browser session.
  const cached = cacheFor(convId);
  let messages;
  let title = null;

  if (cached) {
    messages = cached;
  } else {
    bodyEl.innerHTML = `<div class="chat-loading">${Icon.spinner} <span>Cargando conversación…</span></div>`;
    let data;
    try {
      data = await getConversation(convId);
    } catch {
      bodyEl.innerHTML = `<div class="chat-empty"><p>${escapeText(t('chat.loadError'))}</p></div>`;
      return;
    }
    messages = data?.messages ?? [];
    title = data?.title ?? null;
  }

  bodyEl.innerHTML = '';

  if (messages.length === 0) {
    showWelcome(bodyEl);
  } else {
    messages.forEach(msg => {
      if (msg.role === 'user') {
        bodyEl.appendChild(createUserBubble(msg.content));
      } else if (msg.role === 'assistant') {
        const { el, content } = createAgentMessage();
        content.innerHTML = renderMarkdown(msg.content);
        bodyEl.appendChild(el);
      }
    });
  }

  setupScrollPin(bodyEl);
  scrollToBottom(bodyEl, true);
  initProgressChecklist();

  const titleEl = document.getElementById('task-title');
  if (titleEl && title) titleEl.textContent = title;
}

function showWelcome(bodyEl) {
  bodyEl.innerHTML = `
    <div class="chat-welcome" role="main">
      <div class="welcome-mark" aria-hidden="true">L</div>
      <h1 class="welcome-title">${escapeText(t('chat.welcomeTitle'))}</h1>
      <p class="welcome-subtitle">${escapeText(t('chat.welcomeSubtitle'))}</p>
      <div class="welcome-suggestions" role="list">
        ${[
          t('chat.suggest1'),
          t('chat.suggest2'),
          t('chat.suggest3'),
          t('chat.suggest4'),
        ].map(s => `<button class="suggestion-pill" role="listitem">${escapeText(s)}</button>`).join('')}
      </div>
    </div>`;

  bodyEl.querySelectorAll('.suggestion-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById('composer-input');
      if (input) {
        input.value = btn.textContent;
        input.focus();
        input.dispatchEvent(new Event('input'));
      }
    });
  });
}

// ── New conversation ──────────────────────────────────────────────────────────

export function startNewConversation() {
  _currentConvId = null;
  persistConv(null);
  _userScrolled = false;
  _pinToBottom = true;

  const bodyEl = document.getElementById(BODY_ID);
  if (!bodyEl) return;
  bodyEl.innerHTML = '';
  showWelcome(bodyEl);
  setupScrollPin(bodyEl);
  initProgressChecklist();

  const titleEl = document.getElementById('task-title');
  if (titleEl) titleEl.textContent = 'Nueva tarea';

  if (_stream) { _stream.close(); _stream = null; }
}

// ── Send message ──────────────────────────────────────────────────────────────

export async function sendMessage(text, { onStreamStart, onStreamEnd } = {}) {
  if (_stream) { _stream.close(); _stream = null; }

  const bodyEl = document.getElementById(BODY_ID);
  const statusEl = document.getElementById(STATUS_ID);
  if (!bodyEl) return;

  const welcome = bodyEl.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  // Own the conversation id client-side: the daemon honours payload.conversation_id
  // (else it mints its own and never tells us → refresh lost the thread). Set it
  // BEFORE sending so a mid-stream refresh still resolves to the right thread.
  if (!_currentConvId) {
    _currentConvId = genUUID();
    persistConv(_currentConvId);
  }
  cachePush(_currentConvId, 'user', text);

  const userBubble = createUserBubble(text);
  bodyEl.appendChild(userBubble);
  setupScrollPin(bodyEl);
  scrollToBottom(bodyEl, true);

  // Reset progress checklist for new task
  initProgressChecklist();
  addChecklistStep('send', 'Enviando mensaje');

  const dedupKey = `chat:${Date.now()}:${Math.random().toString(36).slice(2)}`;

  let data;
  try {
    data = await postChat({
      conversation_id: _currentConvId,
      user_message: text,
      dedup_key: dedupKey,
    });
  } catch (err) {
    showToast(err.message ?? 'Error al enviar', 'error');
    onStreamEnd?.();
    return;
  }

  completeChecklistStep('send');

  const taskId = data.task_id;
  persistConv(_currentConvId);

  const { el: agentEl, content: agentContent } = createAgentMessage();
  bodyEl.appendChild(agentEl);
  scrollToBottom(bodyEl, true);
  onStreamStart?.();

  attachLiveStream({
    taskId, convId: _currentConvId, userText: text,
    bodyEl, agentEl, agentContent, statusEl, onStreamEnd,
  });
}

// ── Live in-flight persistence (Claude-Code-style) ──────────────────────────────
// The daemon stream does not replay, so we persist the in-flight turn (thinking,
// tools, partial answer) to localStorage as it streams. On reload we restore that
// snapshot and reconnect to the running task — nothing is lost on refresh.
const LIVE_KEY = 'lumen.live';
let _liveLastWrite = 0;
function liveSet(snap, { force = false } = {}) {
  const now = Date.now();
  if (!force && now - _liveLastWrite < 500) return;
  _liveLastWrite = now;
  try { localStorage.setItem(LIVE_KEY, JSON.stringify(snap)); } catch { /* quota */ }
}
function liveGet() {
  try { return JSON.parse(localStorage.getItem(LIVE_KEY) || 'null'); } catch { return null; }
}
function liveClear() {
  try { localStorage.removeItem(LIVE_KEY); } catch { /* noop */ }
}

/**
 * Drive a task stream into an agent message, persisting a live snapshot so a
 * refresh can restore + reconnect. `seed` (optional) pre-populates state when
 * resuming an in-flight turn after reload.
 */
function attachLiveStream({ taskId, convId, userText, bodyEl, agentEl, agentContent, statusEl, onStreamEnd, seed = null }) {
  setStatus(statusEl, 'running', 'Procesando…');

  let thinkingBlock = null;
  let thinkingContent = seed?.thinking ?? '';
  let thinkingDone = seed?.thinkingDone ?? false;
  let toolSummaryGroup = null;
  let toolCallCount = seed?.toolCallCount ?? 0;
  let agentText = seed?.agentText ?? '';
  let segmentStart = seed?.segmentStart ?? 0;
  let activityEl = null;
  const tools = Array.isArray(seed?.tools) ? seed.tools.slice() : [];

  const snap = (force) => liveSet({
    convId, taskId, userText,
    agentText, segmentStart, thinking: thinkingContent, thinkingDone, toolCallCount, tools,
  }, { force });

  const thinkingDoneSummary = () =>
    `${Icon.thinkingDone} <span class="thinking-block__label">Proceso de pensamiento</span> <span class="thinking-block__done">${Icon.check} Listo</span> <span class="thinking-block__chevron">${Icon.chevronRight}</span>`;

  function ensureThinking() {
    if (!thinkingBlock) { thinkingBlock = createThinkingBlock(); agentEl.insertBefore(thinkingBlock.el, agentContent); }
  }
  function closeThinking() {
    if (thinkingBlock && !thinkingDone) {
      thinkingDone = true;
      thinkingBlock.summary.innerHTML = thinkingDoneSummary();
      thinkingBlock.el.open = false;
    }
  }
  function ensureToolGroup() {
    if (!toolSummaryGroup) {
      toolSummaryGroup = createToolSummaryGroup();
      agentEl.insertBefore(toolSummaryGroup.el, activityEl ?? agentContent);
    }
  }
  function renderToolHeader(name, human, target) {
    const icon = TOOL_ICON[name] ?? Icon.toolWrench;
    toolSummaryGroup.labelSpan.innerHTML =
      `<span class="tool-summary-group__emoji" aria-hidden="true">${icon}</span>` +
      `<span class="tool-summary-group__action">${escapeText(human)}${target ? ` <span class="tool-summary-group__target">${escapeText(String(target).slice(0, 48))}</span>` : ''}</span>` +
      `<span class="tool-summary-group__count" title="${escapeText(t('chat.toolsUsed', { n: toolCallCount, plural: toolCallCount > 1 ? 's' : '' }))}">${toolCallCount}</span>`;
  }
  function addToolStep(name, human, target) {
    const icon = TOOL_ICON[name] ?? Icon.toolWrench;
    const stepEl = document.createElement('div');
    stepEl.className = 'tool-step-item';
    stepEl.innerHTML = `<span class="tool-step-item__emoji" aria-hidden="true">${icon}</span>
      <span class="tool-step-item__label">${escapeText(human)}</span>
      ${target ? `<span class="tool-step-item__target">${escapeText(target)}</span>` : ''}`;
    toolSummaryGroup.body.appendChild(stepEl);
  }
  function showActivity(textStr) {
    if (!activityEl) {
      activityEl = document.createElement('div');
      activityEl.className = 'agent-activity';
      agentEl.insertBefore(activityEl, agentContent);
    }
    const lines = String(textStr).split('\n').map(s => s.trim()).filter(Boolean);
    activityEl.textContent = lines.length ? `· ${lines[lines.length - 1]}` : '';
  }

  initProgressChecklist();

  // Re-render the seeded (resumed) state so the user sees exactly what they had.
  if (seed) {
    if (thinkingContent) { ensureThinking(); thinkingBlock.body.textContent = thinkingContent; if (thinkingDone) closeThinking(); }
    if (tools.length) {
      ensureToolGroup();
      tools.forEach((tl, i) => {
        addToolStep(tl.name, tl.human, tl.target);
        addChecklistStep(`tool:${i + 1}`, `${tl.human}${tl.target ? `: ${String(tl.target).slice(0, 40)}` : ''}`);
      });
      const last = tools[tools.length - 1];
      renderToolHeader(last.name, last.human, last.target);
    }
    const seg = agentText.slice(segmentStart);
    if (seg.trim()) showActivity(seg);
  }
  snap(true);

  function finalize() {
    closeThinking();
    markAllChecklistDone();
    if (toolSummaryGroup && toolCallCount > 0) {
      toolSummaryGroup.labelSpan.innerHTML =
        `<span class="tool-summary-group__emoji" aria-hidden="true">${Icon.check}</span>` +
        `<span class="tool-summary-group__action">${escapeText(t('chat.toolsUsed', { n: toolCallCount, plural: toolCallCount > 1 ? 's' : '' }))}</span>`;
    }
    if (activityEl) { activityEl.remove(); activityEl = null; }
    const answer = (agentText.slice(segmentStart).trim() || agentText.trim());
    agentContent.innerHTML = renderMarkdown(answer);
    cachePush(convId, 'assistant', answer);
    liveClear();
    setStatus(statusEl, 'idle', '');
    _stream = null;
    onStreamEnd?.();
    scrollToBottom(bodyEl, true);
    const titleEl = document.getElementById('task-title');
    if (titleEl && agentText.length > 0) titleEl.textContent = truncate(agentText, 60);
  }

  _stream = openTaskStream(taskId, {
    onDelta(chunk) {
      agentText += chunk;
      showActivity(agentText.slice(segmentStart));
      snap();
      scrollToBottom(bodyEl);
    },
    onThinking(chunk) {
      thinkingContent += chunk;
      ensureThinking();
      thinkingBlock.body.textContent = thinkingContent;
      snap();
      scrollToBottom(bodyEl);
    },
    onToolCall(frame) {
      closeThinking();
      toolCallCount++;
      segmentStart = agentText.length;
      if (activityEl) activityEl.textContent = '';
      ensureToolGroup();
      const d = frame.tool_call ?? frame;
      const name = d.tool ?? d.tool_name ?? 'herramienta';
      const human = d.label ?? String(name).replace(/_/g, ' ');
      const target = String(d.target ?? '').slice(0, 80);
      tools.push({ name, human, target });
      renderToolHeader(name, human, target);
      addToolStep(name, human, target);
      addChecklistStep(`tool:${toolCallCount}`, `${human}${target ? `: ${target.slice(0, 40)}` : ''}`);
      snap(true);
      scrollToBottom(bodyEl);
      triggerApprovalPoll();
    },
    onStatus(msg) { setStatus(statusEl, 'running', msg); },
    onDone() { finalize(); },
    onError(msg) {
      // If we were resuming and already have content, the task likely finished
      // while we were away — finalize from the snapshot instead of erroring.
      const answer = (agentText.slice(segmentStart).trim() || agentText.trim());
      if (seed && answer) { finalize(); return; }
      setStatus(statusEl, 'error', msg);
      showToast(msg, 'error');
      liveClear();
      _stream = null;
      onStreamEnd?.();
    },
  });
}

/** On reload, if a turn was in flight, restore it and reconnect to the task. */
export function resumeLiveIfAny({ onStreamStart, onStreamEnd } = {}) {
  const snapData = liveGet();
  if (!snapData || !snapData.taskId || snapData.convId !== _currentConvId) return false;
  const bodyEl = document.getElementById(BODY_ID);
  if (!bodyEl) return false;
  const welcome = bodyEl.querySelector('.chat-welcome');
  if (welcome) welcome.remove();
  // The user message is already on screen (restored from cache by loadConversation).
  const { el: agentEl, content: agentContent } = createAgentMessage();
  bodyEl.appendChild(agentEl);
  const statusEl = document.getElementById(STATUS_ID);
  onStreamStart?.();
  attachLiveStream({
    taskId: snapData.taskId, convId: snapData.convId, userText: snapData.userText,
    bodyEl, agentEl, agentContent, statusEl, onStreamEnd, seed: snapData,
  });
  return true;
}

export function stopStream() {
  if (_stream) { _stream.close(); _stream = null; }
  const statusEl = document.getElementById(STATUS_ID);
  setStatus(statusEl, 'idle', '');
}

export function getCurrentConvId() { return _currentConvId; }

// ── Status bar ────────────────────────────────────────────────────────────────

function setStatus(el, state, message) {
  if (!el) return;
  el.hidden = state === 'idle';
  el.className = `chat-status chat-status--${state}`;
  if (state === 'running') {
    el.innerHTML = `<span class="status-spinner" aria-hidden="true">${Icon.spinner}</span> <span>${escapeText(message)}</span>`;
  } else if (state === 'error') {
    el.innerHTML = `<span>${Icon.info}</span> <span>${escapeText(message)}</span>`;
  } else {
    el.innerHTML = '';
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function initChat() {
  const bodyEl = document.getElementById(BODY_ID);
  if (bodyEl) {
    setupScrollPin(bodyEl);
    showWelcome(bodyEl);
  }
  initProgressChecklist();

  // Re-localise the welcome screen + new-task title when the language changes.
  onLangChange(() => {
    const body = document.getElementById(BODY_ID);
    if (body && body.querySelector('.chat-welcome')) {
      showWelcome(body);
      const titleEl = document.getElementById('task-title');
      if (titleEl) titleEl.textContent = t('nav.newTask');
    }
  });
}

function truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }
