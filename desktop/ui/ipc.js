function tauri() {
    return window.__TAURI__;
}
export function isTauriRuntime() {
    return tauri() !== undefined;
}
/** Subscribe first, then replay. A late snapshot cannot replace newer live state. */
export async function subscribeToBootstrapState(onState) {
    const api = tauri();
    if (!api)
        return () => { };
    let sequence = 0;
    const accept = (snapshot) => {
        if (!snapshot || !Number.isSafeInteger(snapshot.sequence) || snapshot.sequence <= sequence)
            return;
        sequence = snapshot.sequence;
        onState(snapshot);
    };
    const unlisten = await api.event.listen('safent://bootstrap-state', msg => accept(msg.payload));
    try {
        accept(await api.core.invoke('get_bootstrap_state'));
    }
    catch (error) {
        unlisten();
        throw error;
    }
    return unlisten;
}
/**
 * Contract app-engine.md §8 (wrapper → webview): `boot.rs`'s `TauriNotifier`
 * translates the CLI's raw NDJSON into `EngineEventPayload` and emits it
 * here — NOT a verbatim NDJSON forward (see lifecycle.ts's `EngineEvent`).
 */
const ENGINE_EVENT_CHANNEL = 'safent://engine-event';
/**
 * Contract app-engine.md §8: emitted by `boot.rs` when it enters the
 * `reconnecting` phase (data-model.md EngineLifecycle) instead of navigating
 * the window — i.e. FR-012's safety net at the shell level, distinct from
 * frontend/src/components/ReconnectScreen.tsx which covers the SAME FR-012
 * once the product page itself is already loaded.
 */
const RECONNECT_CHANNEL = 'safent://reconnecting';
/** Subscribes to the engine event stream. Returns an unsubscribe function. */
export async function subscribeToEngineEvents(onEvent) {
    const api = tauri();
    if (!api)
        return () => { };
    return api.event.listen(ENGINE_EVENT_CHANNEL, (msg) => onEvent(msg.payload));
}
export async function subscribeToReconnect(onReconnect) {
    const api = tauri();
    if (!api)
        return () => { };
    return api.event.listen(RECONNECT_CHANNEL, (msg) => onReconnect(msg.payload.reason));
}
// Exact names of the `#[tauri::command]`s boot.rs registers (main.rs's
// `generate_handler!` + capabilities/default.json's `allow-*` entries).
async function invoke(command) {
    const api = tauri();
    if (!api)
        throw new Error('Native Safent runtime is unavailable');
    await api.core.invoke(command);
}
/** "Cancelar": honest per contract §6 — the backend answers with a `failed` event. */
export function requestCancel() {
    return invoke('cancel_bootstrap');
}
/** "Reintentar" on the one failure screen. */
export function requestRetry() {
    return invoke('retry_bootstrap');
}
export async function requestDiagnostics() {
    const api = tauri();
    if (!api)
        throw new Error('Native Safent runtime is unavailable');
    const result = await api.core.invoke('export_diagnostics');
    if (result?.status !== 'saved' && result?.status !== 'cancelled')
        throw new Error('Unconfirmed diagnostic export');
    return result;
}
//# sourceMappingURL=ipc.js.map