/** Simple particle system for confetti and error sparks */

// ── Types ────────────────────────────────────────────────────

interface Particle {
  active: boolean
  x: number
  y: number
  vx: number
  vy: number
  color: string
  life: number
  maxLife: number
  size: number
  gravity: number
}

const POOL_SIZE = 200

const CONFETTI_COLORS = [
  "#FF6B6B",
  "#4ECDC4",
  "#45B7D1",
  "#96CEB4",
  "#FFEAA7",
  "#DDA0DD",
]
const SPARK_COLORS = ["#FF4444", "#FF8800", "#FFCC00"]

// ── Pool ─────────────────────────────────────────────────────

export class ParticlePool {
  particles: Particle[]

  constructor() {
    this.particles = Array.from({ length: POOL_SIZE }, () => ({
      active: false,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      color: "#FFF",
      life: 0,
      maxLife: 1,
      size: 1,
      gravity: 0,
    }))
  }

  private _find(): Particle | null {
    for (const p of this.particles) {
      if (!p.active) return p
    }
    return null
  }

  emit(type: "confetti" | "spark", screenX: number, screenY: number): void {
    const count = type === "confetti" ? 30 : 12
    const colors = type === "confetti" ? CONFETTI_COLORS : SPARK_COLORS

    for (let i = 0; i < count; i++) {
      const p = this._find()
      if (!p) break

      p.active = true
      p.x = screenX + (Math.random() - 0.5) * 4
      p.y = screenY + (Math.random() - 0.5) * 4

      if (type === "confetti") {
        p.vx = (Math.random() - 0.5) * 160
        p.vy = -40 - Math.random() * 80
        p.gravity = 150
        p.life = 0.8 + Math.random() * 0.7
        p.size = 1 + Math.random() * 2
      } else {
        p.vx = (Math.random() - 0.5) * 120
        p.vy = -20 - Math.random() * 60
        p.gravity = 100
        p.life = 0.4 + Math.random() * 0.4
        p.size = 1 + Math.random()
      }

      p.maxLife = p.life
      p.color = colors[Math.floor(Math.random() * colors.length)]!
    }
  }

  update(dt: number): void {
    for (const p of this.particles) {
      if (!p.active) continue
      p.life -= dt
      if (p.life <= 0) {
        p.active = false
        continue
      }
      p.vy += p.gravity * dt
      p.x += p.vx * dt
      p.y += p.vy * dt
    }
  }

  draw(ctx: CanvasRenderingContext2D, zoom: number): void {
    for (const p of this.particles) {
      if (!p.active) continue
      const alpha = Math.max(0, p.life / p.maxLife)
      const sz = Math.max(1, p.size * zoom * 0.3)
      ctx.globalAlpha = alpha
      ctx.fillStyle = p.color
      ctx.fillRect(p.x, p.y, sz, sz)
    }
    ctx.globalAlpha = 1
  }

  hasActive(): boolean {
    return this.particles.some((p) => p.active)
  }
}
