import { act, StrictMode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ getUsageSummary:vi.fn(), getUsageByAgent:vi.fn(), getUsageTimeseries:vi.fn() }))
vi.mock('../api/client', async () => ({ ...await vi.importActual('../api/client'), ...api }))
// Only chart rendering is replaced: expose the actual data prepared for Recharts.
vi.mock('recharts', () => ({ ResponsiveContainer:({children}:any)=>children, AreaChart:({data,children}:any)=><div data-chart={JSON.stringify(data)}>{children}</div>, Area:()=>null, XAxis:()=>null, YAxis:()=>null, Tooltip:()=>null, CartesianGrid:()=>null }))
import UsageView from './UsageView'
let root:Root, host:HTMLDivElement
const summary={available:true,period:'30d',currency:'USD',total_cost_usd:0,projected_cost_usd:0,total_tokens:700,cycles:2,failures:0,self_hosted_cycles:0,top_models:[{model:'subscription-model',cost_usd:0,share:1}]}
beforeEach(()=>{
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT',true)
  api.getUsageSummary.mockReset().mockResolvedValue(summary)
  api.getUsageByAgent.mockReset().mockResolvedValue({available:true,agents:[]})
  api.getUsageTimeseries.mockReset().mockResolvedValue({available:true,points:[{day:'2026-09-11',cost_usd:0,tokens:700,cycles:2}]})
  host=document.createElement('div');document.body.append(host);root=createRoot(host)
})
afterEach(()=>{act(()=>root.unmount());host.remove();vi.unstubAllGlobals()})
const render=()=>act(async()=>root.render(<UsageView/>))
async function click(text:string){await act(async()=>{Array.from(host.querySelectorAll('button')).find(b=>b.textContent===text)!.click()})}
it('shows unavailable summary as failure, not empty activity',async()=>{
  api.getUsageSummary.mockResolvedValue({...summary,available:false});await render()
  expect(host.querySelector('[role="alert"]')).not.toBeNull()
  expect(host.textContent).not.toContain('Sin actividad en este periodo')
})
it('rejects an incomplete summary rather than rendering invented zeroes',async()=>{
  api.getUsageSummary.mockResolvedValue({available:true});await render()
  expect(host.querySelector('[role="alert"]')).not.toBeNull();expect(host.textContent).not.toContain('$0.00')
})
it('keeps known summary while a breakdown is unavailable',async()=>{
  api.getUsageByAgent.mockRejectedValue(new Error('secret-upstream'));await render()
  expect(host.textContent).toContain('Gasto del periodo')
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('No se pudo verificar')
  expect(host.textContent).not.toContain('secret-upstream')
})
it('does not infer self-hosted from a zero recorded model cost',async()=>{
  await render();expect(host.textContent).toContain('subscription-model')
  expect(host.textContent).not.toContain('Propio')
  expect(host.querySelectorAll('.usage-agent-row')).toHaveLength(0)
})
it('uses tokens, not cycles, for the token chart',async()=>{
  await render();await click('Ver tokens')
  expect(JSON.parse(host.querySelector('[data-chart]')!.getAttribute('data-chart')!)[0].value).toBe(700)
})
it('retains the selected period after failure and retries that exact period',async()=>{
  await render();api.getUsageSummary.mockRejectedValueOnce(new Error('offline'));await click('7 días')
  expect(Array.from(host.querySelectorAll('button')).find(b=>b.textContent==='7 días')?.getAttribute('aria-pressed')).toBe('true')
  await click('Reintentar');expect(api.getUsageSummary).toHaveBeenLastCalledWith('7d')
})
it('shows genuine empty activity only after successful available sources',async()=>{
  api.getUsageSummary.mockResolvedValue({...summary,cycles:0,total_tokens:0,top_models:[]});await render()
  expect(host.textContent).toContain('Sin actividad en este periodo')
})
it('ignores the obsolete first read after StrictMode remount',async()=>{
  let resolve!:(value:typeof summary)=>void
  api.getUsageSummary.mockReturnValueOnce(new Promise(r=>{resolve=r}))
  await act(async()=>root.render(<StrictMode><UsageView/></StrictMode>))
  expect(host.textContent).toContain('subscription-model')
  await act(async()=>resolve({...summary,total_cost_usd:999,top_models:[{model:'stale-model',cost_usd:999,share:1}]}))
  expect(host.textContent).not.toContain('stale-model')
})
