import type { EngineEvent } from './lifecycle.js'

// The shell ships with ZERO npm runtime dependencies (see desktop/README.md's
// "reversible shell" goal): `app.withGlobalTauri: true` in tauri.conf.json
// already exposes the IPC surface as `window.__TAURI__`, so we type just the
// two calls this screen needs instead of adding `@tauri-apps/api` + a
// bundler to resolve its import.
interface TauriEventApi {
  listen<T>(event: string, handler: (payload: { payload: T }) => void): Promise<() => void>
}
interface TauriCoreApi {
  invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>
}
interface TauriGlobal {
  event: TauriEventApi
  core: TauriCoreApi
}

function tauri(): TauriGlobal | undefined {
  return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__
}

export function isTauriRuntime(): boolean {
  return tauri() !== undefined
}

/**
 * Contract app-engine.md §8 (wrapper → webview): `boot.rs`'s `TauriNotifier`
 * translates the CLI's raw NDJSON into `EngineEventPayload` and emits it
 * here — NOT a verbatim NDJSON forward (see lifecycle.ts's `EngineEvent`).
 */
const ENGINE_EVENT_CHANNEL = 'safent://engine-event'

/**
 * Contract app-engine.md §8: emitted by `boot.rs` when it enters the
 * `reconnecting` phase (data-model.md EngineLifecycle) instead of navigating
 * the window — i.e. FR-012's safety net at the shell level, distinct from
 * frontend/src/components/ReconnectScreen.tsx which covers the SAME FR-012
 * once the product page itself is already loaded.
 */
const RECONNECT_CHANNEL = 'safent://reconnecting'

export type ReconnectReason = 'token_missing' | 'engine_restarted'

/** Subscribes to the engine event stream. Returns an unsubscribe function. */
export async function subscribeToEngineEvents(
  onEvent: (event: EngineEvent) => void,
): Promise<() => void> {
  const api = tauri()
  if (!api) return () => {}
  return api.event.listen<EngineEvent>(ENGINE_EVENT_CHANNEL, (msg) => onEvent(msg.payload))
}

export async function subscribeToReconnect(
  onReconnect: (reason: ReconnectReason) => void,
): Promise<() => void> {
  const api = tauri()
  if (!api) return () => {}
  return api.event.listen<{ reason: ReconnectReason }>(RECONNECT_CHANNEL, (msg) =>
    onReconnect(msg.payload.reason),
  )
}

// Exact names of the `#[tauri::command]`s boot.rs registers (main.rs's
// `generate_handler!` + capabilities/default.json's `allow-*` entries).
async function invoke(command: string): Promise<void> {
  const api = tauri()
  if (!api) throw new Error('Native Safent runtime is unavailable')
  await api.core.invoke(command)
}

/** "Cancelar": honest per contract §6 — the backend answers with a `failed` event. */
export function requestCancel(): Promise<void> {
  return invoke('cancel_bootstrap')
}

/** "Reintentar" on the one failure screen. */
export function requestRetry(): Promise<void> {
  return invoke('retry_bootstrap')
}
