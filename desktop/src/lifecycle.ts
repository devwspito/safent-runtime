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

export type StageId =
  | 'preflight'
  | 'runtime_staging'
  | 'machine'
  | 'pull_engine'
  | 'pull_companion'
  | 'container'
  | 'health'
  | 'companion_scaffold'
  | 'companion_up'
  | 'companion_reload'
  | 'backup'
  | 'restore'
  | 'cleanup'

export type ProgressUnit = 'bytes' | 'layers' | 'steps'

// Closed, stable set (contract §3's 21 CLI codes) PLUS two wrapper-only
// extensions boot.rs/domain.rs's `FailureCode` synthesizes locally and can
// still emit on this same channel (`cancelled_by_owner`,
// `cli_porcelain_unsupported` — see contracts/app-engine.md §8). Anything
// else arriving on the wire is a contract violation, not a reason to crash
// the UI — see failure-copy.ts's fallback.
export type FailureCode =
  | 'unsupported_os'
  | 'unsupported_arch'
  | 'insufficient_disk'
  | 'insufficient_memory'
  | 'runtime_hash_mismatch'
  | 'machine_create_failed'
  | 'machine_start_failed'
  | 'userns_blocked'
  | 'helper_denied'
  | 'registry_unreachable'
  | 'digest_mismatch'
  | 'pull_interrupted'
  | 'port_exhausted'
  | 'container_start_failed'
  | 'daemon_unhealthy'
  | 'companion_network_conflict'
  | 'companion_migration_failed'
  | 'companion_unreachable'
  | 'backup_failed'
  | 'restore_failed'
  | 'clock_skew'
  | 'cancelled_by_owner'
  | 'cli_porcelain_unsupported'
  | 'repair_ineffective'
  | 'local_storage_conflict'
  | 'engine_digest_missing'

/** One line from `safent://engine-event`, exactly `boot.rs`'s `EngineEventPayload`. */
export type EngineEvent =
  | {
      kind: 'stage'
      stage: StageId
      label: string
      total_bytes: number | null
      point_of_no_return: boolean
    }
  | { kind: 'progress'; stage: StageId; done: number; total: number | null; unit: ProgressUnit }
  | { kind: 'done'; stage: StageId; ms: number }
  | { kind: 'failed'; code: FailureCode; detail: string; retryable: boolean }
  | { kind: 'ready'; app_version: string; engine_digest: string; companion_digest: string | null }

/**
 * Inputs to the state machine beyond the CLI's own NDJSON: the wrapper's own
 * FR-012 safety net (no valid ticket to navigate to, `safent://reconnecting`
 * — boot.rs's `ReconnectingPayload`) and the owner clicking "Reintentar".
 */
export type LifecycleAction =
  | { source: 'engine'; event: EngineEvent }
  | { source: 'reconnect'; reason: 'token_missing' | 'engine_restarted' }
  | { source: 'retry-requested' }

export interface StageProgress {
  readonly id: StageId
  readonly label: string
  readonly totalBytes?: number
  readonly done?: number
  readonly total?: number
  readonly unit?: ProgressUnit
  readonly status: 'active' | 'done'
  readonly ms?: number
}

/**
 * Discriminated union — the screen you render is a pure function of this
 * type, so "cargar sin vale" and "un fallo" can never both be true at once
 * (the impossible-states-impossible rule).
 */
export type UiState =
  | { readonly kind: 'preparing'; readonly stages: readonly StageProgress[]; readonly cancelable: boolean }
  | { readonly kind: 'ready' }
  | {
      readonly kind: 'failed'
      /** Undefined when the failure arrived before any stage started (e.g. a
       *  preflight check) — `Failed` carries no stage of its own on the wire
       *  (boot.rs's `FailureCause` is stage-agnostic by design), so this is
       *  the last stage this state machine itself saw active. */
      readonly stageId: StageId | undefined
      readonly code: FailureCode
      readonly detail: string
      readonly retryable: boolean
      readonly retrying: boolean
    }
  | { readonly kind: 'reconnecting'; readonly reason: 'token_missing' | 'engine_restarted' }

export const initialState: UiState = { kind: 'preparing', stages: [], cancelable: true }

function upsertStage(
  stages: readonly StageProgress[],
  next: Pick<StageProgress, 'id' | 'label' | 'totalBytes'>,
): readonly StageProgress[] {
  const existing = stages.findIndex((s) => s.id === next.id)
  const entry: StageProgress = { ...next, status: 'active' }
  if (existing === -1) return [...stages, entry]
  const copy = stages.slice()
  copy[existing] = { ...copy[existing], ...entry }
  return copy
}

function withStageUpdate(
  stages: readonly StageProgress[],
  id: StageId,
  patch: Partial<StageProgress>,
): readonly StageProgress[] {
  return stages.map((s) => (s.id === id ? { ...s, ...patch } : s))
}

function stagesOf(state: UiState): readonly StageProgress[] {
  return state.kind === 'preparing' ? state.stages : []
}

/** The single reducer driving the preparation / failure / reconnect screens. */
export function reduceLifecycle(state: UiState, action: LifecycleAction): UiState {
  if (action.source === 'reconnect') {
    return { kind: 'reconnecting', reason: action.reason }
  }

  if (action.source === 'retry-requested') {
    return state.kind === 'failed' ? { ...state, retrying: true } : state
  }

  const event = action.event
  switch (event.kind) {
    case 'stage': {
      const stages = upsertStage(stagesOf(state), {
        id: event.stage,
        label: event.label,
        totalBytes: event.total_bytes ?? undefined,
      })
      return { kind: 'preparing', stages, cancelable: !event.point_of_no_return }
    }
    case 'progress': {
      const stages = withStageUpdate(stagesOf(state), event.stage, {
        done: event.done,
        total: event.total ?? undefined,
        unit: event.unit,
      })
      // `progress` carries no point-of-no-return flag of its own (only
      // `stage` does) — trust whatever the most recent `stage` event already
      // established; an out-of-order `progress` before any `stage` is a
      // contract violation this defaults open (cancelable) rather than wedges on.
      const cancelable = state.kind === 'preparing' ? state.cancelable : true
      return { kind: 'preparing', stages, cancelable }
    }
    case 'done': {
      const stages = withStageUpdate(stagesOf(state), event.stage, { status: 'done', ms: event.ms })
      const cancelable = state.kind === 'preparing' ? state.cancelable : true
      return { kind: 'preparing', stages, cancelable }
    }
    case 'failed':
      return {
        kind: 'failed',
        stageId: activeStage(state)?.id,
        code: event.code,
        detail: event.detail,
        retryable: event.retryable,
        retrying: false,
      }
    case 'ready':
      return { kind: 'ready' }
    default: {
      const exhaustive: never = event
      return exhaustive
    }
  }
}

/** The stage currently being worked on, for the headline of the preparation screen. */
export function activeStage(state: UiState): StageProgress | undefined {
  if (state.kind !== 'preparing') return undefined
  for (let i = state.stages.length - 1; i >= 0; i -= 1) {
    if (state.stages[i].status === 'active') return state.stages[i]
  }
  return state.stages[state.stages.length - 1]
}
