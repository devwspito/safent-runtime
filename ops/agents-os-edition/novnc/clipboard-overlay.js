/**
 * Hermes Clipboard — sincronización AUTOMÁTICA y transparente para noVNC.
 *
 * Objetivo: el usuario real copia en su Mac/PC, va al SO remoto, pulsa Cmd/Ctrl+V
 * y PEGA. Sin paneles, sin textareas, sin botones "Enviar/Traer". Cero carpintería.
 *
 * Por qué un puente lateral (y no clipboard RFB nativo):
 *   El compositor se sirve por el plugin VNC de Qt, que NO implementa la extensión
 *   de clipboard de RFB (ClientCutText/ServerCutText). Así que el portapapeles del
 *   SO se expone por HTTP en el servidor QClipboard del compositor (:7519), tunelado
 *   por un 2º quicktunnel y resuelto aquí como `clipboard_bridge`.
 *
 * Cómo se vuelve transparente:
 *   Browser → SO:  interceptamos el Cmd/Ctrl+V de la página (captura), leemos el
 *                  portapapeles local (Clipboard API), lo empujamos al SO por el
 *                  bridge y, SOLO entonces, inyectamos un Ctrl+V limpio a la VM vía
 *                  el RFB de noVNC (window.UI.rfb.sendKey). Esto garantiza el orden
 *                  (QClipboard fijado ANTES de que la app pegue) y normaliza el
 *                  Cmd-de-Mac → Ctrl-de-Linux.
 *   SO → Browser:  además, al enfocar la pestaña y por sondeo, leemos el portapapeles
 *                  del SO y lo escribimos en el portapapeles local (writeText), para
 *                  que lo que copies dentro del SO se pueda pegar en tu Mac/PC.
 *
 * Permisos: la primera vez el navegador pide "ver el portapapeles" (un clic
 * "Permitir"). Es el modelo estándar de todo escritorio remoto web. Tras concederlo
 * todo es automático. En navegadores sin Clipboard API de lectura (Firefox para
 * contenido web) se degrada con honestidad: se inyecta el pegado igualmente.
 *
 * Sin dependencias. Vanilla ES2017.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Bridge URL:
  //   (1) ?clipboard_bridge=  (override explícito, p.ej. dev con hostfwd directo)
  //   (2) MISMO ORIGEN  → el gateway de un solo origen enruta /clipboard a :7519.
  // El mismo-origen es el modo de producción: un único túnel cloudflared sirve
  // noVNC y el clipboard, así que no hay 2ª URL ni CORS.
  // ---------------------------------------------------------------------------
  function resolveBridgeUrl() {
    const params = new URLSearchParams(window.location.search);
    const explicit = params.get("clipboard_bridge");
    if (explicit) return explicit.replace(/\/$/, "");
    return window.location.origin;
  }
  const BRIDGE = resolveBridgeUrl();

  // ---------------------------------------------------------------------------
  // Acceso a la instancia RFB de noVNC (expuesta como window.UI.rfb por el bake).
  // UI.rfb se crea al conectar y se anula al desconectar → leerla SIEMPRE fresca.
  // ---------------------------------------------------------------------------
  function rfb() {
    const ui = window.UI;
    return ui && ui.rfb && typeof ui.rfb.sendKey === "function" ? ui.rfb : null;
  }

  // Keysyms X11.
  const XK_Control_L = 0xffe3;
  const XK_Shift_L = 0xffe1;
  const XK_v = 0x0076;

  // Inyecta Ctrl+V (o Ctrl+Shift+V para terminales) a la VM por el canal RFB.
  function injectPaste(withShift) {
    const r = rfb();
    if (!r) return false;
    r.sendKey(XK_Control_L, "ControlLeft", true);
    if (withShift) r.sendKey(XK_Shift_L, "ShiftLeft", true);
    r.sendKey(XK_v, "KeyV", true);
    r.sendKey(XK_v, "KeyV", false);
    if (withShift) r.sendKey(XK_Shift_L, "ShiftLeft", false);
    r.sendKey(XK_Control_L, "ControlLeft", false);
    return true;
  }

  // ---------------------------------------------------------------------------
  // Llamadas al bridge
  // ---------------------------------------------------------------------------
  let lastPushed = null; // evita POSTs redundantes
  async function pushToOs(text) {
    if (text == null || text === lastPushed) return;
    lastPushed = text;
    await fetch(`${BRIDGE}/clipboard`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  }

  let lastSeenFromOs = null; // evita writeText redundantes
  async function pullFromOs() {
    const resp = await fetch(`${BRIDGE}/clipboard`, { method: "GET" });
    if (!resp.ok) return;
    const body = await resp.json();
    const t = body && typeof body.text === "string" ? body.text : "";
    if (t && t !== lastSeenFromOs) {
      lastSeenFromOs = t;
      // No re-escribir lo que nosotros mismos acabamos de empujar.
      if (t === lastPushed) return;
      try {
        await navigator.clipboard.writeText(t);
      } catch (_) {
        /* writeText requiere foco/permiso; se reintenta en el próximo tick */
      }
    }
  }

  const canRead = !!(navigator.clipboard && navigator.clipboard.readText);

  // ---------------------------------------------------------------------------
  // Toast efímero (única señal visible; NO es un panel ni requiere interacción).
  // ---------------------------------------------------------------------------
  function toast(msg) {
    let el = document.getElementById("hermes-cb-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "hermes-cb-toast";
      el.style.cssText =
        "position:fixed;bottom:16px;right:16px;z-index:9999;background:#12121f;" +
        "color:#cdd0ff;border:1px solid #3a3a6e;border-radius:8px;padding:8px 12px;" +
        "font:600 12px/1 system-ui,sans-serif;box-shadow:0 2px 12px rgba(0,0,0,.45);" +
        "opacity:0;transition:opacity .2s;pointer-events:none;";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = "1";
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.style.opacity = "0"), 2200);
  }

  // ---------------------------------------------------------------------------
  // Browser → SO: interceptar el atajo de pegar.
  // ---------------------------------------------------------------------------
  function isPasteCombo(e) {
    const v = e.key === "v" || e.key === "V" || e.keyCode === 86;
    const mod = e.ctrlKey || e.metaKey;
    return v && mod && !e.altKey;
  }

  window.addEventListener(
    "keydown",
    function (e) {
      if (!isPasteCombo(e)) return;
      // Si noVNC aún no conectó, no secuestramos el atajo.
      if (!rfb()) return;
      // Si el navegador no deja leer el portapapeles, dejamos el pegado nativo:
      // noVNC enviará el Ctrl+V y la app pegará lo que haya en QClipboard (que el
      // sondeo SO→browser puede haber sincronizado). Degradación honesta.
      if (!canRead) return;

      // Tomamos el control: bloqueamos el envío crudo de noVNC y orquestamos
      // push-luego-inyectar para garantizar el orden y normalizar Cmd→Ctrl.
      e.preventDefault();
      e.stopImmediatePropagation();
      const withShift = e.shiftKey;

      navigator.clipboard
        .readText()
        .then(function (text) {
          return pushToOs(text);
        })
        .catch(function () {
          /* permiso denegado / vacío: inyectamos igual con lo que haya en el SO */
        })
        .then(function () {
          injectPaste(withShift);
        });
    },
    true // fase de captura: antes que el manejador de teclado de noVNC
  );

  // ---------------------------------------------------------------------------
  // SO → Browser: presync al enfocar la pestaña + sondeo periódico.
  // ---------------------------------------------------------------------------
  async function syncBothOnFocus() {
    // Al volver a la pestaña: empuja tu portapapeles local al SO (presync) para
    // que cualquier pegado posterior —nativo o inyectado— use el texto correcto.
    if (canRead && document.hasFocus()) {
      try {
        const local = await navigator.clipboard.readText();
        await pushToOs(local);
      } catch (_) {
        /* sin permiso aún; el primer Cmd+V disparará el prompt */
      }
    }
    try {
      await pullFromOs();
    } catch (_) {
      /* red */
    }
  }

  window.addEventListener("focus", syncBothOnFocus);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") syncBothOnFocus();
  });

  // Sondeo SO→browser continuo (cada 1.5 s) para reflejar copias hechas dentro
  // del SO en el portapapeles local.
  setInterval(function () {
    pullFromOs().catch(function () {});
  }, 1500);

  // Señal única, no intrusiva, al cargar.
  window.addEventListener("load", function () {
    toast("Portapapeles sincronizado — copia y pega con Cmd/Ctrl+V");
  });
})();
