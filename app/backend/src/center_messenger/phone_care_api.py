"""Public routes authenticate/authorize, then delegate to the pinned phone worker."""
from uuid import UUID
from fastapi import APIRouter, Request, Response, Query
from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError
from center_messenger.phone_care_bridge import CareBridge
from center_messenger.phone_care_protocol import (BoardInput, CaseInput, PatchInput, TaskInput, CampaignInput,
    SearchInput, CampaignMemberInput, TrainingInput, CallInput, HistoryInput, SettingsInput)
from center_messenger.telephony_audio import WorkerAudio

router = APIRouter(prefix='/api/workspaces/{workspace_id}/phone-care')


def run(request, workspace, action, data=None, *, mutation=False):
    accounts = getattr(request.app.state, 'accounts', None)
    if accounts is None: raise AuthError(503, 'Телефония временно недоступна.')
    return CareBridge(accounts, request.app.state.phone_care_config).call(identity(request, mutation=mutation), workspace, action, data or {})


@router.post('/board')
def board(workspace_id:UUID, model:BoardInput, request:Request):
    return run(request, workspace_id, 'board', model.model_dump(exclude_none=True), mutation=True)


@router.get('/cases/{case_id}')
def detail(workspace_id:UUID, case_id:UUID, request:Request):
    return run(request, workspace_id, 'detail', {'case_id':str(case_id)})


@router.patch('/cases/{case_id}')
def patch(workspace_id:UUID, case_id:UUID, model:PatchInput, request:Request):
    if model.case_id != case_id: raise AuthError(422, 'Неверное обращение.')
    return run(request, workspace_id, 'patch', model.model_dump(exclude_unset=True), mutation=True)


@router.post('/tasks')
def task(workspace_id:UUID, model:TaskInput, request:Request):
    return run(request, workspace_id, 'task', model.model_dump(), mutation=True)


@router.post('/campaigns')
def campaign(workspace_id:UUID, model:CampaignInput, request:Request):
    return run(request, workspace_id, 'campaign', model.model_dump(), mutation=True)


@router.post('/contacts/search')
def search(workspace_id:UUID, model:SearchInput, request:Request):
    return run(request, workspace_id, 'search', model.model_dump(), mutation=True)


@router.post('/campaigns/members')
def campaign_member(workspace_id:UUID, model:CampaignMemberInput, request:Request):
    return run(request, workspace_id, 'add_to_campaign', model.model_dump(), mutation=True)


@router.get('/library')
def library(workspace_id:UUID, request:Request, offset:int=Query(default=0,ge=0,le=10000)):
    return run(request, workspace_id, 'library', {'offset':offset})


@router.post('/library')
def training(workspace_id:UUID, model:TrainingInput, request:Request):
    return run(request, workspace_id, 'training', model.model_dump(), mutation=True)


@router.get('/digest')
def digest(workspace_id:UUID, request:Request): return run(request, workspace_id, 'digest')


@router.put('/settings')
def settings(workspace_id:UUID, model:SettingsInput, request:Request):
    return run(request, workspace_id, 'settings', model.model_dump(), mutation=True)


@router.get('/cases/{case_id}/calls/{call_id}/transcript')
def transcript(workspace_id:UUID, case_id:UUID, call_id:UUID, request:Request, offset:int=Query(default=0,ge=0,le=5000000), run_id:UUID|None=None):
    data = CallInput(case_id=case_id, call_id=call_id, offset=offset, run_id=run_id)
    return run(request, workspace_id, 'call', data.model_dump())


@router.get('/cases/{case_id}/calls')
def history(workspace_id:UUID, case_id:UUID, request:Request, before_time:str|None=None, before_id:UUID|None=None):
    from pydantic import ValidationError
    try: data = HistoryInput(case_id=case_id, before_time=before_time, before_id=before_id)
    except ValidationError: raise AuthError(422, 'Неверный указатель страницы звонков.') from None
    return run(request, workspace_id, 'history', data.model_dump())


@router.get('/cases/{case_id}/calls/{call_id}/recording')
def recording(workspace_id:UUID, case_id:UUID, call_id:UUID, request:Request):
    run(request, workspace_id, 'call', {'case_id':str(case_id), 'call_id':str(call_id), 'recording':True})
    body, headers, status = WorkerAudio(request.app.state.phone_care_config).read(workspace_id, call_id, request.headers.get('range'))
    return Response(body, status_code=status, headers=headers | {'Cache-Control':'private, no-store'})
