import { copyForFailure } from './failure-copy.js';
import { activeStage } from './lifecycle.js';
import { formatProgress, progressPercent } from './format.js';
const RECONNECT_HINT = {
    token_missing: 'Safent necesita volver a autorizar esta ventana.',
    engine_restarted: 'Safent se reinició. Un momento mientras vuelve a conectar.',
};
function setHidden(el, hidden) {
    if (hidden)
        el.setAttribute('hidden', '');
    else
        el.removeAttribute('hidden');
}
function renderStageItem(stage) {
    const isDone = stage.status === 'done';
    const progress = !isDone ? formatProgress(stage) : undefined;
    const marker = isDone ? '✓' : '…';
    const markerLabel = isDone ? 'Hecho' : 'En curso';
    const detail = progress ? ` — ${progress}` : '';
    return (`<li class="stage stage-${stage.status}">` +
        `<span class="stage-marker" aria-hidden="true">${marker}</span>` +
        `<span class="visually-hidden">${markerLabel}: </span>` +
        `<span class="stage-label">${escapeHtml(stage.label)}${escapeHtml(detail)}</span>` +
        `</li>`);
}
function escapeHtml(value) {
    return value
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
function renderPreparing(state, els) {
    const current = activeStage(state);
    els.preparingStatus.textContent = current ? current.label : 'Iniciando…';
    els.preparingStages.innerHTML = state.stages.map(renderStageItem).join('');
    const percent = current ? progressPercent(current) : undefined;
    if (percent === undefined) {
        els.preparingBar.removeAttribute('aria-valuenow');
        els.preparingBar.setAttribute('aria-valuetext', current?.label ?? 'Preparando');
    }
    else {
        els.preparingBar.setAttribute('aria-valuenow', String(percent));
        els.preparingBar.setAttribute('aria-valuetext', `${percent} por ciento`);
    }
    els.preparingBar.style.setProperty('--progress', percent === undefined ? '' : `${percent}%`);
    els.cancelButton.disabled = !state.cancelable;
    setHidden(els.cancelNote, state.cancelable);
}
function renderFailed(state, els) {
    const copy = copyForFailure(state.code, state.retryable);
    els.failedHeading.textContent = copy.headline;
    els.failedHint.textContent = copy.hint;
    els.failedCode.textContent = state.code;
    els.failedStage.textContent = state.stageId ?? '—';
    els.failedDetail.textContent = state.detail;
    setHidden(els.retryButton, !state.retryable);
    els.retryButton.disabled = !state.retryable || state.retrying;
    els.retryButton.textContent = state.retrying ? 'Reintentando…' : 'Reintentar';
}
function renderReconnecting(state, els) {
    els.reconnectingHint.textContent = RECONNECT_HINT[state.reason];
}
/**
 * Renders `state` into the DOM. Idempotent and cheap to call on every
 * reducer transition — there is no virtual-DOM layer to justify one in a
 * three-screen shell (see desktop/README.md's "thin, reversible" goal).
 */
export function render(state, els) {
    setHidden(els.preparing, state.kind !== 'preparing');
    setHidden(els.failed, state.kind !== 'failed');
    setHidden(els.reconnecting, state.kind !== 'reconnecting');
    setHidden(els.ready, state.kind !== 'ready');
    if (state.kind === 'preparing')
        renderPreparing(state, els);
    if (state.kind === 'failed')
        renderFailed(state, els);
    if (state.kind === 'reconnecting')
        renderReconnecting(state, els);
}
/**
 * Moves focus to the screen's heading only the moment we TRANSITION into it
 * (NFR-005) — called with the previous state's `kind` so repeated renders of
 * the same screen (e.g. a `progress` tick) never steal focus back from an
 * owner who is reading the expandable "Detalles".
 */
export function manageFocusOnTransition(previousKind, state, els) {
    if (state.kind === previousKind)
        return;
    if (state.kind === 'preparing')
        els.preparing.querySelector('h1')?.focus();
    if (state.kind === 'failed')
        els.failedHeading.focus();
    if (state.kind === 'reconnecting')
        els.reconnectingHeading.focus();
}
export function renderCancellation(state, phase, hasAttempt, els) {
    if (state.kind !== 'preparing')
        return;
    const pending = phase === 'requesting' || phase === 'requested';
    els.cancelButton.disabled = !hasAttempt || !state.cancelable || pending;
    els.cancelButton.textContent = pending ? 'Cancelación pendiente' : 'Cancelar';
    els.cancelButton.setAttribute('aria-busy', String(phase === 'requesting'));
    els.cancelNote.setAttribute('role', phase === 'error' ? 'alert' : 'status');
    const note = phase === 'requesting' ? 'Solicitando cancelar la preparación…'
        : phase === 'requested' ? 'Cancelación solicitada. Esperando a que Safent termine esta operación.'
            : phase === 'error' ? 'No se pudo confirmar la cancelación. La preparación puede continuar; revisa el estado antes de volver a solicitarla.'
                : !state.cancelable ? 'Esta fase ya no se puede cancelar; espera a que termine.'
                    : '';
    els.cancelNote.textContent = note;
    setHidden(els.cancelNote, !note);
}
//# sourceMappingURL=render.js.map