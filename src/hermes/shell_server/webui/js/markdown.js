/**
 * markdown.js — Markdown → safe HTML for chat messages.
 *
 * Uses `marked` (GFM: tables, strikethrough, task lists, autolinks, fenced code)
 * for parsing and `DOMPurify` for sanitisation. Both are vendored locally under
 * webui/vendor/ so the baked image renders offline (no CDN at runtime).
 *
 * Security: marked emits raw HTML, so every render is passed through DOMPurify
 * before insertion. External links get target="_blank" rel="noopener noreferrer";
 * javascript:/data: URLs are stripped by DOMPurify's default policy.
 */

import { marked } from '../vendor/marked.esm.js';
import DOMPurify from '../vendor/purify.es.mjs';

// GFM with single-newline line breaks (matches a chat feel). No raw HTML pass-through
// beyond what DOMPurify allows; headers get no auto-ids (avoids id collisions in chat).
marked.setOptions({
  gfm: true,
  breaks: true,
  pedantic: false,
});

// External links open in a new tab, hardened. Runs after attribute sanitisation.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    const href = node.getAttribute('href') || '';
    const external = /^(https?:)?\/\//i.test(href) || /^https?:/i.test(href);
    if (external) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  }
  // Task-list checkboxes are display-only.
  if (node.tagName === 'INPUT') {
    node.setAttribute('disabled', '');
  }
});

const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr', 'blockquote', 'pre', 'code', 'span',
    'strong', 'em', 'del', 's', 'b', 'i', 'a',
    'ul', 'ol', 'li', 'input',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
  ],
  ALLOWED_ATTR: ['href', 'title', 'class', 'target', 'rel', 'type', 'checked', 'disabled', 'align'],
  ALLOW_DATA_ATTR: false,
};

/**
 * Render a Markdown string to sanitised HTML.
 * @param {string} md
 * @returns {string}
 */
export function renderMarkdown(md) {
  const raw = marked.parse(md ?? '', { async: false });
  return DOMPurify.sanitize(raw, PURIFY_CONFIG);
}
