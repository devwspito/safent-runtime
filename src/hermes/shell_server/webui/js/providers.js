/**
 * providers.js — Providers capability view.
 *
 * Renders the full native Hermes provider catalog alongside configured providers.
 * Supports: list, add, set-active, test, OAuth flow, delete.
 * Endpoints: GET /providers, GET /providers/native, POST /providers,
 *            POST /providers/{id}/active, POST /providers/{id}/test,
 *            POST /providers/{id}/oauth/start, DELETE /providers/{id}
 */

import {
  listProviders, listNativeProviders,
  addProvider, setActiveProvider, testProvider,
  startProviderOAuth, getProviderOAuthStatus, deleteProvider,
} from './api.js';
import { Icon } from './icons.js';
import { showToast, openExternal, confirmDialog, promptDialog } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function kindBadge(label = '') {
  const map = {
    anthropic: '#D97706', openai: '#10A37F', openai_compatible: '#10A37F',
    google: '#4285F4', gemini: '#4285F4', azure: '#0078D4', mistral: '#FF7000',
    groq: '#F55036', ollama: '#6B7280', nous: '#7C3AED', cohere: '#39594D',
    vllm: '#7C3AED', oauth: '#8B5CF6', 'api key': '#6B7280', subscription: '#8B5CF6',
    modelo: '#6B7280',
  };
  const color = map[String(label).toLowerCase()] ?? '#6B7280';
  return `<span class="provider-badge" style="background:${color}22;color:${color}">${esc(label || 'Modelo')}</span>`;
}

// Native-catalog entries carry `auth_type` (oauth_*, api_key) instead of `kind`.
function badgeLabel(p) {
  if (p.kind) return p.kind;
  const a = String(p.auth_type ?? '').toLowerCase();
  if (a.includes('oauth')) return 'OAuth';
  if (a.includes('api')) return 'API key';
  return 'Modelo';
}

// Providers that authenticate via browser OAuth (subscriptions), mirroring the
// native SO (ProvidersApp.qml): auth_type carries "oauth", or known ids.
const OAUTH_IDS = new Set(['nous', 'openai-codex', 'xai-oauth']);
function isOAuthProvider(p) {
  const id = p.provider_id ?? p.id ?? '';
  return Boolean(p.supports_oauth)
    || /oauth/i.test(String(p.auth_type ?? ''))
    || OAUTH_IDS.has(id);
}

function renderProviderRow(p, isConfigured, onAction) {
  const el = document.createElement('div');
  el.className = `provider-row${isConfigured && p.is_active ? ' provider-row--active' : ''}`;
  el.dataset.providerId = p.provider_id ?? p.id ?? '';
  el.innerHTML = `
    <div class="provider-row__left">
      <div class="provider-row__name">${esc(p.alias ?? p.name ?? p.provider_id)}</div>
      <div class="provider-row__meta">
        ${kindBadge(badgeLabel(p))}
        ${p.default_model ? `<span class="provider-row__model">${esc(p.default_model)}</span>` : ''}
        ${isConfigured && p.is_active ? `<span class="provider-row__active-tag">${esc(t('providers.active'))}</span>` : ''}
      </div>
    </div>
    <div class="provider-row__actions">
      ${isConfigured ? `
        ${!p.is_active ? `<button class="btn btn--secondary btn--sm" data-action="activate" aria-label="${esc(t('providers.activate'))} ${esc(p.alias ?? '')}">${esc(t('providers.activate'))}</button>` : ''}
        <button class="btn btn--ghost btn--sm" data-action="test" aria-label="${esc(t('providers.test'))}">${esc(t('providers.test'))}</button>
        <button class="btn btn--ghost btn--sm btn--danger-ghost" data-action="delete" aria-label="${esc(t('common.delete'))}">${Icon.trash}</button>
      ` : (isOAuthProvider(p) ? `
        <button class="btn btn--secondary btn--sm" data-action="oauth" aria-label="${esc(t('providers.connect'))} ${esc(p.alias ?? p.name ?? '')}">${esc(t('providers.connect'))}</button>
      ` : `
        <button class="btn btn--secondary btn--sm" data-action="add" aria-label="${esc(t('common.add'))} ${esc(p.alias ?? p.name ?? '')}">${esc(t('common.add'))}</button>
      `)}
    </div>`;

  el.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', () => onAction(btn.dataset.action, p, el));
  });

  return el;
}

