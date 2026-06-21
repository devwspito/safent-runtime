/**
 * recents.js — Sidebar "Recientes" panel.
 * Fetches recent conversations and renders a capped list (last 3) with a
 * "load more" / "show less" toggle so the rest of the sidebar stays visible.
 * Emits a 'select' event when the user picks a conversation.
 */

import { listConversations } from './api.js';
import { Icon } from './icons.js';
import { t, onLangChange } from './i18n.js';

const LIST_ID = 'recents-list';
const PREVIEW_COUNT = 3;

const NOW = () => Date.now();

function relativeTime(isoStr) {
  const diff = NOW() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return t('recents.now');
  if (mins < 60) return t('recents.minsAgo', { n: mins });
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return t('recents.hoursAgo', { n: hrs });
  const days = Math.floor(hrs / 24);
  return t('recents.daysAgo', { n: days });
}

function statusIcon(msgCount) {
  if (msgCount > 0) return Icon.statusDone;
  return Icon.statusIdle;
}

function renderSkeleton() {
  return Array.from({ length: PREVIEW_COUNT }, (_, i) =>
    `<li class="recent-item skeleton" aria-hidden="true" style="--delay:${i * 80}ms">
      <span class="recent-status-dot"></span>
      <span class="recent-title"></span>
    </li>`
  ).join('');
}

function renderEmpty() {
  return `<li class="recent-empty">
    <span class="recent-empty-text">${escapeText(t('recents.empty'))}</span>
  </li>`;
}

/**
 * @param {Array} conversations
 * @param {string|null} activeId
 * @param {boolean} expanded
 * @param {(id: string) => void} onSelect
 * @param {() => void} onToggle
 */
function renderList(conversations, activeId, expanded, onSelect, onToggle) {
  const list = document.getElementById(LIST_ID);
  if (!list) return;

  if (conversations.length === 0) {
    list.innerHTML = renderEmpty();
    return;
  }

  const visible = expanded ? conversations : conversations.slice(0, PREVIEW_COUNT);
  const overflow = conversations.length - PREVIEW_COUNT;

  const items = visible.map(c => {
    const isActive = c.conversation_id === activeId;
    return `<li class="recent-item${isActive ? ' recent-item--active' : ''}"
              role="option"
              aria-selected="${isActive}"
              data-conv-id="${c.conversation_id}"
              tabindex="0">
      <span class="recent-status-dot">${statusIcon(c.message_count)}</span>
      <span class="recent-title" title="${escapeAttr(c.title ?? t('common.untitled'))}">${escapeText(truncate(c.title ?? t('common.untitled'), 38))}</span>
      <span class="recent-time">${escapeText(relativeTime(c.last_msg_at))}</span>
    </li>`;
  }).join('');

  let toggle = '';
  if (overflow > 0) {
    const label = expanded ? t('recents.showLess') : t('recents.loadMore', { n: overflow });
    toggle = `<li class="recents-loadmore-wrap">
      <button type="button" class="recents-loadmore" data-recents-toggle aria-expanded="${expanded}">
        <span class="recents-loadmore__chevron${expanded ? ' is-open' : ''}" aria-hidden="true">${Icon.chevronRight}</span>
        <span>${escapeText(label)}</span>
      </button>
    </li>`;
  }

  list.innerHTML = items + toggle;

  list.querySelectorAll('.recent-item[data-conv-id]').forEach(el => {
    const handler = () => onSelect(el.dataset.convId);
    el.addEventListener('click', handler);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(); }
    });
  });

  const toggleBtn = list.querySelector('[data-recents-toggle]');
  if (toggleBtn) toggleBtn.addEventListener('click', onToggle);
}

function truncate(str, n) {
  return str.length > n ? str.slice(0, n) + '…' : str;
}

function escapeText(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttr(str) {
  return str.replace(/"/g, '&quot;');
}

/**
 * Initialise the recents panel.
 * @param {{ onSelect: (id: string) => void }} opts
 */
export function initRecents({ onSelect }) {
  const list = document.getElementById(LIST_ID);
  if (!list) return;
  list.innerHTML = renderSkeleton();

  let activeId = null;
  let expanded = false;
  let conversations = [];

  function rerender() {
    renderList(conversations, activeId, expanded, selectHandler, toggleHandler);
  }

  function selectHandler(id) {
    activeId = id;
    rerender();
    onSelect(id);
  }

  function toggleHandler() {
    expanded = !expanded;
    rerender();
  }

  async function load() {
    const convs = await listConversations();
    conversations = Array.isArray(convs) ? convs : [];
    rerender();
  }

  onLangChange(() => rerender());

  load();

  return {
    /** Call after a new conversation is started to refresh the list. */
    refresh() { load(); },
    /** Highlight a conversation as active. */
    setActive(id) { activeId = id; expanded = false; load(); },
  };
}
