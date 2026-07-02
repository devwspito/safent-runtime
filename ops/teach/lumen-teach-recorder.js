#!/usr/bin/env node
/*
 * lumen-teach-recorder — HOST-SIDE recorder for "teach in your own browser".
 *
 * Runs on the user's machine (NOT the container), connects to their local Chrome's
 * CDP (127.0.0.1:9222 — same host, trivial), injects a small observer into every
 * page, and POSTs each observed click / input / navigation to Lumen's REST ingest
 * (http://localhost:PORT/api/v1/training/{id}/native-step). No container->host
 * networking; identical on macOS and Linux. This is the Playwright-codegen pattern.
 *
 * Env: CDP_URL, LUMEN_URL, SESSION_ID, LUMEN_TOKEN.
 * Deps: none on Node >= 22 (global WebSocket + fetch); falls back to `require('ws')`.
 */
'use strict'

const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222'
const LUMEN_URL = (process.env.LUMEN_URL || 'http://localhost:17517').replace(/\/$/, '')
const SESSION_ID = process.env.SESSION_ID || ''
const TOKEN = process.env.LUMEN_TOKEN || ''
const VERBOSE = process.env.TEACH_VERBOSE === '1'

let WS = globalThis.WebSocket
if (typeof WS !== 'function') { try { WS = require('ws') } catch (_) {
  console.error('[x] Necesitas Node >= 22 (o el paquete ws). Actualiza Node.'); process.exit(1) } }

// Injected into every page. Capture-phase so we see events before stopPropagation.
const OBSERVER_JS = `
(() => {
  if (window.__lumenObserverInstalled) return;
  window.__lumenObserverInstalled = true;
  function esc(s){ try { return CSS.escape(s); } catch(_) { return s; } }
  function sel(el){
    if(!el||el.nodeType!==1) return null;
    if(el.id) return '#'+esc(el.id);
    const parts=[]; let n=el;
    while(n&&n.nodeType===1&&parts.length<5){
      let p=n.tagName.toLowerCase();
      const nm=n.getAttribute&&n.getAttribute('name');
      if(nm){ p+='[name="'+nm+'"]'; parts.unshift(p); break; }
      const par=n.parentElement;
      if(par){ const sib=Array.from(par.children).filter(c=>c.tagName===n.tagName);
        if(sib.length>1) p+=':nth-of-type('+(sib.indexOf(n)+1)+')'; }
      parts.unshift(p); n=n.parentElement;
    }
    return parts.join(' > ');
  }
  function desc(el){
    if(!el||el.nodeType!==1) return {};
    return { tag: el.tagName ? el.tagName.toLowerCase() : null,
      role: (el.getAttribute && el.getAttribute('role')) || null,
      text: ((el.innerText||el.value||el.getAttribute&&el.getAttribute('aria-label')||'')+'').trim().slice(0,80),
      id: el.id||null, name: (el.getAttribute && el.getAttribute('name'))||null, selector: sel(el) };
  }
  function rec(o){ try { window.__lumenRec(JSON.stringify(o)); } catch(_){} }
  document.addEventListener('click', e => rec({type:'click', element:desc(e.target)}), true);
  document.addEventListener('change', e => rec({type:'input',
    value:((e.target&&e.target.value)||'').slice(0,500), element:desc(e.target)}), true);
})();
`

let count = 0
async function postStep(ev) {
  count++
  if (VERBOSE) {
    const el = ev.element || {}
    console.error(`  [${count}] ${ev.type}: ${ev.url || (el.tag + ' ' + JSON.stringify(el.text))} ${el.selector || ''}`)
  }
  try {
    await fetch(`${LUMEN_URL}/api/v1/training/${SESSION_ID}/native-step`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'authorization': `Bearer ${TOKEN}` },
      body: JSON.stringify(ev),
    })
  } catch (e) { if (VERBOSE) console.error('  POST failed:', e.message) }
}

// ---- minimal CDP client (flatten mode) --------------------------------------
class Cdp {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []
    ws.onmessage = (m) => this._onMsg(m.data) }
  _onMsg(data) {
    let msg; try { msg = JSON.parse(data) } catch (_) { return }
    if (msg.id != null && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id); this.pending.delete(msg.id)
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result)
    } else if (msg.method) { for (const h of this.handlers) h(msg) }
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id
    const payload = { id, method, params }; if (sessionId) payload.sessionId = sessionId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject }); this.ws.send(JSON.stringify(payload))
    })
  }
  on(fn) { this.handlers.push(fn) }
}

async function wirePage(cdp, sessionId) {
  try {
    await cdp.send('Runtime.enable', {}, sessionId)
    await cdp.send('Runtime.addBinding', { name: '__lumenRec' }, sessionId)
    await cdp.send('Page.enable', {}, sessionId)
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: OBSERVER_JS }, sessionId)
    try { await cdp.send('Runtime.evaluate', { expression: OBSERVER_JS }, sessionId) } catch (_) {}
  } catch (e) { if (VERBOSE) console.error('  wire failed:', e.message) }
}

async function main() {
  if (!SESSION_ID || !TOKEN) { console.error('[x] SESSION_ID y LUMEN_TOKEN requeridos'); process.exit(1) }
  // discover the browser-level WS endpoint
  let wsUrl
  for (let i = 0; i < 30; i++) {
    try { const r = await fetch(`${CDP_URL}/json/version`); wsUrl = (await r.json()).webSocketDebuggerUrl; if (wsUrl) break }
    catch (_) {}
    await new Promise(r => setTimeout(r, 500))
  }
  if (!wsUrl) { console.error(`[x] No pude conectar al CDP en ${CDP_URL}`); process.exit(1) }
  // Chrome may report 127.0.0.1 in the debugger URL; force the reachable CDP host.
  try { const u = new URL(CDP_URL); wsUrl = wsUrl.replace(/^(wss?:\/\/)[^/]+/, `$1${u.host}`) } catch (_) {}

  const ws = new WS(wsUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = (e) => rej(new Error('ws error')) })
  const cdp = new Cdp(ws)
  const lastNav = {}

  cdp.on(async (msg) => {
    if (msg.method === 'Target.attachedToTarget') {
      const { sessionId, targetInfo } = msg.params
      if (targetInfo.type === 'page') await wirePage(cdp, sessionId)
    } else if (msg.method === 'Runtime.bindingCalled' && msg.params.name === '__lumenRec') {
      try { await postStep(JSON.parse(msg.params.payload)) } catch (_) {}
    } else if (msg.method === 'Page.frameNavigated') {
      const f = msg.params.frame
      if (f && !f.parentId && f.url && f.url !== 'about:blank' && f.url !== lastNav[msg.sessionId]) {
        lastNav[msg.sessionId] = f.url; await postStep({ type: 'navigate', url: f.url })
      }
    }
  })

  // Attach to all current + future page targets (flatten routes by sessionId).
  await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true })
  console.error(`[ok] Grabando en tu navegador. Demuestra la tarea; Ctrl-C o cierra para terminar.`)
  // keep alive
  await new Promise(() => {})
}

main().catch(e => { console.error('[x]', e.message); process.exit(1) })
