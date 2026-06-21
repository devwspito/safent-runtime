/**
 * stream.js — WebSocket streaming client for task output.
 *
 * Protocol: WS ws://<host>/api/v1/chat/stream/{task_id}
 * Frames (JSONL): {kind: "status"|"delta"|"thinking_delta"|"tool_call"|"done"|"error", ...payload}
 *
 * Emits events to a provided callbacks object so the UI layer stays decoupled.
 */

import { t } from './i18n.js';

const WS_BASE = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`;
const WS_PATH = '/api/v1/chat/stream';

/** @typedef {{ kind: string, [k: string]: any }} StreamFrame */

/**
 * @typedef {Object} StreamCallbacks
 * @property {(text: string) => void} onDelta       - incremental agent text
 * @property {(text: string) => void} onThinking    - thinking block text
 * @property {(call: StreamFrame) => void} onToolCall - tool call frame
 * @property {(status: string) => void} onStatus    - status update string
 * @property {() => void} onDone                    - stream completed
 * @property {(err: string) => void} onError        - error string
 */

/**
 * Opens a WebSocket stream for a given task_id.
 * Returns a disposer function that closes the socket.
 *
 * @param {string} taskId
 * @param {StreamCallbacks} callbacks
 * @param {{ maxRetries?: number }} [opts]
 * @returns {{ close: () => void }}
 */
export function openTaskStream(taskId, callbacks, opts = {}) {
  // Higher budget than a one-shot request: an agent task can run for HOURS, and the
  // transport (esp. the VZ relay) may drop the socket transiently. We re-attach to
  // the SAME running task — the daemon replays/continues from the task socket.
  const { maxRetries = 8 } = opts;
  let ws = null;
  let retries = 0;
  let closed = false;
  let retryTimer = null;

  function connect() {
    if (closed) return;
    const url = `${WS_BASE}${WS_PATH}/${encodeURIComponent(taskId)}`;
    ws = new WebSocket(url);

    ws.addEventListener('message', (event) => {
      // Any delivered frame proves the stream is alive and progressing → reset the
      // retry budget, so a long-running task that drops every so often keeps
      // recovering instead of dying after N total drops over its whole lifetime.
      retries = 0;
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return; // ignore malformed frames
      }
      dispatch(frame);
    });

    ws.addEventListener('error', () => {
      // onclose will fire next with the code; error alone is not actionable
    });

    ws.addEventListener('close', (event) => {
      if (closed) return;
      // Code 1000 = normal close (done). Anything else = unexpected.
      if (event.code === 1000) {
        callbacks.onDone?.();
        return;
      }
      if (retries < maxRetries) {
        retries++;
        callbacks.onStatus?.(t('chat.reconnecting'));
        const delay = Math.min(400 * 2 ** retries, 10000);
        retryTimer = setTimeout(connect, delay);
      } else {
        callbacks.onError?.(t('chat.connectionLost'));
      }
    });
  }

  function dispatch(frame) {
    // The daemon emits the chunk text in `delta` (e.g. {kind:"delta",delta:"..."}).
    // Stay tolerant of payload-nested / text variants across protocol versions.
    const p = frame.payload && typeof frame.payload === 'object' ? frame.payload : null;
    const deltaText = frame.delta ?? frame.text ?? p?.delta ?? p?.text ?? '';
    switch (frame.kind) {
      case 'delta':
        callbacks.onDelta?.(deltaText);
        break;
      case 'thinking_delta':
        callbacks.onThinking?.(frame.thinking ?? deltaText);
        break;
      case 'tool_call':
        callbacks.onToolCall?.(frame);
        break;
      case 'status':
        callbacks.onStatus?.(frame.message ?? frame.status ?? p?.message ?? '');
        break;
      case 'done':
        callbacks.onDone?.();
        break;
      case 'error':
        callbacks.onError?.(frame.message ?? 'Error desconocido del agente');
        break;
      default:
        // Unknown frame kinds are silently ignored — forward compat
        break;
    }
  }

  connect();

  return {
    close() {
      closed = true;
      clearTimeout(retryTimer);
      if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000);
    },
  };
}
