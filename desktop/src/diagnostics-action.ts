import type { DiagnosticResult } from './ipc.js'

/** One native chooser at a time; cancel is not failure or a saved file. */
export function diagnosticsAction(
  button: HTMLButtonElement,
  note: HTMLElement,
  invoke: () => Promise<DiagnosticResult>,
): () => Promise<void> {
  let pending = false
  return async () => {
    if (pending) return
    pending = true
    button.disabled = true
    button.setAttribute('aria-busy', 'true')
    button.textContent = 'Exportando…'
    note.setAttribute('role', 'status')
    note.textContent = 'Elige dónde guardar el diagnóstico de arranque.'
    try {
      const result = await invoke()
      if (result.status !== 'saved' && result.status !== 'cancelled') throw new Error('Unconfirmed export')
      note.textContent = result.status === 'saved'
        ? 'Diagnóstico de arranque guardado en la ubicación elegida.'
        : 'Exportación cancelada. No se ha guardado ningún archivo.'
    } catch {
      note.setAttribute('role', 'alert')
      note.textContent = 'No se pudo guardar el diagnóstico de arranque. Comprueba la ubicación y vuelve a intentarlo.'
    } finally {
      pending = false
      button.disabled = false
      button.removeAttribute('aria-busy')
      button.textContent = 'Exportar diagnóstico de arranque'
    }
  }
}
