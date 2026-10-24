"""AEAD credentials bound to their workspace, connection and payload format."""

import base64
import hashlib
import json
import re
import secrets
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from center_messenger.auth_security import AuthError


class ConnectorVault:
    def __init__(self, key_file: Path | None):
        self.key_file = key_file

    def _key(self):
        try:
            if self.key_file is None or not 48 <= self.key_file.stat().st_size <= 256:
                raise ValueError
            raw = self.key_file.read_bytes().strip()
            if re.fullmatch(rb"[A-Za-z0-9_-]{48,128}", raw) is None:
                raise ValueError
        except (OSError, ValueError):
            raise AuthError(503, "Ключ хранилища подключений недоступен на сервере.") from None
        return hashlib.sha256(b"center-messenger-connections-v1\0" + raw).digest()

    @property
    def ready(self):
        try:
            self._key()
            return True
        except AuthError:
            return False

    @staticmethod
    def _aad(workspace: UUID, connection: UUID, purpose="alfa"):
        if purpose not in {"alfa", "alfa-max-browser", "yandex-ai", "deepseek-ai"}:
            raise ValueError("Unsupported connector secret purpose")
        return f"center-messenger:{purpose}:v1:{workspace}:{connection}".encode()

    def seal(self, workspace: UUID, connection: UUID, payload: dict, *, purpose="alfa") -> str:
        nonce = secrets.token_bytes(12)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        encrypted = AESGCM(self._key()).encrypt(nonce, data, self._aad(workspace, connection, purpose))
        return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def open(self, workspace: UUID, connection: UUID, ciphertext: str, *, purpose="alfa") -> dict:
        key = self._key()
        try:
            if not ciphertext.startswith("v1.") or len(ciphertext) > 50000:
                raise ValueError
            raw = base64.b64decode(ciphertext[3:], altchars=b"-_", validate=True)
            payload = json.loads(
                AESGCM(key).decrypt(raw[:12], raw[12:], self._aad(workspace, connection, purpose))
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("api_key"), str):
                raise ValueError
            return payload
        except (ValueError, TypeError, InvalidTag):
            raise AuthError(
                503, "Не удалось прочитать ключ API. Проверьте ключ хранилища подключений."
            ) from None

    def seal_browser_state(self, workspace: UUID, connection: UUID, state: dict) -> str:
        nonce = secrets.token_bytes(12)
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) > 1_000_000:
            raise AuthError(422, "Сессия AlfaCRM слишком большая для безопасного хранения.")
        encrypted = AESGCM(self._key()).encrypt(
            nonce, data, self._aad(workspace, connection, "alfa-max-browser")
        )
        return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def open_browser_state(self, workspace: UUID, connection: UUID, ciphertext: str) -> dict:
        key = self._key()
        try:
            if not ciphertext.startswith("v1.") or len(ciphertext) > 1_400_000:
                raise ValueError
            raw = base64.b64decode(ciphertext[3:], altchars=b"-_", validate=True)
            state = json.loads(
                AESGCM(key).decrypt(
                    raw[:12], raw[12:], self._aad(workspace, connection, "alfa-max-browser")
                )
            )
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError
            return state
        except (ValueError, TypeError, InvalidTag):
            raise AuthError(503, "Не удалось открыть защищённую веб-сессию AlfaCRM.") from None
