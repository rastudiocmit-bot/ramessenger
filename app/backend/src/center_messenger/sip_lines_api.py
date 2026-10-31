from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr

from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError, InputModel
from center_messenger.sip_lines import SipLines

router = APIRouter(prefix='/api/workspaces/{workspace_id}/integrations/novofon/sip-lines')


class VersionInput(InputModel):
    expected_version: int = Field(ge=0, strict=True)


class LineInput(VersionInput):
    line_id: UUID | None = None
    member_id: UUID
    sip_number: str = Field(min_length=1, max_length=80)
    extension: str = Field(default='', max_length=12)
    server: str = Field(default='sip.novofon.ru', min_length=1, max_length=253)
    port: int = Field(default=5060, ge=1, le=65535, strict=True)
    password: SecretStr | None = Field(default=None, max_length=1024)
    enabled: bool = Field(default=True, strict=True)
    primary: bool = Field(default=False, strict=True)


def service(request):
    accounts, alfa = getattr(request.app.state, 'accounts', None), getattr(request.app.state, 'alfa', None)
    if accounts is None or alfa is None:
        raise AuthError(503, 'Настройки SIP-линий временно недоступны.')
    return SipLines(accounts, alfa.vault)


@router.get('')
def settings(workspace_id: UUID, request: Request):
    return service(request).settings(identity(request), workspace_id)


@router.get('/mine')
def mine(workspace_id: UUID, request: Request):
    return service(request).mine(identity(request), workspace_id)


@router.put('')
def save(workspace_id: UUID, model: LineInput, request: Request):
    data = model.model_dump(exclude={'password'})
    data['password'] = model.password.get_secret_value() if model.password is not None else None
    return service(request).save(identity(request, mutation=True), workspace_id, data)


@router.delete('/{line_id}')
def delete(workspace_id: UUID, line_id: UUID, model: VersionInput, request: Request):
    return service(request).delete(identity(request, mutation=True), workspace_id, line_id, model.expected_version)
