import hmac
from ipaddress import ip_address
from uuid import UUID

from fastapi import APIRouter, Request, Response

from center_messenger.accounts import Accounts
from center_messenger.auth_security import (
    AcceptInvitationInput,
    AuthError,
    BootstrapInput,
    Identity,
    InvitationInput,
    LoginInput,
    MemberRoleInput,
    MemberSectionsInput,
    PasswordInput,
    WorkspaceInput,
    csrf_token,
)

router = APIRouter(prefix="/api")


def service(request: Request) -> Accounts:
    accounts = request.app.state.accounts
    if accounts is None:
        raise AuthError(503, "Сервис временно недоступен. Попробуйте позже.")
    return accounts


def identity(request: Request, *, mutation=False) -> Identity:
    accounts = service(request)
    actor = accounts.authenticate(request.cookies.get(accounts.settings.cookie_name))
    if mutation:
        expected = csrf_token(actor.raw_session)
        supplied = request.headers.get("x-csrf-token", "")
        if len(supplied) != 64 or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise AuthError(403, "Обновите страницу и повторите действие.")
    return actor


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if service(request).settings.trust_proxy_ip:
        supplied = request.headers.get("x-real-ip", "")
        try:
            return str(ip_address(supplied))
        except ValueError:
            pass
    return peer


def signed_in(request: Request, response: Response, raw: str) -> dict:
    accounts = service(request)
    actor = accounts.authenticate(raw)
    config = accounts.settings
    response.set_cookie(
        config.cookie_name,
        raw,
        max_age=config.session_hours * 3600,
        httponly=True,
        secure=config.public_url.startswith("https://"),
        samesite="lax",
        path="/",
    )
    return {
        "user": actor.public_user(),
        "csrf": csrf_token(raw),
        "workspaces": accounts.workspaces(actor),
    }


def clear_cookie(request: Request, response: Response):
    config = service(request).settings
    response.delete_cookie(
        config.cookie_name,
        path="/",
        secure=config.public_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )


@router.get("/auth/status")
def status(request: Request):
    return service(request).status()


@router.post("/auth/setup")
def setup(model: BootstrapInput, request: Request, response: Response):
    raw = service(request).bootstrap(
        model, client_ip(request), request.headers.get("user-agent", "")
    )
    return signed_in(request, response, raw)


@router.post("/auth/login")
def login(model: LoginInput, request: Request, response: Response):
    raw = service(request).login(model, client_ip(request), request.headers.get("user-agent", ""))
    return signed_in(request, response, raw)


@router.get("/auth/session")
def session(request: Request):
    actor = identity(request)
    return {
        "user": actor.public_user(),
        "csrf": csrf_token(actor.raw_session),
        "workspaces": service(request).workspaces(actor),
    }


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    actor = identity(request, mutation=True)
    service(request).revoke(actor, actor.session_id)
    clear_cookie(request, response)


@router.post("/auth/logout-all", status_code=204)
def logout_all(request: Request, response: Response):
    service(request).revoke(identity(request, mutation=True))
    clear_cookie(request, response)


@router.get("/auth/sessions")
def sessions(request: Request):
    return {"sessions": service(request).sessions(identity(request))}


@router.post("/auth/sessions/{session_id}/revoke", status_code=204)
def revoke_session(session_id: UUID, request: Request, response: Response):
    actor = identity(request, mutation=True)
    service(request).revoke(actor, session_id)
    if actor.session_id == session_id:
        clear_cookie(request, response)


@router.post("/auth/password", status_code=204)
def password(model: PasswordInput, request: Request, response: Response):
    service(request).change_password(identity(request, mutation=True), model)
    clear_cookie(request, response)


@router.get("/workspaces")
def workspaces(request: Request):
    return {"workspaces": service(request).workspaces(identity(request))}


@router.get("/workspaces/{workspace_id}")
def workspace(workspace_id: UUID, request: Request):
    return service(request).workspace(identity(request), workspace_id)


@router.patch("/workspaces/{workspace_id}", status_code=204)
def rename(workspace_id: UUID, model: WorkspaceInput, request: Request):
    service(request).rename_workspace(identity(request, mutation=True), workspace_id, model.name)


@router.get("/workspaces/{workspace_id}/members")
def members(workspace_id: UUID, request: Request):
    return {"members": service(request).members(identity(request), workspace_id)}


@router.post("/workspaces/{workspace_id}/invitations")
def invite(workspace_id: UUID, model: InvitationInput, request: Request):
    return service(request).invite(identity(request, mutation=True), workspace_id, model)


@router.post("/workspaces/{workspace_id}/members/{user_id}/remove", status_code=204)
def remove_member(workspace_id: UUID, user_id: UUID, request: Request):
    service(request).remove_member(identity(request, mutation=True), workspace_id, user_id)


@router.post("/workspaces/{workspace_id}/members/{user_id}/sections", status_code=204)
def set_member_sections(
    workspace_id: UUID, user_id: UUID, model: MemberSectionsInput, request: Request
):
    service(request).set_member_sections(
        identity(request, mutation=True), workspace_id, user_id, model
    )


@router.post("/workspaces/{workspace_id}/members/{user_id}/role", status_code=204)
def set_member_role(
    workspace_id: UUID, user_id: UUID, model: MemberRoleInput, request: Request
):
    service(request).set_member_role(
        identity(request, mutation=True), workspace_id, user_id, model
    )


@router.post("/invitations/preview")
def preview_invitation(model: AcceptInvitationInput, request: Request):
    return service(request).preview_invitation(model.token.get_secret_value(), client_ip(request))


@router.post("/invitations/accept")
def accept_invitation(model: AcceptInvitationInput, request: Request, response: Response):
    accounts = service(request)
    actor = (
        identity(request, mutation=True)
        if accounts.settings.cookie_name in request.cookies
        else None
    )
    raw = accounts.accept_invitation(
        model, actor, client_ip(request), request.headers.get("user-agent", "")
    )
    return signed_in(request, response, raw)
