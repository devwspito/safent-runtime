/**
 * icons.js — Inline SVG icon library for Lumen.
 * Returns SVG strings. No external deps, no font icons.
 * All icons are 20×20 by default (override via CSS width/height).
 */

export const Icon = {
  /** Navigation & layout */
  sidebarCollapse: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2" y="5" width="7" height="1.5" rx=".75" fill="currentColor"/><rect x="2" y="9.25" width="7" height="1.5" rx=".75" fill="currentColor"/><rect x="2" y="13.5" width="7" height="1.5" rx=".75" fill="currentColor"/><rect x="11" y="3" width="7" height="14" rx="2" stroke="currentColor" stroke-width="1.5"/></svg>`,
  sidebarExpand: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2" y="3" width="7" height="14" rx="2" stroke="currentColor" stroke-width="1.5"/><rect x="11" y="5" width="7" height="1.5" rx=".75" fill="currentColor"/><rect x="11" y="9.25" width="7" height="1.5" rx=".75" fill="currentColor"/><rect x="11" y="13.5" width="7" height="1.5" rx=".75" fill="currentColor"/></svg>`,
  rightPanel: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2" y="3" width="16" height="14" rx="2" stroke="currentColor" stroke-width="1.5"/><line x1="13" y1="4" x2="13" y2="16" stroke="currentColor" stroke-width="1.5"/></svg>`,
  search: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5" stroke="currentColor" stroke-width="1.5"/><path d="m12.5 12.5 4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  chevronDown: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevronRight: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m6 4 4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  filter: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 4h12M4 8h8M6 12h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,

  /** Actions */
  plus: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 4v12M4 10h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  stop: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor"/></svg>`,
  send: `<svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="M2 9h14M9 2l7 7-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  attach: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M16 10l-6 6a4 4 0 0 1-5.657-5.657l6.364-6.364a2.5 2.5 0 1 1 3.536 3.536L7.88 13.88a1 1 0 1 1-1.415-1.414L13 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  trash: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 4h10M6 4V3h4v1M5 4v8a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1V4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  edit: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M11 3l2 2-7 7-3 1 1-3 7-7Z" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  check: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3 8 4 4 6-7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  download: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 2v8M5 7l3 3 3-3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 13h12" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  externalLink: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M6 2H2v10h10V8M8 2h4v4M7 7l5-5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  copy: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="5" y="5" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.2"/><path d="M3 11V3h8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

  /** Status */
  statusDone: `<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="5" fill="var(--ok)"/></svg>`,
  statusIdle: `<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="4" stroke="var(--ink3)" stroke-width="1.5" fill="none"/></svg>`,
  statusWarn: `<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 L11 10.5 H1 Z" fill="var(--warn)" stroke="none"/><text x="6" y="9.5" text-anchor="middle" font-size="7" fill="#000" font-weight="700">!</text></svg>`,
  statusRunning: `<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="4" stroke="var(--accent)" stroke-width="1.5" fill="none" stroke-dasharray="20" stroke-dashoffset="0"><animateTransform attributeName="transform" type="rotate" from="0 5 5" to="360 5 5" dur="1s" repeatCount="indefinite"/></circle></svg>`,
  statusError: `<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="5" fill="var(--danger)"/></svg>`,

  /** Capability nav icons */
  tasks: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="3" width="14" height="14" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M7 10l2 2 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  agents: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="8" r="3.5" stroke="currentColor" stroke-width="1.5"/><path d="M3 18c0-3.314 3.134-6 7-6s7 2.686 7 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="10" cy="8" r="1" fill="currentColor"/></svg>`,
  office: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="3" width="9" height="14" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M12 8h5v9H3" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M6 6.5h3M6 9.5h3M6 12.5h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  skills: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 2l2.2 4.5L17 7.4l-3.5 3.4.8 4.7L10 13.4l-4.3 2.1.8-4.7L3 7.4l4.8-.9L10 2Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
  integrations: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="5" cy="10" r="2.5" stroke="currentColor" stroke-width="1.5"/><circle cx="15" cy="5" r="2.5" stroke="currentColor" stroke-width="1.5"/><circle cx="15" cy="15" r="2.5" stroke="currentColor" stroke-width="1.5"/><path d="M7.5 10h2.5M12.5 5H10l-2.5 5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M12.5 15H10" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  mcp: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="7" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5"/><rect x="11" y="7" width="6" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M9 10h2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  providers: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="7" cy="5" r="2" fill="currentColor"/><circle cx="13" cy="10" r="2" fill="currentColor"/><circle cx="7" cy="15" r="2" fill="currentColor"/></svg>`,
  security: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M10 2L4 5v5c0 3.866 2.686 7.49 6 8.5 3.314-1.01 6-4.634 6-8.5V5L10 2Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="m7.5 10 2 2 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  memory: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="3" width="14" height="4" rx="1" stroke="currentColor" stroke-width="1.5"/><rect x="3" y="9" width="14" height="4" rx="1" stroke="currentColor" stroke-width="1.5"/><circle cx="6" cy="5" r="1" fill="currentColor"/><circle cx="6" cy="11" r="1" fill="currentColor"/></svg>`,

  /** Domain icons */
  projects: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2" y="5" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M7 5V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v1" stroke="currentColor" stroke-width="1.5"/></svg>`,
  artifacts: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4 4h8l4 4v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.5"/><path d="M12 4v4h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  clock: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="1.5"/><path d="M10 6v4l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  despacho: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="3" y="6" width="14" height="10" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M7 6V5a3 3 0 0 1 6 0v1" stroke="currentColor" stroke-width="1.5"/><circle cx="10" cy="11" r="1.5" fill="currentColor"/></svg>`,
  customize: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="3" stroke="currentColor" stroke-width="1.5"/><path d="M10 2v2M10 16v2M2 10h2M16 10h2M4.22 4.22l1.42 1.42M14.36 14.36l1.42 1.42M4.22 15.78l1.42-1.42M14.36 5.64l1.42-1.42" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  leaf: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 13c1-4 3-7 9-8C12 9 10 13 3 13Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M3 13 8 8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>`,
  globe: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="8" stroke="currentColor" stroke-width="1.5"/><ellipse cx="10" cy="10" rx="3.5" ry="8" stroke="currentColor" stroke-width="1.5"/><path d="M2 10h16" stroke="currentColor" stroke-width="1.5"/></svg>`,
  browser: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2.5" y="3.5" width="15" height="13" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M2.5 7h15" stroke="currentColor" stroke-width="1.5"/><circle cx="5" cy="5.25" r=".6" fill="currentColor"/><circle cx="7" cy="5.25" r=".6" fill="currentColor"/></svg>`,
  folder: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M2 7h16v9a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V7Z" stroke="currentColor" stroke-width="1.5"/><path d="M2 7V5a1 1 0 0 1 1-1h4l2 2h8" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
  skill: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 1l1.8 3.6L14 5.5l-3 2.9.7 4.1L8 10.3 4.3 12.5l.7-4.1L2 5.5l4.2-.9L8 1Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>`,
  progress: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.5" stroke-dasharray="44" stroke-dashoffset="11"/><path d="m7 10 2 2 4-4" stroke="var(--ok)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

  /** File type icons (small, 16px) */
  fileXls: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#1D6F42" opacity=".15" stroke="#1D6F42" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="6" fill="#1D6F42" font-weight="700">XLS</text></svg>`,
  fileDoc: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#1755D1" opacity=".15" stroke="#1755D1" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="6" fill="#1755D1" font-weight="700">DOC</text></svg>`,
  filePdf: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#D62B20" opacity=".15" stroke="#D62B20" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="6" fill="#D62B20" font-weight="700">PDF</text></svg>`,
  fileJs: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#F7DF1E" opacity=".2" stroke="#C4A800" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="7" fill="#8A7200" font-weight="700">JS</text></svg>`,
  filePy: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#3572A5" opacity=".15" stroke="#3572A5" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="7" fill="#3572A5" font-weight="700">PY</text></svg>`,
  filePng: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#8B44AC" opacity=".15" stroke="#8B44AC" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="5" fill="#8B44AC" font-weight="700">PNG</text></svg>`,
  fileJson: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="1.5" fill="#E8640C" opacity=".15" stroke="#E8640C" stroke-width="1.2"/><text x="8" y="11" text-anchor="middle" font-size="5" fill="#E8640C" font-weight="700">JSON</text></svg>`,
  fileGeneric: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M3 2h7l4 4v8a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.2"/><path d="M10 2v4h4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>`,

  /** Tool call / step connector */
  toolConnector: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 2v12" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-dasharray="2 2"/><circle cx="8" cy="8" r="2.5" fill="currentColor" opacity=".4"/></svg>`,
  thinking: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="4" cy="8" r="1.5" fill="currentColor"><animate attributeName="opacity" values="0.3;1;0.3" dur="1.2s" begin="0s" repeatCount="indefinite"/></circle><circle cx="8" cy="8" r="1.5" fill="currentColor"><animate attributeName="opacity" values="0.3;1;0.3" dur="1.2s" begin="0.2s" repeatCount="indefinite"/></circle><circle cx="12" cy="8" r="1.5" fill="currentColor"><animate attributeName="opacity" values="0.3;1;0.3" dur="1.2s" begin="0.4s" repeatCount="indefinite"/></circle></svg>`,
  thinkingDone: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><circle cx="7" cy="7" r="6" stroke="var(--ok)" stroke-width="1.3"/><path d="m4.5 7 2 2 3-3" stroke="var(--ok)" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

  /** Spinner */
  spinner: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true" class="spinner"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="2" stroke-dasharray="44" stroke-dashoffset="11" opacity=".3"/><path d="M10 3a7 7 0 0 1 7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,

  /** User / agent */
  user: `<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="7" r="3.5" stroke="currentColor" stroke-width="1.5"/><path d="M3 18c0-3.866 3.134-7 7-7s7 3.134 7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,

  /** Close */
  close: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 4 8 8M12 4 4 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  info: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.3"/><path d="M8 7v5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><circle cx="8" cy="5" r=".8" fill="currentColor"/></svg>`,

  /** Checklist items */
  checkboxEmpty: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2.75" y="2.75" width="10.5" height="10.5" rx="2.25" stroke="currentColor" stroke-width="1.5"/></svg>`,
  checkboxDone: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="2.5" fill="var(--ok)"/><path d="m4.5 8 3 3 4-5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  checkboxRunning: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="2.5" fill="color-mix(in srgb, var(--accent) 15%, transparent)" stroke="var(--accent)" stroke-width="1.5"/><circle cx="8" cy="8" r="2.5" fill="var(--accent)"><animate attributeName="opacity" values="0.5;1;0.5" dur="1s" repeatCount="indefinite"/></circle></svg>`,

  /** Advanced sub-section toggle */
  advancedExpand: `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="m3 5 4 4 4-4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

  /** Tool-call icons (Lucide) — keyed by backend tool name in chat.js TOOL_ICON. */
  toolNavigate: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>`,
  toolClick: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 9l5 12 1.8-5.2L21 14z"/><path d="M7.2 2.2 8 5.1"/><path d="m5.1 8-2.9-.8"/><path d="M14 4.1 12 6"/><path d="m6 12-1.9 2"/></svg>`,
  toolType: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="M6 8h.01"/><path d="M10 8h.01"/><path d="M14 8h.01"/><path d="M18 8h.01"/><path d="M8 12h.01"/><path d="M12 12h.01"/><path d="M16 12h.01"/><path d="M7 16h10"/></svg>`,
  toolCamera: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>`,
  toolBack: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>`,
  toolSearch: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`,
  toolLink: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`,
  toolFileSearch: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h7"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><circle cx="11.5" cy="14.5" r="2.5"/><path d="M13.3 16.3 15 18"/></svg>`,
  toolFolder: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`,
  toolFileText: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>`,
  toolFilePen: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.5 22H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8.5L18 5.5V12"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M21.4 15.6a2 2 0 0 1 0 2.8L17 22l-3 1 1-3 4.4-4.4a2 2 0 0 1 2.8 0z"/></svg>`,
  toolFilePlus: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/><path d="M12 11v6"/><path d="M9 14h6"/></svg>`,
  toolPatch: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="9" x="3" y="7.5" rx="4.5" transform="rotate(45 12 12)"/><path d="M12 12h.01"/></svg>`,
  toolTrash: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
  toolTerminal: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>`,
  toolCode: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5a2 2 0 0 0 2 2h1"/><path d="M16 3h1a2 2 0 0 1 2 2v5a2 2 0 0 1 2 2 2 2 0 0 1-2 2v5a2 2 0 0 1-2 2h-1"/></svg>`,
  toolMonitor: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg>`,
  toolApp: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 4v4"/><path d="M2 8h20"/><path d="M6 4v4"/></svg>`,
  toolBrain: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5a3 3 0 1 0-5.997.142 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.142 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/></svg>`,
  toolHelp: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>`,
  toolWrench: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`,

  /** Theme toggle + assorted (Lucide) */
  sun: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>`,
  moon: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>`,
  arrowRight: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>`,
  arrowUp: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>`,
  arrowDown: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>`,
  arrowLeftRight: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3 4 7l4 4"/><path d="M4 7h16"/><path d="m16 21 4-4-4-4"/><path d="M20 17H4"/></svg>`,
  command: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/></svg>`,
};

/**
 * Renders an icon into a container element by innerHTML.
 */
export function renderIcon(name, extraClass = '') {
  const svg = Icon[name] ?? Icon.fileGeneric;
  if (!extraClass) return svg;
  return svg.replace('<svg ', `<svg class="${extraClass}" `);
}

/**
 * Returns the file-type icon key for a given filename.
 */
export function fileIcon(filename = '') {
  const ext = filename.split('.').pop()?.toLowerCase();
  const map = {
    xls: 'fileXls', xlsx: 'fileXls',
    doc: 'fileDoc', docx: 'fileDoc',
    pdf: 'filePdf',
    js: 'fileJs', ts: 'fileJs',
    py: 'filePy',
    png: 'filePng', jpg: 'filePng', jpeg: 'filePng', gif: 'filePng', webp: 'filePng',
    json: 'fileJson',
  };
  return map[ext] ?? 'fileGeneric';
}
