/**
 * SwarmView — the live agent "brain/swarm".
 *
 * An organic, force-directed graph (react-force-graph-2d, Canvas — no Three.js):
 * a central Cerebro (orchestrator) with department clusters of agents around it.
 * Everything drawn here is TRUTHFUL:
 *   • node "working" state ← runtimeStatus.activity[] (a real in-flight tool),
 *   • a bright spark Cerebro→specialist ← runtimeStatus.delegations[] (a real
 *     `delegate_task` event the backend now emits, kept for a short TTL),
 *   • ruflo_active → the swarm ring lights up.
 * Idle nodes just breathe. No fabricated traffic.
 *
 * The graph topology (brain → department → agents) comes from the roster, reusing
 * the exact same grouping/activity helpers as the other Agentes modes.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D, { type ForceGraphMethods, type NodeObject, type LinkObject } from 'react-force-graph-2d'
import { Users } from 'lucide-react'

import type { AgentRoster, AgentStatsResponse, RosterAgent, RuntimeStatus } from '../api/types'
import { activeAgentIds, groupDepartmentsByKind } from '../lib/agentRoster'
import { useT } from '../lib/i18n'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import styles from './SwarmView.module.css'

// ── Graph model ────────────────────────────────────────────────────────────────

type NodeKind = 'brain' | 'dept' | 'agent'

interface SwarmNode extends NodeObject {
  id: string
  kind: NodeKind
  label: string
  color: string
  agentId?: string
  deptId?: string
}

interface SwarmLink extends LinkObject {
  source: string | SwarmNode
  target: string | SwarmNode
  /** id of the agent this link ultimately feeds (dept→agent, or brain→dept-of-agent). */
  agentEndpoint?: string
}

interface GraphData {
  nodes: SwarmNode[]
  links: SwarmLink[]
  brainId: string
  /** agentId → its department node id, for routing delegation sparks. */
  deptOfAgent: Map<string, string>
}

const SYNTHETIC_BRAIN = '__brain__'

/** Build the static graph topology from the roster (memoised on roster identity). */
function buildGraph(roster: AgentRoster, brainLabel: string): GraphData {
  const { cerebroDepts, customDepts, factoryDepts } = groupDepartmentsByKind(roster.departments)
  const nodes: SwarmNode[] = []
  const links: SwarmLink[] = []
  const deptOfAgent = new Map<string, string>()

  const brainAgent = cerebroDepts[0]?.agents[0]
  const brainId = brainAgent?.id ?? SYNTHETIC_BRAIN
  nodes.push({
    id: brainId,
    kind: 'brain',
    label: brainAgent?.name ?? brainLabel,
    color: brainAgent?.color ?? 'var(--color-accent)',
    agentId: brainAgent?.id,
  })

  // Every department except the brain's own single-agent cerebro cluster.
  const allDepts = [...cerebroDepts, ...customDepts, ...factoryDepts]
  for (const dept of allDepts) {
    const agents = dept.agents.filter((a) => a.id !== brainId)
    if (agents.length === 0) continue // e.g. the cerebro dept once the CEO is lifted to the center
    const deptNodeId = `dept:${dept.id}`
    nodes.push({ id: deptNodeId, kind: 'dept', label: dept.name, color: 'var(--color-text-muted)', deptId: dept.id })
    links.push({ source: brainId, target: deptNodeId })
    for (const a of agents) {
      nodes.push({
        id: a.id,
        kind: 'agent',
        label: a.name,
        color: a.color ?? 'var(--color-accent)',
        agentId: a.id,
        deptId: dept.id,
      })
      links.push({ source: deptNodeId, target: a.id, agentEndpoint: a.id })
      deptOfAgent.set(a.id, deptNodeId)
    }
  }

  return { nodes, links, brainId, deptOfAgent }
}

// ── Canvas palette (CSS vars aren't usable inside a <canvas>) ────────────────────

interface Palette {
  accent: string
  working: string
  spark: string
  ink: string
  muted: string
  line: string
  surface: string
  bg: string
}

function readVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function readPalette(): Palette {
  return {
    accent: readVar('--color-accent', '#6aa6ff'),
    working: readVar('--color-warning', '#f5a623'),
    spark: readVar('--color-accent', '#6aa6ff'),
    ink: readVar('--color-text', '#e8ecf3'),
    muted: readVar('--color-text-muted', '#8b93a3'),
    line: readVar('--color-border', '#2a2f3a'),
    surface: readVar('--color-surface', '#161a22'),
    bg: readVar('--color-bg', '#0d1016'),
  }
}

/** Resolve a possibly-CSS-var color string to a concrete canvas color. */
function resolveColor(c: string, pal: Palette): string {
  if (c.startsWith('var(')) return pal.accent
  return c
}

// ── Component ────────────────────────────────────────────────────────────────

export interface SwarmViewProps {
  roster: AgentRoster
  runtimeStatus: RuntimeStatus
  agentStats: AgentStatsResponse
  hasRuflo: boolean
  onAgentClick: (agent: RosterAgent) => void
}

