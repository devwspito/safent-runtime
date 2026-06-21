/**
 * governance.js — Owner MFA enrollment + Security Policies (per-command + presets +
 * MFA-on-dangers). Mounted at the top of the Security view.
 *
 * The owner is sovereign: enroll MFA, then govern what the agent may do. Every policy
 * mutation is most-delicate → it requires the owner's MFA code + riddle answer (so the
 * agent can never open its own cage). Turning MFA-on-dangers OFF shows a clear warning.
 */

import {
  mfaStatus, mfaEnroll, mfaSetRiddle,
  getPolicies, setPolicyPreset, setPolicyTool, setMfaOnDangers,
} from './api.js';
import { showToast } from './shell.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const PRESETS = [
  ['equilibrado', 'Equilibrado', 'Todo activo salvo lo más delicado (recomendado)'],
  ['permisivo', 'Permisivo', 'Todo activo — tu responsabilidad'],
  ['bloqueado', 'Bloqueado', 'Todo desactivado (máximo bloqueo)'],
];

export async function renderGovernance(container) {
  const [mfa, pol] = await Promise.all([mfaStatus(), getPolicies()]);
  container.innerHTML = _mfaSection(mfa) + _policySection(pol);
  _wireMfa(container);
  _wirePolicies(container, pol);
}

// ── MFA enrollment ──────────────────────────────────────────────────────────
function _mfaSection(mfa) {
  const enrolled = !!mfa.enrolled;
  const riddle = !!mfa.riddle_set;
  return `<div class="cv-section">
    <div class="cv-section-label">Tu verificación (MFA)</div>
    <div class="cv-card">
      <p class="cv-subtitle" style="margin:0 0 10px">
        ${enrolled
          ? '✅ MFA activo. Aprobar acciones peligrosas y cambiar políticas requiere tu código.'
          : 'Sin MFA no puedes aprobar acciones peligrosas. Actívalo con tu app de autenticación.'}
        ${enrolled ? (riddle ? ' Acertijo configurado.' : ' ⚠️ Falta tu acertijo (necesario para lo más delicado).') : ''}
      </p>
      ${!enrolled ? `<button id="gov-mfa-enroll" class="btn btn--primary">Activar MFA</button>
        <div id="gov-mfa-uri" class="cv-subtitle" style="margin-top:10px;word-break:break-all"></div>` : ''}
      ${enrolled ? `
        <details class="gov-details">
          <summary>${riddle ? 'Cambiar' : 'Configurar'} acertijo personal</summary>
          <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
            <input id="gov-riddle-q" class="cv-input" placeholder="Pregunta (ej. ciudad donde nací)" />
            <input id="gov-riddle-a" class="cv-input" placeholder="Respuesta" />
            <input id="gov-riddle-totp" class="cv-input" inputmode="numeric" placeholder="Tu código MFA actual" />
            <button id="gov-riddle-save" class="btn btn--primary">Guardar acertijo</button>
          </div>
        </details>` : ''}
    </div>
  </div>`;
}

function _wireMfa(root) {
  root.querySelector('#gov-mfa-enroll')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const r = await mfaEnroll(null);
      const uri = r?.otpauth_uri || '';
      const box = root.querySelector('#gov-mfa-uri');
      if (box) box.innerHTML = `Escanéalo en tu app (Google Authenticator, Aegis…):<br><code>${esc(uri)}</code>`;
      showToast('MFA activado — escanea el código', 'ok');
    } catch (err) {
      e.target.disabled = false;
      showToast(`No se pudo activar MFA: ${err.message}`, 'error');
    }
  });
  root.querySelector('#gov-riddle-save')?.addEventListener('click', async () => {
    const q = root.querySelector('#gov-riddle-q')?.value.trim();
    const a = root.querySelector('#gov-riddle-a')?.value.trim();
    const totp = root.querySelector('#gov-riddle-totp')?.value.trim();
    if (!q || !a || !totp) { showToast('Rellena pregunta, respuesta y código MFA', 'error'); return; }
    try {
      await mfaSetRiddle(totp, q, a);
      showToast('Acertijo guardado', 'ok');
    } catch (err) { showToast(`No se pudo guardar: ${err.message}`, 'error'); }
  });
}

