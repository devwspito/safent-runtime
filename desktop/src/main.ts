import { initialState, reduceLifecycle, type UiState } from './lifecycle.js'
import { isTauriRuntime, requestCancel, requestRetry, requestDiagnostics, subscribeToBootstrapState } from './ipc.js'
import { reduceBootstrapSnapshot } from './bootstrap-state.js'
import { diagnosticsAction } from './diagnostics-action.js'
import { nativeAction, nativeCancellation } from './native-action.js'
import { renderNativeUpdater } from './native-updater.js'
import { manageFocusOnTransition, render, renderCancellation, type ScreenElements } from './render.js'

function requireElement<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id)
  if (!el) throw new Error(`safent-desktop: missing #${id} in index.html`)
  return el as T
}

function collectElements(): ScreenElements {
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
  }
}

function main(): void {
  const els = collectElements()
  renderNativeUpdater(requireElement('native-updater'),
    (window as unknown as { __safentNativeUpdater?: unknown }).__safentNativeUpdater)
  let state: UiState = initialState
  let attemptId: number | undefined
  const cancel = nativeCancellation(requestCancel, () => {
    renderCancellation(state, cancel.phase, attemptId !== undefined, els)
  })
  render(state, els)
  els.cancelButton.disabled = true

  const apply = (next: UiState): void => {
    const previousKind = state.kind
    state = next
    cancel.sync(attemptId, state.kind === 'preparing')
    render(state, els)
    renderCancellation(state, cancel.phase, attemptId !== undefined, els)
    manageFocusOnTransition(previousKind, state, els)
  }

  const actionError = requireElement('action-error')
  function showActionError(message: string) {
    actionError.textContent = message
    actionError.hidden = !message
  }
  const eventError = () => showActionError('No se pudo conectar con el servicio de la aplicación. Cierra y vuelve a abrir Safent.')
  void subscribeToBootstrapState((snapshot) => {
    attemptId = snapshot.attempt_id
    showActionError('')
    apply(reduceBootstrapSnapshot(state, snapshot))
  }).catch(eventError)

  const currentAttempt = () => {
    if (attemptId === undefined) throw new Error('Bootstrap attempt is not available')
    return attemptId
  }
  let retryAttempt: number | undefined
  const retry = nativeAction(() => requestRetry(currentAttempt()), message => {
    if (retryAttempt === attemptId && state.kind === 'failed') showActionError(message)
  },
    'No se pudo solicitar el reintento. Puedes volver a intentarlo sin perder los detalles del fallo.')

  const diagnosticsNote = requireElement('diagnostics-note')
  const exportDiagnostic = diagnosticsAction(els.diagnosticsButton, diagnosticsNote, requestDiagnostics)
  els.diagnosticsButton.disabled = !isTauriRuntime()
  if (!isTauriRuntime()) diagnosticsNote.textContent = 'La exportación necesita la aplicación nativa de Safent.'
  els.diagnosticsButton.addEventListener('click', () => {
    if (!els.diagnosticsButton.disabled) void exportDiagnostic()
  })

  els.cancelButton.addEventListener('click', () => {
    if (els.cancelButton.disabled) return
    const heldFocus = document.activeElement === els.cancelButton
    void cancel.request()
    if (heldFocus) els.cancelNote.focus()
  })

  els.retryButton.addEventListener('click', () => {
    if (els.retryButton.disabled) return
    retryAttempt = attemptId
    const previous = state
    const pending = reduceLifecycle(state, { source: 'retry-requested' })
    apply(pending)
    void retry().then(ok => {
      // Never overwrite a newer engine event while an IPC reply was in flight.
      if (!ok && state === pending) apply(previous)
    })
  })

  // Disable the browser's own right-click menu on every screen this shell
  // ever shows (T013 — "sin menú contextual de navegador", FR-002). This
  // covers the loader window itself; window_policy.rs applies the SAME
  // script as a Tauri initialization_script so it also reaches the remote
  // product origin once the window navigates there.
  document.addEventListener('contextmenu', (e) => e.preventDefault())
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main)
} else {
  main()
}
