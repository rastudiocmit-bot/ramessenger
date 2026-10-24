"""HTTP routes for Yandex Cloud voice input and the workspace's AI agents."""

import base64
import binascii
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from center_messenger.accounts_api import identity
from center_messenger.ai_yandex import MAX_AUDIO_BYTES
from center_messenger.auth_security import AuthError, InputModel

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


class AiSettingsInput(InputModel):
    folder_id: str = Field(default="", max_length=50)
    api_key: str | None = Field(default=None, max_length=200)
    clear_key: bool = False
    voice_enabled: bool = False
    monthly_limit_rub: float | None = Field(default=None, ge=0, le=1_000_000)


class AgentInput(InputModel):
    name: str = Field(min_length=1, max_length=120)
    task: str = Field(default="voice_input", max_length=40)
    model: str = Field(max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    temperature: float = Field(default=0.2, ge=0, le=1)
    enabled: bool = True
    is_default: bool = False


class AgentTestInput(InputModel):
    text: str = Field(min_length=1, max_length=2000)
    model: str = Field(max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    temperature: float = Field(default=0.2, ge=0, le=1)


class TranscribeInput(InputModel):
    # 16 kHz, 16-bit, mono LPCM, base64: the API accepts JSON bodies only.
    audio: str = Field(min_length=4, max_length=(MAX_AUDIO_BYTES * 4) // 3 + 8)
    agent_id: UUID | None = None


def voice(request: Request):
    service = getattr(request.app.state, "ai_voice", None)
    if service is None:
        raise AuthError(503, "Сервис временно недоступен.")
    return service


@router.get("/ai/settings")
def ai_settings(workspace_id: UUID, request: Request):
    return voice(request).settings(identity(request), workspace_id)


@router.put("/ai/settings")
def save_ai_settings(workspace_id: UUID, model: AiSettingsInput, request: Request):
    data = model.model_dump()
    return voice(request).save_settings(identity(request, mutation=True), workspace_id, data)


@router.post("/ai/settings/verify")
def verify_ai_settings(workspace_id: UUID, request: Request):
    return voice(request).verify(identity(request, mutation=True), workspace_id)


@router.get("/ai/agents")
def ai_agents(workspace_id: UUID, request: Request):
    return voice(request).agents(identity(request), workspace_id)


@router.post("/ai/agents", status_code=201)
def create_ai_agent(workspace_id: UUID, model: AgentInput, request: Request):
    return voice(request).create_agent(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/ai/agents/test")
def test_ai_agent(workspace_id: UUID, model: AgentTestInput, request: Request):
    return voice(request).test_agent(identity(request, mutation=True), workspace_id, model.model_dump())


@router.put("/ai/agents/{agent_id}")
def update_ai_agent(workspace_id: UUID, agent_id: UUID, model: AgentInput, request: Request):
    return voice(request).update_agent(identity(request, mutation=True), workspace_id, agent_id, model.model_dump())


@router.delete("/ai/agents/{agent_id}", status_code=204)
def delete_ai_agent(workspace_id: UUID, agent_id: UUID, request: Request):
    voice(request).delete_agent(identity(request, mutation=True), workspace_id, agent_id)


@router.get("/ai/agents/{agent_id}/versions")
def ai_agent_versions(workspace_id: UUID, agent_id: UUID, request: Request):
    return voice(request).versions(identity(request), workspace_id, agent_id)


@router.post("/voice/transcribe")
def transcribe(workspace_id: UUID, model: TranscribeInput, request: Request):
    try:
        audio = base64.b64decode(model.audio, validate=True)
    except (binascii.Error, ValueError):
        raise AuthError(422, "Запись повреждена. Попробуйте записать ещё раз.") from None
    return voice(request).transcribe(identity(request, mutation=True), workspace_id, audio, model.agent_id)
