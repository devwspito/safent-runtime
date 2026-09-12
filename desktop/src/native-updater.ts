type NativeCommands = { invoke(command: string, args?: Record<string, unknown>): Promise<unknown> }
const sessions = new WeakMap<HTMLElement, object>()
const validVersion = (value: unknown): value is string => typeof value === 'string'
  && value.length <= 64 && /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(value)

/** Build capability is not a release check. Only a human click invokes the host. */
export function renderNativeUpdater(element: HTMLElement, value: unknown, commands?: NativeCommands): void {
  const session = {}
  sessions.set(element, session)
  element.hidden = true
  element.textContent = ''
  if (!value || typeof value !== 'object') return
  const data = value as Record<string, unknown>
  if (!validVersion(data.app_version)) return
  if (data.status === 'unavailable' && ['integration_missing', 'signing_configuration_missing'].includes(String(data.reason))) {
    element.textContent = `App nativa ${data.app_version} · El actualizador de la app nativa no está disponible en esta compilación.`
    element.hidden = false
    return
  }
  if (data.status !== 'available' || data.reason !== 'app_only') return
  const api = commands ?? (element.ownerDocument.defaultView as unknown as {
    __TAURI__?: { core?: NativeCommands }
  })?.__TAURI__?.core
  if (!api?.invoke) return
  const document = element.ownerDocument
  const status = document.createElement('span')
  status.setAttribute('role', 'status')
  status.setAttribute('aria-live', 'polite')
  status.textContent = `App nativa ${data.app_version} · El motor y Ads se actualizan por separado.`
  const button = document.createElement('button')
  button.type = 'button'
  button.className = 'btn btn-secondary'
  button.textContent = 'Buscar actualización de la app'
  element.append(status, document.createElement('br'), button)
  element.hidden = false
  let busy = false
  let checked: { id: number, version: string } | undefined
  const current = () => sessions.get(element) === session
  button.addEventListener('click', async () => {
    if (busy || !current()) return
    busy = true
    button.disabled = true
    const pending = checked
    checked = undefined
    button.textContent = pending ? 'Actualización en curso…' : 'Comprobando…'
    status.textContent = pending
      ? `Confirma Safent ${pending.version} en el diálogo nativo. Si aceptas, se descargará, verificará e instalará antes de reiniciar la app.`
      : 'Buscando una versión más reciente de la app nativa…'
    try {
      if (pending) {
        const result = await api.invoke('install_native_update', { checkId: pending.id })
        if (!current()) return
        if (result !== 'cancelled') throw new Error('unconfirmed_install_result')
        status.textContent = 'Actualización cancelada. No se ha iniciado la instalación.'
      } else {
        const result = await api.invoke('check_native_update') as Record<string, unknown> | null
        if (!current()) return
        if (!result || result.app_version !== data.app_version) throw new Error('invalid_update_result')
        if (result.status === 'up_to_date' && result.version === null && result.check_id === null) {
          status.textContent = `App nativa ${data.app_version} · No hay una versión más reciente.`
        } else if (result.status === 'available' && validVersion(result.version)
          && typeof result.check_id === 'number' && Number.isSafeInteger(result.check_id) && result.check_id > 0) {
          checked = { id: result.check_id, version: result.version }
          status.textContent = `Safent ${result.version} disponible. La firma del archivo se verificará antes de instalar.`
        } else throw new Error('invalid_update_result')
      }
    } catch (error) {
      if (!current()) return
      const code = typeof error === 'string' ? error : ''
      status.textContent = code === 'bootstrap_in_progress'
        ? 'Espera a que termine el arranque o la reparación antes de actualizar.'
        : code === 'updater_busy' ? 'Ya hay una comprobación o actualización en curso.'
        : code === 'check_required' ? 'La comprobación ha caducado o cambiado. Busca de nuevo la actualización antes de instalar.'
        : code === 'update_download_or_signature_failed' ? 'No se pudo descargar o verificar el archivo. No se ha iniciado la instalación.'
        : pending ? 'No se pudo completar la actualización. Comprueba la versión instalada antes de volver a intentarlo.'
        : 'No se pudo comprobar la actualización. Revisa la conexión e inténtalo de nuevo.'
    } finally {
      if (current()) {
        busy = false
        button.disabled = false
        button.textContent = checked ? `Actualizar a ${checked.version}…` : 'Buscar actualización de la app'
      }
    }
  })
}
