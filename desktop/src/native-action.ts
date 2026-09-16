/** Single-flight IPC feedback. A fulfilled invocation is not engine completion. */
export function nativeAction(
  invoke: () => Promise<void>,
  showError: (message: string) => void,
  failureMessage: string,
): () => Promise<boolean> {
  let inFlight = false
  return async () => {
    if (inFlight) return false
    inFlight = true
    showError('')
    try {
      await invoke()
      return true
    } catch {
      showError(failureMessage)
      return false
    } finally {
      inFlight = false
    }
  }
}

export type CancellationPhase = 'idle' | 'requesting' | 'requested' | 'error'

/** Renderer feedback scoped to the native attempt. An IPC acknowledgement is
 * never a completed cancellation; a later bootstrap snapshot owns that state. */
export function nativeCancellation(invoke: (attemptId: number) => Promise<void>, changed: () => void) {
  let attempt: number | undefined
  let active = false
  let generation = 0
  let phase: CancellationPhase = 'idle'
  return {
    get phase(): CancellationPhase { return phase },
    sync(nextAttempt: number | undefined, preparing: boolean) {
      if (nextAttempt !== attempt || !preparing) {
        generation += 1
        phase = 'idle'
      }
      attempt = nextAttempt
      active = preparing
    },
    async request() {
      if (!active || attempt === undefined || phase === 'requesting' || phase === 'requested') return
      const epoch = generation
      phase = 'requesting'
      changed()
      try {
        await invoke(attempt)
        if (generation !== epoch) return
        phase = 'requested'
      } catch {
        if (generation !== epoch) return
        phase = 'error'
      }
      changed()
    },
  }
}
