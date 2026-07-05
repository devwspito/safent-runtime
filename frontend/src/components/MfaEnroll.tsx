/**
 * MfaEnroll — self-contained MFA enrollment widget.
 *
 * Calls mfaEnroll(null) to generate the OTP URI, shows the QR + manual key,
 * then fires onEnrolled() when the user confirms they've added the account
 * in their authenticator app.
 *
 * Reuses existing seg-mfa-enroll* CSS classes from styles.css.
 */

import { useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { sileo } from 'sileo'
import { mfaEnroll } from '../api/client'
import { useT } from '../lib/i18n'

interface MfaEnrollProps {
  onEnrolled(): void
}

export default function MfaEnroll({ onEnrolled }: MfaEnrollProps) {
  const t = useT()
  const [otpauthUri, setOtpauthUri] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const secret = otpauthUri
    ? (otpauthUri.match(/[?&]secret=([^&]+)/)?.[1] ?? '')
    : ''

  async function handleStartEnroll() {
    setBusy(true)
    try {
      const res = await mfaEnroll(null)
      setOtpauthUri(res.otpauth_uri ?? null)
    } catch (err) {
      sileo.error({ title: t('mfa_enroll.err').replace('{err}', err instanceof Error ? err.message : String(err)) })
    } finally {
      setBusy(false)
    }
  }

  if (!otpauthUri) {
    return (
      <button
        className="cv-btn cv-btn--primary"
        onClick={handleStartEnroll}
        disabled={busy}
        type="button"
      >
        {busy ? t('mfa_enroll.activating') : t('mfa_enroll.activate')}
      </button>
    )
  }

  return (
    <div className="seg-mfa-enroll">
      <p className="seg-mfa-enroll__step">
        <strong>1.</strong> Escanea este código con tu app de autenticación
        (Google Authenticator, Authy, Aegis…):
      </p>
      <div className="seg-mfa-enroll__qr">
        <QRCodeSVG value={otpauthUri} size={188} level="M" marginSize={2} />
      </div>
      {secret && (
        <p className="seg-mfa-enroll__manual">
          ¿No puedes escanear? Introduce esta clave a mano:
          <br />
          <code className="seg-mfa-enroll__secret">{secret}</code>
        </p>
      )}
      <p className="seg-mfa-enroll__step">
        <strong>2.</strong> La app mostrará un código de 6 dígitos que cambia
        cada 30&nbsp;s — eso es lo que pedirá Safent al aprobar acciones.
      </p>
      <button
        className="cv-btn cv-btn--primary"
        onClick={onEnrolled}
        type="button"
      >
        {t('mfa_enroll.done')}
      </button>
    </div>
  )
}