// ── Policies ────────────────────────────────────────────────────────────────
function _policySection(pol) {
  const tools = pol.tools || {};
  const names = Object.keys(tools).sort();
  const presetBtns = PRESETS.map(([id, label, desc]) =>
    `<button class="btn ${pol.preset === id ? 'btn--primary' : 'btn--secondary'} gov-preset" data-preset="${id}"
        title="${esc(desc)}">${label}</button>`).join(' ');
  const rows = names.map(n =>
    `<label class="gov-tool-row"><input type="checkbox" class="gov-tool" data-tool="${esc(n)}" ${tools[n] ? 'checked' : ''}/> <span>${esc(n)}</span></label>`
  ).join('');
  return `<div class="cv-section">
    <div class="cv-section-label">Políticas de seguridad — qué puede hacer el agente</div>
    <div class="cv-card">
      <p class="cv-subtitle" style="margin:0 0 10px">Cambiar cualquier política requiere tu código MFA + acertijo (así el agente nunca abre su propia jaula).</p>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input id="gov-pol-totp" class="cv-input" inputmode="numeric" placeholder="Código MFA" style="flex:1" />
        <input id="gov-pol-riddle" class="cv-input" placeholder="Respuesta del acertijo" style="flex:1" />
      </div>

      <label class="gov-tool-row" style="font-weight:600;margin-bottom:10px">
        <input type="checkbox" id="gov-mfa-dangers" ${pol.mfa_on_dangers ? 'checked' : ''}/>
        <span>Pedir mi MFA para los comandos peligrosos (recomendado)</span>
      </label>
      <p class="cv-subtitle" style="margin:0 0 12px">Si lo desactivas, el agente ejecutará acciones peligrosas (mensajes salientes, instalar, programar…) en autónomo sin pedírtelo. La jaula sigue conteniendo el daño, pero tú te haces responsable.</p>

      <div class="cv-section-label" style="margin-bottom:6px">Preset</div>
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">${presetBtns}</div>

      <details class="gov-details">
        <summary>Comandos uno a uno (${names.length})</summary>
        <div class="gov-tool-list" style="max-height:320px;overflow:auto;margin-top:8px">${rows}</div>
      </details>
    </div>
  </div>`;
}

function _factors(root) {
  return {
    totp: root.querySelector('#gov-pol-totp')?.value.trim() || '',
    riddle: root.querySelector('#gov-pol-riddle')?.value.trim() || '',
  };
}

function _wirePolicies(root, pol) {
  // MFA-on-dangers toggle (with warning when turning OFF).
  root.querySelector('#gov-mfa-dangers')?.addEventListener('change', async (e) => {
    const enabled = e.target.checked;
    const { totp, riddle } = _factors(root);
    if (!enabled && !confirm(
      'Vas a permitir que el agente ejecute comandos PELIGROSOS en autónomo sin tu aprobación.\n' +
      'La jaula sigue conteniendo el daño, pero TÚ te haces responsable.\n\n¿Continuar?')) {
      e.target.checked = true; return;
    }
    try {
      await setMfaOnDangers(enabled, totp, riddle);
      showToast(enabled ? 'MFA en peligrosos: ACTIVO' : 'MFA en peligrosos: desactivado', 'ok');
    } catch (err) { e.target.checked = !enabled; showToast(`No se pudo cambiar: ${err.message}`, 'error'); }
  });
  // Presets.
  root.querySelectorAll('.gov-preset').forEach(btn => btn.addEventListener('click', async () => {
    const { totp, riddle } = _factors(root);
    try {
      await setPolicyPreset(btn.dataset.preset, totp, riddle);
      showToast(`Preset «${btn.dataset.preset}» aplicado`, 'ok');
      renderGovernance(root).catch(() => {});
    } catch (err) { showToast(`No se pudo aplicar: ${err.message}`, 'error'); }
  }));
  // Per-tool toggles.
  root.querySelectorAll('.gov-tool').forEach(cb => cb.addEventListener('change', async () => {
    const { totp, riddle } = _factors(root);
    try {
      await setPolicyTool(cb.dataset.tool, cb.checked, totp, riddle);
      showToast(`${cb.dataset.tool}: ${cb.checked ? 'activado' : 'desactivado'}`, 'ok');
    } catch (err) { cb.checked = !cb.checked; showToast(`No se pudo cambiar: ${err.message}`, 'error'); }
  }));
}
