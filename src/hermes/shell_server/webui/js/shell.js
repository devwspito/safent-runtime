/**
 * shell.js — Top-level shell wiring: layout, keyboard shortcuts, toasts,
 * sidebar collapse (with always-visible rail), search overlay,
 * capability view switching.
 */

import { Icon } from './icons.js';
import { t, onLangChange } from './i18n.js';

// ── Toast system ──────────────────────────────────────────────────────────────

let _toastContainer = null;

export function showToast(message, type = 'info', durationMs = 4000) {
  if (!_toastContainer) {
    _toastContainer = document.createElement('div');
    _toastContainer.className = 'toast-container';
    _toastContainer.setAttribute('aria-live', 'assertive');
    _toastContainer.setAttribute('aria-atomic', 'false');
    _toastContainer.setAttribute('role', 'status');
    document.body.appendChild(_toastContainer);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.setAttribute('role', 'alert');
  toast.textContent = message;

  const closeBtn = document.createElement('button');
  closeBtn.className = 'toast__close';
  closeBtn.setAttribute('aria-label', 'Cerrar notificación');
  closeBtn.innerHTML = Icon.close;
  closeBtn.addEventListener('click', () => dismiss(toast));
  toast.appendChild(closeBtn);

  _toastContainer.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('toast--visible'));

  const timer = setTimeout(() => dismiss(toast), durationMs);
  closeBtn.addEventListener('click', () => clearTimeout(timer));
}

function dismiss(toast) {
  toast.classList.remove('toast--visible');
  toast.addEventListener('transitionend', () => toast.remove(), { once: true });
}

// ── External links (documentation, repos) ──────────────────────────────────────
// A doc/repo link must open in the user's OWN browser, not navigate the Lumen
// webview away (and not the agent's confined VM browser — that's for the agent).
// In the Tauri desktop shell, window.open is swallowed, so we route through the
// native opener. Over the web (tunnel) the plain <a target="_blank"> just works.

function _escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Render an external documentation link styled as a ghost button.
 * Returns '' when no URL is available, so callers can inline it unconditionally.
 * @param {string|undefined|null} url
 * @param {string} label
 */
export function docLinkHtml(url, label) {
  const u = String(url ?? '').trim();
  if (!u || !/^https?:\/\//i.test(u)) return '';
  return `<a class="btn btn--ghost btn--sm" href="${_escAttr(u)}" target="_blank" rel="noopener noreferrer" data-external>${_escAttr(label)}</a>`;
}

/** Open a URL in the user's system browser (Tauri-aware, web fallback). */
export function openExternal(url) {
  const u = String(url ?? '').trim();
  if (!/^https?:\/\//i.test(u)) return;
  const invoke = window.__TAURI__?.core?.invoke;
  if (invoke) {
    // Native open_external command (tauri-plugin-opener); web fallback on error.
    invoke('open_external', { url: u }).catch(() => window.open(u, '_blank', 'noopener'));
    return;
  }
  window.open(u, '_blank', 'noopener');
}

/** Delegate clicks on [data-external] anchors to the system browser. */
export function initExternalLinks() {
  document.addEventListener('click', (ev) => {
    const a = ev.target instanceof Element ? ev.target.closest('a[data-external]') : null;
    if (!a) return;
    // In the desktop shell the webview would otherwise swallow or hijack it.
    if (window.__TAURI__) {
      ev.preventDefault();
      openExternal(a.getAttribute('href'));
    }
    // On the web, let the native <a target="_blank"> proceed.
  });
}

// ── In-app dialogs (confirm / prompt) ───────────────────────────────────────────
// The Tauri webview does NOT implement window.confirm()/prompt()/alert() — they
// return false/null, so any "if (!confirm(...)) return" silently did nothing
// (delete/uninstall/add buttons looked dead). These custom modals replace them.

function _buildModal(inner) {
  const overlay = document.createElement('div');
  overlay.className = 'lumen-modal-overlay';
  const modal = document.createElement('div');
  modal.className = 'lumen-modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.appendChild(inner);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  return overlay;
}

/** In-app replacement for window.confirm(). Returns Promise<boolean>. */
export function confirmDialog(message, { okLabel, cancelLabel, danger = true } = {}) {
  return new Promise((resolve) => {
    const box = document.createElement('div');
    box.innerHTML = `
      <p class="lumen-modal__msg"></p>
      <div class="lumen-modal__actions">
        <button class="btn btn--secondary btn--sm" data-act="cancel"></button>
        <button class="btn ${danger ? 'btn--danger' : 'btn--primary'} btn--sm" data-act="ok"></button>
      </div>`;
    box.querySelector('.lumen-modal__msg').textContent = message;
    box.querySelector('[data-act="cancel"]').textContent = cancelLabel || t('common.cancel');
    box.querySelector('[data-act="ok"]').textContent = okLabel || t('common.ok');
    const overlay = _buildModal(box);
    const done = (v) => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    box.querySelector('[data-act="ok"]').addEventListener('click', () => done(true));
    box.querySelector('[data-act="cancel"]').addEventListener('click', () => done(false));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) done(false); });
    const onKey = (e) => { if (e.key === 'Escape') done(false); else if (e.key === 'Enter') done(true); };
    document.addEventListener('keydown', onKey);
    box.querySelector('[data-act="ok"]').focus();
  });
}

