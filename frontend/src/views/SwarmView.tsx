/**
 * SwarmView — the live agent "synaptic brain" (Enterprise candidate).
 *
 * react-force-graph-3d + UnrealBloomPass: agents float in real 3D as glowing
 * Fresnel orbs, wired by a visible neural web — curved links that carry energy
 * particles (ambient "synapses firing" at rest, bright fast flow on activity).
 *
 * Everything drawn/listed is TRUTHFUL:
 *   • node "working" ← runtimeStatus.activity[] (a real in-flight tool),
 *   • bright Cerebro→specialist spark ← runtimeStatus.delegations[] (real
 *     delegate_task events, short TTL),
 *   • ruflo_active → the brain blazes.
 * Idle nodes just breathe. Ambient particles show the network is ALIVE, not that
 * anyone is falsely "working".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph3D, { type ForceGraphMethods, type NodeObject, type LinkObject } from 'react-force-graph-3d'
import * as THREE from 'three'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
// d3-force-3d ships with the force-graph stack (no published types).
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

// ── Graph model (unchanged — same truth, new look) ────────────────────────────

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
  nodes.push({ id: brainId, kind: 'brain', label: brainAgent?.name ?? brainLabel, color: brainAgent?.color ?? '#6aa6ff', agentId: brainAgent?.id })

  const allDepts = [...cerebroDepts, ...customDepts, ...factoryDepts]
  for (const dept of allDepts) {
    const agents = dept.agents.filter((a) => a.id !== brainId)
    if (agents.length === 0) continue
    const deptNodeId = `dept:${dept.id}`
    nodes.push({ id: deptNodeId, kind: 'dept', label: dept.name, color: '#7f8aa3', deptId: dept.id })
    links.push({ source: brainId, target: deptNodeId })
    for (const a of agents) {
      nodes.push({ id: a.id, kind: 'agent', label: a.name, color: a.color ?? '#6aa6ff', agentId: a.id, deptId: dept.id })
      links.push({ source: deptNodeId, target: a.id, agentEndpoint: a.id })
      deptOfAgent.set(a.id, deptNodeId)
    }
  }
  return { nodes, links, brainId, deptOfAgent }
}

// ── Palette ───────────────────────────────────────────────────────────────────

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

// ── Fresnel "energy orb" shell material (rim glow — the premium node look) ─────

const FRESNEL_VERT = `
  varying vec3 vN;
  varying vec3 vV;
  void main() {
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vN = normalize(mat3(modelMatrix) * normal);
    vV = normalize(cameraPosition - wp.xyz);
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`
const FRESNEL_FRAG = `
  uniform vec3 uColor;
  uniform float uPower;
  uniform float uIntensity;
  varying vec3 vN;
  varying vec3 vV;
  void main() {
    float f = pow(1.0 - abs(dot(normalize(vN), normalize(vV))), uPower);
    gl_FragColor = vec4(uColor * f * uIntensity, f);
  }
`
function makeShell(color: THREE.Color): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    uniforms: {
      uColor: { value: color.clone() },
      uPower: { value: 2.4 },
      uIntensity: { value: 1.0 },
    },
    vertexShader: FRESNEL_VERT,
    fragmentShader: FRESNEL_FRAG,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.FrontSide,
  })
}

// ── Per-node three objects (kept so the pulse loop can mutate them) ───────────

interface NodeObjects {
  kind: NodeKind
  core: THREE.Mesh<THREE.SphereGeometry, THREE.MeshStandardMaterial>
  shell: THREE.Mesh<THREE.SphereGeometry, THREE.ShaderMaterial> | null
  label: SpriteText & THREE.Sprite
  baseColor: THREE.Color
}

const NODE_R: Record<NodeKind, number> = { brain: 9, dept: 1.7, agent: 4.6 }

// ── Live feed model ───────────────────────────────────────────────────────────

interface FeedEvent { id: string; ts: number; type: 'deleg' | 'tool'; agentId: string; title: string; detail?: string }
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
    // Dispose the previous frame's GPU resources before rebuilding.
    for (const o of objsRef.current.values()) {
      o.core.geometry.dispose(); o.core.material.dispose()
      o.shell?.geometry.dispose(); o.shell?.material.dispose()
      o.label.material.dispose()
    }
    objsRef.current.clear()
    return buildGraph(rosterRef.current, brainLabel)
  }, [rosterKey, brainLabel])

  const agentById = useMemo(() => {
    const m = new Map<string, RosterAgent>()
    for (const d of roster.departments) for (const a of d.agents) m.set(a.id, a)
    return m
  }, [roster])

  const activeIds = useMemo(() => activeAgentIds(runtimeStatus), [runtimeStatus])
  const activeRef = useRef(activeIds); activeRef.current = activeIds
  const brainBusy = !!runtimeStatus.ruflo_active || (runtimeStatus.delegations?.length ?? 0) > 0
  const brainBusyRef = useRef(brainBusy); brainBusyRef.current = brainBusy

  const activeDeptNodes = useMemo(() => {
    const s = new Set<string>()
    for (const [agentId, deptNode] of graph.deptOfAgent) if (activeIds.has(agentId)) s.add(deptNode)
    return s
  }, [graph, activeIds])
  const linkActive = useCallback((link: SwarmLink) => (
    link.agentEndpoint
      ? activeIds.has(link.agentEndpoint)
      : activeDeptNodes.has(typeof link.target === 'object' ? (link.target as SwarmNode).id : String(link.target))
  ), [activeIds, activeDeptNodes])

  useEffect(() => { palRef.current = readPalette() }, [rosterKey])

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

  // Real 3D layout: no plane. Collide keeps orbs from overlapping; brain pinned.
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    fg.d3Force('charge')?.strength(-300)
    fg.d3Force('link')?.distance((l: SwarmLink) => ((typeof l.source === 'object' && (l.source as SwarmNode).kind === 'brain') ? 170 : 70))
    fg.d3Force('collide', forceCollide((n: SwarmNode) => (n.kind === 'brain' ? 26 : n.kind === 'dept' ? 8 : 18)))
    const brain = graph.nodes.find((n) => n.kind === 'brain') as (SwarmNode & { fx?: number; fy?: number; fz?: number }) | undefined
    if (brain) { brain.fx = 0; brain.fy = 0; brain.fz = 0 }
    fg.d3ReheatSimulation()
    const id = setTimeout(() => { try { fg.zoomToFit(800, 80) } catch { /* not ready */ } }, 1800)
    return () => clearTimeout(id)
  }, [graph])

  // Bloom + starfield + fog — the atmosphere (once per mount).
  const dressedRef = useRef(false)
  useEffect(() => {
    const fg = fgRef.current
    if (!fg || dressedRef.current) return
    dressedRef.current = true
    try {
      const bloom = new UnrealBloomPass(new THREE.Vector2(size.w || 800, size.h || 600), 1.15, 0.5, 0.15)
      fg.postProcessingComposer().addPass(bloom)
    } catch { /* WebGL edge cases — graph still renders */ }
    try {
      const scene = fg.scene()
      scene.fog = new THREE.FogExp2(0x00030a, 0.0016)
      const N = 480
      const pos = new Float32Array(N * 3)
      for (let i = 0; i < N; i++) {
        const a = ((i * 137.508) % 360) / 180 * Math.PI
        const b = (((i * 61) % 180) - 90) / 180 * Math.PI
        const r = 750 + ((i * 97) % 620)
        pos[i * 3] = r * Math.cos(b) * Math.cos(a)
        pos[i * 3 + 1] = r * Math.sin(b)
        pos[i * 3 + 2] = r * Math.cos(b) * Math.sin(a)
      }
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
      scene.add(new THREE.Points(geo, new THREE.PointsMaterial({
        color: 0x8fa2c8, size: 1.4, sizeAttenuation: true, transparent: true, opacity: 0.5,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })))
    } catch { /* ambience optional */ }
  }, [size.w, size.h])

  useEffect(() => {
    const onVis = () => {
      const fg = fgRef.current
      if (!fg) return
      if (document.hidden) fg.pauseAnimation(); else fg.resumeAnimation()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  // Pulse loop: orbs breathe LIGHT (bloom → glow), and the whole brain spins
  // slowly around Y (labels are billboards → always readable).
  useEffect(() => {
    if (reduced) return
    let raf = 0
    const loop = () => {
      const now = performance.now()
      const pal = palRef.current
      const fg = fgRef.current
      if (fg) { try { fg.scene().rotation.y += 0.0006 } catch { /* not mounted */ } }
      for (const [id, o] of objsRef.current) {
        const working = o.kind === 'agent'
          ? activeRef.current.has(id)
          : o.kind === 'brain'
            ? brainBusyRef.current || activeRef.current.has(id)
            : false
        const labelMat = o.label.material as THREE.SpriteMaterial
        if (working) {
          const p = 0.5 + 0.5 * Math.sin(now / 300)
          o.core.material.emissiveIntensity = 1.3 + 1.2 * p
          if (o.shell) o.shell.material.uniforms.uIntensity.value = 1.8 + 1.0 * p
          if (o.shell) o.shell.material.uniforms.uColor.value.set(o.kind === 'agent' ? pal.working : pal.accent)
          o.label.color = pal.ink
          labelMat.opacity = 1
        } else {
          const breathe = 0.5 + 0.5 * Math.sin(now / 1700 + (o.core.position.x || 0))
          o.core.material.emissiveIntensity = o.kind === 'brain' ? 0.55 : o.kind === 'dept' ? 0.15 : 0.28 + 0.06 * breathe
          if (o.shell) { o.shell.material.uniforms.uIntensity.value = o.kind === 'brain' ? 1.1 : 0.85; o.shell.material.uniforms.uColor.value.copy(o.baseColor) }
          o.label.color = pal.muted
          labelMat.opacity = o.kind === 'dept' ? 0.4 : o.kind === 'brain' ? 1 : 0.7
        }
      }
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [reduced])

  // Real delegation sparks (dedup).
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
        try { fg.emitParticle(l) } catch { /* stale link never crashes the view */ }
      }
    }
    if (seenDeleg.current.size > 256) seenDeleg.current = new Set(Array.from(seenDeleg.current).slice(-128))
  }, [runtimeStatus.delegations, graph])

  const flyTo = useCallback((agentId: string) => {
    const fg = fgRef.current
    if (!fg) return
    const n = graph.nodes.find((x) => x.id === agentId) as (SwarmNode & { x?: number; y?: number; z?: number }) | undefined
    if (!n || n.x == null) return
    const dist = Math.hypot(n.x, n.y ?? 0, n.z ?? 0) || 1
    const ratio = 1 + 120 / dist
    fg.cameraPosition({ x: n.x * ratio, y: (n.y ?? 0) * ratio, z: (n.z ?? 0) * ratio }, n as { x: number; y: number; z: number }, 900)
  }, [graph])

  const handleNodeClick = useCallback((node: NodeObject) => {
    const n = node as SwarmNode
    if (n.kind !== 'agent' || !n.agentId) return
    flyTo(n.id)
    const a = agentById.get(n.agentId)
    if (a) onAgentClick(a)
  }, [flyTo, agentById, onAgentClick])

  // Node factory — glowing Fresnel energy orb.
  const nodeThreeObject = useCallback((node: NodeObject) => {
    const n = node as SwarmNode
    const pal = palRef.current
    const r = NODE_R[n.kind]
    const color = new THREE.Color(resolveColor(n.color, pal))

    const group = new THREE.Group()
    const core = new THREE.Mesh(
      new THREE.SphereGeometry(r * 0.62, 24, 24),
      new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: n.kind === 'brain' ? 0.55 : 0.28, roughness: 0.35, metalness: 0.1 }),
    )
    group.add(core)

    let shell: THREE.Mesh<THREE.SphereGeometry, THREE.ShaderMaterial> | null = null
    if (n.kind !== 'dept') {
      shell = new THREE.Mesh(new THREE.SphereGeometry(r, 32, 32), makeShell(color))
      group.add(shell)
    }

    const label = new SpriteText(n.label) as SpriteText & THREE.Sprite
    label.color = pal.muted
    label.textHeight = n.kind === 'brain' ? 6 : n.kind === 'dept' ? 3 : 3.6
    label.fontWeight = '600'
    label.strokeColor = 'rgba(0,0,10,0.95)'
    label.strokeWidth = 1
    label.position.y = -(r + 7)
    const lm = label.material as THREE.SpriteMaterial
    lm.depthWrite = false; lm.transparent = true
    group.add(label)

    objsRef.current.set(n.id, { kind: n.kind, core, shell, label, baseColor: color })
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
              backgroundColor="#00030a"
              showNavInfo={false}
              enableNodeDrag={false}
              warmupTicks={60}
              cooldownTime={9000}
              onEngineStop={() => { try { fgRef.current?.zoomToFit(800, 80) } catch { /* fine */ } }}
              d3AlphaDecay={0.045}
              d3VelocityDecay={0.34}
              nodeThreeObject={nodeThreeObject}
              nodeLabel={() => ''}
              onNodeClick={handleNodeClick}
              linkCurvature={0.25}
              linkColor={(l) => (linkActive(l as SwarmLink) ? palRef.current.accent : '#5a76b8')}
              linkOpacity={0.7}
              linkWidth={(l) => (linkActive(l as SwarmLink) ? 1.8 : 0.6)}
              /* Particles on EVERY link = the visible synaptic web (ambient at
                 rest, bright/fast on real activity). This is what makes the
                 "neural network" legible. */
              linkDirectionalParticles={(l) => (linkActive(l as SwarmLink) ? 3 : 1)}
              linkDirectionalParticleWidth={(l) => (linkActive(l as SwarmLink) ? 2.6 : 1.3)}
              linkDirectionalParticleSpeed={(l) => (linkActive(l as SwarmLink) ? 0.012 : 0.004)}
              linkDirectionalParticleColor={(l) => (linkActive(l as SwarmLink) ? palRef.current.accent : '#6d84c0')}
            />
          ) : null}

          {hasAgents && (
            <div className={styles.controls}>
              <Button type="button" variant="ghost" size="sm" onClick={() => fgRef.current?.zoomToFit(800, 70)}>
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
      fresh.push({ id: `d:${key}`, ts: Date.now(), type: 'deleg', agentId: d.to, title: `${name(d.from) || name(brainId)} → ${name(d.to)}`, detail: d.label || undefined })
    }
    for (const a of runtimeStatus.activity ?? []) {
      const key = `${a.task_id ?? ''}|${a.agent_id}`
      const tool = a.tool ?? ''
      if (!tool || lastTool.current.get(key) === tool) continue
      lastTool.current.set(key, tool)
      fresh.push({ id: `t:${key}:${tool}:${Date.now()}`, ts: Date.now(), type: 'tool', agentId: a.agent_id, title: name(a.agent_id), detail: toolLabel(tool) ?? tool })
    }
    if (fresh.length > 0) {
      setEvents((prev) => {
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
