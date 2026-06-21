"""Máquina de estados del turno de chat — lógica pura, sin dependencia de GTK.

Esta clase es el corazón del chat moderno (spec 011, US3). Al separar la
lógica de estados del código GTK, los tests unitarios pueden ejercerla sin
un display.

Estados (TurnState):
  IDLE                — sin turno en vuelo
  USER_PINNED         — bubble de usuario pintado, esperando enqueue
  AWAITING_FIRST_TOKEN — enqueue OK, typing dots visibles
  STREAMING           — primer delta recibido, caret visible
  TOOL_RUNNING        — procesando tool_call mid-turn
  AWAITING_APPROVAL   — status=pending_approval recibido
  TURN_ERROR          — error recibido; tarjeta Reintentar
  INTERRUPTED         — stream interrumpido por conexión

Transiciones permitidas:
  IDLE → USER_PINNED          (on_user_message)
  USER_PINNED → AWAITING_FIRST_TOKEN  (on_enqueue_ok)
  USER_PINNED → TURN_ERROR            (on_enqueue_fail)
  AWAITING_FIRST_TOKEN → STREAMING    (on_first_delta)
  AWAITING_FIRST_TOKEN → TURN_ERROR   (on_error_frame)
  STREAMING → STREAMING               (on_delta)
  STREAMING → TOOL_RUNNING            (on_tool_call)
  STREAMING → IDLE                    (on_done)
  STREAMING → TURN_ERROR              (on_error_frame)
  TOOL_RUNNING → STREAMING            (on_delta)
  TOOL_RUNNING → IDLE                 (on_done)
  TOOL_RUNNING → AWAITING_APPROVAL    (on_approval_needed)
  AWAITING_APPROVAL → IDLE            (on_done / on_error_frame)
  TURN_ERROR → IDLE                   (on_retry)
  INTERRUPTED → IDLE                  (on_retry)
  * → INTERRUPTED                     (on_stream_interrupted)
  * → IDLE                            (on_stop / forced)
"""

from __future__ import annotations

from enum import Enum, auto


class TurnState(Enum):
    IDLE = auto()
    USER_PINNED = auto()
    AWAITING_FIRST_TOKEN = auto()
    STREAMING = auto()
    TOOL_RUNNING = auto()
    AWAITING_APPROVAL = auto()
    TURN_ERROR = auto()
    INTERRUPTED = auto()


# Conjunto de estados en los que el compositor debe mostrar el botón "Detener"
# en vez de "Enviar".
STOP_BUTTON_STATES = frozenset({
    TurnState.USER_PINNED,
    TurnState.AWAITING_FIRST_TOKEN,
    TurnState.STREAMING,
    TurnState.TOOL_RUNNING,
    TurnState.AWAITING_APPROVAL,
})

# Estados en los que el turno está "vivo" (hay una tarea en vuelo).
LIVE_TURN_STATES = frozenset({
    TurnState.AWAITING_FIRST_TOKEN,
    TurnState.STREAMING,
    TurnState.TOOL_RUNNING,
    TurnState.AWAITING_APPROVAL,
})


