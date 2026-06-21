/**
 * theme.js — Dark/light theme management.
 * Reads from localStorage, respects prefers-color-scheme, applies [data-theme].
 */

import { Icon } from './icons.js';

const STORAGE_KEY = 'lumen-theme';

/** @returns {'dark'|'light'} */
function systemPreference() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/** @returns {'dark'|'light'} */
export function getTheme() {
  // Default to dark (the premium Cowork look) when the operator hasn't chosen.
  // An explicit toggle is persisted to localStorage and always wins.
  return localStorage.getItem(STORAGE_KEY) ?? 'dark';
}

/** @param {'dark'|'light'} theme */
export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(STORAGE_KEY, theme);
  // Update any toggle buttons (Lucide icon: sun in dark mode, moon in light mode)
  document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
    btn.setAttribute('aria-label', theme === 'dark' ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro');
    btn.setAttribute('title', theme === 'dark' ? 'Tema claro' : 'Tema oscuro');
    btn.innerHTML = theme === 'dark' ? Icon.sun : Icon.moon;
  });
}

export function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  return next;
}

export function initTheme() {
  applyTheme(getTheme());
  // Sync when OS preference changes (only if user hasn't overridden)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!localStorage.getItem(STORAGE_KEY)) {
      applyTheme(systemPreference());
    }
  });
}
