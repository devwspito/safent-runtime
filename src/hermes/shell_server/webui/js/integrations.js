/**
 * integrations.js — Composio integrations view.
 * Endpoints: GET /integrations/composio/status, GET /integrations/composio/connected,
 *            GET /integrations/composio/apps, POST /integrations/composio/connect/{slug},
 *            POST /integrations/composio/apikey
 */

import {
  getComposioStatus, listComposioConnected, listComposioApps, connectComposioApp, setComposioApiKey,
  getWebSearchStatus, setWebSearchKey,
} from './api.js';
import { Icon } from './icons.js';
import { showToast, openExternal } from './shell.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderAppRow(app, isConnected, onConnect) {
  const el = document.createElement('div');
  el.className = `integration-row${isConnected ? ' integration-row--connected' : ''}`;
  el.innerHTML = `
    <div class="integration-row__icon" aria-hidden="true">${app.logo ? `<img src="${esc(app.logo)}" alt="" width="20" height="20">` : Icon.integrations}</div>
    <div class="integration-row__info">
      <div class="integration-row__name">${esc(app.name ?? app.slug)}</div>
      ${app.description ? `<div class="integration-row__desc">${esc(app.description)}</div>` : ''}
    </div>
    <div class="integration-row__status">
      ${isConnected
        ? `<span class="integration-connected-tag">${Icon.check} ${esc(t('integrations.connected'))}</span>`
        : `<button class="btn btn--secondary btn--sm" aria-label="${esc(t('integrations.connectAriaLabel', { name: app.name ?? app.slug }))}">${esc(t('integrations.connect'))}</button>`}
    </div>`;

  if (!isConnected) {
    el.querySelector('button')?.addEventListener('click', () => onConnect(app));
  }

  return el;
}

