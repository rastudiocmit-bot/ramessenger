"""Authenticated endpoints for the separate AlfaCRM native-MAX web session."""

import re
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import EmailStr, Field, SecretStr, field_validator

from center_messenger.accounts_api import identity, service
from center_messenger.auth_security import InputModel

router = APIRouter(prefix="/api/workspaces/{workspace_id}/integrations/alfa-max")


class ConnectInput(InputModel):
    email: EmailStr
    password: SecretStr

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
    return request.app.state.alfa_max.connect(
        identity(request, mutation=True),
        workspace_id,
        str(model.email).casefold(),
        model.password,
    )


@router.post("/verify")
def verify(workspace_id: UUID, model: OTPInput, request: Request):
    service(request)
    return request.app.state.alfa_max.verify_otp(
        identity(request, mutation=True), workspace_id, model.code
    )


@router.post("/sync", status_code=202)
def sync(workspace_id: UUID, request: Request):
    service(request)
    return request.app.state.alfa_max.sync(identity(request, mutation=True), workspace_id)


@router.delete("", status_code=204)
def disconnect(workspace_id: UUID, request: Request):
    service(request)
    request.app.state.alfa_max.disconnect(identity(request, mutation=True), workspace_id)