/** In-app replacement for window.prompt(). Returns Promise<string|null>. */
export function promptDialog({ message, placeholder = '', password = false, okLabel } = {}) {
  return new Promise((resolve) => {
    const box = document.createElement('div');
    box.innerHTML = `
      <p class="lumen-modal__msg"></p>
      <input class="cv-input lumen-modal__input" type="${password ? 'password' : 'text'}" autocomplete="off">
      <div class="lumen-modal__actions">
        <button class="btn btn--secondary btn--sm" data-act="cancel"></button>
        <button class="btn btn--primary btn--sm" data-act="ok"></button>
      </div>`;
    box.querySelector('.lumen-modal__msg').textContent = message || '';
    const input = box.querySelector('input');
    input.placeholder = placeholder;
    box.querySelector('[data-act="cancel"]').textContent = t('common.cancel');
    box.querySelector('[data-act="ok"]').textContent = okLabel || t('common.ok');
    const overlay = _buildModal(box);
    const done = (v) => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const submit = () => { const v = input.value.trim(); done(v || null); };
    box.querySelector('[data-act="ok"]').addEventListener('click', submit);
    box.querySelector('[data-act="cancel"]').addEventListener('click', () => done(null));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) done(null); });
    const onKey = (e) => { if (e.key === 'Escape') done(null); else if (e.key === 'Enter' && document.activeElement === input) submit(); };
    document.addEventListener('keydown', onKey);
    input.focus();
  });
}

// ── Sidebar collapse ──────────────────────────────────────────────────────────
// When collapsed, the sidebar shrinks to 0 but a slim "rail" (fixed icon strip)
// remains visible so the user can reopen it. This is the always-visible toggle.

let _sidebarOpen = true;

export function toggleSidebar() {
  const shell = document.getElementById('shell');
  _sidebarOpen = !_sidebarOpen;
  shell?.classList.toggle('sidebar-collapsed', !_sidebarOpen);

  const btn = document.getElementById('sidebar-toggle');
  if (btn) {
    btn.setAttribute('aria-label', _sidebarOpen ? 'Ocultar barra lateral' : 'Mostrar barra lateral');
    btn.innerHTML = _sidebarOpen ? Icon.sidebarCollapse : Icon.sidebarExpand;
    btn.setAttribute('aria-expanded', String(_sidebarOpen));
  }

  // Show/hide the floating reopen rail
  const rail = document.getElementById('sidebar-rail');
  if (rail) rail.hidden = _sidebarOpen;
}

// ── Right panel ───────────────────────────────────────────────────────────────

let _rightPanelOpen = true;

export function toggleRightPanel() {
  const shell = document.getElementById('shell');
  _rightPanelOpen = !_rightPanelOpen;
  shell?.classList.toggle('right-panel-collapsed', !_rightPanelOpen);
  const btn = document.getElementById('right-panel-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', String(_rightPanelOpen));
    btn.setAttribute('aria-label', _rightPanelOpen ? 'Ocultar panel derecho' : 'Mostrar panel derecho');
  }
}

// ── Search overlay ────────────────────────────────────────────────────────────

export function openSearch() {
  const overlay = document.getElementById('search-overlay');
  if (!overlay) return;
  overlay.hidden = false;
  overlay.querySelector('input')?.focus();
}

export function closeSearch() {
  const overlay = document.getElementById('search-overlay');
  if (!overlay) return;
  overlay.hidden = true;
}

// ── Capability view switching ─────────────────────────────────────────────────

let _activeCapability = 'chat';
let _capabilityRenderers = {};