// Native SO OAuth flow (mirrors ProvidersApp.qml): start → open the browser at
// auth_url/verification_url (loopback opens chromium; device-code shows a code) →
// poll get_provider_oauth_status by session until approved/expired.
async function runOAuthFlow(id, name, rowEl, refreshFn) {
  const btn = rowEl?.querySelector('[data-action="oauth"]');
  if (btn) { btn.disabled = true; btn.textContent = t('providers.testing'); }
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = t('providers.connect'); } };

  let r;
  try {
    r = await startProviderOAuth(id);
  } catch (e) {
    showToast(t('providers.oauthError', { reason: e.message }), 'error');
    restore();
    return;
  }
  if (!r || r.error) {
    showToast(t('providers.oauthError', { reason: r?.error ?? 'unknown' }), 'error');
    restore();
    return;
  }

  const session = r.session_id;
  const url = r.auth_url ?? r.verification_url ?? '';
  const code = r.user_code ?? '';

  // Open the login in the user's NATIVE browser (Safari/Chrome). OAuth returns a
  // token to the daemon, which the agent then uses via API — no shared browser
  // session needed. Native = real fullscreen/scroll, far better than a screencast.
  if (url) {
    openExternal(url);
    showToast(t('providers.oauthOpening', { name }), 'ok');
  }
  // Device-code: surface the code + URL so the user can complete it anywhere.
  if (code) {
    showToast(t('providers.oauthCode', { url, code }), 'info', 15000);
  } else {
    showToast(t('providers.oauthWaiting', { name }), 'info');
  }

  if (!session) { restore(); return; }

  // Poll until the daemon reports the session resolved.
  const intervalMs = Math.max(2000, (r.poll_interval ?? 4) * 1000);
  const deadline = Date.now() + Math.max(60, r.expires_in ?? 600) * 1000;
  const poll = async () => {
    if (Date.now() > deadline) {
      showToast(t('providers.oauthExpired'), 'warn');
      restore();
      return;
    }
    let st;
    try { st = await getProviderOAuthStatus(session); } catch { st = { status: 'unknown' }; }
    const status = String(st?.status ?? '').toLowerCase();
    if (status === 'approved' || status === 'connected' || status === 'success') {
      showToast(t('providers.oauthConnected', { name }), 'ok');
      restore();
      refreshFn();
      return;
    }
    if (status === 'error' || status === 'failed') {
      showToast(t('providers.oauthError', { reason: st?.error_message ?? st?.error ?? 'unknown' }), 'error');
      restore();
      return;
    }
    if (status === 'expired') {
      showToast(t('providers.oauthExpired'), 'warn');
      restore();
      return;
    }
    setTimeout(poll, intervalMs);
  };
  setTimeout(poll, intervalMs);
}

async function handleAction(action, provider, rowEl, refreshFn) {
  const id = provider.provider_id ?? provider.id ?? '';
  const name = provider.alias ?? provider.name ?? id;
  if (action === 'activate') {
    try {
      await setActiveProvider(id);
      showToast(t('providers.activated', { name }), 'ok');
      refreshFn();
    } catch (e) { showToast(e.message, 'error'); }
  } else if (action === 'test') {
    const btn = rowEl.querySelector('[data-action="test"]');
    if (btn) { btn.disabled = true; btn.textContent = t('providers.testing'); }
    try {
      const r = await testProvider(id);
      showToast(r?.ok ? t('providers.testOk') : t('providers.testNoResp'), r?.ok ? 'ok' : 'warn');
    } catch (e) { showToast(e.message, 'error'); }
    finally { if (btn) { btn.disabled = false; btn.textContent = t('providers.test'); } }
  } else if (action === 'delete') {
    if (!(await confirmDialog(t('common.confirmDelete', { name })))) return;
    try {
      await deleteProvider(id);
      showToast(t('providers.deleted'), 'ok');
      refreshFn();
    } catch (e) { showToast(e.message, 'error'); }
  } else if (action === 'oauth') {
    await runOAuthFlow(id, name, rowEl, refreshFn);
  } else if (action === 'add') {
    const apiKey = await promptDialog({ message: t('providers.apiKeyPrompt', { name }), password: true });
    if (!apiKey) return;
    try {
      await addProvider({ provider_id: id, alias: provider.alias ?? provider.name, api_key: apiKey, kind: provider.kind ?? provider.category });
      showToast(t('providers.added'), 'ok');
      refreshFn();
    } catch (e) { showToast(e.message, 'error'); }
  }
}

