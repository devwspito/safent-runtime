import { describe, expect, it } from 'vitest'
import { formatProgress, progressPercent } from './format.js'
import type { StageProgress } from './lifecycle.js'

function stage(patch: Partial<StageProgress>): StageProgress {
  return { id: 'pull_engine', label: 'Descargando Safent', status: 'active', ...patch }
}

describe('formatProgress — owner-language byte counts', () => {
  it('renders bytes as whole MB, never raw bytes', () => {
    expect(formatProgress(stage({ done: 45_000_000, total: 90_000_000, unit: 'bytes' }))).toBe('43 de 86 MB')
  })

  it('omits the total when unknown (indeterminate download)', () => {
    expect(formatProgress(stage({ done: 45_000_000, unit: 'bytes' }))).toBe('43 MB')
  })

  it('renders layers/steps units without MB conversion', () => {
    expect(formatProgress(stage({ done: 3, total: 5, unit: 'layers' }))).toBe('3 de 5 partes')
  })

  it('is undefined before any progress arrives', () => {
    expect(formatProgress(stage({}))).toBeUndefined()
  })
})

describe('progressPercent', () => {
  it('computes a clamped 0-100', () => {
    expect(progressPercent(stage({ done: 50, total: 200, unit: 'bytes' }))).toBe(25)
    expect(progressPercent(stage({ done: 999, total: 200, unit: 'bytes' }))).toBe(100)
  })

  it('is undefined without a known total — renders as indeterminate, never a fake 0%', () => {
    expect(progressPercent(stage({ done: 50, unit: 'bytes' }))).toBeUndefined()
    expect(progressPercent(stage({ total: 0, unit: 'bytes' }))).toBeUndefined()
  })
})
