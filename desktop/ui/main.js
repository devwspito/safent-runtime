import { initialState, reduceLifecycle } from './lifecycle.js';
import { isTauriRuntime, requestCancel, requestRetry, requestDiagnostics, subscribeToBootstrapState } from './ipc.js';
import { reduceBootstrapSnapshot } from './bootstrap-state.js';
import { diagnosticsAction } from './diagnostics-action.js';
import { nativeAction } from './native-action.js';
import { renderNativeUpdater } from './native-updater.js';
import { manageFocusOnTransition, render } from './render.js';
function requireElement(id) {
    const el = document.getElementById(id);
    if (!el)
        throw new Error(`safent-desktop: missing #${id} in index.html`);
    return el;
}
function collectElements() {
    return {
        preparing: requireElement('screen-preparing'),
        preparingStatus: requireElement('preparing-status'),
        preparingBar: requireElement('preparing-bar'),
        preparingStages: requireElement('preparing-stages'),
        cancelButton: requireElement('btn-cancel'),
        cancelNote: requireElement('cancel-note'),
        failed: requireElement('screen-failed'),
        failedHeading: requireElement('failed-heading'),
        failedHint: requireElement('failed-hint'),
        failedCode: requireElement('failed-code'),
        failedStage: requireElement('failed-stage'),
        failedDetail: requireElement('failed-detail'),
        retryButton: requireElement('btn-retry'),
        diagnosticsButton: requireElement('btn-diagnostics'),
        reconnecting: requireElement('screen-reconnecting'),
        reconnectingHeading: requireElement('reconnecting-heading'),
        reconnectingHint: requireElement('reconnecting-hint'),
        ready: requireElement('screen-ready'),
    };
}
function main() {
    const els = collectElements();
    renderNativeUpdater(requireElement('native-updater'), window.__safentNativeUpdater);
    let state = initialState;
    let attemptId;
    render(state, els);
    els.cancelButton.disabled = true;
    const apply = (next) => {
        const previousKind = state.kind;
        state = next;
        render(state, els);
        if (attemptId === undefined)
            els.cancelButton.disabled = true;
        manageFocusOnTransition(previousKind, state, els);
    };
    const actionError = requireElement('action-error');
    function showActionError(message) {
        actionError.textContent = message;
        actionError.hidden = !message;
    }
    const eventError = () => showActionError('No se pudo conectar con el servicio de la aplicación. Cierra y vuelve a abrir Safent.');
    void subscribeToBootstrapState((snapshot) => {
        attemptId = snapshot.attempt_id;
        showActionError('');
        apply(reduceBootstrapSnapshot(state, snapshot));
    }).catch(eventError);
    const currentAttempt = () => {
        if (attemptId === undefined)
            throw new Error('Bootstrap attempt is not available');
        return attemptId;
    };
    const cancel = nativeAction(() => requestCancel(currentAttempt()), showActionError, 'No se pudo solicitar la cancelación. Safent puede seguir preparando tu espacio; comprueba el estado antes de reintentar.');
    const retry = nativeAction(() => requestRetry(currentAttempt()), showActionError, 'No se pudo solicitar el reintento. Puedes volver a intentarlo sin perder los detalles del fallo.');
    const diagnosticsNote = requireElement('diagnostics-note');
    const exportDiagnostic = diagnosticsAction(els.diagnosticsButton, diagnosticsNote, requestDiagnostics);
    els.diagnosticsButton.disabled = !isTauriRuntime();
    if (!isTauriRuntime())
        diagnosticsNote.textContent = 'La exportación necesita la aplicación nativa de Safent.';
    els.diagnosticsButton.addEventListener('click', () => {
        if (!els.diagnosticsButton.disabled)
            void exportDiagnostic();
    });
    els.cancelButton.addEventListener('click', () => {
        if (els.cancelButton.disabled)
            return;
        void cancel();
    });
    els.retryButton.addEventListener('click', () => {
        if (els.retryButton.disabled)
            return;
        const previous = state;
        const pending = reduceLifecycle(state, { source: 'retry-requested' });
        apply(pending);
        void retry().then(ok => {
            // Never overwrite a newer engine event while an IPC reply was in flight.
            if (!ok && state === pending)
                apply(previous);
        });
    });
    // Disable the browser's own right-click menu on every screen this shell
    // ever shows (T013 — "sin menú contextual de navegador", FR-002). This
    // covers the loader window itself; window_policy.rs applies the SAME
    // script as a Tauri initialization_script so it also reaches the remote
    // product origin once the window navigates there.
    document.addEventListener('contextmenu', (e) => e.preventDefault());
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main);
}
else {
    main();
}
//# sourceMappingURL=main.js.map