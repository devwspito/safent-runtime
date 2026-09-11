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
