import type { Skill } from '../api/types'

/**
 * "Live" = the skill drives the browser (taught from a live demonstration, or it
 * has a browser surface) → you can watch it work in real time when it runs.
 * Single source of truth for the "en vivo" tag everywhere (Habilidades view,
 * chat "+" menu, context panel).
 */
export function isLiveSkill(sk: Skill): boolean {
  const surfaces = Array.isArray(sk.surface_kinds)
    ? sk.surface_kinds
    : (sk.surface_kinds ? [sk.surface_kinds] : [])
  return sk.teaching_origin === 'teaching_live'
    || surfaces.some((k) => String(k).toLowerCase().includes('browser'))
}
