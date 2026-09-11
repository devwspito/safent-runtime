import { JSDOM } from 'jsdom'
import { describe, expect, it } from 'vitest'
import { renderNativeUpdater } from './native-updater.js'

describe('native updater capability', () => {
  const metadata = { status: 'unavailable', reason: 'integration_missing', app_version: '0.9.0' }

  it('shows a build limitation, not latest/up-to-date, without an action or request', () => {
    const element = new JSDOM('<p></p>').window.document.querySelector('p')!
    renderNativeUpdater(element, metadata)
    expect(element.hidden).toBe(false)
    expect(element.textContent).toContain('App nativa 0.9.0')
    expect(element.textContent).toContain('no está disponible en esta compilación')
    expect(element.querySelector('button, a')).toBeNull()
  })

  it.each([undefined, null, {}, { ...metadata, status: 'up_to_date' },
    { ...metadata, reason: 'untrusted' }, { ...metadata, app_version: '<img src=x onerror=alert(1)>' }])(
    'does not invent capability from missing or unsupported metadata %j', value => {
      const element = new JSDOM('<p>Previous value</p>').window.document.querySelector('p')!
      renderNativeUpdater(element, value)
      expect(element.hidden).toBe(true)
      expect(element.textContent).toBe('')
    },
  )
})
