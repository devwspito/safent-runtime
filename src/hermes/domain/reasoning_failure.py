"""Safe reasoning failure contract shared by engines and task orchestration.

Messages are application-owned: provider bodies, prompts, URLs and credentials
must never become exception text that the task journal/UI will persist.
"""

_MESSAGES = {
    "auth": "El proveedor rechazó la autorización. Revisa la conexión del modelo.",
    "billing": "El proveedor no permite continuar con el saldo o plan actual.",
    "rate_limit": "El proveedor ha limitado temporalmente las solicitudes.",
    "unavailable": "El proveedor no pudo completar la solicitud.",
    "timeout": "El proveedor no respondió dentro del tiempo disponible.",
    "tls": "No se pudo verificar la conexión segura con el proveedor.",
    "request": "El proveedor rechazó la configuración de esta solicitud.",
    "policy": "El proveedor bloqueó la solicitud por su política.",
    "incomplete": "El motor terminó sin confirmar que la respuesta estuviera completa.",
    "invalid_result": "El motor devolvió un resultado incompatible; no se ha marcado como éxito.",
    "failed": "El motor no pudo completar la respuesta.",
}


class NativeTurnFailedError(Exception):
    """Structured failure with a safe message and explicit retry policy."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code if code in _MESSAGES else "failed"
        self.retryable = retryable if type(retryable) is bool else False
        super().__init__(_MESSAGES[self.code])
