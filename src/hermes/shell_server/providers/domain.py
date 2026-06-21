"""Domain: provider LLM configurable por el usuario en Settings.

Sin acoplamiento a LiteLLM ni FastAPI — solo VOs + invariantes.

Un Provider es una conexion a un endpoint que sirve modelos. Puede ser:
  - cloud (Anthropic, OpenAI, Gemini, Azure, Bedrock, Mistral, Cohere, etc.)
  - cloud-cn (Deepseek, Moonshot, Zhipu GLM, Doubao)
  - proxy/aggregator (OpenRouter, Together, Fireworks, Replicate)
  - self-hosted (vLLM, Ollama, LM Studio, llama.cpp, TGI)

LiteLLM normaliza todos detras del mismo API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class ProviderKind(StrEnum):
    """Tipos de provider — usado para UX y para defaults razonables de LiteLLM."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    GEMINI = "gemini"
    BEDROCK = "bedrock"
    MISTRAL = "mistral"
    COHERE = "cohere"
    GROQ = "groq"
    # Chinos baratos.
    DEEPSEEK = "deepseek"
    MOONSHOT = "moonshot"
    ZHIPU = "zhipu"
    DOUBAO = "doubao"
    QWEN_DASHSCOPE = "qwen_dashscope"
    # Proxies / aggregators.
    OPENROUTER = "openrouter"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    # Self-hosted (OpenAI-compatible).
    VLLM = "vllm"
    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    LLAMA_CPP = "llama_cpp"
    TGI = "tgi"
    # Generic OpenAI-compatible endpoint.
    OPENAI_COMPATIBLE = "openai_compatible"
    # Suscripción OAuth device-code (sin clave API). Las credenciales viven en
    # el auth-store de hermes_cli bajo HERMES_HOME, no en el vault: el row del
    # provider lleva has_api_key=False y la resolución va por REGISTERED_SLUG.
    NOUS = "nous"


# Mapping ProviderKind -> default LiteLLM model prefix.
LITELLM_PREFIX: dict[ProviderKind, str] = {
    ProviderKind.ANTHROPIC: "anthropic",
    ProviderKind.OPENAI: "openai",
    ProviderKind.AZURE_OPENAI: "azure",
    ProviderKind.GEMINI: "gemini",
    ProviderKind.BEDROCK: "bedrock",
    ProviderKind.MISTRAL: "mistral",
    ProviderKind.COHERE: "cohere",
    ProviderKind.GROQ: "groq",
    ProviderKind.DEEPSEEK: "deepseek",
    ProviderKind.MOONSHOT: "moonshot",
    ProviderKind.ZHIPU: "zhipu",
    ProviderKind.DOUBAO: "volcengine",
    ProviderKind.QWEN_DASHSCOPE: "dashscope",
    ProviderKind.OPENROUTER: "openrouter",
    ProviderKind.TOGETHER: "together_ai",
    ProviderKind.FIREWORKS: "fireworks_ai",
    ProviderKind.VLLM: "hosted_vllm",
    ProviderKind.OLLAMA: "ollama",
    ProviderKind.LM_STUDIO: "openai",  # LM Studio expone OpenAI compatible
    ProviderKind.LLAMA_CPP: "openai",
    ProviderKind.TGI: "huggingface",
    ProviderKind.OPENAI_COMPATIBLE: "openai",
    # Prefix "nous" a propósito: nous_request_from_model_config hace reverse-map
    # prefix→canonical, y "openai" colisionaría con OPENAI (enrutaría a
    # api.openai.com). litellm no entiende "nous/" pero ese engine es fallback
    # no usado (HERMES_ENGINE=nous); la resolución correcta del slug manda.
    ProviderKind.NOUS: "nous",
}


# Default endpoints sugeridos por kind (cuando aplica).
DEFAULT_BASE_URL: dict[ProviderKind, str] = {
    ProviderKind.OLLAMA: "http://localhost:11434",
    ProviderKind.LM_STUDIO: "http://localhost:1234/v1",
    ProviderKind.VLLM: "http://localhost:8000/v1",
    ProviderKind.LLAMA_CPP: "http://localhost:8080/v1",
}


class ProviderConnectivity(StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


@dataclass(slots=True)
class ProviderModel:
    """Modelo expuesto por el provider (resultado de list_models)."""

    id: str
    context_window: int | None = None
    supports_tools: bool | None = None


@dataclass(slots=True)
class Provider:
    """Conexion LLM configurada por el usuario.

    Invariantes:
      - api_key NUNCA se serializa de vuelta (security/secrets.py la cifra).
      - alias es unico por sesion de SO.
    """

    provider_id: UUID
    alias: str  # human-friendly: "vLLM local Qwen", "Anthropic prod"
    kind: ProviderKind
    base_url: str | None  # None para providers cloud con endpoint hardcoded
    has_api_key: bool  # solo flag — el secret vive en secrets.py
    default_model: str  # ej "qwen3-coder-35b" o "claude-opus-4-7"
    enabled: bool = True
    is_active: bool = False  # provider activo del usuario (1 a la vez)
    connectivity: ProviderConnectivity = ProviderConnectivity.UNKNOWN
    last_checked_at: datetime | None = None
    available_models: tuple[ProviderModel, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def _assert_safe_base_url(base_url: str | None) -> None:
    """V (SSRF): reject cloud-metadata / link-local base_url before any fetch.

    Blocks ONLY 169.254.0.0/16 (link-local, incl. the 169.254.169.254 cloud
    metadata endpoint) — never a legitimate model endpoint. Private ranges
    (10/8, 192.168/16) are intentionally NOT blocked: a local model on the host
    gateway is a supported configuration.
    """
    if not base_url:
        return
    from urllib.parse import urlparse  # noqa: PLC0415
    import ipaddress  # noqa: PLC0415

    host = (urlparse(base_url).hostname or "").strip("[]")
    if not host:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname (incl. cloud tunnels / DNS) — allowed
    if ip.is_link_local:
        raise ValueError(
            f"base_url host blocked (link-local/metadata SSRF): {host}"
        )


def new_provider(
    *,
    alias: str,
    kind: ProviderKind,
    default_model: str,
    base_url: str | None = None,
    has_api_key: bool = False,
) -> Provider:
    """Factory."""
    _assert_safe_base_url(base_url)
    return Provider(
        provider_id=uuid4(),
        alias=alias,
        kind=kind,
        base_url=base_url or DEFAULT_BASE_URL.get(kind),
        has_api_key=has_api_key,
        default_model=default_model,
    )


def provider_model_string(provider: Provider, model: str) -> str:
    """Construye el string `provider/model` (formato estándar nous/hermes-agent)."""
    prefix = LITELLM_PREFIX[provider.kind]
    if model.startswith(f"{prefix}/"):
        return model
    return f"{prefix}/{model}"


# Alias histórico — borrar cuando se purguen los callers viejos.
litellm_model_string = provider_model_string


class ProviderAliasConflict(ValueError):
    """Otro provider ya usa ese alias."""


class ProviderNotFound(LookupError):
    pass
