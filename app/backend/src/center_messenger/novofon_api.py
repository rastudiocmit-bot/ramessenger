from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr

from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError, InputModel

router = APIRouter(prefix='/api/workspaces/{workspace_id}/integrations/novofon')


class VersionInput(InputModel):
    expected_version: int = Field(ge=0, strict=True)


class SettingsInput(VersionInput):
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    support_secret: SecretStr | None = Field(default=None, max_length=4096)
    sample_hours: int = Field(default=24, ge=1, le=168, strict=True)


class CheckInput(VersionInput):
    probe_recording: bool = False


def service(request):
    value = getattr(request.app.state, 'novofon', None)
    if value is None:
        raise AuthError(503, 'Подключение Новофона временно недоступно.')
    return value


@router.get('')
def settings(workspace_id: UUID, request: Request):
    return service(request).settings(identity(request), workspace_id)


@router.put('')
def save(workspace_id: UUID, model: SettingsInput, request: Request):
    data = model.model_dump(exclude={'api_key', 'support_secret'})
    data['api_key'] = model.api_key.get_secret_value() if model.api_key is not None else None
    data['support_secret'] = model.support_secret.get_secret_value() if model.support_secret is not None else None
    return service(request).save(identity(request, mutation=True), workspace_id, data)


@router.post('/verify')
def verify(workspace_id: UUID, model: CheckInput, request: Request):
    return service(request).verify(identity(request, mutation=True), workspace_id,
                                   model.expected_version, model.probe_recording)


@router.delete('')
def disconnect(workspace_id: UUID, model: VersionInput, request: Request):
    return service(request).disconnect(identity(request, mutation=True), workspace_id, model.expected_version)
