import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { beforeEach, describe, expect, it } from 'vitest'
import { manageFocusOnTransition, render, renderCancellation, type ScreenElements } from './render.js'
import type { UiState } from './lifecycle.js'

const here = dirname(fileURLToPath(import.meta.url))
const html = readFileSync(join(here, 'index.html'), 'utf-8')

let dom: JSDOM
let els: ScreenElements

function byId<T extends HTMLElement>(id: string): T {
  const el = dom.window.document.getElementById(id)
  if (!el) throw new Error(`fixture missing #${id}`)
  return el as T
}

beforeEach(() => {
  dom = new JSDOM(html, { pretendToBeVisual: true })
  els = {
    preparing: byId('screen-preparing'),
    preparingStatus: byId('preparing-status'),
    preparingBar: byId('preparing-bar'),
    preparingStages: byId('preparing-stages'),
    cancelButton: byId('btn-cancel'),
    cancelNote: byId('cancel-note'),
    failed: byId('screen-failed'),
    failedHeading: byId('failed-heading'),
    failedHint: byId('failed-hint'),
    failedCode: byId('failed-code'),
    failedStage: byId('failed-stage'),
    failedDetail: byId('failed-detail'),
    retryButton: byId('btn-retry'),
    diagnosticsButton: byId('btn-diagnostics'),
    reconnecting: byId('screen-reconnecting'),
    reconnectingHeading: byId('reconnecting-heading'),
    reconnectingHint: byId('reconnecting-hint'),
    ready: byId('screen-ready'),
  }
})

describe('render — index.html fixture starts with only the preparing screen visible', () => {
  it('hides failed/reconnecting/ready by default in the markup', () => {
    expect(els.failed.hasAttribute('hidden')).toBe(true)
    expect(els.reconnecting.hasAttribute('hidden')).toBe(true)
    expect(els.ready.hasAttribute('hidden')).toBe(true)
    expect(els.preparing.hasAttribute('hidden')).toBe(false)
  })
})

describe('render(preparing)', () => {
  it('describes a requested cancellation without claiming it is complete', () => {
    const state: UiState = { kind: 'preparing', stages: [], cancelable: true }
    render(state, els)
    renderCancellation(state, 'requested', true, els)
    expect(els.cancelNote.textContent).toContain('Esperando')
    expect(els.cancelNote.hasAttribute('hidden')).toBe(false)
    expect(els.cancelButton.disabled).toBe(true)
    expect(els.cancelButton.textContent).toBe('Cancelación pendiente')
  })
  it('never reenables cancellation past the irreversible stage even after IPC failure', () => {
    const state: UiState = { kind: 'preparing', stages: [], cancelable: false }
    renderCancellation(state, 'error', true, els)
    expect(els.cancelButton.disabled).toBe(true)
    expect(els.cancelNote.getAttribute('role')).toBe('alert')
  })
  it('shows the live stage label and a real, non-fabricated progress value', () => {
    const state: UiState = {
      kind: 'preparing',
      cancelable: true,
      stages: [
        { id: 'pull_engine', label: 'Descargando Safent', status: 'active', done: 45_000_000, total: 90_000_000, unit: 'bytes' },
      ],
    }
    render(state, els)
    expect(els.preparingStatus.textContent).toBe('Descargando Safent')
    expect(els.preparingStages.textContent).toContain('Descargando Safent')
    expect(els.preparingStages.textContent).toContain('43 de 86 MB')
    expect(els.preparingBar.getAttribute('aria-valuenow')).toBe('50')
  })

  it('marks the bar indeterminate (no fabricated percentage) when the stage has no known total', () => {
    const state: UiState = {
      kind: 'preparing',
      cancelable: true,
      stages: [{ id: 'preflight', label: 'Comprobando el equipo', status: 'active' }],
    }
    render(state, els)
    expect(els.preparingBar.hasAttribute('aria-valuenow')).toBe(false)
  })

  it('disables Cancelar and shows the declared note past the point of no return', () => {
    const state: UiState = {
      kind: 'preparing',
      cancelable: false,
      stages: [{ id: 'container', label: 'Arrancando Safent', status: 'active' }],
    }
    render(state, els)
    expect(els.cancelButton.disabled).toBe(true)
    expect(els.cancelNote.hasAttribute('hidden')).toBe(false)
  })
})

