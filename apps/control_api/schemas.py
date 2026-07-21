"""Pydantic models for control API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    ok: bool = True
    project_root: str
    token_required: bool
    password_login_enabled: bool = False
    users_configured: bool = False
    auth_required: bool = False


class OverviewOut(BaseModel):
    project_root: str
    product_ok: int
    run: dict[str, Any] | None = None


class ConfigOut(BaseModel):
    config: dict[str, Any]
    path: str


class ConfigPutIn(BaseModel):
    config: dict[str, Any]


class ConfigPutOut(BaseModel):
    ok: bool
    backup: str | None = None
    changed_keys: list[str] = Field(default_factory=list)
    config: dict[str, Any]


class StartRunRequest(BaseModel):
    kind: Literal["grok_supervisor", "register_sh"] = "grok_supervisor"
    product: Literal["grok", "mimo", "chatgpt"] = "grok"
    mode: Literal["ordinary", "residential"] = "ordinary"
    target: int = Field(default=100, ge=1, le=100_000)
    threads: int = Field(default=1, ge=1, le=32)
    tag: str = Field(default="batch_web", min_length=1, max_length=64)
    extra_env: dict[str, str] = Field(default_factory=dict)


class RunActionOut(BaseModel):
    ok: bool
    run: dict[str, Any] | None = None
    detail: str = ""


class LogsOut(BaseModel):
    path: str | None = None
    text: str = ""


class ImportResultOut(BaseModel):
    ok: bool
    detail: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    ok: bool
    username: str | None = None


class MeOut(BaseModel):
    authenticated: bool
    username: str | None = None
    auth_required: bool = True
    password_login_enabled: bool = True
    token_auth_enabled: bool = False
    users_configured: bool = False
