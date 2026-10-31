import logging
import asyncio
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from center_messenger import __version__
from center_messenger.accounts import Accounts
from center_messenger.accounts_api import router as accounts_router
from center_messenger.alfa_api import router as alfa_router
from center_messenger.alfa_client import AlfaClient
from center_messenger.alfa_max import AlfaMaxConnections
from center_messenger.alfa_max_api import router as alfa_max_router
from center_messenger.alfa_service import AlfaConnections
from center_messenger.alfa_sync import AlfaSync
from center_messenger.auth_security import AuthError, Passwords
from center_messenger.loader.bootstrap import build_loader
from center_messenger.chat_api import ChatReader
from center_messenger.chat_api import hooks as max_hooks
from center_messenger.chat_api import router as chat_router
from center_messenger.config import Settings
from center_messenger.connector_vault import ConnectorVault
from center_messenger.crm_api import DirectoryReader
from center_messenger.crm_api import router as directory_router
from center_messenger.database import SCHEMA_REVISION, database_is_ready, log_database_error, make_engine
from center_messenger.ai_api import router as ai_router
from center_messenger.tasks_api import router as tasks_router
from center_messenger.ai_voice import AiVoice
from center_messenger.tasks import Tasks
from center_messenger.http_security import SecurityMiddleware, StaticCompressionMiddleware
from center_messenger.max_client import MaxClient
from center_messenger.max_connections import MaxConnections
from center_messenger.messaging import Messaging
from center_messenger.novofon_api import router as novofon_router
from center_messenger.novofon_service import Novofon
from center_messenger.telephony_calls import TelephonyCalls
from center_messenger.telephony_api import router as telephony_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    engine_factory: Callable[[Settings], Engine] = make_engine,
    readiness_check: Callable[[Engine, str], bool] = database_is_ready,
    passwords: Passwords | None = None,
    alfa_client_factory=AlfaClient,
    max_client_factory=MaxClient,
) -> FastAPI:
    config = settings or Settings()
    if config.runtime_role in {'max','telephony'}:
        raise ValueError('MAX runtime must use center_messenger.max_worker:app')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        try:
            engine = engine_factory(config)
        except Exception:
            # Do not log DSNs, file contents, driver errors or configuration secrets.
            logger.error("Database configuration unavailable")
        app.state.db_engine = engine
        app.state.accounts = Accounts(engine, config, passwords) if engine is not None else None
        app.state.alfa = (
            AlfaConnections(
                app.state.accounts,
                ConnectorVault(config.connection_master_key_file),
                alfa_client_factory,
            )
            if engine is not None
            else None
        )
        app.state.alfa_sync = AlfaSync(app.state.alfa) if app.state.alfa else None
        app.state.ai_voice = AiVoice(app.state.alfa) if app.state.alfa else None
        app.state.novofon = Novofon(app.state.accounts, app.state.alfa.vault) if app.state.alfa else None
        app.state.tasks = Tasks(app.state.alfa, app.state.ai_voice) if app.state.alfa else None
        if app.state.alfa and config.runtime_role == 'api':
            from center_messenger.max_worker_control import BrowserDisabled
            app.state.alfa_max = AlfaMaxConnections(app.state.alfa, browser=BrowserDisabled())
        else:
            app.state.alfa_max = AlfaMaxConnections(app.state.alfa) if app.state.alfa else None
        app.state.directory = DirectoryReader(app.state.alfa) if app.state.alfa else None
        app.state.max_connections = (
            MaxConnections(app.state.alfa, max_client_factory) if app.state.alfa else None
        )
        app.state.messaging = (
            Messaging(app.state.max_connections, app.state.alfa_max)
            if app.state.max_connections
            else None
        )
        app.state.chats = ChatReader(app.state.messaging) if app.state.messaging else None
        app.state.telephony = TelephonyCalls(app.state.accounts,app.state.alfa.vault,app.state.alfa,app.state.chats,config) if app.state.alfa else None
        if app.state.novofon:
            app.state.novofon.calls = app.state.telephony
        app.state.loader = build_loader(app.state, config)
        app.state.loader.start()
        try:
            yield
        finally:
            if not app.state.loader.stop():
                logger.warning("Loader stopping with in-flight operations; durable leases remain authoritative")
            if app.state.ai_voice:
                await asyncio.to_thread(app.state.ai_voice.jobs.pool.shutdown, wait=True, cancel_futures=True)
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="Center Messenger",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(SecurityMiddleware, settings=config)
    app.add_middleware(StaticCompressionMiddleware)
    app.include_router(accounts_router)
    app.include_router(alfa_router)
    app.include_router(alfa_max_router)
    app.include_router(directory_router)
    app.include_router(chat_router)
    app.include_router(ai_router)
    app.include_router(tasks_router)
    app.include_router(novofon_router)
    app.include_router(telephony_router)
    app.include_router(max_hooks)
    web = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.exception_handler(AuthError)
    async def auth_error(_request, error):
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return JSONResponse({"detail": error.detail}, status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _error):
        # The default validation response can echo plaintext passwords and setup tokens.
        return JSONResponse({"detail": "Проверьте заполнение полей."}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def integrity_error(_request, _error):
        return JSONResponse(
            {"detail": "Данные уже изменились. Обновите страницу."}, status_code=409
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        route = getattr(_request.scope.get("route"), "path", "unknown")
        log_database_error(logger, _error, operation=f"{_request.method} {route}")
        return JSONResponse({"detail": "Сервис временно недоступен."}, status_code=503)

    @app.get("/", response_class=FileResponse)
    def index():
        return FileResponse(web / "index.html", media_type="text/html")

    @app.get("/api/system/info")
    def info():
        return {
            "service": "center-messenger",
            "version": __version__,
            "stage": "message_unread",
            "build": os.environ.get("CM_BUILD_SHA", "unknown")[:12],
            "schema": SCHEMA_REVISION,
            "runtime_role": config.runtime_role,
        }

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        engine = app.state.db_engine
        try:
            ok = engine is not None and readiness_check(engine, config.expected_db_role)
        except Exception:
            ok = False
        return JSONResponse(
            {"status": "ready" if ok else "not_ready"},
            status_code=200 if ok else 503,
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
