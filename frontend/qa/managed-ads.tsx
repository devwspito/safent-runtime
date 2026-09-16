/** Visual QA only: synthetic local responses, no provider or Enterprise traffic. */
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import AdsView from '../src/views/AdsView'
import { I18nProvider } from '../src/lib/i18n'
import { adsPolicyFixture, adsBindingFixture, adsCampaignFixture, adsProposalFixture, adsReviewFixture } from '../src/api/managedAds.fixtures'
import '../src/styles/globals.css'
import '../src/styles.css'

let revoked = false
const policy = { ...adsPolicyFixture, bindings: [adsBindingFixture, { ...adsBindingFixture, grant_id: 'grant-two', platform: 'google',
  connection_id: '55555555-5555-4555-8555-555555555555', external_account_id: '987654321' }] }
window.fetch = async (input, init) => {
  const path = String(input)
  if (path === '/api/v1/ads/managed') return Response.json({ policy: { ...policy, bindings: revoked ? [] : policy.bindings } })
  if (!path.startsWith('/api/v1/ads/managed/tools/') || revoked) return Response.json({}, { status: 403 })
  const operation = path.split('/').pop()
  const body = JSON.parse(String(init?.body || '{}'))
  const replies: Record<string, unknown> = {
    list_campaigns: { items: [adsCampaignFixture, { ...adsCampaignFixture, entity_ref: 'another-campaign', name: 'Reconocimiento · Arturo Soria', status: 'paused', budget: { amount: 8, currency: 'EUR' } }], cursor: null },
    get_entity_metrics: { entity_ref: body.arguments.entity_ref, granularity: 'daily', points: [
      { period_start: '2026-09-10T00:00:00Z', spend: { amount: 11.5, currency: 'EUR' }, conversions: 3 },
      { period_start: '2026-09-11T00:00:00Z', spend: { amount: 12, currency: 'EUR' }, conversions: 4 },
    ] },
    propose_budget_change: adsProposalFixture, propose_pause: adsProposalFixture, get_approval_review: adsReviewFixture,
  }
  return Response.json({ result: replies[operation || ''] })
}
function Review() {
  const [revision, rerender] = useState(0)
  return <I18nProvider><MemoryRouter><main style={{ height: '100dvh', display: 'flex', flexDirection: 'column' }}>
    <div style={{ padding: '8px 16px', fontSize: 12, color: 'var(--color-text-muted)', borderBottom: '1px solid var(--color-border-subtle)' }}>
      QA visual · Datos ficticios · Sin proveedores · <button type="button" onClick={() => { revoked = !revoked; rerender(revision + 1) }}>Alternar revocación</button>
    </div><AdsView key={revision} />
  </main></MemoryRouter></I18nProvider>
}
createRoot(document.getElementById('root')!).render(<Review />)