export async function renderIntegrationsView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('integrations.title'))}</h2>
        <p class="cv-subtitle">${esc(t('integrations.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('websearch.section'))}</div>
        <div class="teach-card" id="websearch-card">
          <div class="teach-card__title-row">
            <span class="websearch-brave-mark" aria-hidden="true">${Icon.toolSearch}</span>
            <strong>${esc(t('websearch.title'))}</strong>
          </div>
          <p class="teach-card__intro">${esc(t('websearch.intro'))}</p>
          <p class="teach-card__privacy">${esc(t('websearch.steps'))}
            <a href="https://api.search.brave.com/app/keys" target="_blank" rel="noopener noreferrer" class="websearch-getkey">${esc(t('websearch.getKey'))}</a>
          </p>
          <div id="websearch-status" class="websearch-status"></div>
          <div class="teach-card__actions">
            <input id="websearch-key" class="cv-input" type="password" placeholder="${esc(t('websearch.placeholder'))}" autocomplete="off" aria-label="${esc(t('websearch.placeholder'))}">
            <button class="btn btn--primary btn--sm" id="websearch-save">${esc(t('websearch.save'))}</button>
          </div>
        </div>
      </div>
      <div class="cv-section" id="composio-status-section">
        <div class="cv-section-label">${esc(t('integrations.composioStatusSection'))}</div>
        <div id="composio-status-card" class="composio-status-card"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('integrations.connectedSection'))}</div>
        <div class="cv-list" id="composio-connected-list"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('integrations.appsSection'))}</div>
        <div class="cv-list" id="composio-apps-list"><div class="cv-skeleton"></div></div>
      </div>
    </div>`;

  function wireSaveKey(statusCard) {
    const btn = statusCard.querySelector('#composio-save-key');
    btn?.addEventListener('click', async () => {
      const input = statusCard.querySelector('#composio-apikey-input');
      const key = input?.value.trim();
      if (!key) { showToast(t('integrations.apiKeyRequired'), 'warn'); return; }
      // Visual feedback while the await is in flight (the user's complaint: the
      // button looked dead). Disable + spinner text, restore on error.
      btn.disabled = true;
      const orig = btn.textContent;
      btn.textContent = t('integrations.connectingKey');
      try {
        await setComposioApiKey(key);
        showToast(t('integrations.apiKeySaved'), 'ok');
        load();
      } catch (e) {
        showToast(e.message, 'error');
        btn.disabled = false;
        btn.textContent = orig;
      }
    });
  }

  function renderSetupForm(statusCard) {
    statusCard.innerHTML = `
      <div class="composio-setup">
        <p class="composio-setup__text">${esc(t('integrations.setupText'))}</p>
        <div class="composio-setup__form">
          <input id="composio-apikey-input" class="cv-input" type="password" placeholder="${esc(t('integrations.apiKeyPlaceholder'))}" autocomplete="off" aria-label="${esc(t('integrations.apiKeyLabel'))}">
          <button class="btn btn--primary btn--sm" id="composio-save-key">${esc(t('integrations.saveKey'))}</button>
        </div>
      </div>`;
    wireSaveKey(statusCard);
  }

  async function load() {
    const statusCard = document.getElementById('composio-status-card');
    const connectedList = document.getElementById('composio-connected-list');
    const appsList = document.getElementById('composio-apps-list');

    // Status FIRST — it's a fast call and decides everything. Without a key we must
    // NOT call connected/apps (they 503 and, over the VM relay, hang for minutes —
    // the "3-4 min to notice Composio isn't set up" the user hit). Render the setup
    // state instantly instead.
    const status = await getComposioStatus().catch(() => ({ has_key: false }));

    if (!status.has_key) {
      if (statusCard) renderSetupForm(statusCard);
      if (connectedList) connectedList.innerHTML = `<div class="cv-empty">${esc(t('integrations.connectFirst'))}</div>`;
      if (appsList) appsList.innerHTML = `<div class="cv-empty">${esc(t('integrations.connectFirst'))}</div>`;
      return;
    }

    if (statusCard) {
      statusCard.innerHTML = `
        <div class="composio-status-ok">
          ${Icon.check} ${esc(t('integrations.composioActive'))} <code>${esc(status.entity_id ?? '')}</code>
        </div>`;
    }

    // Has a key → now it's safe to fetch the lists (each bounded by the fetch timeout).
    const [connected, apps] = await Promise.all([
      listComposioConnected().catch(() => []),
      listComposioApps().catch(() => []),
    ]);

    if (connectedList) {
      connectedList.innerHTML = '';
      const connArr = Array.isArray(connected) ? connected : [];
      const connSlugs = new Set(connArr.map(c => c.slug));
      if (connArr.length === 0) {
        connectedList.innerHTML = `<div class="cv-empty">${esc(t('integrations.noConnected'))}</div>`;
      } else {
        connArr.forEach(c => connectedList.appendChild(renderAppRow(c, true, () => {})));
      }

      if (appsList) {
        appsList.innerHTML = '';
        const appsArr = Array.isArray(apps) ? apps : [];
        const remaining = appsArr.filter(a => !connSlugs.has(a.slug));
        if (remaining.length === 0) {
          appsList.innerHTML = `<div class="cv-empty">${esc(t('integrations.noApps'))}</div>`;
        } else {
          remaining.forEach(a => appsList.appendChild(renderAppRow(a, false, async (app) => {
            try {
              const r = await connectComposioApp(app.slug);
              if (r?.redirect_url) openExternal(r.redirect_url);
              showToast(t('integrations.connecting', { name: app.name ?? app.slug }), 'info');
              setTimeout(load, 3000);
            } catch (e) { showToast(e.message, 'error'); }
          })));
        }
      }
    }
  }

  // ── Web search (Brave) — enter key → works immediately, ddgs fallback ──────
  async function loadWebSearch() {
    const statusEl = container.querySelector('#websearch-status');
    if (!statusEl) return;
    const st = await getWebSearchStatus();
    statusEl.innerHTML = st?.brave
      ? `${Icon.check} ${esc(t('websearch.active'))}`
      : esc(t('websearch.fallback'));
    statusEl.classList.toggle('is-active', !!st?.brave);
  }
  container.querySelector('#websearch-save')?.addEventListener('click', async () => {
    const input = container.querySelector('#websearch-key');
    const key = input?.value.trim();
    if (!key) { showToast(t('websearch.required'), 'warn'); return; }
    const btn = container.querySelector('#websearch-save');
    if (btn) { btn.disabled = true; }
    try {
      const r = await setWebSearchKey('brave', key);
      if (r?.ok === false) throw new Error(r.error || 'error');
      showToast(t('websearch.saved'), 'ok');
      if (input) input.value = '';
      loadWebSearch();
    } catch (e) {
      showToast(t('websearch.error', { reason: e.message }), 'error');
    } finally { if (btn) btn.disabled = false; }
  });

  load();
  loadWebSearch();
}
