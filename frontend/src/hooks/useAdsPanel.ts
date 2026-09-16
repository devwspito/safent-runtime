/**
 * useAdsPanel — origin-derivation helper for a self-hosted safent-ads
 * managed-remote MCP URL (McpView's "Presets gestionados" card, the FR-6
 * fallback when the preinstalled companion is skipped, `--no-companion`).
 * The ads service serves its own React panel at `/` on the SAME host as
 * the MCP bridge (`/mcp`), so the panel origin is just the MCP URL's
 * origin — never built from free text, always parsed out of a URL the
 * backend already validated (https-only, no IP literal, port 443; see
 * hermes.shell_server.managed_remote_endpoints).
 *
 * The sidebar/AdsView's OWN companion path (026, contracts/sso.md) uses
 * `useAdsAvailability` + the same-origin `/ads/*` proxy instead — this
 * module only serves McpView's external "open panel" link for the
 * self-hosted fallback case.
 */

/** Returns the https origin of *mcpUrl*, or null if it isn't a valid https URL. */
export function panelOriginFromMcpUrl(mcpUrl: string): string | null {
  try {
    const parsed = new URL(mcpUrl)
    return parsed.protocol === 'https:' ? parsed.origin : null
  } catch {
    return null
  }
}
