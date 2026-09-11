import { initialState, reduceLifecycle } from './lifecycle.js';
import { requestCancel, requestRetry, subscribeToEngineEvents, subscribeToReconnect } from './ipc.js';
import { nativeAction } from './native-action.js';
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
    let state = initialState;
    render(state, els);
    const apply = (next) => {
        const previousKind = state.kind;
        state = next;
        render(state, els);
        manageFocusOnTransition(previousKind, state, els);
    };
    const actionError = requireElement('action-error');
    function showActionError(message) {
        actionError.textContent = message;
        actionError.hidden = !message;
    }
    const eventError = () => showActionError('No se pudo conectar con el servicio de la aplicación. Cierra y vuelve a abrir Safent.');
    void subscribeToEngineEvents((event) => {
        showActionError('');
        apply(reduceLifecycle(state, { source: 'engine', event }));
    }).catch(eventError);
    void subscribeToReconnect((reason) => apply(reduceLifecycle(state, { source: 'reconnect', reason }))).catch(eventError);
    const cancel = nativeAction(requestCancel, showActionError, 'No se pudo solicitar la cancelación. Safent puede seguir preparando tu espacio; comprueba el estado antes de reintentar.');
    const retry = nativeAction(requestRetry, showActionError, 'No se pudo solicitar el reintento. Puedes volver a intentarlo sin perder los detalles del fallo.');
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