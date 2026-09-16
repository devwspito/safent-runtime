// Wire shape for `safent://engine-event` — the WRAPPER's own re-emission to
// the webview (boot.rs's `EngineEventPayload`/`ReconnectingPayload`), now
// documented at contracts/app-engine.md §8. This is NOT byte-identical to
// §3's CLI↔wrapper NDJSON (discriminant `t`+`id` there vs `kind`+`stage`
// here, a richer `ready` carrying digests instead of `endpoint_ref`, no
// `facts` kind at all): boot.rs translates the CLI's raw protocol into this
// shape before forwarding it, and this module owns ZERO knowledge of Tauri,
// the DOM, or the CLI process beyond consuming exactly what boot.rs emits —
// it is a pure state machine so the "una pantalla de fallo" / "cancelar sin
// dejar el equipo a medias" rules (FR-031..FR-033) are enforceable with
// plain unit tests.
export const initialState = { kind: 'preparing', stages: [], cancelable: true };
function upsertStage(stages, next) {
    const existing = stages.findIndex((s) => s.id === next.id);
    const entry = { ...next, status: 'active' };
    if (existing === -1)
        return [...stages, entry];
    const copy = stages.slice();
    copy[existing] = { ...copy[existing], ...entry };
    return copy;
}
function withStageUpdate(stages, id, patch) {
    return stages.map((s) => (s.id === id ? { ...s, ...patch } : s));
}
function stagesOf(state) {
    return state.kind === 'preparing' ? state.stages : [];
}
/** The single reducer driving the preparation / failure / reconnect screens. */
export function reduceLifecycle(state, action) {
    if (action.source === 'reconnect') {
        return { kind: 'reconnecting', reason: action.reason };
    }
    if (action.source === 'retry-requested') {
        return state.kind === 'failed' && state.retryable && !state.retrying
            ? { ...state, retrying: true } : state;
    }
    const event = action.event;
    switch (event.kind) {
        case 'stage': {
            const stages = upsertStage(stagesOf(state), {
                id: event.stage,
                label: event.label,
                totalBytes: event.total_bytes ?? undefined,
            });
            return { kind: 'preparing', stages, cancelable: !event.point_of_no_return };
        }
        case 'progress': {
            const stages = withStageUpdate(stagesOf(state), event.stage, {
                done: event.done,
                total: event.total ?? undefined,
                unit: event.unit,
            });
            // `progress` carries no point-of-no-return flag of its own (only
            // `stage` does) — trust whatever the most recent `stage` event already
            // established; an out-of-order `progress` before any `stage` is a
            // contract violation this defaults open (cancelable) rather than wedges on.
            const cancelable = state.kind === 'preparing' ? state.cancelable : true;
            return { kind: 'preparing', stages, cancelable };
        }
        case 'done': {
            const stages = withStageUpdate(stagesOf(state), event.stage, { status: 'done', ms: event.ms });
            const cancelable = state.kind === 'preparing' ? state.cancelable : true;
            return { kind: 'preparing', stages, cancelable };
        }
        case 'failed':
            return {
                kind: 'failed',
                stageId: activeStage(state)?.id,
                code: event.code,
                detail: event.detail,
                retryable: event.retryable,
                retrying: false,
            };
        case 'ready':
            return { kind: 'ready' };
        default: {
            const exhaustive = event;
            return exhaustive;
        }
    }
}
/** The stage currently being worked on, for the headline of the preparation screen. */
export function activeStage(state) {
    if (state.kind !== 'preparing')
        return undefined;
    for (let i = state.stages.length - 1; i >= 0; i -= 1) {
        if (state.stages[i].status === 'active')
            return state.stages[i];
    }
    return state.stages[state.stages.length - 1];
}
//# sourceMappingURL=lifecycle.js.map