export function SwarmView({ roster, runtimeStatus, hasRuflo, onAgentClick }: SwarmViewProps) {
  const t = useT()
  const fgRef = useRef<ForceGraphMethods<SwarmNode, SwarmLink> | undefined>(undefined)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const palRef = useRef<Palette>(readPalette())

  const graph = useMemo(() => buildGraph(roster, t('swarm.center')), [roster, t])

  // Fast lookup of the RosterAgent behind an id (for the click handler).
  const agentById = useMemo(() => {
    const m = new Map<string, RosterAgent>()
    for (const d of roster.departments) for (const a of d.agents) m.set(a.id, a)
    return m
  }, [roster])

  const activeIds = useMemo(() => activeAgentIds(runtimeStatus), [runtimeStatus])
  // A dept "hums" when any of its agents is working.
  const activeDeptNodes = useMemo(() => {
    const s = new Set<string>()
    for (const [agentId, deptNode] of graph.deptOfAgent) if (activeIds.has(agentId)) s.add(deptNode)
    return s
  }, [graph, activeIds])

  // Refresh the palette when the locale/theme container repaints (cheap, on mount + roster change).
  useEffect(() => { palRef.current = readPalette() }, [roster])

  // Measure the container so the canvas fills it and stays responsive.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect
      if (r) setSize({ w: Math.max(0, Math.floor(r.width)), h: Math.max(0, Math.floor(r.height)) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Gentle forces + pin the brain at the centre so the swarm orbits it.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(-140)
    fg.d3Force('link')?.distance((l: SwarmLink) => ((typeof l.source === 'object' && l.source.kind === 'brain') ? 90 : 46))
    const brain = graph.nodes.find((n) => n.kind === 'brain')
    if (brain) { brain.fx = 0; brain.fy = 0 }
    fg.d3ReheatSimulation()
  }, [graph, size.w, size.h])

  // Frame the whole swarm once it has laid out (cooldownTime=Infinity means the
  // engine never "stops", so we fit on a short timer instead of onEngineStop).
  useEffect(() => {
    const id = setTimeout(() => fgRef.current?.zoomToFit(500, 55), 1500)
    return () => clearTimeout(id)
  }, [graph, size.w, size.h])

  // Fire a real spark Cerebro→specialist for each NEW delegation event.
  const seenDeleg = useRef<Set<string>>(new Set())
  useEffect(() => {
    const fg = fgRef.current
    const dels = runtimeStatus.delegations ?? []
    if (!fg || dels.length === 0) return
    const linkId = (l: SwarmLink) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source
      const tg = typeof l.target === 'object' ? l.target.id : l.target
      return `${s}→${tg}`
    }
    for (const d of dels) {
      const key = `${d.task_id ?? ''}|${d.to}|${d.since ?? ''}`
      if (seenDeleg.current.has(key)) continue
      seenDeleg.current.add(key)
      const deptNode = graph.deptOfAgent.get(d.to)
      if (!deptNode) continue
      const path = new Set([`${graph.brainId}→${deptNode}`, `${deptNode}→${d.to}`])
      for (const l of graph.links) if (path.has(linkId(l))) fg.emitParticle(l)
    }
    // Bound the memory of seen keys.
    if (seenDeleg.current.size > 256) seenDeleg.current = new Set(Array.from(seenDeleg.current).slice(-128))
  }, [runtimeStatus.delegations, graph])

  const hasAgents = graph.nodes.some((n) => n.kind === 'agent')

  return (
    <div className={styles.root}>
      <div className={styles.canvasWrap} ref={wrapRef}>
        {!hasAgents ? (
          <div className={styles.emptyCenter}>
            <EmptyState compact icon={<Users size={20} aria-hidden="true" />} title={t('swarm.empty.title')} />
          </div>
        ) : size.w > 0 && size.h > 0 ? (
          <ForceGraph2D<SwarmNode, SwarmLink>
            ref={fgRef}
            graphData={graph}
            width={size.w}
            height={size.h}
            backgroundColor="rgba(0,0,0,0)"
            /* Never freeze the engine: keeps the canvas repainting so the halos
               breathe/pulse continuously. Node positions still settle (alpha decays),
               so it stays readable — only the halo radius oscillates. */
            cooldownTime={Infinity}
            warmupTicks={40}
            d3AlphaDecay={0.045}
            d3VelocityDecay={0.32}
            enableNodeDrag={false}
            nodeRelSize={1}
            nodeLabel={(n) => (n as SwarmNode).kind === 'agent' ? (n as SwarmNode).label : ''}
            onNodeClick={(n) => {
              const node = n as SwarmNode
              if (node.kind !== 'agent' || !node.agentId) return
              const a = agentById.get(node.agentId)
              if (a) onAgentClick(a)
            }}
            linkColor={(l) => {
              const link = l as SwarmLink
              const active = link.agentEndpoint ? activeIds.has(link.agentEndpoint) : activeDeptNodes.has(typeof link.target === 'object' ? link.target.id : String(link.target))
              return active ? palRef.current.accent : palRef.current.line
            }}
            linkWidth={(l) => {
              const link = l as SwarmLink
              const active = link.agentEndpoint ? activeIds.has(link.agentEndpoint) : activeDeptNodes.has(typeof link.target === 'object' ? link.target.id : String(link.target))
              return active ? 1.6 : 0.6
            }}
            linkDirectionalParticles={(l) => {
              const link = l as SwarmLink
              const active = link.agentEndpoint ? activeIds.has(link.agentEndpoint) : activeDeptNodes.has(typeof link.target === 'object' ? link.target.id : String(link.target))
              return active ? 2 : 0
            }}
            linkDirectionalParticleWidth={2.2}
            linkDirectionalParticleSpeed={0.012}
            linkDirectionalParticleColor={() => palRef.current.spark}
            nodeCanvasObject={(node, ctx, scale) => {
              const n = node as SwarmNode
              const pal = palRef.current
              const x = n.x ?? 0
              const y = n.y ?? 0
              const working = n.kind === 'agent' ? activeIds.has(n.id) : n.kind === 'brain' ? (n.agentId ? activeIds.has(n.agentId) : false) || !!runtimeStatus.ruflo_active : activeDeptNodes.has(n.id)
              const base = n.kind === 'brain' ? 9 : n.kind === 'dept' ? 5 : 4
              // Collective slow breathing + a faster pulse on working nodes.
              const now = typeof performance !== 'undefined' ? performance.now() : 0
              const breathe = 1 + 0.05 * Math.sin(now / 1400 + x * 0.05)
              const pulse = working ? 1 + 0.18 * (0.5 + 0.5 * Math.sin(now / 320)) : 1
              const r = base * breathe * pulse
              const fill = resolveColor(n.color, pal)

              // Halo for working / brain nodes.
              if (working || n.kind === 'brain') {
                const halo = working ? (n.kind === 'agent' ? pal.working : pal.accent) : pal.accent
                ctx.beginPath()
                ctx.arc(x, y, r + (working ? 5 : 3), 0, 2 * Math.PI)
                ctx.fillStyle = halo
                ctx.globalAlpha = working ? 0.16 + 0.08 * Math.sin(now / 320) : 0.10
                ctx.fill()
                ctx.globalAlpha = 1
              }

              // Node body.
              ctx.beginPath()
              ctx.arc(x, y, r, 0, 2 * Math.PI)
              ctx.fillStyle = n.kind === 'dept' ? pal.surface : fill
              ctx.fill()
              ctx.lineWidth = working ? 1.6 / scale : 0.8 / scale
              ctx.strokeStyle = working ? (n.kind === 'agent' ? pal.working : pal.accent) : pal.line
              ctx.stroke()

              // Brain / agent glyph.
              if (n.kind !== 'dept') {
                const initial = n.label.charAt(0).toUpperCase()
                ctx.fillStyle = '#fff'
                ctx.font = `600 ${(n.kind === 'brain' ? 8 : 4.5)}px system-ui, sans-serif`
                ctx.textAlign = 'center'
                ctx.textBaseline = 'middle'
                ctx.fillText(initial, x, y)
              }

              // Labels (dept always; agent only when zoomed in enough to read).
              const showLabel = n.kind === 'brain' || n.kind === 'dept' || scale > 1.6
              if (showLabel) {
                ctx.fillStyle = n.kind === 'agent' && !working ? pal.muted : pal.ink
                ctx.font = `${n.kind === 'brain' ? 5 : 4}px system-ui, sans-serif`
                ctx.textAlign = 'center'
                ctx.textBaseline = 'top'
                ctx.fillText(n.label, x, y + r + 2)
              }
            }}
          />
        ) : null}

        {/* Recenter affordance */}
        {hasAgents && (
          <div className={styles.controls}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => fgRef.current?.zoomToFit(400, 40)}
            >
              {t('swarm.recenter')}
            </Button>
          </div>
        )}
      </div>

      {/* Legend + one-line hint that reads on its own. */}
      <div className={styles.footer}>
        <p className={styles.hint}>{t('swarm.hint')}</p>
        <ul className={styles.legend} role="list">
          <li><span className={`${styles.dot} ${styles.dotWorking}`} aria-hidden="true" />{t('swarm.legend.working')}</li>
          <li><span className={`${styles.dot} ${styles.dotIdle}`} aria-hidden="true" />{t('swarm.legend.idle')}</li>
          <li><span className={`${styles.dot} ${styles.dotSpark}`} aria-hidden="true" />{t('swarm.legend.delegating')}</li>
          {hasRuflo && runtimeStatus.ruflo_active && (
            <li className={styles.ruflo}><span className={`${styles.dot} ${styles.dotSpark}`} aria-hidden="true" />ruflo</li>
          )}
        </ul>
      </div>
    </div>
  )
}

export default SwarmView
