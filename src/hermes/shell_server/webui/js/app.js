/**
 * app.js — Entry point. Orchestrates all modules after DOM is ready.
 */

import { initTheme, toggleTheme } from './theme.js';
import {
  toggleSidebar, toggleRightPanel, openSearch, closeSearch,
  initNavItems, initAdvancedToggle, initKeyboardShortcuts,
  initResponsive, initMobileOverlays, initExternalLinks,
  registerCapability, switchView, showToast,
} from './shell.js';
import { initRecents } from './recents.js';
import { initChat, loadConversation, startNewConversation, sendMessage, stopStream, getPersistedConvId, resumeLiveIfAny } from './chat.js';
import { initComposer } from './composer.js';
import { startApprovalPolling } from './approvals.js';
import { loadContextPanel } from './context-panel.js';
import { getProfile } from './api.js';
import { Icon } from './icons.js';
import { applyStaticI18n, getLang, setLang, t } from './i18n.js';

// Capability views (lazy-loaded on first visit)
import { renderProvidersView } from './providers.js';
import { renderAgentsView } from './agents.js';
import { renderOfficeView } from './office.js';
import { renderSkillsView } from './skills.js';
import { renderIntegrationsView } from './integrations.js';
import { renderMcpView } from './mcp.js';
import { renderTasksView } from './tasks-view.js';
import { renderSecurityView } from './security.js';
import { renderMemoryView } from './memory.js';

