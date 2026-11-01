"""Strict public/internal phone-care inputs. No credentials in envelopes."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import Field, field_validator, model_validator
from center_messenger.auth_security import InputModel


class CareInput(InputModel):
    @field_validator('*', mode='before')
    @classmethod
    def valid_text(cls, value):
        if isinstance(value, str):
            try: value.encode('utf-8')
            except UnicodeEncodeError: raise ValueError('Invalid text') from None
            if '\0' in value: raise ValueError('Invalid text')
        return value


class BoardInput(CareInput):
    status: Literal['active','open','working','waiting','resolved','dismissed','observed'] | None = None
    campaign: UUID | None = None
    interest: str | None = Field(default=None, max_length=30)
    branch: str | None = Field(default=None, max_length=30)
    source: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=30)
    since: datetime | None = None
    until: datetime | None = None
    offset: int = Field(default=0, ge=0, le=5000, strict=True)

    @field_validator('since', 'until')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None: raise ValueError('Timezone required')
        return value

    @model_validator(mode='after')
    def interval(self):
        if self.since and self.until and self.since >= self.until: raise ValueError('Invalid interval')
        return self


class CaseInput(CareInput):
    case_id: UUID


class PatchInput(CaseInput):
    expected_version: int = Field(ge=0, strict=True)
    status: Literal['open','working','waiting','resolved','dismissed'] | None = None
    stage: Literal['contact','offered','trial_booked','trial_attended','subscription','renewed'] | None = None
    journey: Literal['new','existing','return','unknown'] | None = None
    label: str | None = Field(default=None, max_length=200)
    branch: str | None = Field(default=None, max_length=30)
    source: str | None = Field(default=None, max_length=100)
    assignee_id: UUID | None = None
    due_at: datetime | None = None
    note: str = Field(default='', max_length=1000)
    dismiss_signal: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    reactivate_signal: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')

    @model_validator(mode='before')
    @classmethod
    def nullable_fields(cls, value):
        if isinstance(value, dict) and any(k in value and value[k] is None for k in ('status','stage','journey','label','branch','source')):
            raise ValueError('Null is not supported for this field')
        return value

    @field_validator('due_at')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None: raise ValueError('Timezone required')
        return value


class TaskInput(CaseInput):
    expected_version: int = Field(ge=0, strict=True)
    signal_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default='', max_length=2000)
    assignee_id: UUID | None = None
    due_at: datetime

    @field_validator('due_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None: raise ValueError('Timezone required')
        return value


class CampaignInput(CareInput):
    request_id: UUID
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['interest','waitlist','trial','renewal','return','new_group','schedule']


class SearchInput(CareInput):
    query: str = Field(min_length=3, max_length=100)


class LibraryInput(CareInput):
    offset: int = Field(default=0, ge=0, le=10000, strict=True)


class CampaignMemberInput(CareInput):
    campaign_id: UUID
    contact_id: UUID
    phone: str = Field(min_length=8, max_length=30)
    assignee_id: UUID | None = None
    due_at: datetime
    expected_version: int | None = Field(default=None, ge=0, strict=True)
    _aware = field_validator('due_at')(TaskInput.aware.__func__)


class TrainingInput(CaseInput):
    request_id: UUID
    signal_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    title: str = Field(min_length=1, max_length=160)
    category: Literal['makeup','course','price','conflict','agreement']
    good: str = Field(default='', max_length=1000)
    improve: str = Field(default='', max_length=1000)


class CallInput(CaseInput):
    call_id: UUID
    offset: int = Field(default=0, ge=0, le=5000000, strict=True)
    run_id: UUID | None = None
    recording: bool = Field(default=False, strict=True)


class HistoryInput(CaseInput):
    before_time: datetime | None = None
    before_id: UUID | None = None

    @model_validator(mode='after')
    def cursor(self):
        if (self.before_time is None) != (self.before_id is None): raise ValueError('Complete cursor required')
        if self.before_time and self.before_time.tzinfo is None: raise ValueError('Timezone required')
        return self


class SettingsInput(CareInput):
    expected_version: int = Field(ge=0, strict=True)
    enabled: bool = Field(strict=True)


class ActorInput(CareInput):
    user_id: UUID
    session_id: UUID
    auth_epoch: int = Field(ge=0, strict=True)


class WorkerInput(CareInput):
    contract: Literal['phone-family-care-v1']
    workspace: UUID
    actor: ActorInput
    action: Literal['board','detail','patch','task','campaign','search','add_to_campaign','training','library','call','history','digest','settings']
    data: dict


INPUTS = {'board': BoardInput, 'detail': CaseInput, 'patch': PatchInput, 'task': TaskInput,
          'campaign': CampaignInput, 'search': SearchInput, 'add_to_campaign': CampaignMemberInput,
          'training': TrainingInput, 'library': LibraryInput, 'call': CallInput, 'history': HistoryInput, 'digest': CareInput, 'settings': SettingsInput}
