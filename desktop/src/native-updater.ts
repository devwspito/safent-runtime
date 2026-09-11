/** Read-only native build metadata. This is not a check for a newer release. */
export function renderNativeUpdater(element: HTMLElement, value: unknown): void {
  element.hidden = true
  element.textContent = ''
  if (!value || typeof value !== 'object') return
  const data = value as Record<string, unknown>
  if (data.status !== 'unavailable' || data.reason !== 'integration_missing') return
  if (typeof data.app_version !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(data.app_version) || data.app_version.length > 64) return
  element.textContent = `App nativa ${data.app_version} · El actualizador de la app nativa no está disponible en esta compilación.`
  element.hidden = false
}
