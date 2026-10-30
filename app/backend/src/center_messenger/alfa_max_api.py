"""Authenticated endpoints for the separate AlfaCRM native-MAX web session."""

import re
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import EmailStr, Field, SecretStr, field_validator

from center_messenger.accounts_api import identity, service
from center_messenger.auth_security import InputModel

router = APIRouter(prefix="/api/workspaces/{workspace_id}/integrations/alfa-max")


class ConnectInput(InputModel):
    email: EmailStr
    password: SecretStr
    auto_reconnect: bool = True

    @field_validator("password", mode="before")
    @classmethod
    def password_size(cls, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("Invalid password")
        return value


class OTPInput(InputModel):
    code: str = Field(min_length=4, max_length=12)

    @field_validator("code")
    @classmethod
    def code_value(cls, value):
        code = re.sub(r"[\s-]", "", value)
        if not re.fullmatch(r"[0-9]{4,10}", code):
            raise ValueError("Invalid code")
        return code


@router.get("")
def status(workspace_id: UUID, request: Request):
    service(request)
    return request.app.state.alfa_max.status(identity(request), workspace_id)


@router.post("/connect")
def connect(workspace_id: UUID, model: ConnectInput, request: Request):
    service(request)
    if getattr(service(request).settings, 'runtime_role', 'combined') == 'api':
        from center_messenger.max_worker_control import forward_control
        return forward_control(request, workspace_id, 'connect', model)
    return request.app.state.alfa_max.connect(
        identity(request, mutation=True),
        workspace_id,
        str(model.email).casefold(),
        model.password,
        model.auto_reconnect,
    )


@router.post("/verify")
def verify(workspace_id: UUID, model: OTPInput, request: Request):
    service(request)
    if getattr(service(request).settings, 'runtime_role', 'combined') == 'api':
        from center_messenger.max_worker_control import forward_control
        return forward_control(request, workspace_id, 'verify', model)
    return request.app.state.alfa_max.verify_otp(
        identity(request, mutation=True), workspace_id, model.code
    )


class SyncInput(InputModel):
    branch_id: int | None = Field(default=None, gt=0, le=2147483647)
    direction: Literal["receive", "sent"] | None = None
    review_active: bool = False
    archive_paused: bool | None = None


@router.post("/sync", status_code=202)
def sync(workspace_id: UUID, request: Request, model: SyncInput | None = None):
    service(request)
    model = model or SyncInput()
    if ((model.branch_id is None) != (model.direction is None)
        or (model.review_active and model.branch_id is not None)
        or (model.archive_paused is not None and (model.review_active or model.branch_id is not None))):
        from center_messenger.auth_security import AuthError
        raise AuthError(422, "Выберите одно действие: журнал, проверку действующих или паузу архива.")
    return request.app.state.alfa_max.sync(identity(request, mutation=True), workspace_id,
        branch=model.branch_id, direction=model.direction, review_active=model.review_active,
        archive_paused=model.archive_paused)


@router.get("/sync-progress")
def sync_progress(workspace_id: UUID, request: Request, activity_only: bool = False):
    service(request)
    return request.app.state.alfa_max.progress(identity(request), workspace_id, activity_only=activity_only)


@router.delete("", status_code=204)
def disconnect(workspace_id: UUID, request: Request):
    service(request)
    request.app.state.alfa_max.disconnect(identity(request, mutation=True), workspace_id)
