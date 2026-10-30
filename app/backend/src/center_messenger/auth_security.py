"""Password and session primitives. No tokens or passwords are logged."""

import hashlib
import hmac
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator


class AuthError(Exception):
    def __init__(self, status: int, detail: str, *, retry_after: int | None = None):
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class Passwords:
    def __init__(self, hasher: PasswordHasher | None = None):
        # Two concurrent hashes bound memory usage within the API's 512 MiB limit.
        self.hasher = hasher or PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=1, type=Type.ID
        )
        self.slots = BoundedSemaphore(2)
        self.dummy = self.hasher.hash(secrets.token_urlsafe(32))

    @contextmanager
    def _slot(self):
        if not self.slots.acquire(timeout=1):
            raise AuthError(503, "Сервис занят. Повторите вход немного позже.", retry_after=2)
        try:
            yield
        finally:
            self.slots.release()

    def hash(self, password: str) -> str:
        with self._slot():
            return self.hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        with self._slot():
            try:
                return self.hasher.verify(encoded, password)
            except (VerificationError, InvalidHashError):
                return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def valid_token(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None


def csrf_token(raw_session: str) -> str:
    return hmac.new(raw_session.encode(), b"center-messenger-csrf-v1", hashlib.sha256).hexdigest()


def check_password(value: str) -> str:
    if not isinstance(value, str) or not 15 <= len(value) <= 128:
        raise ValueError("Use a password with 15 to 128 characters")
    if value.casefold() in {
        "passwordpassword",
        "passwordpasswordpassword",
        "123456789012345",
        "1234567890123456",
        "qwertyqwertyqwerty",
    }:
        raise ValueError("Choose a less predictable password")
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LoginInput(InputModel):
    email: EmailStr
    password: SecretStr

    @field_validator("password", mode="before")
    @classmethod
    def password_size(cls, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError("Invalid password length")
        return value


class BootstrapInput(LoginInput):
    setup_code: SecretStr
    display_name: str = Field(min_length=1, max_length=100)
    workspace_name: str = Field(min_length=1, max_length=160)

    @field_validator("password", mode="before")
    @classmethod
    def strong_password(cls, value):
        return check_password(value)

    @field_validator("setup_code", mode="before")
    @classmethod
    def code_size(cls, value):
        if not isinstance(value, str) or not 32 <= len(value.strip()) <= 128:
            raise ValueError("Invalid setup code")
        return value.strip()

    @field_validator("display_name", "workspace_name")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("Name is empty")
        return value.strip()


class PasswordInput(InputModel):
    current_password: SecretStr
    new_password: SecretStr

    @field_validator("current_password", mode="before")
    @classmethod
    def current_size(cls, value):
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError("Invalid password length")
        return value

    @field_validator("new_password", mode="before")
    @classmethod
    def new_size(cls, value):
        return check_password(value)


class WorkspaceInput(InputModel):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def non_empty(cls, value):
        if not value.strip():
            raise ValueError("Name is empty")
        return value.strip()


# Sidebar sections that a non-admin member may be granted.
MEMBER_SECTIONS = ("overview", "security", "messages", "tasks")


def can_send_messages(role, sections):
    return role in {"owner", "admin"} or (
        role in {"operator", "branch_manager"} and "messages" in (sections or [])
    )


class MemberSectionsInput(InputModel):
    sections: list[str] = Field(max_length=len(MEMBER_SECTIONS))

    @field_validator("sections")
    @classmethod
    def allowed_sections(cls, value):
        cleaned = sorted(set(value))
        if any(section not in MEMBER_SECTIONS for section in cleaned):
            raise ValueError("Unknown section")
        return cleaned


class MemberRoleInput(InputModel):
    role: str

    @field_validator("role")
    @classmethod
    def assignable_role(cls, value):
        if value not in {"admin", "branch_manager", "operator", "observer"}:
            raise ValueError("Invalid member role")
        return value


class MemberAccessInput(MemberRoleInput, MemberSectionsInput):
    @model_validator(mode="after")
    def effective_access(self):
        if self.role == "admin":
            self.sections = []  # Admin has full access, never a misleading partial grant.
        elif not self.sections:
            raise ValueError("Select at least one section")
        elif "tasks" in self.sections and "messages" not in self.sections:
            raise ValueError("Tasks require messages")
        return self


class InvitationInput(MemberAccessInput):
    email: EmailStr
    # Old invitation clients retain their previous effective default.
    sections: list[str] = Field(default_factory=lambda: ["overview", "security"], max_length=len(MEMBER_SECTIONS))


class AcceptInvitationInput(InputModel):
    token: SecretStr
    display_name: str = Field(default="", max_length=100)
    password: SecretStr | None = None

    @field_validator("token", mode="before")
    @classmethod
    def token_size(cls, value):
        if not isinstance(value, str) or not valid_token(value):
            raise ValueError("Invalid invitation token")
        return value

    @field_validator("password", mode="before")
    @classmethod
    def password_size(cls, value):
        return None if value is None else check_password(value)


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    session_id: UUID
    email: str
    display_name: str
    email_verified: bool
    auth_epoch: int
    raw_session: str = field(repr=False)

    def public_user(self) -> dict:
        return {
            "id": self.user_id,
            "email": self.email,
            "display_name": self.display_name,
            "email_verified": self.email_verified,
        }
