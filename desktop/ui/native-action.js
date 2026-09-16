/** Single-flight IPC feedback. A fulfilled invocation is not engine completion. */
export function nativeAction(invoke, showError, failureMessage) {
    let inFlight = false;
    return async () => {
        if (inFlight)
            return false;
        inFlight = true;
        showError('');
        try {
            await invoke();
            return true;
        }
        catch {
            showError(failureMessage);
            return false;
        }
        finally {
            inFlight = false;
        }
    };
}
/** Renderer feedback scoped to the native attempt. An IPC acknowledgement is
 * never a completed cancellation; a later bootstrap snapshot owns that state. */
export function nativeCancellation(invoke, changed) {
    let attempt;
    let active = false;
    let generation = 0;
    let phase = 'idle';
    return {
        get phase() { return phase; },
        sync(nextAttempt, preparing) {
            if (nextAttempt !== attempt || !preparing) {
                generation += 1;
                phase = 'idle';
            }
            attempt = nextAttempt;
            active = preparing;
        },
        async request() {
            if (!active || attempt === undefined || phase === 'requesting' || phase === 'requested')
                return;
            const epoch = generation;
            phase = 'requesting';
            changed();
            try {
                await invoke(attempt);
                if (generation !== epoch)
                    return;
                phase = 'requested';
            }
            catch {
                if (generation !== epoch)
                    return;
                phase = 'error';
            }
            changed();
        },
    };
}
//# sourceMappingURL=native-action.js.map