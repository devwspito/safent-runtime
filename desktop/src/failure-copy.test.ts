import { describe, expect, it } from 'vitest'
import { copyForFailure } from './failure-copy.js'
import type { FailureCode } from './lifecycle.js'

const ALL_CODES: readonly FailureCode[] = [
  'unsupported_os',
  'unsupported_arch',
  'insufficient_disk',
  'insufficient_memory',
  'runtime_hash_mismatch',
  'machine_create_failed',
  'machine_start_failed',
  'userns_blocked',
  'helper_denied',
  'registry_unreachable',
  'digest_mismatch',
  'pull_interrupted',
  'port_exhausted',
  'container_start_failed',
  'daemon_unhealthy',
  'companion_network_conflict',
  'companion_migration_failed',
  'companion_unreachable',
  'backup_failed',
  'restore_failed',
  'clock_skew',
  'cancelled_by_owner',
  'cli_porcelain_unsupported',
  'repair_ineffective',
  'local_storage_conflict',
  'engine_digest_missing',
]

const JARGON = /podman|docker|contenedor|container|\bvm\b|máquina virtual|digest|daemon|puertos|terminal|comando/i

describe('copyForFailure — FR-007 named states, NFR-004 owner vocabulary', () => {
  it('explains a blocked machine start without assuming a detected foreign VM or promising a retry', () => {
    const copy = copyForFailure('machine_start_failed', false)
    expect(copy.hint).not.toMatch(/vuelve a intentarlo/i)
    expect(copy.hint).toContain('No necesitas configurar otras aplicaciones')
    expect(copy.hint).toContain('diagnóstico')
    expect(copyForFailure('machine_start_failed', true).hint).toBe('Vuelve a intentarlo.')
  })
  it('does not suggest an unavailable retry when the embedded CLI is missing', () => {
    const copy = copyForFailure('cli_porcelain_unsupported')
    expect(copy.hint).not.toMatch(/vuelve a intentarlo/i)
    expect(copy.hint).toContain('diagnóstico de arranque')
  })
  it.each(ALL_CODES)('has a non-empty headline and hint for every contract FailureCode (%s)', (code) => {
    const copy = copyForFailure(code)
    expect(copy.headline.length).toBeGreaterThan(0)
    expect(copy.hint.length).toBeGreaterThan(0)
  })

  it.each(ALL_CODES)('keeps infrastructure jargon out of the headline (%s)', (code) => {
    expect(copyForFailure(code).headline).not.toMatch(JARGON)
  })

  it.each(ALL_CODES)('keeps every recovery hint non-technical and aligned with blocked retry (%s)', (code) => {
    for (const retryable of [true, false]) {
      const copy = copyForFailure(code, retryable)
      expect(copy.hint).not.toMatch(JARGON)
      if (!retryable) expect(copy.hint).not.toMatch(/vuelve a intentarlo|volver a intentarlo|reintentar|concede el permiso/i)
    }
  })

  it('does not promise unverified recovery or data preservation after a failure', () => {
    expect(copyForFailure('pull_interrupted').hint).not.toContain('desde donde se quedó')
    expect(copyForFailure('companion_migration_failed').hint).not.toContain('intactos')
    expect(copyForFailure('unknown' as FailureCode, false).hint).not.toMatch(/reintentar|vuelve a intentarlo/i)
  })

  it('degrades to an honest generic message for a code outside the closed contract enum', () => {
    // The CLI is a separate process across a version boundary — a future or
    // malformed FailureCode must never crash the one failure screen (FR-033).
    const unknown = 'some_future_code' as FailureCode
    expect(copyForFailure(unknown)).toEqual({
      headline: 'Algo detuvo la preparación.',
      hint: 'Vuelve a intentarlo. Si el problema sigue, exporta el diagnóstico.',
    })
  })
})
