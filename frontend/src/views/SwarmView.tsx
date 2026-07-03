/**
 * SwarmView — the live agent "brain/swarm", 3D edition (Enterprise candidate).
 *
 * react-force-graph-3d + UnrealBloomPass: emissive spheres with REAL bloom, a
 * deterministic starfield, cinematic camera (soft auto-orbit at rest, fly-to on
 * click) and a live activity feed beside the canvas.
 *
 * Everything drawn/listed is TRUTHFUL:
 *   • node "working" state ← runtimeStatus.activity[] (a real in-flight tool),
 *   • sparks + feed entries ← runtimeStatus.delegations[] (real delegate_task
 *     events, short TTL) and activity[] transitions,
 *   • ruflo_active → the brain lights up.
 * Idle nodes just breathe. No fabricated traffic.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph3D, { type ForceGraphMethods, type NodeObject, type LinkObject } from 'react-force-graph-3d'
import * as THREE from 'three'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
// d3-force-3d ships with the force-graph stack (no types published); collide
// keeps nodes+labels apart.
// eslint-disable-next-line import/no-extraneous-dependencies
// @ts-expect-error — d3-force-3d has no type declarations
import { forceCollide } from 'd3-force-3d'
import SpriteText from 'three-spritetext'
import { Users } from 'lucide-react'

import type { AgentRoster, AgentStatsResponse, RosterAgent, RuntimeStatus } from '../api/types'
import { activeAgentIds, groupDepartmentsByKind } from '../lib/agentRoster'
import { useT } from '../lib/i18n'
import { toolLabel } from '../lib/toolLabels'
import { EmptyState } from '../components/ui/EmptyState'
import { Button } from '../components/ui/Button'
import styles from './SwarmView.module.css'

// ── Graph model (unchanged from the 2D swarm — same truth, new renderer) ──────

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
  deptOfAgent: Map<string, string>
}

const SYNTHETIC_BRAIN = '__brain__'

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
    color: brainAgent?.color ?? '#6aa6ff',
    agentId: brainAgent?.id,
  })

  const allDepts = [...cerebroDepts, ...customDepts, ...factoryDepts]
  for (const dept of allDepts) {
    const agents = dept.agents.filter((a) => a.id !== brainId)
    if (agents.length === 0) continue
    const deptNodeId = `dept:${dept.id}`
    nodes.push({ id: deptNodeId, kind: 'dept', label: dept.name, color: '#8b93a3', deptId: dept.id })
    links.push({ source: brainId, target: deptNodeId })
    for (const a of agents) {
      nodes.push({
        id: a.id,
        kind: 'agent',
        label: a.name,
        color: a.color ?? '#6aa6ff',
        agentId: a.id,
        deptId: dept.id,
      })
      links.push({ source: deptNodeId, target: a.id, agentEndpoint: a.id })
      deptOfAgent.set(a.id, deptNodeId)
    }
  }
  return { nodes, links, brainId, deptOfAgent }
}

// ── Palette from CSS tokens (three.js needs concrete colors) ──────────────────

interface Palette { accent: string; working: string; ink: string; muted: string }

function readVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function readPalette(): Palette {
  return {
    accent: readVar('--color-accent', '#6aa6ff'),
    working: readVar('--color-warning', '#f5a623'),
    ink: readVar('--color-text', '#e8ecf3'),
    muted: readVar('--color-text-muted', '#8b93a3'),
  }
}

function resolveColor(c: string, pal: Palette): string {
  return c.startsWith('var(') ? pal.accent : c
}

// ── Per-node three.js objects (kept so the pulse loop can mutate materials) ───

interface NodeObjects {
  kind: NodeKind
  sphere: THREE.Mesh<THREE.SphereGeometry, THREE.MeshPhongMaterial>
  halo: THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>
  label: SpriteText
  baseColor: THREE.Color
}

const NODE_R: Record<NodeKind, number> = { brain: 11, dept: 2.6, agent: 6.2 }

// ── Live feed model ───────────────────────────────────────────────────────────

interface FeedEvent {
  id: string
  ts: number
  type: 'deleg' | 'tool'
  agentId: string
  title: string
  detail?: string
}

const FEED_MAX = 30

// ── Component ────────────────────────────────────────────────────────────────

export interface SwarmViewProps {
  roster: AgentRoster
  runtimeStatus: RuntimeStatus
  agentStats: AgentStatsResponse
  hasRuflo: boolean
  onAgentClick: (agent: RosterAgent) => void
}

export function SwarmView({ roster, runtimeStatus, onAgentClick }: SwarmViewProps) {
  const t = useT()
  const fgRef = useRef<ForceGraphMethods<SwarmNode, SwarmLink> | undefined>(undefined)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const palRef = useRef<Palette>(readPalette())
  const objsRef = useRef<Map<string, NodeObjects>>(new Map())
  const reduced = useMemo(
    () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
    [],
  )

  // Graph rebuilt ONLY on roster CONTENT change (identity churn must never reset
  // node positions — see the 0.8.3 "jumping pile" postmortem).
  const brainLabel = t('swarm.center')
  const rosterKey = useMemo(
    () => roster.departments
      .map((d) => `${d.id}~${d.kind}~${d.agents.map((a) => `${a.id}:${a.name}:${a.color ?? ''}`).join(',')}`)
      .join('|'),
    [roster],
  )
  const rosterRef = useRef(roster)
  rosterRef.current = roster
  // eslint-disable-next-line react-hooks/exhaustive-deps -- rosterKey IS the roster identity
  const graph = useMemo(() => {
    objsRef.current.clear()
    return buildGraph(rosterRef.current, brainLabel)
  }, [rosterKey, brainLabel])

  const agentById = useMemo(() => {
    const m = new Map<string, RosterAgent>()
    for (const d of roster.departments) for (const a of d.agents) m.set(a.id, a)
    return m
  }, [roster])

  const activeIds = useMemo(() => activeAgentIds(runtimeStatus), [runtimeStatus])
  const activeRef = useRef(activeIds)
  activeRef.current = activeIds
  const brainBusy = !!runtimeStatus.ruflo_active || (runtimeStatus.delegations?.length ?? 0) > 0
  const brainBusyRef = useRef(brainBusy)
  brainBusyRef.current = brainBusy

  const activeDeptNodes = useMemo(() => {
    const s = new Set<string>()
    for (const [agentId, deptNode] of graph.deptOfAgent) if (activeIds.has(agentId)) s.add(deptNode)
    return s
  }, [graph, activeIds])

  useEffect(() => { palRef.current = readPalette() }, [rosterKey])

  // Container size → canvas dimensions.
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

  // Forces + pin the brain at the origin. Layout is PLANAR (numDimensions=2):
  // the readability of the 2D constellation, rendered with 3D light and depth.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(-380)
    fg.d3Force('link')?.distance((l: SwarmLink) => ((typeof l.source === 'object' && (l.source as SwarmNode).kind === 'brain') ? 200 : 75))
    // Collision keeps nodes AND their labels from ever overlapping (the owner's
    // screenshot showed mashed labels — this is the structural cure).
    fg.d3Force('collide', forceCollide((n: SwarmNode) => (n.kind === 'brain' ? 30 : n.kind === 'dept' ? 14 : 25)))
    const brain = graph.nodes.find((n) => n.kind === 'brain') as (SwarmNode & { fx?: number; fy?: number; fz?: number }) | undefined
    if (brain) { brain.fx = 0; brain.fy = 0; brain.fz = 0 }
    fg.d3ReheatSimulation()
    // Frame the whole swarm once the layout has mostly settled.
    const id = setTimeout(() => { try { fg.zoomToFit(700, 70) } catch { /* not ready */ } }, 1800)
    return () => clearTimeout(id)
  }, [graph])

  // ── Scene dressing: BLOOM + starfield (once per mount) ─────────────────────
  const dressedRef = useRef(false)
  useEffect(() => {
    const fg = fgRef.current
    if (!fg || dressedRef.current) return
    dressedRef.current = true
    try {
      // REAL glow: bright emissive pixels bleed light. Calibrated for REAL GPUs
      // (the owner's retina Mac blooms much harder than SwiftShader): threshold
      // ABOVE every idle emissive level, so at rest the swarm is calm colored
      // dots and ONLY working nodes (and sparks) truly blaze.
      const bloom = new UnrealBloomPass(new THREE.Vector2(size.w || 800, size.h || 600), 1.0, 0.45, 0.18)
      fg.postProcessingComposer().addPass(bloom)
    } catch { /* WebGL edge cases — the graph still renders without bloom */ }
    try {
      // Deterministic star shell (ambience only — carries no data).
      const N = 420
      const pos = new Float32Array(N * 3)
      for (let i = 0; i < N; i++) {
        const a = (i * 137.508) % 360 / 180 * Math.PI      // golden angle
        const b = ((i * 61) % 180 - 90) / 180 * Math.PI
        const r = 700 + ((i * 97) % 500)
        pos[i * 3] = r * Math.cos(b) * Math.cos(a)
        pos[i * 3 + 1] = r * Math.sin(b) * 0.6
        pos[i * 3 + 2] = r * Math.cos(b) * Math.sin(a)
      }
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
      const mat = new THREE.PointsMaterial({
        color: 0x8899bb, size: 1.5, sizeAttenuation: true,
        transparent: true, opacity: 0.55, blending: THREE.AdditiveBlending, depthWrite: false,
      })
      fg.scene().add(new THREE.Points(geo, mat))
    } catch { /* ambience is optional */ }
  }, [size.w, size.h])

  // ── Cinematic motion: the GALAXY spins slowly in its own plane (labels are
  // billboards, so they stay horizontal and readable at every moment). The old
  // camera auto-orbit is gone — it kept landing on ugly edge-on angles.
  // The spin lives in the pulse rAF loop below. Manual drag/zoom untouched.

  // Pause the whole render loop when the tab is hidden (battery/perf).
  useEffect(() => {
    const onVis = () => {
      const fg = fgRef.current
      if (!fg) return
      if (document.hidden) fg.pauseAnimation()
      else fg.resumeAnimation()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  // ── Pulse loop: working nodes breathe LIGHT (bloom turns it into glow) and
  // the whole constellation spins slowly in-plane like a galaxy. Idle nodes
  // stay BELOW the bloom threshold — calm colored dots, never false "working".
  useEffect(() => {
    if (reduced) return
    let raf = 0
    const loop = () => {
      const now = performance.now()
      const pal = palRef.current
      const fg = fgRef.current
      if (fg) {
        try { fg.scene().rotation.z += 0.00035 } catch { /* not mounted yet */ }
      }
      for (const [id, o] of objsRef.current) {
        const working = o.kind === 'agent'
          ? activeRef.current.has(id)
          : o.kind === 'brain'
            ? brainBusyRef.current || (activeRef.current.size > 0 && activeRef.current.has(id))
            : false
        const labelMat = o.label.material as THREE.SpriteMaterial
        if (working) {
          const p = 0.5 + 0.5 * Math.sin(now / 300)
          o.sphere.material.emissiveIntensity = 1.1 + 1.1 * p
          o.halo.material.opacity = 0.18 + 0.2 * p
          o.halo.material.color.set(o.kind === 'agent' ? pal.working : pal.accent)
          o.label.color = pal.ink
          labelMat.opacity = 1
        } else {
          o.sphere.material.emissiveIntensity = o.kind === 'brain' ? 0.5 : o.kind === 'dept' ? 0.12 : 0.22
          o.halo.material.opacity = o.kind === 'brain' ? 0.10 : 0.03
          o.halo.material.color.copy(o.baseColor)
          o.label.color = pal.muted
          labelMat.opacity = o.kind === 'dept' ? 0.55 : o.kind === 'brain' ? 1 : 0.72
        }
      }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [reduced])

  // ── Real delegation sparks (same dedup as always) ──────────────────────────
  const seenDeleg = useRef<Set<string>>(new Set())
  useEffect(() => {
    const fg = fgRef.current
    const dels = runtimeStatus.delegations ?? []
    if (!fg || dels.length === 0) return
    const linkId = (l: SwarmLink) => {
      const s = typeof l.source === 'object' ? (l.source as SwarmNode).id : l.source
      const tg = typeof l.target === 'object' ? (l.target as SwarmNode).id : l.target
      return `${s}→${tg}`
    }
    for (const d of dels) {
      const key = `${d.task_id ?? ''}|${d.to}|${d.since ?? ''}`
      if (seenDeleg.current.has(key)) continue
      seenDeleg.current.add(key)
      const deptNode = graph.deptOfAgent.get(d.to)
      if (!deptNode) continue
      const path = new Set([`${graph.brainId}→${deptNode}`, `${deptNode}→${d.to}`])
      for (const l of graph.links) {
        if (!path.has(linkId(l))) continue
        try { fg.emitParticle(l) } catch { /* stale link must never crash the view */ }
      }
    }
    if (seenDeleg.current.size > 256) seenDeleg.current = new Set(Array.from(seenDeleg.current).slice(-128))
  }, [runtimeStatus.delegations, graph])

  // ── Fly-to (click on node / feed row) ──────────────────────────────────────
  const flyTo = useCallback((agentId: string) => {
    const fg = fgRef.current
    if (!fg) return
    const n = graph.nodes.find((x) => x.id === agentId) as (SwarmNode & { x?: number; y?: number; z?: number }) | undefined
    if (!n || n.x == null) return
    const dist = Math.hypot(n.x, n.y ?? 0, n.z ?? 0) || 1
    const ratio = 1 + 110 / dist
    fg.cameraPosition({ x: n.x * ratio, y: (n.y ?? 0) * ratio, z: (n.z ?? 0) * ratio }, n as { x: number; y: number; z: number }, 900)
  }, [graph])

  const handleNodeClick = useCallback((node: NodeObject) => {
    const n = node as SwarmNode
    if (n.kind !== 'agent' || !n.agentId) return
    flyTo(n.id)
    const a = agentById.get(n.agentId)
    if (a) onAgentClick(a)
  }, [flyTo, agentById, onAgentClick])

  // ── Node factory ───────────────────────────────────────────────────────────
  const nodeThreeObject = useCallback((node: NodeObject) => {
    const n = node as SwarmNode
    const pal = palRef.current
    const r = NODE_R[n.kind]
    const color = new THREE.Color(resolveColor(n.color, pal))

    const group = new THREE.Group()
    const sphere = new THREE.Mesh(
      new THREE.SphereGeometry(r, 24, 24),
      new THREE.MeshPhongMaterial({
        color, emissive: color,
        // Idle emissive BELOW the bloom threshold — see the bloom calibration.
        emissiveIntensity: n.kind === 'brain' ? 0.5 : n.kind === 'dept' ? 0.12 : 0.22,
        shininess: 60,
      }),
    )
    group.add(sphere)

    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(r * 1.28, 16, 16),
      new THREE.MeshBasicMaterial({
        color, transparent: true, opacity: n.kind === 'brain' ? 0.10 : 0.03,
        blending: THREE.AdditiveBlending, depthWrite: false,
      }),
    )
    group.add(halo)

    // SpriteText extends THREE.Sprite but its d.ts doesn't expose the base class
    // fields — cast for position/material access. Dark stroke = readable over
    // anything (the owner's screenshot had labels mashed into glow).
    const label = new SpriteText(n.label) as SpriteText & THREE.Sprite
    label.color = pal.muted
    label.textHeight = n.kind === 'brain' ? 6 : n.kind === 'dept' ? 3.4 : 3.8
    label.fontWeight = '600'
    label.strokeColor = 'rgba(0,0,8,0.95)'
    label.strokeWidth = 1
    label.position.y = -(r + 8)
    const lm = label.material as THREE.SpriteMaterial
    lm.depthWrite = false
    lm.transparent = true
    group.add(label)

    objsRef.current.set(n.id, { kind: n.kind, sphere, halo, label, baseColor: color })
    return group
  }, [])

  const hasAgents = graph.nodes.some((n) => n.kind === 'agent')

  return (
    <div className={styles.root}>
      <div className={styles.stage}>
        <div className={styles.canvasWrap} ref={wrapRef}>
          {!hasAgents ? (
            <div className={styles.emptyCenter}>
              <EmptyState compact icon={<Users size={20} aria-hidden="true" />} title={t('swarm.empty.title')} />
            </div>
          ) : size.w > 0 && size.h > 0 ? (
            <ForceGraph3D<SwarmNode, SwarmLink>
              ref={fgRef}
              graphData={graph}
              width={size.w}
              height={size.h}
              backgroundColor="#000004"
              showNavInfo={false}
              enableNodeDrag={false}
              /* PLANAR layout: 2D readability, 3D light. The plane + collide force
                 is what killed the "clumped blobs" look from the owner's Mac. */
              numDimensions={2}
              warmupTicks={60}
              cooldownTime={9000}
              onEngineStop={() => { try { fgRef.current?.zoomToFit(700, 70) } catch { /* fine */ } }}
              d3AlphaDecay={0.045}
              d3VelocityDecay={0.32}
              nodeThreeObject={nodeThreeObject}
              nodeLabel={() => ''}
              onNodeClick={handleNodeClick}
              linkCurvature={0.2}
              linkColor={(l) => {
                const link = l as SwarmLink
                const active = link.agentEndpoint
                  ? activeIds.has(link.agentEndpoint)
                  : activeDeptNodes.has(typeof link.target === 'object' ? (link.target as SwarmNode).id : String(link.target))
                return active ? palRef.current.accent : '#4a5f8f'
              }}
              linkOpacity={0.55}
              linkWidth={(l) => {
                const link = l as SwarmLink
                const active = link.agentEndpoint
                  ? activeIds.has(link.agentEndpoint)
                  : activeDeptNodes.has(typeof link.target === 'object' ? (link.target as SwarmNode).id : String(link.target))
                return active ? 2 : 0.8
              }}
              linkDirectionalParticles={(l) => {
                const link = l as SwarmLink
                const active = link.agentEndpoint
                  ? activeIds.has(link.agentEndpoint)
                  : activeDeptNodes.has(typeof link.target === 'object' ? (link.target as SwarmNode).id : String(link.target))
                return active ? 3 : 0
              }}
              linkDirectionalParticleWidth={2.6}
              linkDirectionalParticleSpeed={0.011}
              linkDirectionalParticleColor={() => palRef.current.accent}
            />
          ) : null}

          {hasAgents && (
            <div className={styles.controls}>
              <Button type="button" variant="ghost" size="sm" onClick={() => fgRef.current?.zoomToFit(700, 60)}>
                {t('swarm.recenter')}
              </Button>
            </div>
          )}
        </div>

        <LiveFeed runtimeStatus={runtimeStatus} agentById={agentById} brainId={graph.brainId} onFocus={flyTo} />
      </div>

      <div className={styles.footer}>
        <p className={styles.hint}>{t('swarm.hint')}</p>
        <ul className={styles.legend} role="list">
          <li><span className={`${styles.dot} ${styles.dotWorking}`} aria-hidden="true" />{t('swarm.legend.working')}</li>
          <li><span className={`${styles.dot} ${styles.dotIdle}`} aria-hidden="true" />{t('swarm.legend.idle')}</li>
          <li><span className={`${styles.dot} ${styles.dotSpark}`} aria-hidden="true" />{t('swarm.legend.delegating')}</li>
        </ul>
      </div>
    </div>
  )
}

// ── Live activity feed (REAL events only) ────────────────────────────────────

function LiveFeed({ runtimeStatus, agentById, brainId, onFocus }: {
  runtimeStatus: RuntimeStatus
  agentById: Map<string, RosterAgent>
  brainId: string
  onFocus: (agentId: string) => void
}) {
  const t = useT()
  const [events, setEvents] = useState<FeedEvent[]>([])
  const seenDeleg = useRef<Set<string>>(new Set())
  const lastTool = useRef<Map<string, string>>(new Map())
  // Re-render every 30 s so relative timestamps stay honest.
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setTick((v) => v + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    const fresh: FeedEvent[] = []
    const name = (id: string) => agentById.get(id)?.name ?? id
    for (const d of runtimeStatus.delegations ?? []) {
      const key = `${d.task_id ?? ''}|${d.to}|${d.since ?? ''}`
      if (seenDeleg.current.has(key)) continue
      seenDeleg.current.add(key)
      fresh.push({
        id: `d:${key}`, ts: Date.now(), type: 'deleg', agentId: d.to,
        title: `${name(d.from) || name(brainId)} → ${name(d.to)}`,
        detail: d.label || undefined,
      })
    }
    for (const a of runtimeStatus.activity ?? []) {
      const key = `${a.task_id ?? ''}|${a.agent_id}`
      const tool = a.tool ?? ''
      if (!tool || lastTool.current.get(key) === tool) continue
      lastTool.current.set(key, tool)
      fresh.push({
        id: `t:${key}:${tool}:${Date.now()}`, ts: Date.now(), type: 'tool', agentId: a.agent_id,
        title: name(a.agent_id),
        detail: toolLabel(tool) ?? tool,
      })
    }
    if (fresh.length > 0) {
      setEvents((prev) => {
        // Visual dedup: don't stack identical consecutive rows within 60 s
        // (e.g. a backend re-emitting the same delegation with a fresh ts).
        const out = [...prev]
        for (const e of fresh) {
          const top = out[0]
          if (top && top.title === e.title && top.detail === e.detail && e.ts - top.ts < 60_000) continue
          out.unshift(e)
        }
        return out.slice(0, FEED_MAX)
      })
    }
    if (seenDeleg.current.size > 256) seenDeleg.current = new Set(Array.from(seenDeleg.current).slice(-128))
    if (lastTool.current.size > 128) lastTool.current = new Map(Array.from(lastTool.current.entries()).slice(-64))
  }, [runtimeStatus, agentById, brainId])

  const rel = (ts: number): string => {
    const m = Math.floor((Date.now() - ts) / 60_000)
    if (m < 1) return t('swarm.feed.now')
    return t('swarm.feed.mins').replace('{n}', String(m))
  }

  return (
    <aside className={styles.feed} aria-label={t('swarm.feed.title')}>
      <div className={styles.feedHead}>
        <span className={styles.feedLiveDot} aria-hidden="true" />
        {t('swarm.feed.title')}
      </div>
      {events.length === 0 ? (
        <p className={styles.feedEmpty}>{t('swarm.feed.empty')}</p>
      ) : (
        <ul className={styles.feedList} role="list">
          {events.map((e) => (
            <li key={e.id}>
              <button type="button" className={styles.feedRow} onClick={() => onFocus(e.agentId)}>
                <span className={`${styles.dot} ${e.type === 'deleg' ? styles.dotSpark : styles.dotWorking}`} aria-hidden="true" />
                <span className={styles.feedBody}>
                  <span className={styles.feedTitle}>{e.title}</span>
                  {e.detail && <span className={styles.feedDetail}>{e.detail}</span>}
                </span>
                <span className={styles.feedTime}>{rel(e.ts)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  )
}

export default SwarmView
