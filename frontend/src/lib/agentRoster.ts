/**
 * Shared helpers over the agent roster + live runtime status.
 * Used by every "Agentes" view mode (Tarjetas, En vivo, Premium) so they
 * read the exact same grouping/activity rules from a single place.
 */
import type { RosterAgent, RosterDepartment, RuntimeStatus } from '../api/types'

export interface GroupedDepartments {
  cerebroDepts: RosterDepartment[]
  customDepts: RosterDepartment[]
  factoryDepts: RosterDepartment[]
  hasCustomDepts: boolean
}

/** Splits the roster's departments into the three sections the Agentes view renders. */
export function groupDepartmentsByKind(departments: RosterDepartment[]): GroupedDepartments {
  const cerebroDepts = departments.filter((d) => d.kind === 'cerebro')
  const customDepts = departments.filter((d) => d.kind === 'custom')
  const factoryDepts = departments.filter((d) => d.kind === 'factory')
  return { cerebroDepts, customDepts, factoryDepts, hasCustomDepts: customDepts.length > 0 }
}

/** IDs of agents currently running a task, derived from the live runtime status. */
export function activeAgentIds(runtimeStatus: RuntimeStatus): Set<string> {
  const ids = new Set<string>()
  if (runtimeStatus.active_agent_id) ids.add(runtimeStatus.active_agent_id)
  for (const a of runtimeStatus.activity ?? []) ids.add(a.agent_id)
  return ids
}

/** Strips the legacy `custom:` prefix some department slugs carry. */
export function departmentDisplayLabel(agent: RosterAgent): string {
  return agent.department ? agent.department.replace(/^custom:/i, '') : ''
}