/**
 * Register a capability view renderer.
 * @param {string} id - nav item data-view value
 * @param {(container: HTMLElement) => void} renderFn
 */
export function registerCapability(id, renderFn) {
  _capabilityRenderers[id] = renderFn;
}

/**
 * Switch to a capability view (or back to chat).
 * @param {string} viewId
 */
export function switchView(viewId) {
  _activeCapability = viewId;

  const chatArea = document.getElementById('chat-area');
  const capabilityArea = document.getElementById('capability-area');
  const taskTopbar = document.querySelector('.task-topbar');
  const composerWrap = document.querySelector('.composer-wrap');
  const chatStatus = document.getElementById('chat-status');

  const isChat = viewId === 'chat';

  if (chatArea) chatArea.hidden = !isChat;
  if (capabilityArea) capabilityArea.hidden = isChat;
  if (composerWrap) composerWrap.hidden = !isChat;
  if (chatStatus && !isChat) chatStatus.hidden = true;

  // Update task topbar title for non-chat views
  const titleEl = document.getElementById('task-title');
  if (titleEl && !isChat) {
    titleEl.textContent = t(`nav.${viewId}`);
  }

  // Update nav item active state
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    btn.classList.toggle('nav-item--active', btn.dataset.view === viewId);
    btn.setAttribute('aria-current', btn.dataset.view === viewId ? 'page' : 'false');
  });

  if (!isChat && capabilityArea) {
    const renderer = _capabilityRenderers[viewId];
    if (renderer) {
      capabilityArea.innerHTML = '';
      renderer(capabilityArea);
    } else {
      capabilityArea.innerHTML = `<div class="cv-unavailable">Vista "${viewId}" no disponible.</div>`;
    }
  }
}

export function initNavItems() {
  document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view ?? 'chat';
      switchView(view);
      // On mobile, close sidebar after selection
      const shell = document.getElementById('shell');
      if (shell?.classList.contains('mobile') && _sidebarOpen) toggleSidebar();
    });
  });

  // Re-render the open capability view when the language changes.
  onLangChange(() => {
    if (_activeCapability && _activeCapability !== 'chat') switchView(_activeCapability);
  });
}

// ── Advanced section expand ───────────────────────────────────────────────────

export function initAdvancedToggle() {
  const toggle = document.getElementById('advanced-toggle');
  const section = document.getElementById('advanced-section');
  if (!toggle || !section) return;

  toggle.addEventListener('click', () => {
    const expanded = section.hidden === false;
    section.hidden = expanded;
    toggle.setAttribute('aria-expanded', String(!expanded));
    const chevron = toggle.querySelector('.advanced-chevron');
    if (chevron) chevron.style.transform = expanded ? '' : 'rotate(180deg)';
  });
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

export function initKeyboardShortcuts({ onNewTask, onSearch }) {
  document.addEventListener('keydown', e => {
    const meta = e.metaKey || e.ctrlKey;

    if (meta && e.key === 'k') {
      e.preventDefault();
      onSearch?.();
    }
    if (meta && e.key === 'n') {
      const tag = document.activeElement?.tagName?.toLowerCase();
      if (tag !== 'textarea' && tag !== 'input') {
        e.preventDefault();
        onNewTask?.();
      }
    }
    if (e.key === 'Escape') {
      closeSearch();
    }
  });
}

// ── Mobile drawer state ───────────────────────────────────────────────────────

const MOBILE_BREAKPOINT = 768;

export function initResponsive() {
  const handleResize = () => {
    const isMobile = window.innerWidth < MOBILE_BREAKPOINT;
    const shell = document.getElementById('shell');
    if (shell) {
      shell.classList.toggle('mobile', isMobile);
      if (isMobile) {
        shell.classList.add('sidebar-collapsed', 'right-panel-collapsed');
        _sidebarOpen = false;
        _rightPanelOpen = false;
        const rail = document.getElementById('sidebar-rail');
        if (rail) rail.hidden = false;
      }
    }
  };

  const ro = new ResizeObserver(handleResize);
  ro.observe(document.body);
  handleResize();
}

export function initMobileOverlays() {
  const overlay = document.getElementById('mobile-overlay');
  if (!overlay) return;
  overlay.addEventListener('click', () => {
    const shell = document.getElementById('shell');
    if (!shell?.classList.contains('mobile')) return;
    if (_sidebarOpen) toggleSidebar();
    if (_rightPanelOpen) toggleRightPanel();
  });
}
