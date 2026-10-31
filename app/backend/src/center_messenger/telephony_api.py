from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError,InputModel
from center_messenger.telephony_agent import TelephonyAgent
from center_messenger.telephony_processing import Processing

router=APIRouter(prefix='/api/workspaces/{workspace_id}')


class LoaderInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    enabled:bool=True
    history_days:int=Field(default=7,ge=1,le=7,strict=True)
    poll_seconds:int=Field(default=60,ge=30,le=3600,strict=True)


def service(request):
    value=getattr(request.app.state,'telephony',None)
    if value is None:raise AuthError(503,'Телефония временно недоступна.')
    return value


class PhoneAgentInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    name:str=Field(min_length=1,max_length=120)
    model:str=Field(min_length=1,max_length=80)
    recognition_provider:str=Field(default='yandex',max_length=20)
    prompt:str=Field(min_length=1,max_length=4000)
    temperature:float=Field(default=0.1,ge=0,le=1)


class PhoneAgentTest(InputModel):
    expected_version:int=Field(ge=1,strict=True)
    text:str=Field(min_length=1,max_length=20000)


def agent_service(request):
    value=service(request)
    return TelephonyAgent(value.accounts,value.vault)


class ProcessingInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    auto_new:bool=False


class ArchiveInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    enabled:bool


class CallAIInput(InputModel):
    request_id:UUID
    reprocess:bool=False
    expected_run:UUID|None=None
    acknowledge_cost:bool=False


class SummaryInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    summary:str|None=Field(default=None,min_length=1,max_length=6000)


@router.get('/integrations/novofon/processing')
def processing_settings(workspace_id:UUID,request:Request):
    return Processing(service(request)).settings(identity(request),workspace_id)


@router.put('/integrations/novofon/processing')
def processing_configure(workspace_id:UUID,model:ProcessingInput,request:Request):
    return Processing(service(request)).configure(identity(request,mutation=True),workspace_id,model.model_dump())


@router.put('/integrations/novofon/archive')
def archive_configure(workspace_id:UUID,model:ArchiveInput,request:Request):
    return Processing(service(request)).configure_archive(identity(request,mutation=True),workspace_id,model.model_dump())


@router.post('/chats/{subject_id}/calls/{call_id}/ai')
@router.post('/directory/{subject_id}/calls/{call_id}/ai')
def request_call_ai(workspace_id:UUID,subject_id:UUID,call_id:UUID,model:CallAIInput,request:Request):
    return Processing(service(request)).request(identity(request,mutation=True),workspace_id,subject_id,call_id,model.model_dump(),contact='/directory/' in request.url.path)


@router.get('/chats/{subject_id}/calls/{call_id}/transcript')
@router.get('/directory/{subject_id}/calls/{call_id}/transcript')
def call_transcript(workspace_id:UUID,subject_id:UUID,call_id:UUID,request:Request,run:UUID|None=None,
                    after:str=Query(default='',max_length=24,pattern=r'^(asr:[0-9]{5}:[01])?$')):
    return Processing(service(request)).transcript(identity(request),workspace_id,subject_id,call_id,run,after,contact='/directory/' in request.url.path)


@router.put('/chats/{subject_id}/calls/{call_id}/summary')
@router.put('/directory/{subject_id}/calls/{call_id}/summary')
def edit_call_summary(workspace_id:UUID,subject_id:UUID,call_id:UUID,model:SummaryInput,request:Request):
    return Processing(service(request)).edit(identity(request,mutation=True),workspace_id,subject_id,call_id,model.model_dump(),contact='/directory/' in request.url.path)


@router.get('/integrations/novofon/agent')
def phone_agent(workspace_id:UUID,request:Request):
    return agent_service(request).settings(identity(request),workspace_id)


@router.put('/integrations/novofon/agent')
def save_phone_agent(workspace_id:UUID,model:PhoneAgentInput,request:Request):
    return agent_service(request).save(identity(request,mutation=True),workspace_id,model.model_dump())


@router.post('/integrations/novofon/agent/test')
def test_phone_agent(workspace_id:UUID,model:PhoneAgentTest,request:Request):
    return agent_service(request).test(identity(request,mutation=True),workspace_id,model.model_dump())


@router.get('/integrations/novofon/loader')
def settings(workspace_id:UUID,request:Request):
    return service(request).settings(identity(request),workspace_id)


@router.put('/integrations/novofon/loader')
def configure(workspace_id:UUID,model:LoaderInput,request:Request):
    return service(request).configure(identity(request,mutation=True),workspace_id,model.model_dump())


@router.get('/chats/{conversation_id}/calls')
def calls(workspace_id:UUID,conversation_id:UUID,request:Request,
          before_time:datetime|None=None,before_id:UUID|None=None,limit:int=Query(default=40,ge=1,le=100)):
    if before_time is not None and before_time.tzinfo is None:raise AuthError(422,'Укажите часовой пояс даты.')
    return service(request).listing(identity(request),workspace_id,conversation_id,
        before_time=before_time,before_id=before_id,limit=limit)


@router.get('/chats/{conversation_id}/calls/{call_id}/recording')
def recording(workspace_id:UUID,conversation_id:UUID,call_id:UUID,request:Request):
    body,headers,status=service(request).recording(identity(request),workspace_id,conversation_id,call_id,request.headers.get('range'))
    return Response(body,status_code=status,headers=headers | {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


@router.get('/directory/{contact_id}/calls')
def contact_calls(workspace_id:UUID,contact_id:UUID,request:Request,
                  before_time:datetime|None=None,before_id:UUID|None=None,limit:int=Query(default=40,ge=1,le=100)):
    if before_time is not None and before_time.tzinfo is None:raise AuthError(422,'Укажите часовой пояс даты.')
    return service(request).listing(identity(request),workspace_id,contact_id,
        before_time=before_time,before_id=before_id,limit=limit,contact=True)


@router.get('/directory/{contact_id}/calls/{call_id}/recording')
def contact_recording(workspace_id:UUID,contact_id:UUID,call_id:UUID,request:Request):
    body,headers,status=service(request).recording(identity(request),workspace_id,contact_id,call_id,request.headers.get('range'),contact=True)
    return Response(body,status_code=status,headers=headers | {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