describe('render(failed) — the ONE failure screen', () => {
  it('shows owner-language headline, not the raw code, with the code only under Detalles', () => {
    const state: UiState = {
      kind: 'failed',
      stageId: 'pull_engine',
      code: 'registry_unreachable',
      detail: 'dial tcp 10.0.0.1:443: i/o timeout',
      retryable: true,
      retrying: false,
    }
    render(state, els)
    expect(els.failed.hasAttribute('hidden')).toBe(false)
    expect(els.failedHeading.textContent).toBe('Safent no pudo conectarse para descargar lo que falta.')
    expect(els.failedHeading.textContent).not.toContain('registry_unreachable')
    expect(els.failedCode.textContent).toBe('registry_unreachable')
    expect(els.failedDetail.textContent).toBe('dial tcp 10.0.0.1:443: i/o timeout')
    expect(els.retryButton.hasAttribute('hidden')).toBe(false)
    expect(els.retryButton.disabled).toBe(false)
  })

  it('shows an honest placeholder for Etapa when the failure has no associated stage (e.g. preflight)', () => {
    const state: UiState = {
      kind: 'failed',
      stageId: undefined,
      code: 'insufficient_disk',
      detail: 'x',
      retryable: false,
      retrying: false,
    }
    render(state, els)
    expect(els.failedStage.textContent).toBe('—')
  })

  it('hides Reintentar when the backend says this failure is not retryable', () => {
    const state: UiState = {
      kind: 'failed',
      stageId: 'restore',
      code: 'restore_failed',
      detail: 'x',
      retryable: false,
      retrying: false,
    }
    render(state, els)
    expect(els.retryButton.hasAttribute('hidden')).toBe(true)
  })

  it('shows a pending Reintentar while a retry is in flight (no double submission)', () => {
    const state: UiState = {
      kind: 'failed',
      stageId: 'health',
      code: 'daemon_unhealthy',
      detail: 'x',
      retryable: true,
      retrying: true,
    }
    render(state, els)
    expect(els.retryButton.disabled).toBe(true)
    expect(els.retryButton.textContent).toBe('Reintentando…')
  })
})

describe('render(reconnecting) — FR-012 safety net', () => {
  it('renders a distinct, honest hint per reason', () => {
    render({ kind: 'reconnecting', reason: 'token_missing' }, els)
    expect(els.reconnecting.hasAttribute('hidden')).toBe(false)
    expect(els.reconnectingHint.textContent).toMatch(/autorizar/)

    render({ kind: 'reconnecting', reason: 'engine_restarted' }, els)
    expect(els.reconnectingHint.textContent).toMatch(/reinició/)
  })
})

describe('manageFocusOnTransition — NFR-005', () => {
  it('moves focus out of the hidden failure screen when retry returns to preparing', () => {
    const state: UiState = { kind: 'preparing', stages: [], cancelable: true }
    els.retryButton.focus()
    render(state, els)
    manageFocusOnTransition('failed', state, els)
    expect(dom.window.document.activeElement === byId('preparing-heading')).toBe(true)
  })
  it('moves focus to the failed heading only when entering the failed screen', () => {
    render({ kind: 'failed', stageId: 'health', code: 'daemon_unhealthy', detail: 'x', retryable: true, retrying: false }, els)
    manageFocusOnTransition('preparing', { kind: 'failed', stageId: 'health', code: 'daemon_unhealthy', detail: 'x', retryable: true, retrying: false }, els)
    expect(dom.window.document.activeElement).toBe(els.failedHeading)
  })

  it('does not steal focus back on a re-render of the SAME screen (e.g. a progress tick)', () => {
    els.failedHeading.focus()
    els.retryButton.focus()
    manageFocusOnTransition('failed', { kind: 'failed', stageId: 'health', code: 'daemon_unhealthy', detail: 'x', retryable: true, retrying: true }, els)
    expect(dom.window.document.activeElement).toBe(els.retryButton)
  })
})
