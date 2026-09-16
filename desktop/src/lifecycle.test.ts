import { describe, expect, it } from 'vitest'
import { activeStage, initialState, reduceLifecycle, type EngineEvent, type LifecycleAction, type UiState } from './lifecycle.js'

function engine(event: EngineEvent): LifecycleAction {
  return { source: 'engine', event }
}

function run(events: readonly LifecycleAction[]): UiState {
  return events.reduce(reduceLifecycle, initialState)
}

describe('reduceLifecycle — preparation stages (contract app-engine.md §8, safent://engine-event)', () => {
  it('starts with an empty, cancelable preparing state', () => {
    expect(initialState).toEqual({ kind: 'preparing', stages: [], cancelable: true })
  })

  it('adds a stage as active on `stage` and updates progress on `progress`', () => {
    const state = run([
      engine({
        kind: 'stage',
        stage: 'runtime_staging',
        label: 'Preparando la base de ejecución',
        total_bytes: 90_000_000,
        point_of_no_return: false,
      }),
      engine({ kind: 'progress', stage: 'runtime_staging', done: 45_000_000, total: 90_000_000, unit: 'bytes' }),
    ])
    expect(state).toEqual({
      kind: 'preparing',
      cancelable: true,
      stages: [
        {
          id: 'runtime_staging',
          label: 'Preparando la base de ejecución',
          totalBytes: 90_000_000,
          status: 'active',
          done: 45_000_000,
          total: 90_000_000,
          unit: 'bytes',
        },
      ],
    })
  })

  it('a null total_bytes/total (serde default for None) is treated as unknown, not zero', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'preflight', label: 'Comprobando el equipo', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'progress', stage: 'preflight', done: 1, total: null, unit: 'steps' }),
    ])
    if (state.kind !== 'preparing') throw new Error('unreachable')
    expect(state.stages[0].totalBytes).toBeUndefined()
    expect(state.stages[0].total).toBeUndefined()
  })

  it('marks a stage done without losing earlier stages, and activeStage() reports the live one', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'preflight', label: 'Comprobando el equipo', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'done', stage: 'preflight', ms: 120 }),
      engine({
        kind: 'stage',
        stage: 'runtime_staging',
        label: 'Preparando la base de ejecución',
        total_bytes: null,
        point_of_no_return: false,
      }),
    ])
    expect(state.kind).toBe('preparing')
    if (state.kind !== 'preparing') throw new Error('unreachable')
    expect(state.stages.map((s) => [s.id, s.status])).toEqual([
      ['preflight', 'done'],
      ['runtime_staging', 'active'],
    ])
    expect(activeStage(state)?.id).toBe('runtime_staging')
  })

  it('tracks concurrent stages independently (pull_engine + pull_companion)', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'pull_engine', label: 'Descargando Safent', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'stage', stage: 'pull_companion', label: 'Descargando Anuncios', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'progress', stage: 'pull_companion', done: 10, total: 100, unit: 'layers' }),
    ])
    if (state.kind !== 'preparing') throw new Error('unreachable')
    expect(state.stages).toHaveLength(2)
    expect(state.stages[1]).toMatchObject({ id: 'pull_companion', done: 10, total: 100 })
  })

  it('disables cancel the moment the wire declares a stage as the point of no return (today: container)', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'container', label: 'Arrancando Safent', total_bytes: null, point_of_no_return: true }),
    ])
    expect(state).toMatchObject({ kind: 'preparing', cancelable: false })
  })

  it('trusts each `stage` event\'s own flag rather than latching cancelable to false forever', () => {
    // boot.rs's current bootstrap_point_of_no_return never actually produces
    // this exact sequence (every stage from `container` on stays true) — this
    // proves the REDUCER's mechanism (no client-side memory of "have we ever
    // seen true"), independent of what today's specific stage list happens
    // to send.
    const state = run([
      engine({ kind: 'stage', stage: 'container', label: 'Arrancando Safent', total_bytes: null, point_of_no_return: true }),
      engine({ kind: 'stage', stage: 'pull_companion', label: 'Descargando Anuncios', total_bytes: null, point_of_no_return: false }),
    ])
    expect(state).toMatchObject({ cancelable: true })
  })
})

describe('reduceLifecycle — the ONE failure screen (FR-033)', () => {
  it('ignores a retry request when the current failure is not retryable', () => {
    const failed = run([engine({ kind: 'failed', code: 'machine_start_failed', detail: 'x', retryable: false })])
    expect(reduceLifecycle(failed, { source: 'retry-requested' })).toBe(failed)
  })
  it('turns `failed` into the failed state, deriving stageId from the last active stage (the wire carries no stage on `failed`)', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'pull_engine', label: 'Descargando Safent', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'failed', code: 'registry_unreachable', detail: 'dial tcp: timeout', retryable: true }),
    ])
    expect(state).toEqual({
      kind: 'failed',
      stageId: 'pull_engine',
      code: 'registry_unreachable',
      detail: 'dial tcp: timeout',
      retryable: true,
      retrying: false,
    })
  })

  it('a failure before any stage started (e.g. preflight) has an undefined stageId, not a crash', () => {
    const state = run([engine({ kind: 'failed', code: 'insufficient_disk', detail: 'x', retryable: false })])
    expect(state).toMatchObject({ kind: 'failed', stageId: undefined })
  })

  it('a cancel is just a `failed` event per contract §6 — same one screen, no separate "cancelled" UI state', () => {
    const state = run([
      engine({ kind: 'stage', stage: 'machine', label: 'Preparando la máquina', total_bytes: null, point_of_no_return: false }),
      engine({ kind: 'failed', code: 'cancelled_by_owner', detail: 'cancelado por el dueño', retryable: false }),
    ])
    expect(state.kind).toBe('failed')
  })

  it('"Reintentar" marks retrying only while already failed, and clears on the next real event', () => {
    const failed = run([engine({ kind: 'failed', code: 'daemon_unhealthy', detail: 'x', retryable: true })])
    const retrying = reduceLifecycle(failed, { source: 'retry-requested' })
    expect(retrying).toMatchObject({ kind: 'failed', retrying: true })

    // clicking retry before any failure is a no-op — nothing to retry yet
    expect(reduceLifecycle(initialState, { source: 'retry-requested' })).toBe(initialState)

    const resumed = reduceLifecycle(
      retrying,
      engine({ kind: 'stage', stage: 'health', label: 'Comprobando salud', total_bytes: null, point_of_no_return: false }),
    )
    expect(resumed).toMatchObject({ kind: 'preparing' })
  })
})

describe('reduceLifecycle — reconnect safety net (FR-012, distinct from frontend/ReconnectScreen.tsx)', () => {
  it('a missing ticket resolves to ONE reconnecting state, not a request storm', () => {
    const state = reduceLifecycle(initialState, { source: 'reconnect', reason: 'token_missing' })
    expect(state).toEqual({ kind: 'reconnecting', reason: 'token_missing' })
  })

  it('an engine restart mid-session also resolves to reconnecting, with its own honest reason', () => {
    const ready = run([
      engine({ kind: 'ready', app_version: '0.2.0', engine_digest: 'sha256:engine-good', companion_digest: null }),
    ])
    expect(ready).toEqual({ kind: 'ready' })
    const state = reduceLifecycle(ready, { source: 'reconnect', reason: 'engine_restarted' })
    expect(state).toEqual({ kind: 'reconnecting', reason: 'engine_restarted' })
  })
})
