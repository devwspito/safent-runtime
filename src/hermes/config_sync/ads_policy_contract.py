"""Signed Ads routing contract; mirrored byte-for-byte between Enterprise and Runtime."""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SPACE = 32
_DELETE = 127


class AdsBindingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=2147483647)
    org_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    employee_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    business_id: str = Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
    platform: Literal["google", "meta"]
    connection_id: str = Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
    external_account_id: str = Field(min_length=1, max_length=128, pattern=r"^[0-9]+$")
    resource_revision: int = Field(ge=1, le=2147483647)
    role: Literal["ads"]
    capabilities: list[Literal["read", "propose", "approve", "execute"]]

    @model_validator(mode="after")
    def exact_capabilities(self) -> Self:
        if self.capabilities != ["read", "propose", "approve", "execute"]:
            raise ValueError("Invalid capability ceiling")
        return self


class AdsPolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["free", "managed"]
    instance_id: str = Field(min_length=1, max_length=128)
    central_origin: str | None = Field(default=None, max_length=512)
    bindings: list[AdsBindingSpec] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def exact_scope(self) -> Self:
        if self.mode == "free":
            if self.central_origin is not None or self.bindings:
                raise ValueError("Free policy cannot contain managed authority")
        else:
            value = self.central_origin or ""
            try:
                origin = urlsplit(value)
                valid = (
                    value.isascii()
                    and all(_SPACE < ord(c) < _DELETE for c in value)
                    and "\\" not in value
                    and origin.scheme == "https"
                    and origin.hostname
                    and origin.port in (None, 443)
                    and not origin.username
                    and not origin.password
                    and not origin.query
                    and not origin.fragment
                    and origin.path == ""
                    and not value.endswith("/")
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("HTTPS central origin required")
        if any(binding.instance_id != self.instance_id for binding in self.bindings):
            raise ValueError("Cross-instance assignment")
        if len({binding.grant_id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("Duplicate assignment")
        return self
