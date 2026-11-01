"""Pinned Novofon process: call facts, audio and telephone AI, no MAX/browser routes."""
import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI,Request,Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.accounts import Accounts,context
from center_messenger.auth_security import AuthError,InputModel
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import database_is_ready,make_engine
from center_messenger.loader.contracts import Capability,Manifest,Step
from center_messenger.loader.registry import Registry
from center_messenger.loader.scheduler import Scheduler
from center_messenger.telephony_audio import control_key,fetch
from center_messenger.telephony_import import TelephonyImport
from center_messenger.telephony_ai_worker import TelephoneWorker
from center_messenger.phone_care import FamilyCare
from center_messenger.phone_care_worker import CareWorker,install_routes


class AudioInput(InputModel):
    workspace:UUID
    call:UUID


def build_worker_loader(importer,processor=None,care=None):
    registry=Registry()
    steps=[Step('calls',Capability.CALLS,lambda op:importer.run_next(),lane='maintenance',active_delay=1,idle_delay=2,budget=45)]
    capabilities={Capability.CALLS}
    if processor is not None:
        capabilities.add(Capability.TRANSCRIBE)
        steps.append(Step('ai',Capability.TRANSCRIBE,lambda op:processor.run_next(),lane='media',active_delay=1,idle_delay=3,budget=180))
    if care is not None:
        steps.append(Step('care',Capability.CALLS,lambda op:care.run_next(),lane='maintenance',active_delay=0.5,idle_delay=10,budget=10))
    registry.register(Manifest('novofon',frozenset(capabilities)),lambda:steps)
    return Scheduler(registry,limits={'messages':1,'maintenance':1,'media':1})


def create_worker(settings=None,*,engine_factory=make_engine,readiness_check=database_is_ready):
    config=settings or Settings()
    if config.runtime_role!='telephony':raise ValueError('Telephony requires CM_RUNTIME_ROLE=telephony')
    key=control_key(config)

    @asynccontextmanager
    async def lifespan(app):
        engine=engine_factory(config)
        accounts=Accounts(engine,config)
        vault=ConnectorVault(config.connection_master_key_file)
        importer=TelephonyImport(accounts,vault)
        processor=TelephoneWorker(accounts,vault)
        loader=build_worker_loader(importer,processor,CareWorker(accounts))
        app.state.family_care=FamilyCare(accounts)
        app.state.db_engine,app.state.vault,app.state.loader=engine,vault,loader
        loader.start()
        try:yield
        finally:
            await asyncio.to_thread(loader.stop,timeout=30)
            processor.cache.close()
            engine.dispose()

    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

    @app.middleware('http')
    async def private(request,call_next):
        if request.url.path not in ('/health/live','/health/ready') and not hmac.compare_digest(request.headers.get('x-cm-telephony-key','').encode(),key.encode()):
            return JSONResponse({'detail':'Недоступно.'},status_code=403)
        limit=32768 if request.url.path=='/care' else 1024
        if request.headers.get('content-length','').isdigit() and int(request.headers['content-length'])>limit:
            return JSONResponse({'detail':'Слишком большой запрос.'},status_code=413)
        return await call_next(request)

    @app.exception_handler(AuthError)
    async def denied(request,error):
        return JSONResponse({'detail':error.detail},status_code=error.status)

    @app.exception_handler(SQLAlchemyError)
    async def db_error(request,error):
        return JSONResponse({'detail':'Сервис телефонии временно недоступен.'},status_code=503)

    @app.get('/health/live')
    def live():return {'status':'alive','role':'telephony','build':os.environ.get('CM_BUILD_SHA','unknown')[:12]}

    @app.get('/health/ready')
    def ready():
        try:
            loader=app.state.loader
            ok=(readiness_check(app.state.db_engine,config.expected_db_role) and
                set(loader.steps)=={'novofon.calls','novofon.ai','novofon.care'} and not loader.registration_errors and
                not loader.stopping.is_set() and bool(loader._threads) and all(t.is_alive() for t in loader._threads))
        except Exception:ok=False
        return JSONResponse({'status':'ready' if ok else 'not_ready','role':'telephony'},status_code=200 if ok else 503)

    @app.post('/recording')
    def recording(model:AudioInput,request:Request):
        with app.state.db_engine.begin() as db:
            context(db,workspace_id=model.workspace,membership_admin='true')
            row=db.execute(text("SELECT c.recording_ciphertext,c.connection_id FROM telephony_calls c JOIN telephony_state s ON s.workspace_id=c.workspace_id AND s.account_id=c.account_id WHERE c.workspace_id=:w AND c.id=:id AND s.connected AND c.recording_ciphertext IS NOT NULL"),{'w':model.workspace,'id':model.call}).mappings().one_or_none()
            if not row:raise AuthError(404,'Запись недоступна.')
            url=app.state.vault.open_recording(model.workspace,row['connection_id'],model.call,row['recording_ciphertext'])
        body,headers,status=fetch(url,request.headers.get('range'))
        return Response(body,status_code=status,headers=headers)
    install_routes(app)
    return app


def app():return create_worker()