export async function renderProvidersView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('providers.title'))}</h2>
        <p class="cv-subtitle">${esc(t('providers.subtitle'))}</p>
      </div>
      <div id="pv-configured" class="cv-section">
        <div class="cv-section-label">${esc(t('providers.configured'))}</div>
        <div class="cv-list" id="pv-configured-list"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('providers.customSection'))}</div>
        <div class="teach-card">
          <p class="teach-card__intro">${esc(t('providers.customIntro'))}</p>
          <div id="pv-custom-idle">
            <button class="btn btn--secondary btn--sm" id="pv-custom-toggle">${esc(t('providers.customAdd'))}</button>
          </div>
          <div id="pv-custom-form" hidden>
            <input id="pv-c-alias" class="cv-input" type="text" autocomplete="off" placeholder="${esc(t('providers.customAliasPh'))}">
            <input id="pv-c-url" class="cv-input" type="text" autocomplete="off" placeholder="${esc(t('providers.customUrlPh'))}">
            <input id="pv-c-model" class="cv-input" type="text" autocomplete="off" placeholder="${esc(t('providers.customModelPh'))}">
            <input id="pv-c-key" class="cv-input" type="password" autocomplete="off" placeholder="${esc(t('providers.customKeyPh'))}">
            <p class="teach-card__privacy">${esc(t('providers.customHint'))}</p>
            <div class="teach-card__actions">
              <button class="btn btn--primary btn--sm" id="pv-c-save">${esc(t('providers.customSave'))}</button>
              <button class="btn btn--ghost btn--sm" id="pv-c-cancel">${esc(t('common.cancel'))}</button>
            </div>
          </div>
        </div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('providers.catalog'))}</div>
        <div class="cv-list" id="pv-native-list"><div class="cv-skeleton"></div></div>
      </div>
    </div>`;

  async function load() {
    const [configured, native] = await Promise.all([listProviders(), listNativeProviders()]);
    const configuredList = document.getElementById('pv-configured-list');
    const nativeList = document.getElementById('pv-native-list');

    const configuredArr = Array.isArray(configured) ? configured : [];
    const nativeArr = Array.isArray(native) ? native : [];
    const configuredIds = new Set(configuredArr.map(p => p.provider_id ?? p.id));

    if (configuredList) {
      configuredList.innerHTML = '';
      if (configuredArr.length === 0) {
        configuredList.innerHTML = `<div class="cv-empty">${esc(t('providers.empty'))}</div>`;
      } else {
        configuredArr.forEach(p => {
          configuredList.appendChild(renderProviderRow(p, true, (action, prov, el) => handleAction(action, prov, el, load)));
        });
      }
    }

    if (nativeList) {
      nativeList.innerHTML = '';
      if (nativeArr.length === 0) {
        nativeList.innerHTML = `<div class="cv-empty">${esc(t('providers.catalogEmpty'))}</div>`;
      } else {
        nativeArr.forEach(p => {
          if (!configuredIds.has(p.provider_id ?? p.id)) {
            nativeList.appendChild(renderProviderRow(p, false, (action, prov, el) => handleAction(action, prov, el, load)));
          }
        });
      }
    }
  }

  // ── Custom provider (OpenAI-compatible: vLLM, LM Studio, Ollama, etc.) ───────
  const cIdle = container.querySelector('#pv-custom-idle');
  const cForm = container.querySelector('#pv-custom-form');
  container.querySelector('#pv-custom-toggle')?.addEventListener('click', () => {
    if (cIdle) cIdle.hidden = true;
    if (cForm) cForm.hidden = false;
    container.querySelector('#pv-c-url')?.focus();
  });
  container.querySelector('#pv-c-cancel')?.addEventListener('click', () => {
    if (cForm) cForm.hidden = true;
    if (cIdle) cIdle.hidden = false;
  });
  container.querySelector('#pv-c-save')?.addEventListener('click', async () => {
    const val = (id) => (container.querySelector(id)?.value ?? '').trim();
    const base_url = val('#pv-c-url');
    const default_model = val('#pv-c-model');
    const alias = val('#pv-c-alias') || default_model || 'Modelo local';
    const api_key = val('#pv-c-key') || undefined;
    if (!base_url || !default_model) {
      showToast(t('providers.customMissing'), 'warn');
      return;
    }
    const saveBtn = container.querySelector('#pv-c-save');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = t('providers.customSaving'); }
    try {
      await addProvider({ kind: 'openai_compatible', alias, default_model, base_url, api_key, set_active: true });
      showToast(t('providers.added'), 'ok');
      if (cForm) cForm.hidden = true;
      if (cIdle) cIdle.hidden = false;
      load();
    } catch (e) {
      showToast(e.message ?? String(e), 'error');
    } finally {
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = t('providers.customSave'); }
    }
  });

  load();
}
