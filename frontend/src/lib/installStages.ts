/**
 * Shared stage vocabulary for the two flows that ride the install-request
 * contract (contracts/install-request.md, contracts/app-engine.md §3): the
 * Ads companion install/repair action and the system update footer. Pure
 * mapping + formatting — no state, no I/O — so both consumers render the
 * exact same honest, named-stage copy instead of two drifting variants.
 *
 * `InstallRequestStatus.stage` echoes the engine's StageId; this frontend is
 * the one that owns turning that id into a sentence in the owner's language
 * (app-engine.md only promises the id, not a label, over this endpoint).
 */
import type { TranslationKey } from './i18n'
import type { InstallRequestProgress } from '../api/types'

const STAGE_KEY: Record<string, TranslationKey> = {
  preflight: 'install.stage.preflight',
  runtime_staging: 'install.stage.runtime_staging',
  machine: 'install.stage.machine',
  pull_engine: 'install.stage.pull_engine',
  pull_companion: 'install.stage.pull_companion',
  container: 'install.stage.container',
  health: 'install.stage.health',
  companion_scaffold: 'install.stage.companion_scaffold',
  companion_up: 'install.stage.companion_up',
  companion_reload: 'install.stage.companion_reload',
  backup: 'install.stage.backup',
  restore: 'install.stage.restore',
  cleanup: 'install.stage.cleanup',
}

/** Translation key for a stage id, or the generic "working on it" fallback for an unknown/absent one. */
export function stageLabelKey(stage: string | undefined): TranslationKey {
  return (stage && STAGE_KEY[stage]) || 'install.stage.generic'
}

const UNIT_DIVISOR: Record<'bytes', number> = { bytes: 1024 }

function formatBytes(n: number): string {
  const units = ['B', 'KB', 'MB', 'GB']
  let value = n
  let i = 0
  while (value >= UNIT_DIVISOR.bytes && i < units.length - 1) {
    value /= UNIT_DIVISOR.bytes
    i++
  }
  const rounded = i === 0 ? String(Math.round(value)) : value.toFixed(1)
  return `${rounded} ${units[i]}`
}

/** "42 MB / 120 MB" | "3/12" | "180 MB" | "" (nothing worth showing yet). */
export function formatProgress(progress: InstallRequestProgress | undefined): string {
  if (!progress) return ''
  const { done, total, unit } = progress
  const doneText = unit === 'bytes' ? formatBytes(done) : String(done)
  if (total == null) return doneText
  const totalText = unit === 'bytes' ? formatBytes(total) : String(total)
  return `${doneText} / ${totalText}`
}
