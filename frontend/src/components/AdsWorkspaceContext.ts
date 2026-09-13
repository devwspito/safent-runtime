import { createContext } from 'react'

/** Presentation only: the local iframe owns navigation while mounted.
 * Managed Ads and unavailable states never claim the workspace. */
export const AdsWorkspaceContext = createContext<{
  setPanelActive(active: boolean): void
  returnToSafent(): void
} | null>(null)