// ── Global safety net ───────────────────────────────────────────────────────
// A desktop app must NEVER let a single uncaught error or rejected promise take
// down the whole UI (a stuck handler used to cascade: chat error → broken state →
// Recientes/Composio/everything dead). These catch-alls surface the problem as a
// toast and keep the app interactive instead of silently wedging.
function installGlobalSafetyNet() {
  window.addEventListener('error', (ev) => {
    console.error('[lumen] uncaught error:', ev.error ?? ev.message);
    try { showToast(t('common.unexpectedError'), 'error'); } catch { /* toast itself failed — ignore */ }
  });
  window.addEventListener('unhandledrejection', (ev) => {
    console.error('[lumen] unhandled rejection:', ev.reason);
    ev.preventDefault(); // stop it from bubbling to the console as fatal
    try { showToast(t('common.unexpectedError'), 'error'); } catch { /* ignore */ }
  });
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

async function boot() {
  installGlobalSafetyNet();
  // 0. Language (instant, before paint) — localise static markup
  document.documentElement.lang = getLang();
  applyStaticI18n();
  initLangToggle();
  const taskTitleEl = document.getElementById('task-title');
  if (taskTitleEl) taskTitleEl.textContent = t('nav.newTask');

  // 1. Theme (instant, before paint)
  initTheme();

  // 2. Responsive layout
  initResponsive();
  initMobileOverlays();
  initExternalLinks();

  // 3. Wire icons that are placed via data attributes
  injectDataIcons();

  // 4. Profile — never surface internal OS account names (hermes, hermes-user,
  //    root, …). The chip is the human OWNER; if no real name is set, show a
  //    neutral owner label instead of leaking the engine account.
  const profile = await getProfile();
  const SYSTEM_NAMES = new Set(['hermes', 'hermes-user', 'hermes-rc', 'root', 'lumen', 'unknown', '']);
  const rawName = String(profile.user ?? '').trim();
  const ownerName = SYSTEM_NAMES.has(rawName.toLowerCase()) ? t('user.owner') : rawName;
  const userNameEl = document.getElementById('user-name');
  if (userNameEl) userNameEl.textContent = ownerName;
  const userAvatarEl = document.getElementById('user-avatar');
  if (userAvatarEl) {
    userAvatarEl.textContent = (ownerName[0] || 'T').toUpperCase();
    userAvatarEl.setAttribute('aria-label', ownerName);
  }

  // 5. Register all capability views
  registerCapability('chat', () => switchView('chat'));
  registerCapability('tasks', renderTasksView);
  registerCapability('agents', renderAgentsView);
  registerCapability('office', renderOfficeView);
  registerCapability('skills', renderSkillsView);
  registerCapability('integrations', renderIntegrationsView);
  registerCapability('mcp', renderMcpView);
  registerCapability('providers', renderProvidersView);
  registerCapability('security', renderSecurityView);
  registerCapability('memory', renderMemoryView);

  // 6. Nav items (capability view switching)
  initNavItems();

  // 7. Sidebar toggle
  document.getElementById('sidebar-toggle')?.addEventListener('click', toggleSidebar);
  document.getElementById('right-panel-toggle')?.addEventListener('click', toggleRightPanel);

  // Rail reopen button (shown when sidebar is collapsed)
  document.getElementById('sidebar-rail-btn')?.addEventListener('click', toggleSidebar);

  // 8. Search
  document.getElementById('search-btn')?.addEventListener('click', openSearch);
  document.getElementById('search-close')?.addEventListener('click', closeSearch);
  document.getElementById('search-overlay')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) closeSearch();
  });
  document.getElementById('search-input')?.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSearch();
  });

  // 9. Theme toggle
  document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
    btn.addEventListener('click', toggleTheme);
  });

  // 10. Advanced section toggle
  initAdvancedToggle();

  // 11. Composer
  const composer = initComposer({
    onSend: async (text) => {
      switchView('chat'); // ensure chat is visible
      composer.setStreaming(true);
      await sendMessage(text, {
        // Refresh recents at stream START too: the daemon persists the new
        // conversation synchronously at enqueue, so it exists now — without this
        // the new chat doesn't appear in "Recientes" until the stream fully ends
        // (or never, if stopped), which is why the list looked empty mid-session.
        onStreamStart: () => { composer.setStreaming(true); recents.refresh(); },
        onStreamEnd: () => {
          composer.setStreaming(false);
          recents.refresh();
        },
      });
    },
    onStop: () => {
      stopStream();
      composer.setStreaming(false);
      recents.refresh();   // stopping never fires onStreamEnd, so refresh here too
    },
  });

  // 12. Chat init
  initChat();

  // 13. Recents
  const recents = initRecents({
    onSelect: async (convId) => {
      switchView('chat');
      await loadConversation(convId);
      recents.setActive(convId);
      const shell = document.getElementById('shell');
      if (shell?.classList.contains('mobile')) toggleSidebar();
    },
  });

  // 13b. Restore the last conversation on reload (don't lose state on refresh),
  // then reconnect to any in-flight turn so the thinking/answer keep going.
  const lastConv = getPersistedConvId();
  if (lastConv) {
    switchView('chat');
    loadConversation(lastConv)
      .then(() => {
        recents.setActive(lastConv);
        resumeLiveIfAny({
          onStreamStart: () => composer.setStreaming(true),
          onStreamEnd: () => { composer.setStreaming(false); recents.refresh(); },
        });
      })
      .catch(() => {});
  }

  // 14. New task button
  document.getElementById('new-task-btn')?.addEventListener('click', () => {
    switchView('chat');
    startNewConversation();
    recents.setActive(null);
    composer.focus();
  });

  // 15. Keyboard shortcuts
  initKeyboardShortcuts({
    onNewTask: () => {
      switchView('chat');
      startNewConversation();
      recents.setActive(null);
      composer.focus();
    },
    onSearch: openSearch,
  });

  // 16. Approvals polling
  const approvalsMount = document.getElementById('approvals-mount');
  if (approvalsMount) startApprovalPolling(approvalsMount);

  // 17. Right-panel context data
  loadContextPanel();
}

// ── Language toggle ─────────────────────────────────────────────────────────────

function initLangToggle() {
  const btn = document.getElementById('lang-toggle');
  if (!btn) return;
  const sync = () => { btn.textContent = getLang().toUpperCase(); };
  sync();
  btn.addEventListener('click', () => {
    setLang(getLang() === 'es' ? 'en' : 'es');
    sync();
  });
}

// ── Icon injection ────────────────────────────────────────────────────────────

function injectDataIcons() {
  document.querySelectorAll('[data-icon]').forEach(el => {
    const name = el.dataset.icon;
    const icon = Icon[name];
    if (icon) el.innerHTML = icon;
  });
}

// ── Start ─────────────────────────────────────────────────────────────────────

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
