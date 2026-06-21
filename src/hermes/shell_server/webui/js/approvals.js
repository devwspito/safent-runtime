/**
 * approvals.js — Inline HITL approval card rendering and polling.
 *
 * Cards are injected into the chat body when pending approvals exist.
 * Resolved approvals are removed from the DOM immediately, then the
 * list is re-polled so any newly arrived ones show up.
 */

import { listPendingApprovals, resolveApproval } from './api.js';
import { Icon } from './icons.js';
import { showToast } from './shell.js';

const POLL_INTERVAL_MS = 3000;
let _pollTimer = null;
let _container = null;

function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
  return String(s).replace(/"/g, '&quot;');
}

/** Render the redacted action parameters so the owner approves a SPECIFIC action. */
function renderParams(p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) return '';
  const entries = Object.entries(p).slice(0, 8);
  if (!entries.length) return '';
  return `<dl class="approval-card__params">` + entries.map(([k, v]) =>
    `<dt>${escapeText(k)}</dt><dd>${escapeText(typeof v === 'object' ? JSON.stringify(v) : v)}</dd>`
  ).join('') + `</dl>`;
}

/**
 * Renders a single approval card. Approving reveals an inline MFA form (the gate
 * verifies the tier server-side: TOTP always; + riddle/humanity for delicate/most).
 * @param {{ proposal_id: string, kind: string, summary: string, target: string, parameters?: object }} a
 */
function renderCard(a) {
  return `<div class="approval-card" role="alertdialog"
              aria-label="Aprobación requerida: ${escapeAttr(a.summary)}"
              data-proposal-id="${escapeAttr(a.proposal_id)}">
    <div class="approval-card__icon">${Icon.globe}</div>
    <div class="approval-card__body">
      <p class="approval-card__question">${escapeText(a.summary)}</p>
      ${a.target ? `<p class="approval-card__target">${escapeText(a.target)}</p>` : ''}
      ${renderParams(a.parameters)}
    </div>
    <div class="approval-card__actions" role="group" aria-label="Acciones de aprobación">
      <button class="btn btn--primary approval-approve"
              aria-label="Permitir esta acción (requiere tu MFA)">Permitir…</button>
      <button class="btn btn--ghost approval-action" data-decision="deny"
              aria-label="Denegar esta acción">Denegar</button>
    </div>
    <form class="approval-card__mfa" hidden aria-label="Verificación del dueño">
      <input class="mfa-totp" inputmode="numeric" autocomplete="one-time-code"
             maxlength="8" placeholder="Código MFA (6 dígitos)" aria-label="Código MFA" />
      <input class="mfa-riddle" type="text"
             placeholder="Respuesta del acertijo (si se pide)" aria-label="Respuesta del acertijo" />
      <label class="mfa-humanity-label">
        <input type="checkbox" class="mfa-humanity" /> Confirmo que soy yo
      </label>
      <div class="approval-card__mfa-actions">
        <button type="submit" class="btn btn--primary approval-confirm">Confirmar</button>
        <button type="button" class="btn btn--ghost approval-cancel">Cancelar</button>
      </div>
      <p class="approval-card__mfa-error" role="alert" hidden></p>
    </form>
  </div>`;
}

async function resolve(proposalId, decision, cardEl, factors = {}) {
  const errEl = cardEl.querySelector('.approval-card__mfa-error');
  if (errEl) errEl.hidden = true;
  cardEl.style.pointerEvents = 'none';
  try {
    await resolveApproval(proposalId, decision, factors);
    cardEl.remove();
    showToast(decision === 'deny' ? 'Denegado' : 'Aprobado', 'ok');
  } catch (err) {
    cardEl.style.pointerEvents = '';
    if (decision !== 'deny' && errEl) { errEl.textContent = err.message; errEl.hidden = false; }
    showToast(`No se pudo resolver: ${err.message}`, 'error');
  }
}

function attachHandlers(card) {
  const form = card.querySelector('.approval-card__mfa');
  const proposalId = card.dataset.proposalId;
  // Deny is immediate (no MFA — rejecting is always safe).
  card.querySelector('.approval-action[data-decision="deny"]')
    ?.addEventListener('click', () => resolve(proposalId, 'deny', card));
  // Approve reveals the MFA form; the gate verifies the required tier server-side.
  card.querySelector('.approval-approve')?.addEventListener('click', () => {
    if (form) { form.hidden = false; card.querySelector('.mfa-totp')?.focus(); }
  });
  card.querySelector('.approval-cancel')?.addEventListener('click', () => {
    if (form) form.hidden = true;
  });
  form?.addEventListener('submit', (e) => {
    e.preventDefault();
    resolve(proposalId, 'once', card, {
      totp: card.querySelector('.mfa-totp')?.value.trim() || null,
      riddle_answer: card.querySelector('.mfa-riddle')?.value.trim() || null,
      humanity: card.querySelector('.mfa-humanity')?.checked ? 'confirmado' : null,
    });
  });
}

/**
 * Renders pending approvals into `container`.
 * @param {HTMLElement} container
 * @param {Array} approvals
 */
function render(container, approvals) {
  // Remove cards whose proposal_id is no longer in the list
  const currentIds = new Set(approvals.map(a => a.proposal_id));
  container.querySelectorAll('.approval-card[data-proposal-id]').forEach(el => {
    if (!currentIds.has(el.dataset.proposalId)) el.remove();
  });

  // Add new cards
  const existingIds = new Set(
    [...container.querySelectorAll('.approval-card[data-proposal-id]')].map(el => el.dataset.proposalId)
  );
  approvals.forEach(a => {
    if (!existingIds.has(a.proposal_id)) {
      const tmp = document.createElement('div');
      tmp.innerHTML = renderCard(a);
      const card = tmp.firstElementChild;
      container.prepend(card);
      attachHandlers(card);
    }
  });
}

async function poll() {
  if (!_container) return;
  const approvals = await listPendingApprovals();
  render(_container, Array.isArray(approvals) ? approvals : []);
}

/**
 * Start polling for pending approvals, injecting cards into the given container.
 * Call stopApprovalPolling() when the view is unmounted.
 * @param {HTMLElement} container
 */
export function startApprovalPolling(container) {
  _container = container;
  poll();
  _pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}

export function stopApprovalPolling() {
  clearInterval(_pollTimer);
  _container = null;
}

/** Manually trigger a poll (call after sending a message). */
export function triggerApprovalPoll() {
  if (_container) poll();
}
