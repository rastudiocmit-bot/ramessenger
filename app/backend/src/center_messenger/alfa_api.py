from uuid import UUID

from fastapi import APIRouter, Request

from center_messenger.accounts_api import identity, service
from center_messenger.alfa_client import AlfaSettingsInput

router = APIRouter(prefix="/api/workspaces/{workspace_id}/integrations/alfa")


@router.get("")
def status(workspace_id: UUID, request: Request):
    service(request)
    return request.app.state.alfa.status(identity(request), workspace_id)


@router.put("", status_code=204)
def save(workspace_id: UUID, model: AlfaSettingsInput, request: Request):
    service(request)
    request.app.state.alfa.save(identity(request, mutation=True), workspace_id, model)


@router.post("/check")
def check(workspace_id: UUID, request: Request):
    service(request)
    return request.app.state.alfa.check(identity(request, mutation=True), workspace_id)


@router.delete("", status_code=204)
def disconnect(workspace_id: UUID, request: Request):
    service(request)
    request.app.state.alfa.disconnect(identity(request, mutation=True), workspace_id)
