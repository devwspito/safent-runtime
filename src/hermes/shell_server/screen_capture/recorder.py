"""GstScreenRecorder — grabación NATIVA pantalla+audio a archivo (.webm).

Usa el grabador nativo del SO: GStreamer. Compone en UNA tubería el vídeo
del compositor (mutter ScreenCast → PipeWire node) con el audio del micro
(PipeWire/PulseAudio), los codifica (VP8 + Opus) y los muxa a WebM.

  pipewiresrc(node) ! videoconvert ! vp8enc ! webmmux name=mux ! filesink
  pipewiresrc/pulsesrc(audio) ! audioconvert ! opusenc ! mux.

Degrada a solo-vídeo si no hay fuente de audio (p.ej. VM sin tarjeta).
Captura navegador Y apps de escritorio (todo lo que mutter compone).

Scope:
  Esta clase sirve exclusivamente para la herramienta LLM `screen.record`
  (os_native_skills/executors.py).  El flujo de entrenamiento (training/)
  usa ScreenCaptureService (screenshots) + GstMicAudioBackend (audio WAV)
  por separado — NO pasa por GstScreenRecorder.

  require_audio=True está disponible para callers que necesiten audio
  obligatorio; actualmente ningún caller lo activa.  Si en el futuro se
  añade un caller, debe también añadirse un test que valide que start()
  lanza CaptureError cuando no hay fuente de audio.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .domain import CaptureError

logger = logging.getLogger(__name__)


def _ensure_gst() -> None:
    if not Gst.is_initialized():
        Gst.init(None)


def _audio_source_available() -> str | None:
    """Devuelve el elemento de audio source si hay un DEVICE REAL, o None.

    Usa Gst.DeviceMonitor (filtro Audio/Source): no basta con que la factory
    pipewiresrc exista (siempre existe). Si NO hay micrófono real, devuelve
    None y el recorder graba solo vídeo en un único intento — así evitamos un
    intento de audio condenado que además podría envenenar el nodo mutter
    ScreenCast para el fallback de vídeo (probado en el SO real).
    """
    try:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        monitor.start()
        devices = monitor.get_devices()
        monitor.stop()
    except Exception:  # noqa: BLE001
        return None
    if not devices:
        return None
    # Hay micrófono real: pipewiresrc es el source nativo en Wayland.
    return "pipewiresrc"


class GstScreenRecorder:
    """Graba pantalla (+audio si hay) a un .webm vía GStreamer nativo.

    Args:
        node_id:   PipeWire node del compositor (de MutterScreenCastSource).
        out_path:  ruta del .webm de salida.
        fps:       frames por segundo del vídeo.
        with_audio: intentar capturar audio (degrada si no hay fuente).
    """

    def __init__(
        self,
        *,
        node_id: int,
        out_path: Path,
        fps: int = 15,
        with_audio: bool = True,
        require_audio: bool = False,
    ) -> None:
        """
        with_audio:    intentar capturar audio.
        require_audio: el audio es OBLIGATORIO para este caller.
                       Si no hay fuente de audio disponible, start() lanza
                       CaptureError en vez de degradar a vídeo mudo.
                       Nota: el flujo de training NO usa GstScreenRecorder;
                       usa GstMicAudioBackend separado para audio WAV.
        """
        _ensure_gst()
        self._node_id = node_id
        self._out_path = out_path
        self._fps = fps
        self._with_audio = with_audio or require_audio
        self._require_audio = require_audio
        self._pipeline: Gst.Pipeline | None = None
        self._has_audio = False

    @property
    def has_audio(self) -> bool:
        return self._has_audio

    def _build_description(self, *, with_audio: bool) -> str:
        # NOTA: nada de `videorate ! caps(framerate)` aquí. videorate forzando
        # un framerate fijo ESTANCA el preroll sobre el source de mutter
        # ScreenCast (entrega a ritmo variable) y el pipeline no alcanza PLAYING.
        # Grabamos al ritmo que entrega el compositor; vp8enc cpu-used=8 da
        # encode realtime de baja latencia en CPU débil sin GPU.
        video = (
            f"pipewiresrc path={self._node_id} ! "
            "videoconvert ! vp8enc deadline=1 cpu-used=8 ! "
            "queue ! mux."
        )
        sink = f"webmmux name=mux ! filesink location={self._out_path}"
        if with_audio:
            audio_src = _audio_source_available()
            if audio_src is not None:
                audio = (
                    f"{audio_src} ! queue ! audioconvert ! audioresample ! "
                    "opusenc ! queue ! mux."
                )
                return f"{sink} {video} {audio}"
        return f"{sink} {video}"

    # Preroll de vp8enc por software en CPU débil/cargada puede tardar varios
    # segundos; damos una ventana amplia antes de declarar fallo.
    _PLAYING_TIMEOUT_S = 15

    def _try_start(self, *, with_audio: bool) -> bool:
        """Intenta arrancar el pipeline; True si alcanza PLAYING.

        Verifica el estado real (no solo el retorno inmediato): un device de
        audio inexistente puede dar ASYNC y fallar después. Espera hasta
        _PLAYING_TIMEOUT_S a que alcance PLAYING (preroll del encoder).
        """
        desc = self._build_description(with_audio=with_audio)
        logger.info("recorder pipeline (audio=%s): %s", with_audio, desc)
        pipeline = Gst.parse_launch(desc)
        rv = pipeline.set_state(Gst.State.PLAYING)
        if rv == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            return False
        state_rv, state, _pending = pipeline.get_state(
            self._PLAYING_TIMEOUT_S * Gst.SECOND
        )
        if state_rv != Gst.StateChangeReturn.SUCCESS or state != Gst.State.PLAYING:
            pipeline.set_state(Gst.State.NULL)
            return False
        self._pipeline = pipeline
        self._has_audio = with_audio
        return True

    def start(self) -> None:
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        # Intenta con audio primero.
        if self._with_audio and self._try_start(with_audio=True):
            return
        # require_audio: NO degradamos a vídeo mudo. Sin la explicación
        # hablada del formador no hay nada que transcribir → training inútil.
        if self._require_audio:
            self._pipeline = None
            raise CaptureError(
                "audio obligatorio no disponible: el entrenamiento necesita "
                "tu explicación hablada (activa un micrófono)"
            )
        if self._with_audio:
            logger.info("recorder: audio no disponible, grabo solo vídeo")
        if self._try_start(with_audio=False):
            return
        self._pipeline = None
        raise CaptureError("recorder pipeline no arrancó (ni vídeo)")

    def stop(self) -> Path:
        """Cierra el muxer limpiamente (EOS) y devuelve la ruta del .webm."""
        if self._pipeline is None:
            raise CaptureError("recorder no está activo")
        # EOS para que webmmux escriba el índice final del contenedor.
        self._pipeline.send_event(Gst.Event.new_eos())
        bus = self._pipeline.get_bus()
        bus.timed_pop_filtered(
            5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        return self._out_path
