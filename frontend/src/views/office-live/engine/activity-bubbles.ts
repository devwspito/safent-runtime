/** Activity bubbles — Habbo-style speech bubbles above characters */

export interface ActivityBubble {
  agentId: string
  type: "thinking" | "tool_call" | "responding"
  text: string
  createdAt: number
  opacity: number
}

const BUBBLE_LIFETIME_MS = 4000
const FADE_DURATION_MS = 600

/** Update bubble opacity. Returns false if expired. */
export function updateBubble(bubble: ActivityBubble, now: number): boolean {
  const age = now - bubble.createdAt
  if (age > BUBBLE_LIFETIME_MS) return false

  const fadeStart = BUBBLE_LIFETIME_MS - FADE_DURATION_MS
  if (age > fadeStart) {
    bubble.opacity = 1 - (age - fadeStart) / FADE_DURATION_MS
  } else {
    bubble.opacity = 1
  }
  return true
}

/** Draw a Habbo-style speech bubble above a character */
export function drawBubble(
  ctx: CanvasRenderingContext2D,
  bubble: ActivityBubble,
  charScreenX: number,
  charScreenY: number,
  zoom: number,
  frameCount: number
): void {
  if (bubble.opacity <= 0) return

  ctx.save()
  ctx.globalAlpha = bubble.opacity

  // Build display text
  let text: string
  if (bubble.type === "thinking") {
    const dots = ".".repeat((Math.floor(frameCount / 20) % 3) + 1)
    text = dots
  } else if (bubble.type === "responding") {
    // Typing animation instead of showing partial message content
    const bars = ["▏", "▎▏", "▎▏▎", "▎▏"][Math.floor(frameCount / 15) % 4]!
    text = bars
  } else if (bubble.type === "tool_call") {
    text = `⚡ ${bubble.text}`
  } else {
    text =
      bubble.text.length > 35 ? bubble.text.slice(0, 35) + "…" : bubble.text
  }

  const fontSize = Math.max(8, zoom * 2.8)
  ctx.font = `${fontSize}px monospace`
  const textWidth = ctx.measureText(text).width
  const padX = zoom * 2
  const padY = zoom * 1.2
  const bubbleW = textWidth + padX * 2
  const bubbleH = fontSize + padY * 2
  const tailH = zoom * 2
  const cornerR = zoom * 1.5

  // Position bubble above character
  const bx = charScreenX - bubbleW / 2
  const by = charScreenY - bubbleH - tailH - zoom * 6

  // Bubble body (white rounded rect with dark border)
  ctx.fillStyle = "#FFFFFF"
  ctx.strokeStyle = "#333333"
  ctx.lineWidth = Math.max(1, zoom * 0.4)
  ctx.beginPath()
  ctx.roundRect(bx, by, bubbleW, bubbleH, cornerR)
  ctx.fill()
  ctx.stroke()

  // Tail (triangle pointing down to character)
  const tailX = charScreenX
  const tailTop = by + bubbleH - 1
  const tailBottom = tailTop + tailH
  ctx.fillStyle = "#FFFFFF"
  ctx.beginPath()
  ctx.moveTo(tailX - zoom * 1.5, tailTop)
  ctx.lineTo(tailX, tailBottom)
  ctx.lineTo(tailX + zoom * 1.5, tailTop)
  ctx.closePath()
  ctx.fill()

  // Tail border (just the two outer edges)
  ctx.beginPath()
  ctx.moveTo(tailX - zoom * 1.5, tailTop)
  ctx.lineTo(tailX, tailBottom)
  ctx.lineTo(tailX + zoom * 1.5, tailTop)
  ctx.stroke()

  // Cover the tail-body junction with white
  ctx.fillStyle = "#FFFFFF"
  ctx.fillRect(tailX - zoom * 1.5 + 1, tailTop - 1, zoom * 3 - 2, 3)

  // Text
  ctx.fillStyle = "#222222"
  ctx.textAlign = "center"
  ctx.textBaseline = "middle"
  ctx.fillText(text, charScreenX, by + bubbleH / 2)

  ctx.restore()
}
