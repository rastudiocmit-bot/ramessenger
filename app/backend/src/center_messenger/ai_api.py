"""HTTP routes for Yandex Cloud voice input and the workspace's AI agents."""

import base64
import binascii
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from center_messenger.accounts_api import identity
from center_messenger.ai_yandex import MAX_AUDIO_BYTES
from center_messenger.ai_voice import VOICE_LOG
from center_messenger.auth_security import AuthError, InputModel

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


class AiSettingsInput(InputModel):
    folder_id: str = Field(default="", max_length=50)
    api_key: str | None = Field(default=None, max_length=200)
    clear_key: bool = False
    voice_enabled: bool = False
    monthly_limit_rub: float | None = Field(default=None, ge=0, le=1_000_000)
    voice_max_seconds: int | None = Field(default=None, ge=15, le=180, strict=True)


class DeepSeekInput(InputModel):
    api_key: str | None = Field(default=None, max_length=200)
    clear_key: bool = False


class GatewayInput(DeepSeekInput):
    base_url: str | None = Field(default=None, max_length=150)
    protocol: Literal["chat", "responses"] | None = None


class GatewayCheck(InputModel):
    capability: Literal["text", "recognition", "synthesis"] = "text"


class ActionRoutes(InputModel):
    recognition_provider: Literal["yandex", "mimo", "mimo_plan"]
    recognition_language: Literal["ru", "auto", "en", "zh"]
    synthesis_provider: Literal["disabled", "yandex", "mimo", "mimo_plan"] = "disabled"
    synthesis_voice: str = Field(default="Mia", max_length=30)


class SynthesisInput(InputModel):
    text: str = Field(min_length=1, max_length=500)


class AgentInput(InputModel):
    name: str = Field(min_length=1, max_length=120)
    task: str = Field(default="voice_input", max_length=40)
    model: str = Field(max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    temperature: float = Field(default=0.2, ge=0, le=1)
    enabled: bool = True
    is_default: bool = False


class AgentTestInput(InputModel):
    task: str = Field(default="voice_input", max_length=40)
    text: str = Field(min_length=1, max_length=2000)
    model: str = Field(max_length=80)
    prompt: str = Field(min_length=1, max_length=4000)
    temperature: float = Field(default=0.2, ge=0, le=1)


class TranscribeInput(InputModel):
    # 16 kHz, 16-bit, mono LPCM, base64: the API accepts JSON bodies only.
    audio: str = Field(min_length=4, max_length=(MAX_AUDIO_BYTES * 4) // 3 + 8)
    agent_id: UUID | None = None
    attempt_id: UUID | None = None
    background: bool = False


class VoiceDiagnosticInput(InputModel):
    attempt_id: UUID
    outcome: Literal["complete", "cancelled", "error"]
    stage: Literal["starting", "recording", "working", "preview"]
    code: Literal["none", "permission_denied", "microphone_busy", "microphone_missing",
                  "permission_timeout", "resume_timeout", "audio_interrupted", "no_frames",
                  "capture_stalled", "audio_short", "network", "timeout", "http_error", "browser_error"]
    audio_state: Literal["running", "suspended", "interrupted", "closed", "unknown"]
    elapsed_ms: int = Field(ge=0, le=300000, strict=True)
    first_frame_ms: int | None = Field(default=None, ge=0, le=120000, strict=True)
    captured_ms: int = Field(ge=0, le=180000, strict=True)
    processing_ms: int | None = Field(default=None, ge=0, le=300000, strict=True)


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
    data = model.model_dump(exclude_unset=True)
    return voice(request).save_settings(identity(request, mutation=True), workspace_id, data)


@router.post("/ai/settings/verify")
def verify_ai_settings(workspace_id: UUID, request: Request):
    return voice(request).verify(identity(request, mutation=True), workspace_id)


@router.put("/ai/deepseek")
def save_deepseek(workspace_id: UUID, model: DeepSeekInput, request: Request):
    return voice(request).save_deepseek(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/ai/deepseek/verify")
def verify_deepseek(workspace_id: UUID, request: Request):
    return voice(request).verify_deepseek(identity(request, mutation=True), workspace_id)


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
    actor = identity(request, mutation=True)
    try:
        audio = base64.b64decode(model.audio, validate=True)
    except (binascii.Error, ValueError):
        raise AuthError(422, "Запись повреждена. Попробуйте записать ещё раз.") from None
    started, status = monotonic(), 200
    try:
        if model.background:
            return voice(request).jobs.create(actor, workspace_id, audio, model.agent_id, attempt_id=model.attempt_id)
        return voice(request).transcribe(actor, workspace_id, audio, model.agent_id)
    except AuthError as error:
        status = error.status
        raise
    except Exception:
        status = 500
        raise
    finally:
        VOICE_LOG.info(
            "Voice request workspace=%s attempt=%s status=%s elapsed_ms=%s",
            workspace_id, model.attempt_id, status, round((monotonic() - started) * 1000),
        )


@router.get("/voice/jobs/{job_id}")
def voice_job(workspace_id: UUID, job_id: UUID, request: Request):
    return voice(request).jobs.get(identity(request), workspace_id, job_id)


@router.delete("/voice/jobs/{job_id}", status_code=204)
def cancel_voice_job(workspace_id: UUID, job_id: UUID, request: Request):
    voice(request).jobs.get(identity(request, mutation=True), workspace_id, job_id, cancel=True)


@router.post("/voice/diagnostic", status_code=204)
def voice_diagnostic(workspace_id: UUID, model: VoiceDiagnosticInput, request: Request):
    voice(request).diagnostic(identity(request, mutation=True), workspace_id, model.model_dump())


@router.put("/ai/gateways/routes")
def gateway_routes(workspace_id: UUID, model: ActionRoutes, request: Request):
    return voice(request).gateways.routes(identity(request, mutation=True), workspace_id, model.model_dump())


@router.put("/ai/gateways/{provider}")
def save_gateway(workspace_id: UUID, provider: str, model: GatewayInput, request: Request):
    return voice(request).gateways.save(identity(request, mutation=True), workspace_id, provider, model.model_dump(exclude_unset=True))


@router.post("/ai/gateways/{provider}/verify")
def verify_gateway(workspace_id: UUID, provider: str, model: GatewayCheck, request: Request):
    return voice(request).gateways.verify(identity(request, mutation=True), workspace_id, provider, model.capability)


@router.post("/ai/synthesize")
def synthesize(workspace_id: UUID, model: SynthesisInput, request: Request):
    return voice(request).gateways.synthesize(identity(request, mutation=True), workspace_id, model.text)
