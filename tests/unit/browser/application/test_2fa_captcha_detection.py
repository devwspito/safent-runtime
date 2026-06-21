"""Tests T805: TwoFaCaptchaDetector — heurísticas DOM.

Phase 8 / US6 / T805.

Security review (T815 inline / SC-013):
  - SOLO detecta. NUNCA intenta bypass automático (Constitución IV / FR-021).
  - Falsos negativos inaceptables; falsos positivos aceptables.
  - 100% de CAPTCHAs detectados → HITL inmediato.
"""

from __future__ import annotations

from uuid import uuid4

from hermes.browser.application.self_healing import InterventionReason
from hermes.browser.application.two_fa_captcha_detector import (
    detect_and_request_intervention,
    detect_captcha,
    detect_two_fa,
)

# ---------------------------------------------------------------------------
# 2FA detection tests
# ---------------------------------------------------------------------------


def test_input_one_time_code_tel_detected_as_two_fa() -> None:
    """input type=tel autocomplete=one-time-code → OperatorInterventionRequest{TWO_FA_CODE}."""
    dom = """
    <!DOCTYPE html>
    <html>
    <body>
      <h1>Verificación en dos pasos</h1>
      <form>
        <label>Código SMS:
          <input type="tel" autocomplete="one-time-code" maxlength="6" name="otp_code">
        </label>
        <button type="submit">Verificar</button>
      </form>
    </body>
    </html>
    """

    assert detect_two_fa(dom) is True

    session_id = uuid4()
    request = detect_and_request_intervention(
        dom,
        session_id=session_id,
        step_id="step_2fa",
        site_id="banco_test",
        flow_id="login_flow",
    )

    assert request is not None
    assert request.reason == InterventionReason.TWO_FA_CODE
    assert request.site_id == "banco_test"
    assert request.flow_id == "login_flow"
    assert request.step_id == "step_2fa"


# ---------------------------------------------------------------------------
# CAPTCHA detection tests
# ---------------------------------------------------------------------------


def test_recaptcha_iframe_detected_as_captcha() -> None:
    """DOM con iframe src Google reCAPTCHA → OperatorInterventionRequest{reason=CAPTCHA}."""
    dom = """
    <!DOCTYPE html>
    <html>
    <body>
      <h1>Verificación de seguridad</h1>
      <form>
        <div>
          <iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=6Lckey"
                  frameborder="0"></iframe>
        </div>
        <button type="submit">Continuar</button>
      </form>
    </body>
    </html>
    """

    provider = detect_captcha(dom)
    assert provider == "recaptcha"

    session_id = uuid4()
    request = detect_and_request_intervention(
        dom,
        session_id=session_id,
        step_id="step_captcha",
        site_id="sede_test",
        flow_id="login_flow",
    )

    assert request is not None
    assert request.reason == InterventionReason.CAPTCHA
    assert request.metadata.get("captcha_provider") == "recaptcha"


def test_hcaptcha_div_detected_as_captcha() -> None:
    """DOM con <div class='h-captcha'> → OperatorInterventionRequest{reason=CAPTCHA}."""
    dom = """
    <!DOCTYPE html>
    <html>
    <body>
      <form>
        <div class="h-captcha" data-sitekey="abc123"></div>
        <button type="submit">Submit</button>
      </form>
    </body>
    </html>
    """

    provider = detect_captcha(dom)
    assert provider == "hcaptcha"

    session_id = uuid4()
    request = detect_and_request_intervention(
        dom,
        session_id=session_id,
        step_id="step_hcaptcha",
    )
    assert request is not None
    assert request.reason == InterventionReason.CAPTCHA


def test_cloudflare_turnstile_detected_as_captcha() -> None:
    """DOM con <div class='cf-turnstile'> → OperatorInterventionRequest{reason=CAPTCHA}."""
    dom = """
    <!DOCTYPE html>
    <html>
    <body>
      <form>
        <div class="cf-turnstile" data-sitekey="0x4AAAA"></div>
        <button>Submit</button>
      </form>
    </body>
    </html>
    """

    provider = detect_captcha(dom)
    assert provider == "turnstile"


def test_normal_dom_returns_none() -> None:
    """DOM sin 2FA ni CAPTCHA → detect_and_request_intervention returns None."""
    dom = """
    <!DOCTYPE html>
    <html>
    <body>
      <form>
        <input type="text" name="username">
        <input type="password" name="password">
        <button type="submit">Entrar</button>
      </form>
    </body>
    </html>
    """

    assert detect_two_fa(dom) is False
    assert detect_captcha(dom) is None

    request = detect_and_request_intervention(
        dom,
        session_id=uuid4(),
        step_id="step_normal",
    )
    assert request is None


def test_detector_never_attempts_resolution() -> None:
    """NUNCA intenta resolver — solo detecta y emite OperatorInterventionRequest.

    SC-013: 100% de CAPTCHAs detectados degradan a HITL.
    Verificamos que la función solo devuelve una solicitud; no hay
    side effects de resolución.
    """
    captcha_dom = """
    <div class="g-recaptcha" data-sitekey="6Lctest"></div>
    """

    # La función es puramente funcional: sin side effects de browser
    request = detect_and_request_intervention(
        captcha_dom,
        session_id=uuid4(),
        step_id="step_captcha",
    )

    assert request is not None
    assert request.reason == InterventionReason.CAPTCHA
    # La verificación de "no intenta resolver" es estructural:
    # la función no tiene acceso a ningún driver/browser — es pura.
