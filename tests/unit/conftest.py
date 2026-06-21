"""Unit-test env defaults.

Los unit-tests corren SIN systemd privilegiado / sin el exec-launcher root, así
que el confinamiento del terminal por systemd-run no es aplicable aquí. Forzamos
el modo RAW documentado (HERMES_TERMINAL_SCOPE=0) para ejercitar la lógica del
adapter (capture/replay) sin depender del cage — el hardening real (launcher /
systemd-run con privilegio) se valida en integración/imagen baked, no en unit.
"""
import os

os.environ.setdefault("HERMES_TERMINAL_SCOPE", "0")
