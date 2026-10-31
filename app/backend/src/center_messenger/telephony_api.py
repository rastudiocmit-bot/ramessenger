from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field

from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError,InputModel

router=APIRouter(prefix='/api/workspaces/{workspace_id}')


class LoaderInput(InputModel):
    expected_version:int=Field(ge=0,strict=True)
    enabled:bool=True
    history_days:int=Field(default=7,ge=1,le=90,strict=True)
    poll_seconds:int=Field(default=60,ge=30,le=3600,strict=True)


def service(request):
    value=getattr(request.app.state,'telephony',None)
    if value is None:raise AuthError(503,'Телефония временно недоступна.')
    return value


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
