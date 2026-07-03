/**
 * TeachPanel — demonstrate a skill in the real (noVNC) browser and save it.
 *
 * Lives in the Habilidades view (skills are created where they're managed). Name the
 * skill → "Empezar a enseñar" (creates a RECORDING session + a CDP observer on the
 * jailed browser) → drive the real browser in the panel → "Guardar habilidad"
 * (compiles a signed SKILL.md, teaching_origin=teaching_live).
 */
import { useState } from 'react'
import { sileo } from 'sileo'
import { useT } from '../lib/i18n'
import { Button } from './ui/Button'
import { VncFrame } from './VncView'
import { startTeaching, signTeaching, ApiError } from '../api/client'

export function TeachPanel({ onSaved, fullscreen }: { onSaved?: () => void; fullscreen?: boolean }) {
  const t = useT()
  const [skill, setSkill] = useState('')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleStart() {
    if (!skill.trim()) { sileo.error({ title: t('teach.err.name') }); return }
    setBusy(true)
    try {
      const r = await startTeaching(skill.trim())
      setSessionId(r.session_id)
      sileo.success({ title: t('teach.toast.recording') })
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : t('teach.err.start') })
    } finally {
      setBusy(false)
    }
  }

  async function handleSave() {
    if (!sessionId) return
    setBusy(true)
    try {
      await signTeaching(sessionId)
      sileo.success({ title: t('teach.toast.saved') })
      setSessionId(null)
      setSkill('')
      onSaved?.()
    } catch (e) {
      sileo.error({ title: e instanceof ApiError ? e.message : t('teach.err.save') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 'var(--space-4)',
      ...(fullscreen ? { flex: 1, minHeight: 0 } : {}),
    }}>
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', flexWrap: 'wrap' }}>
        <input
          type="text"
          value={skill}
          onChange={(e) => setSkill(e.target.value)}
          disabled={!!sessionId}
          placeholder={t('teach.name.placeholder')}
          style={{
            flex: 1, minWidth: 240,
            padding: 'var(--space-2) var(--space-3)',
            border: '1px solid var(--color-border-subtle)',
            borderRadius: 'var(--radius-md)',
            background: 'var(--color-bg-subtle)', color: 'var(--color-text)',
            fontSize: 'var(--text-sm)',
          }}
        />
        {sessionId ? (
          <Button variant="primary" size="sm" loading={busy} onClick={() => void handleSave()}>
            {t('teach.save')}
          </Button>
        ) : (
          <Button variant="primary" size="sm" loading={busy} onClick={() => void handleStart()}>
            {t('teach.start')}
          </Button>
        )}
      </div>
      <p style={{ color: 'var(--color-text-dim)', fontSize: 'var(--text-sm)', margin: 0 }}>
        {sessionId
          ? t('teach.help.recording')
          : t('teach.help.idle')}
      </p>
      <VncFrame fill={fullscreen} />
    </div>
  )
}