class ChatTurnStateMachine:
    """Máquina de estados del turno — pura Python, testeable sin GTK.

    No emite callbacks; el caller lee `.state` después de cada transición
    y decide qué actualizar en la UI.
    """

    def __init__(self) -> None:
        self._state = TurnState.IDLE
        # Texto del último mensaje de usuario — para reintento automático.
        self._last_user_text: str = ""
        # Texto del último mensaje de usuario que originó un error "sin modelo"
        # para poder reenviarlo cuando el modelo se conecte.
        self._pending_no_model_text: str = ""

    # ------------------------------------------------------------------
    # Propiedad principal
    # ------------------------------------------------------------------

    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def last_user_text(self) -> str:
        return self._last_user_text

    @property
    def pending_no_model_text(self) -> str:
        return self._pending_no_model_text

    @property
    def show_stop_button(self) -> bool:
        return self._state in STOP_BUTTON_STATES

    @property
    def is_live(self) -> bool:
        return self._state in LIVE_TURN_STATES

    # ------------------------------------------------------------------
    # Transiciones de entrada (llamadas desde la UI / pipeline de frames)
    # ------------------------------------------------------------------

    def on_user_message(self, text: str) -> None:
        """El usuario envió un mensaje. Transición IDLE → USER_PINNED."""
        self._last_user_text = text
        self._state = TurnState.USER_PINNED

    def on_enqueue_ok(self) -> None:
        """El mensaje se encoló OK. USER_PINNED → AWAITING_FIRST_TOKEN."""
        if self._state == TurnState.USER_PINNED:
            self._state = TurnState.AWAITING_FIRST_TOKEN

    def on_enqueue_fail(self) -> None:
        """El enqueue falló. USER_PINNED → TURN_ERROR."""
        if self._state == TurnState.USER_PINNED:
            self._state = TurnState.TURN_ERROR

    def on_first_delta(self) -> None:
        """Primer delta/thinking_delta recibido. AWAITING_FIRST_TOKEN → STREAMING."""
        if self._state == TurnState.AWAITING_FIRST_TOKEN:
            self._state = TurnState.STREAMING

    def on_delta(self) -> None:
        """Delta adicional. STREAMING → STREAMING (permanece)."""
        if self._state == TurnState.AWAITING_FIRST_TOKEN:
            # Por si llega delta sin pasar por on_first_delta explícito.
            self._state = TurnState.STREAMING

    def on_tool_call(self) -> None:
        """Frame tool_call recibido. STREAMING|AWAITING → TOOL_RUNNING."""
        if self._state in (TurnState.STREAMING, TurnState.AWAITING_FIRST_TOKEN):
            self._state = TurnState.TOOL_RUNNING

    def on_approval_needed(self) -> None:
        """status=pending_approval. TOOL_RUNNING → AWAITING_APPROVAL."""
        if self._state in (TurnState.TOOL_RUNNING, TurnState.STREAMING):
            self._state = TurnState.AWAITING_APPROVAL

    def on_error_frame(self) -> None:
        """Frame error recibido. Cualquier estado vivo → TURN_ERROR."""
        if self._state in LIVE_TURN_STATES or self._state == TurnState.USER_PINNED:
            self._state = TurnState.TURN_ERROR

    def on_done(self) -> None:
        """Frame done recibido. Cualquier estado vivo → IDLE."""
        if self._state in LIVE_TURN_STATES:
            self._state = TurnState.IDLE

    def on_stop(self) -> None:
        """El usuario pulsó Detener o Esc. Fuerza → IDLE desde cualquier estado vivo."""
        if self._state in STOP_BUTTON_STATES:
            self._state = TurnState.IDLE

    def on_stream_interrupted(self) -> None:
        """El stream se interrumpió por conexión. → INTERRUPTED."""
        if self._state in LIVE_TURN_STATES:
            self._state = TurnState.INTERRUPTED

    def on_retry(self) -> None:
        """El usuario pulsó Reintentar. TURN_ERROR|INTERRUPTED → USER_PINNED."""
        if self._state in (TurnState.TURN_ERROR, TurnState.INTERRUPTED):
            self._state = TurnState.USER_PINNED

    def on_no_model_sent(self, text: str) -> None:
        """El usuario envió un mensaje sin modelo activo.

        Guarda el texto para reintento automático cuando se conecte un servicio.
        El estado visual permanece en IDLE (el bubble ya se pintó antes de llamar
        a esto; la tarjeta ChatActionCard será añadida por la UI).
        """
        self._pending_no_model_text = text
        self._state = TurnState.IDLE

    def on_model_connected(self) -> bool:
        """Un modelo se conectó. Si hay texto pendiente, devuelve True para reintento.

        El caller debe leer pending_no_model_text antes de llamar a on_user_message.
        """
        return bool(self._pending_no_model_text)

    def clear_pending_no_model(self) -> str:
        """Consume y devuelve el texto pendiente de reintento."""
        text = self._pending_no_model_text
        self._pending_no_model_text = ""
        return text


# ------------------------------------------------------------------
# Lógica de autoscroll — también pura Python, testeable sin GTK.
# ------------------------------------------------------------------

class AutoscrollTracker:
    """Rastrea si el scroll está "pegado al fondo" y debe autoscrollear.

    Umbral: ≤ 48px del borde inferior → stuck=True → autoscroll activado.
    Si el usuario sube manualmente → stuck=False → FAB "↓ nuevos mensajes"
    visible. Click en FAB → stuck=True de nuevo.

    Algoritmo:
      - Se llama a update(value, page_size, upper) en cada notify::value
        del Gtk.Adjustment del ScrolledWindow.
      - stuck_to_bottom = (upper - page_size - value) <= THRESHOLD_PX
    """

    THRESHOLD_PX = 48

    def __init__(self) -> None:
        self._stuck = True  # Al inicio, asumimos que estamos al fondo.

    @property
    def stuck_to_bottom(self) -> bool:
        return self._stuck

    def update(self, value: float, page_size: float, upper: float) -> None:
        """Actualiza el estado stuck_to_bottom con los valores del Adjustment."""
        distance_to_bottom = upper - page_size - value
        self._stuck = distance_to_bottom <= self.THRESHOLD_PX

    def force_stick(self) -> None:
        """Llamado cuando el usuario hace clic en el FAB o al enviar un mensaje."""
        self._stuck = True
