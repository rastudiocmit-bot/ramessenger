"""HTTP routes for operator tasks made from chat messages."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import Field

from center_messenger.accounts_api import identity
from center_messenger.auth_security import AuthError, InputModel

router = APIRouter(prefix="/api/workspaces/{workspace_id}")
Kind = Literal["makeup", "subscription", "group", "call", "other"]


class TaskInput(InputModel):
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=2000)
    kind: Kind | None = None
    due_at: datetime | None = None
    conversation_id: UUID | None = None
    client_label: str = Field(default="", max_length=200)
    message_ids: list[UUID] = Field(default_factory=list, max_length=5)
    assignee_id: UUID | None = None
    origin: Literal["menu", "select", "voice", "ai_hint", "manual"] = "manual"


class TaskPatch(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=2000)
    kind: Kind | None = None
    due_at: datetime | None = None
    assignee_id: UUID | None = None
    message_ids: list[UUID] | None = Field(default=None, max_length=5)


class VoiceTaskInput(InputModel):
    text: str = Field(min_length=1, max_length=1000)
    conversation_id: UUID | None = None
    client_label: str = Field(default="", max_length=200)
    create: bool = True


class DraftInput(InputModel):
    conversation_id: UUID | None = None
    message_ids: list[UUID] = Field(default_factory=list, max_length=5)
    client_label: str = Field(default="", max_length=200)


class HintInput(InputModel):
    conversation_id: UUID
    text: str = Field(min_length=1, max_length=4000)


class SnoozeInput(InputModel):
    due_at: datetime


class StudioInput(InputModel):
    day_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    day_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    work_days: list[int] | None = Field(default=None, max_length=7)
    default_due: Literal["end_of_day", "two_hours", "tomorrow_morning"] | None = None
    visibility: Literal["all", "own"] | None = None
    alfa_copy: bool | None = None


class PersonalInput(InputModel):
    press: Literal["split", "voice", "menu"] | None = None
    voice_task: Literal["instant", "confirm"] | None = None
    reminders: bool | None = None
    hints: bool | None = None
    digest: bool | None = None


def tasks(request: Request):
    service = getattr(request.app.state, "tasks", None)
    if service is None:
        raise AuthError(503, "Сервис временно недоступен.")
    return service


def _given(model):
    return {key: value for key, value in model.model_dump(exclude_unset=True).items() if value is not None or key == "kind"}


@router.get("/tasks/options")
def task_context(workspace_id: UUID, request: Request):
    return tasks(request).context(identity(request), workspace_id)


@router.put("/tasks/settings")
def task_studio_settings(workspace_id: UUID, model: StudioInput, request: Request):
    return tasks(request).save_studio(identity(request, mutation=True), workspace_id, _given(model))


@router.put("/tasks/preferences")
def task_preferences(workspace_id: UUID, model: PersonalInput, request: Request):
    data = {key: value for key, value in model.model_dump(exclude_unset=True).items() if value is not None}
    return tasks(request).save_personal(identity(request, mutation=True), workspace_id, data)


@router.get("/tasks/summary")
def task_summary(workspace_id: UUID, request: Request):
    return tasks(request).summary(identity(request), workspace_id)


@router.get("/tasks")
def task_list(workspace_id: UUID, request: Request, scope: Literal["mine", "all"] = "mine",
              status: Literal["open", "closed"] = "open", conversation: UUID | None = None,
              limit: int = Query(default=200, ge=1, le=500)):
    return tasks(request).list(identity(request), workspace_id, scope=scope, status=status,
                               conversation=conversation, limit=limit)


@router.post("/tasks")
def task_create(workspace_id: UUID, model: TaskInput, request: Request):
    return tasks(request).create(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/tasks/voice")
def task_voice(workspace_id: UUID, model: VoiceTaskInput, request: Request):
    return tasks(request).voice(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/tasks/draft")
def task_draft(workspace_id: UUID, model: DraftInput, request: Request):
    return tasks(request).draft(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/tasks/hint")
def task_hint(workspace_id: UUID, model: HintInput, request: Request):
    return tasks(request).hint(identity(request, mutation=True), workspace_id, model.model_dump())


@router.post("/tasks/{task_id}/reply")
def task_reply(workspace_id: UUID, task_id: UUID, request: Request):
    return tasks(request).reply(identity(request, mutation=True), workspace_id, task_id)


@router.get("/tasks/{task_id}")
def task_history(workspace_id: UUID, task_id: UUID, request: Request):
    return tasks(request).history(identity(request), workspace_id, task_id)


@router.patch("/tasks/{task_id}")
def task_update(workspace_id: UUID, task_id: UUID, model: TaskPatch, request: Request):
    return tasks(request).update(identity(request, mutation=True), workspace_id, task_id, _given(model))


@router.post("/tasks/{task_id}/complete")
def task_complete(workspace_id: UUID, task_id: UUID, request: Request):
    return tasks(request).transition(identity(request, mutation=True), workspace_id, task_id, "complete")


@router.post("/tasks/{task_id}/cancel")
def task_cancel(workspace_id: UUID, task_id: UUID, request: Request):
    return tasks(request).transition(identity(request, mutation=True), workspace_id, task_id, "cancel")


@router.post("/tasks/{task_id}/reopen")
def task_reopen(workspace_id: UUID, task_id: UUID, request: Request):
    return tasks(request).transition(identity(request, mutation=True), workspace_id, task_id, "reopen")


@router.post("/tasks/{task_id}/snooze")
def task_snooze(workspace_id: UUID, task_id: UUID, model: SnoozeInput, request: Request):
    return tasks(request).transition(identity(request, mutation=True), workspace_id, task_id, "snooze", model.due_at)
