/**
 * memory.js — Memory view.
 * Endpoints: GET /memory, GET /memory/search?q=
 */

import { listMemory, searchMemory } from './api.js';
import { Icon } from './icons.js';
import { t } from './i18n.js';

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderMemoryItem(item) {
  const el = document.createElement('div');
  el.className = 'memory-item';
  el.innerHTML = `
    <div class="memory-item__content">${esc(item.content ?? item.text ?? JSON.stringify(item))}</div>
    ${item.created_at ? `<div class="memory-item__time">${new Date(item.created_at).toLocaleString('es')}</div>` : ''}`;
  return el;
}

export async function renderMemoryView(container) {
  container.innerHTML = `
    <div class="capability-view">
      <div class="cv-header">
        <h2 class="cv-title">${esc(t('memory.title'))}</h2>
        <p class="cv-subtitle">${esc(t('memory.subtitleView'))}</p>
      </div>
      <div class="cv-section">
        <div class="skills-hub-search">
          <input id="memory-search-input" class="cv-input" type="search" placeholder="${esc(t('memory.searchPlaceholder'))}" autocomplete="off" aria-label="${esc(t('memory.searchAriaLabel'))}">
          <button class="btn btn--secondary btn--sm" id="memory-search-btn">${esc(t('memory.searchBtn'))}</button>
        </div>
      </div>
      <div class="cv-section">
        <div class="cv-section-label">${esc(t('memory.recentSection'))}</div>
        <div class="cv-list" id="memory-list"><div class="cv-skeleton"></div></div>
      </div>
    </div>`;

  async function load(query = '') {
    const list = document.getElementById('memory-list');
    if (!list) return;
    list.innerHTML = `<div class="cv-skeleton"></div>`;
    const items = query ? await searchMemory(query) : await listMemory();
    list.innerHTML = '';
    const arr = Array.isArray(items) ? items : [];
    if (arr.length === 0) {
      list.innerHTML = `<div class="cv-empty">${esc(query ? t('memory.noResults', { q: query }) : t('memory.noEntries'))}</div>`;
    } else {
      arr.forEach(item => list.appendChild(renderMemoryItem(item)));
    }
  }

  const searchBtn = container.querySelector('#memory-search-btn');
  const searchInput = container.querySelector('#memory-search-input');

  searchBtn?.addEventListener('click', () => load(searchInput?.value.trim() ?? ''));
  searchInput?.addEventListener('keydown', e => { if (e.key === 'Enter') load(searchInput?.value.trim() ?? ''); });

  load();
}
