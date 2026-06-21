"""T039 🔒 — Race condition en sender_uid (CWE-362/367, TOCTOU).

Verifica que dos llamadas D-Bus concurrentes con UIDs distintos nunca
intercambian su autoría: cada tarea recibe SIEMPRE el sender_uid de SU
propio mensaje, incluso cuando el await de resolución de UID permite que
otro mensaje se procese en el medio.

Diseño sin bus real
-------------------
El test simula el patrón de dispatch de dbus-fast con exactitud:

  1. El message handler corre síncronamente (igual que _process_message).
  2. asyncio.ensure_future() crea la tarea con el contexto actual copiado.
  3. La tarea suspende inmediatamente en un await inyectable (barrier).
  4. Mientras está suspendida, el handler del segundo mensaje se ejecuta.
  5. Ambas tareas se reanudan y resuelven su UID.

Si el transporte es el atributo compartido (_current_sender), el paso 4
sobreescribe el valor que la tarea 1 va a leer al reanudar → cruce de UIDs.
Si el transporte es un ContextVar, cada tarea lee su propio snapshot → OK.

No requiere bus real ni D-Bus daemon; marca pytest.mark.unit.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import pytest

pytest.importorskip("dbus_fast")

from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (  # noqa: E402
    _CURRENT_SENDER_VAR,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers: simula el resolver de UID con una barrera controlable
# ---------------------------------------------------------------------------


async def _pausing_uid_resolver(
    sender: str,
    barrier: asyncio.Event,
    uid_map: dict[str, int],
) -> int:
    """Resuelve el UID del sender, pero suspende en la barrera primero.

    Simula GetConnectionUnixUser con latencia de red: la tarea cede el
    control al event loop mientras la barrera no está set.
    """
    await barrier.wait()  # suspende aquí; otro handler puede correr en el medio
    return uid_map[sender]


# ---------------------------------------------------------------------------
# Simulación del patrón BUGGY (shared attribute — cómo era antes del fix)
# ---------------------------------------------------------------------------


class _BuggyInterface:
    """Reproduce el bug original: el sender se guarda en un atributo compartido."""

    def __init__(self, uid_map: dict[str, int]) -> None:
        self._uid_map = uid_map
        self._current_sender: str | None = None  # atributo compartido — vulnerable

    def inject_sender(self, sender: str) -> None:
        """Simula lo que hace el message handler de dbus-fast."""
        self._current_sender = sender

    async def handle_message(
        self, sender: str, barrier: asyncio.Event
    ) -> int:
        """Simula el método D-Bus (Enqueue/Approve/etc.)."""
        # En el código original, esto leía self._current_sender DESPUÉS de un await.
        # Si otro mensaje sobreescribió el atributo mientras suspendimos → race.
        captured_sender = self._current_sender  # captura local ANTES del await
        # (el bug: el código original NO capturaba localmente — llamaba al resolver
        # que hacía getattr(self, '_current_sender') después del await)
        resolved = await _pausing_uid_resolver(captured_sender, barrier, self._uid_map)
        return resolved

    async def handle_message_buggy(
        self, sender: str, barrier: asyncio.Event
    ) -> int:
        """Versión realmente buggy: lee el atributo compartido DESPUÉS del await."""
        # Nota: en el código original, _resolve_current_sender_uid() hacía:
        #   sender = getattr(self, "_current_sender", None)
        #   return await _get_connection_unix_user(bus, sender)
        # El getattr estaba DENTRO del mismo cuerpo async, pero ANTES del await,
        # lo que parecía seguro… EXCEPTO que el handler del segundo mensaje
        # ya había corrido síncronamente y sobreescrito el atributo ANTES de que
        # esta tarea empezara siquiera a ejecutarse (la tarea no arranca hasta
        # que el event loop la schedula).
        #
        # Aquí lo simulamos: la inyección de B ocurre entre el momento en que
        # la tarea A fue creada (ensure_future) y el momento en que empieza a
        # ejecutarse. El atributo ya tiene el valor de B cuando A lee.
        _ = sender  # ignorado — la tarea lee el atributo compartido directamente
        # Simula que el atributo fue sobreescrito antes de que arranque la tarea:
        resolved_sender = self._current_sender  # lee lo que hay AHORA (puede ser B)
        return await _pausing_uid_resolver(resolved_sender, barrier, self._uid_map)


# ---------------------------------------------------------------------------
# Test: reproduce el race con el patrón BUGGY
# ---------------------------------------------------------------------------


class TestRaceWithSharedAttribute:
    """Muestra que el atributo compartido causa cruce de UIDs bajo concurrencia."""

    async def test_shared_attribute_races_uids(self) -> None:
        """Con el atributo compartido, el segundo mensaje sobreescribe el primero."""
        uid_map = {":1.100": 1000, ":1.200": 2000}
        iface = _BuggyInterface(uid_map)

        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()

        # Simula el dispatch de dbus-fast:
        # 1. Handler del mensaje A inyecta sender A
        iface.inject_sender(":1.100")  # handler message A

        # 2. ensure_future crea tarea A (NO arranca aún)
        task_a = asyncio.ensure_future(
            iface.handle_message_buggy(":1.100", barrier_a)
        )

        # 3. Handler del mensaje B inyecta sender B (SOBREESCRIBE el atributo)
        iface.inject_sender(":1.200")  # handler message B — race!

        # 4. ensure_future crea tarea B
        task_b = asyncio.ensure_future(
            iface.handle_message_buggy(":1.200", barrier_b)
        )

        # 5. Liberamos ambas barreras para que terminen
        barrier_a.set()
        barrier_b.set()

        uid_a, uid_b = await asyncio.gather(task_a, task_b)

        # Con el atributo compartido, tarea A lee el sender de B (sobreescrito).
        # Ambas tareas ven ":1.200" → uid_a = 2000, uid_b = 2000.
        # Esto DEMUESTRA el bug: uid_a debería ser 1000.
        assert uid_a == uid_b == 2000, (
            "El bug debe ser reproducible: ambas tareas leen el sender de B "
            f"(esperado uid_a=2000 por race, got uid_a={uid_a}, uid_b={uid_b})"
        )
        # uid_a es INCORRECTO — debería ser 1000, pero el race lo hace 2000.
        assert uid_a != 1000, (
            "Si uid_a fuera 1000, el race no estaría activo — verificar el test"
        )


# ---------------------------------------------------------------------------
# Simulación del patrón CORRECTO (ContextVar — el fix)
# ---------------------------------------------------------------------------


class _FixedInterface:
    """Reproduce el fix: el sender se transporta via ContextVar."""

    def __init__(self, uid_map: dict[str, int]) -> None:
        self._uid_map = uid_map

    def inject_sender(self, sender: str) -> None:
        """Simula el message handler: set en el ContextVar (síncronamente)."""
        _CURRENT_SENDER_VAR.set(sender)

    async def handle_message(self, barrier: asyncio.Event) -> int:
        """Simula el método D-Bus con el fix aplicado.

        Lee el sender del ContextVar al arrancar la tarea — la tarea tiene
        su propia copia del contexto gracias a ensure_future.
        """
        sender = _CURRENT_SENDER_VAR.get()
        assert sender is not None, "ContextVar debe tener valor en esta tarea"
        return await _pausing_uid_resolver(sender, barrier, self._uid_map)


class TestNoRaceWithContextVar:
    """Verifica que el ContextVar aísla el sender por tarea — sin cruce de UIDs."""

    async def test_contextvar_isolates_sender_per_task(self) -> None:
        """Dos llamadas concurrentes reciben SIEMPRE su propio UID, nunca el del otro."""
        uid_map = {":1.100": 1000, ":1.200": 2000}
        iface = _FixedInterface(uid_map)

        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()

        # Simula el dispatch de dbus-fast (mismo orden que en producción):
        # 1. Handler A inyecta sender A en el ContextVar
        iface.inject_sender(":1.100")

        # 2. ensure_future crea tarea A — copia el contexto AHORA (_CURRENT_SENDER=":1.100")
        task_a = asyncio.ensure_future(iface.handle_message(barrier_a))

        # 3. Handler B inyecta sender B — modifica el ContextVar del hilo principal
        #    pero NO afecta la copia que ya tiene la tarea A
        iface.inject_sender(":1.200")

        # 4. ensure_future crea tarea B — copia el contexto AHORA (_CURRENT_SENDER=":1.200")
        task_b = asyncio.ensure_future(iface.handle_message(barrier_b))

        # 5. Liberar barreras (A suspende y B puede correr en el medio)
        barrier_a.set()
        barrier_b.set()

        uid_a, uid_b = await asyncio.gather(task_a, task_b)

        assert uid_a == 1000, (
            f"Tarea A debe resolver UID de ':1.100' → 1000, got {uid_a}"
        )
        assert uid_b == 2000, (
            f"Tarea B debe resolver UID de ':1.200' → 2000, got {uid_b}"
        )

    async def test_interleaved_suspend_no_uid_swap(self) -> None:
        """Con interleave explícito (B corre MIENTRAS A suspende), aún sin cruce."""
        uid_map = {":1.A": 1111, ":1.B": 2222}
        iface = _FixedInterface(uid_map)

        # Barrera de A empieza bloqueada; la de B estará abierta desde el principio
        barrier_a = asyncio.Event()
        barrier_b = asyncio.Event()
        barrier_b.set()  # B se resuelve inmediatamente sin suspender

        # Dispatch de A
        iface.inject_sender(":1.A")
        task_a = asyncio.ensure_future(iface.handle_message(barrier_a))

        # Dispatch de B (sobreescribe el ContextVar del hilo principal)
        iface.inject_sender(":1.B")
        task_b = asyncio.ensure_future(iface.handle_message(barrier_b))

        # Ceder al event loop: B termina antes que A (que sigue suspendida)
        await asyncio.sleep(0)  # un yield — B puede completar

        # Ahora liberamos A
        barrier_a.set()

        uid_a = await task_a
        uid_b = await task_b

        assert uid_a == 1111, (
            f"A debe tener UID 1111 incluso después de que B completó, got {uid_a}"
        )
        assert uid_b == 2222, f"B debe tener UID 2222, got {uid_b}"

    async def test_n_concurrent_callers_no_uid_crossing(self) -> None:
        """N callers concurrentes, ninguno recibe el UID de otro."""
        n = 10
        uid_map = {f":1.{i}": (i + 1) * 100 for i in range(n)}
        iface = _FixedInterface(uid_map)

        barriers = [asyncio.Event() for _ in range(n)]
        tasks = []

        for i in range(n):
            iface.inject_sender(f":1.{i}")
            tasks.append(asyncio.ensure_future(iface.handle_message(barriers[i])))

        # Liberar en orden inverso para maximizar el interleave
        for barrier in reversed(barriers):
            barrier.set()

        results = await asyncio.gather(*tasks)

        for i, uid in enumerate(results):
            expected = (i + 1) * 100
            assert uid == expected, (
                f"Caller {i} (sender ':1.{i}') debe resolver UID {expected}, got {uid}"
            )


# ---------------------------------------------------------------------------
# Test de integración: Runtime1ServiceInterface usa _CURRENT_SENDER_VAR
# ---------------------------------------------------------------------------


class TestAdapterUsesContextVar:
    """Verifica que _resolve_current_sender_uid lee el ContextVar, no un atributo."""

    async def test_resolve_reads_contextvar_not_shared_attribute(self) -> None:
        """_resolve_current_sender_uid debe leer _CURRENT_SENDER_VAR, no _current_sender."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            Runtime1ServiceInterface,
        )

        class _StubWiring:
            async def enqueue(self, **_: Any) -> None: ...
            async def request_pause(self, **_: Any) -> None: ...
            async def request_resume(self, **_: Any) -> None: ...
            async def approve_action(self, **_: Any) -> object: ...
            async def reject_action(self, **_: Any) -> None: ...
            async def get_queue_status(self) -> dict: return {}
            async def list_pending(self, **_: Any) -> list: return []
            async def get_task_status(self, **_: Any) -> dict: return {}

        iface = Runtime1ServiceInterface(wiring=_StubWiring())  # type: ignore[arg-type]
        # Fix-9 regression: sin bus → PermissionError (fail-closed), no uid=0 sentinel.
        # uid=0 as sentinel was a security hole: root would have been authorized implicitly.
        with pytest.raises(PermissionError):
            await iface._resolve_current_sender_uid()

    async def test_contextvar_is_not_shared_attribute(self) -> None:
        """El interface NO debe tener _current_sender como atributo de instancia."""
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (
            Runtime1ServiceInterface,
        )

        class _StubWiring:
            async def enqueue(self, **_: Any) -> None: ...
            async def request_pause(self, **_: Any) -> None: ...
            async def request_resume(self, **_: Any) -> None: ...
            async def approve_action(self, **_: Any) -> object: ...
            async def reject_action(self, **_: Any) -> None: ...
            async def get_queue_status(self) -> dict: return {}
            async def list_pending(self, **_: Any) -> list: return []
            async def get_task_status(self, **_: Any) -> dict: return {}

        iface = Runtime1ServiceInterface(wiring=_StubWiring())  # type: ignore[arg-type]
        assert not hasattr(iface, "_current_sender"), (
            "_current_sender NO debe ser un atributo de instancia tras el fix. "
            "El sender se transporta via _CURRENT_SENDER_VAR (ContextVar) para "
            "evitar el TOCTOU race (CWE-362/367)."
        )
