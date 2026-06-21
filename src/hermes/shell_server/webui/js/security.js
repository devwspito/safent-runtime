/**
 * security.js — Security view (read-only).
 * Endpoints: GET /security/scans, GET /security/audit/head, GET /security/policy
 */

import {
  getSecurityScans, getAuditChainHead, getSecurityPolicy,
  listEgressDomains, grantEgressDomain, revokeEgressDomain, recordInstallDecision,
} from './api.js';
import { Icon } from './icons.js';
import { t } from './i18n.js';
import { showToast } from './shell.js';
import { renderGovernance } from './governance.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function severityBadge(severity = '') {
  const map = {
    critical: ['#FF453A', t('security.severityCritical')],
    high:     ['#FF8C00', t('security.severityHigh')],
    medium:   ['#F5B945', t('security.severityMedium')],
    low:      ['#34D399', t('security.severityLow')],
    info:     ['#9A9AA2', 'INFO'],
  };
  const [color, label] = map[severity.toLowerCase()] ?? ['#9A9AA2', esc(severity)];
  return `<span class="sec-badge" style="color:${color};background:${color}22">${label}</span>`;
}

function renderScanRow(scan) {
  const el = document.createElement('div');
  el.className = 'sec-scan-row';
  const verdict = String(scan.verdict ?? '').toUpperCase();
  const sev = String(scan.severity ?? '').toLowerCase();
  const flagged = verdict === 'FAIL' || verdict === 'WARN' || sev === 'critical' || sev === 'high';
  const allowed = String(scan.decision ?? '').toUpperCase() === 'ALLOWED';
  el.innerHTML = `
    <div class="sec-scan-row__left">
      <div class="sec-scan-row__name">${esc(scan.name ?? scan.identifier ?? scan.scan_id ?? t('security.defaultScanName'))}</div>
      ${scan.target || scan.identifier ? `<div class="sec-scan-row__target">${esc(scan.target ?? scan.identifier)}</div>` : ''}
    </div>
    <div class="sec-scan-row__right" style="display:flex;align-items:center;gap:8px">
      ${scan.severity ? severityBadge(scan.severity) : ''}
      ${scan.score != null ? `<span class="sec-score">${scan.score}</span>` : ''}
      ${allowed ? '<span class="sec-badge" style="color:#34D399;background:#34D39922">PERMITIDO</span>' : ''}
      ${(flagged && !allowed && (scan.scan_id || scan.id)) ? '<button class="btn btn--ghost btn--sm sec-allow-btn">Permitir igualmente</button>' : ''}
    </div>
    <form class="sec-allow-form" hidden style="flex-basis:100%;display:flex;gap:8px;margin-top:8px">
      <input class="cv-input sec-allow-totp" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="Código MFA" style="width:130px" />
      <input class="cv-input sec-allow-riddle" type="text" placeholder="Respuesta del acertijo" style="flex:1" />
      <button type="submit" class="btn btn--primary btn--sm">Confirmar</button>
    </form>`;
  const btn = el.querySelector('.sec-allow-btn');
  const form = el.querySelector('.sec-allow-form');
  if (btn && form) {
    btn.addEventListener('click', () => { form.hidden = !form.hidden; if (!form.hidden) el.querySelector('.sec-allow-totp').focus(); });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const totp = el.querySelector('.sec-allow-totp').value.trim();
      const riddle_answer = el.querySelector('.sec-allow-riddle').value.trim() || null;
      const submitBtn = form.querySelector('button[type="submit"]');
      submitBtn.disabled = true;
      try {
        await recordInstallDecision({
          scan_id: scan.scan_id ?? scan.id, decision: 'allow',
          identifier: scan.identifier ?? scan.target ?? '', kind: scan.kind ?? '',
          score: scan.score ?? -1, verdict: verdict || '', risks_json: '[]',
          totp, riddle_answer,
        });
        showToast('Instalación permitida (decisión soberana, auditada). Reinténtala.', 'ok');
        form.remove(); btn.remove();
        el.querySelector('.sec-scan-row__right').insertAdjacentHTML('beforeend',
          '<span class="sec-badge" style="color:#34D399;background:#34D39922">PERMITIDO</span>');
      } catch (err) {
        showToast(`No se pudo permitir: ${err.message || err}`, 'error');
        submitBtn.disabled = false;
      }
    });
  }
  return el;
}

