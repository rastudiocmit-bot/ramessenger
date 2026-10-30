"""Dedicated native MAX worker. No public chat/UI/AI routes or bot workers."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from center_messenger.accounts import Accounts
from center_messenger.alfa_service import AlfaConnections
from center_messenger.alfa_max import AlfaMaxConnections
from center_messenger.alfa_max_api import router
from center_messenger.auth_security import AuthError
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.database import database_is_ready, make_engine
from center_messenger.http_security import SecurityMiddleware
from center_messenger.loader.bootstrap import build_loader
from center_messenger.max_connections import MaxConnections
from center_messenger.max_worker_control import WorkerControlAuth, control_key
from center_messenger.messaging import Messaging

logger = logging.getLogger(__name__)


def loader_ready(loader):
    return (set(loader.steps) == {'alfa_max.history', 'alfa_max.outgoing'}
            and not loader.registration_errors and not loader.stopping.is_set()
            and bool(loader._threads) and all(t.is_alive() for t in loader._threads))


def create_worker(settings=None, *, engine_factory=make_engine, readiness_check=database_is_ready):
    config = settings or Settings()
    if config.runtime_role != 'max':
        raise ValueError('Dedicated MAX worker requires CM_RUNTIME_ROLE=max')
    control_key(config)

    @asynccontextmanager
    async def lifespan(app):
        engine = engine_factory(config)
        accounts = Accounts(engine, config)
        alfa = AlfaConnections(accounts, ConnectorVault(config.connection_master_key_file))
        native = AlfaMaxConnections(alfa)
        messaging = Messaging(MaxConnections(alfa), native)
        services = SimpleNamespace(accounts=accounts, alfa=alfa, alfa_sync=None,
                                   alfa_max=native, messaging=messaging)
        loader = build_loader(services, config)
        app.state.accounts, app.state.alfa_max = accounts, native
        app.state.db_engine, app.state.loader = engine, loader
        loader.start()
        try:
            yield
        finally:
            if not await asyncio.to_thread(loader.stop, timeout=30):
                logger.warning('MAX worker stopping with in-flight operations; durable leases remain authoritative')
            engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityMiddleware, settings=config)
    app.add_middleware(WorkerControlAuth, settings=config)
    app.include_router(router)

    @app.exception_handler(AuthError)
    async def auth_error(_request, error):
        headers = {'Retry-After': str(error.retry_after)} if error.retry_after else None
        return JSONResponse({'detail': error.detail}, status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({'detail': 'Проверьте заполнение полей.'}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def db_error(_request, _error):
        return JSONResponse({'detail': 'Сервис MAX временно недоступен.'}, status_code=503)

    @app.get('/health/live')
    def live():
        return {'status': 'alive', 'role': 'max', 'build': os.environ.get('CM_BUILD_SHA', 'unknown')[:12]}

    @app.get('/health/ready')
    def ready():
        try:
            ok = (readiness_check(app.state.db_engine, config.expected_db_role)
                  and loader_ready(app.state.loader))
        except Exception:
            ok = False
        return JSONResponse({'status': 'ready' if ok else 'not_ready', 'role': 'max'}, status_code=200 if ok else 503)

    return app


def app():
    """Uvicorn --factory avoids initializing credentials at module import."""
    return create_worker()
