/**
 * composer.js — Message composer bar.
 * Handles text input, keyboard shortcuts, mode switching, model picker.
 * Calls back onSend(message: string) when the user submits.
 */

import { listProviders } from './api.js';
import { t, onLangChange } from './i18n.js';
import { Icon } from './icons.js';
import { switchView } from './shell.js';

function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

let _providers = [];
let _activeProvider = null;

/**
 * Load providers and populate the model picker.
 */
async function loadProviders() {
  const data = await listProviders();
  _providers = Array.isArray(data) ? data : [];
  _activeProvider = _providers.find(p => p.is_active) ?? _providers[0] ?? null;
  renderModelPicker();
}

function renderModelPicker() {
  const el = document.getElementById('model-picker');
  if (!el) return;
  if (_activeProvider) {
    const label = _activeProvider.default_model ?? _activeProvider.alias;
    el.innerHTML = `<span class="model-picker__label">${escAttr(label)}</span>${Icon.chevronDown}`;
    el.title = _activeProvider.alias;
  } else {
    el.innerHTML = `<span class="model-picker__label">${escAttr(t('composer.noModel'))}</span>${Icon.chevronDown}`;
    el.title = t('composer.noModel');
  }
}

/**
 * Init the composer. Returns control object.
 * @param {{ onSend: (msg: string) => void, onStop: () => void }} opts
 */
export function initComposer({ onSend, onStop }) {
  const textarea = document.getElementById('composer-input');
  const sendBtn = document.getElementById('composer-send');
  const stopBtn = document.getElementById('composer-stop');
  const charCounter = document.getElementById('composer-char-count');

  if (!textarea || !sendBtn) return { setStreaming() {}, reset() {} };

  // Auto-grow textarea
  function autoGrow() {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 240) + 'px';
  }
  textarea.addEventListener('input', () => {
    autoGrow();
    if (charCounter) {
      const len = textarea.value.length;
      charCounter.textContent = len > 0 ? `${len}` : '';
      charCounter.hidden = len === 0;
    }
  });

  // Send on Enter, newline on Shift+Enter
  textarea.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      triggerSend();
    }
  });

  sendBtn.addEventListener('click', triggerSend);
  stopBtn?.addEventListener('click', () => onStop?.());

  function triggerSend() {
    const msg = textarea.value.trim();
    if (!msg) return;
    textarea.value = '';
    textarea.style.height = '';
    if (charCounter) { charCounter.textContent = ''; charCounter.hidden = true; }
    onSend(msg);
  }

  // Model picker click → jump to the Providers view to add/choose a model.
  const modelBtn = document.getElementById('model-picker');
  modelBtn?.addEventListener('click', () => {
    switchView('providers');
  });

  loadProviders();

  let _streaming = false;
  function setStreaming(active) {
    _streaming = active;
    sendBtn.hidden = active;
    stopBtn.hidden = !active;
    textarea.disabled = active;
    textarea.placeholder = active ? t('composer.thinking') : t('composer.placeholder');
  }

  // Keep the idle placeholder localised when the language changes.
  onLangChange(() => {
    if (!_streaming) textarea.placeholder = t('composer.placeholder');
    renderModelPicker();
  });

  function reset() {
    setStreaming(false);
    textarea.value = '';
    textarea.style.height = '';
    textarea.focus();
  }

  function focus() {
    textarea.focus();
  }

  return { setStreaming, reset, focus, refreshProviders: loadProviders };
}