export async function renderSecurityView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('security.title'))}</h2>
        <p class="cv-subtitle">${esc(t('security.subtitleView'))}</p>
      </div>
      <div id="governance-mount"></div>
      <div class="cv-section">
        <div class="cv-section-label">Permisos de red — dominios permitidos</div>
        <div class="cv-card">
          <p class="cv-subtitle" style="margin:0 0 10px">Por defecto el agente no accede a ninguna web (default-deny). Autoriza aquí los dominios que quieras permitir (p.ej. <code>pypi.org</code>, <code>github.com</code>). Aplica al navegador y al terminal del agente.</p>
          <div style="display:flex; gap:8px; margin-bottom:10px">
            <input id="egress-input" class="cv-input" type="text" placeholder="dominio (ej. github.com)" autocomplete="off" spellcheck="false" style="flex:1" />
            <button id="egress-grant-btn" class="btn btn--primary">Autorizar</button>
          </div>
          <div id="egress-status" class="cv-subtitle" style="min-height:18px"></div>
          <div id="egress-list" class="cv-list"><div class="cv-skeleton"></div></div>
        </div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('security.auditSection'))}</div>
        <div id="audit-chain-card" class="cv-card"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('security.scansSection'))}</div>
        <div class="cv-list" id="security-scans-list"><div class="cv-skeleton"></div></div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('security.policySection'))}</div>
        <div id="security-policy-card" class="cv-card"><div class="cv-skeleton"></div></div>
      </div>
    </div>`;

  const [scans, auditHead, policy] = await Promise.all([
    getSecurityScans(), getAuditChainHead(), getSecurityPolicy(),
  ]);

  const auditCard = document.getElementById('audit-chain-card');
  if (auditCard) {
    if (!auditHead) {
      auditCard.innerHTML = `<div class="cv-empty">${esc(t('security.noAudit'))}</div>`;
    } else {
      auditCard.innerHTML = `
        <div class="audit-head">
          <div class="audit-head__hash">${Icon.security} <code>${esc(auditHead.hash ?? auditHead.head ?? '—')}</code></div>
          ${auditHead.timestamp ? `<div class="audit-head__time">${new Date(auditHead.timestamp).toLocaleString('es')}</div>` : ''}
        </div>`;
    }
  }

  const scansList = document.getElementById('security-scans-list');
  if (scansList) {
    scansList.innerHTML = '';
    const arr = Array.isArray(scans) ? scans : [];
    if (arr.length === 0) {
      scansList.innerHTML = `<div class="cv-empty">${esc(t('security.noScans'))}</div>`;
    } else {
      arr.forEach(s => scansList.appendChild(renderScanRow(s)));
    }
  }

  const policyCard = document.getElementById('security-policy-card');
  if (policyCard) {
    if (!policy) {
      policyCard.innerHTML = `<div class="cv-empty">${esc(t('security.noPolicy'))}</div>`;
    } else {
      policyCard.innerHTML = `<pre class="cv-policy-pre">${esc(JSON.stringify(policy, null, 2))}</pre>`;
    }
  }

  _wireEgressPermissions();

  const govMount = document.getElementById('governance-mount');
  if (govMount) renderGovernance(govMount).catch(() => {});
}

function _setEgressStatus(msg, kind = '') {
  const el = document.getElementById('egress-status');
  if (el) {
    el.textContent = msg || '';
    el.style.color = kind === 'error' ? '#FF6B6B' : (kind === 'ok' ? '#34D399' : '');
  }
}

async function _renderEgressList() {
  const list = document.getElementById('egress-list');
  if (!list) return;
  const res = await listEgressDomains();
  const domains = (res && res.domains) || [];
  if (domains.length === 0) {
    list.innerHTML = `<div class="cv-empty">Ningún dominio autorizado — el agente no accede a la red.</div>`;
    return;
  }
  list.innerHTML = '';
  domains.forEach((d) => {
    const row = document.createElement('div');
    row.className = 'cv-row';
    row.style.cssText = 'display:flex; align-items:center; justify-content:space-between; padding:6px 0';
    row.innerHTML = `<code>${esc(d)}</code>`;
    const btn = document.createElement('button');
    btn.className = 'btn btn--ghost btn--sm';
    btn.textContent = 'Revocar';
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      _setEgressStatus(`Revocando ${d}…`);
      try {
        await revokeEgressDomain(d);
        _setEgressStatus(`${d} revocado.`, 'ok');
        await _renderEgressList();
      } catch (e) {
        _setEgressStatus(`No se pudo revocar: ${e.message || e}`, 'error');
        btn.disabled = false;
      }
    });
    row.appendChild(btn);
    list.appendChild(row);
  });
}

function _wireEgressPermissions() {
  const input = document.getElementById('egress-input');
  const btn = document.getElementById('egress-grant-btn');
  if (!input || !btn) return;
  const doGrant = async () => {
    const d = (input.value || '').trim().toLowerCase();
    if (!d) return;
    btn.disabled = true;
    _setEgressStatus(`Autorizando ${d}…`);
    try {
      const r = await grantEgressDomain(d);
      if (r && r.ok === false) {
        _setEgressStatus(r.error || 'Dominio inválido.', 'error');
      } else {
        _setEgressStatus(`${d} autorizado.`, 'ok');
        input.value = '';
        await _renderEgressList();
      }
    } catch (e) {
      _setEgressStatus(`No se pudo autorizar: ${e.message || e}`, 'error');
    } finally {
      btn.disabled = false;
    }
  };
  btn.addEventListener('click', doGrant);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doGrant(); });
  _renderEgressList();
}
