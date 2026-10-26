"""Bounded voice diagnostics and request correlation; never accept message content."""
import base64
import types
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError
from center_messenger import ai_api
from center_messenger.auth_security import AuthError


class VoiceApiTest(unittest.TestCase):
    def test_background_submission_uses_same_mutation_identity_and_separate_poll_scope(self):
        actor,workspace,job=object(),uuid4(),uuid4()
        service=Mock()
        service.jobs.create.return_value={'job_id':str(job),'status':'pending'}
        request=types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(ai_voice=service)))
        with patch.object(ai_api,'identity',return_value=actor) as identity:
            reply=ai_api.transcribe(workspace,ai_api.TranscribeInput(audio='AAAA',background=True),request)
            self.assertEqual(reply['job_id'],str(job))
            identity.assert_called_once_with(request,mutation=True)
        service.transcribe.assert_not_called()
        with patch.object(ai_api,'identity',return_value=actor) as identity:
            ai_api.voice_job(workspace,job,request)
            identity.assert_called_once_with(request)
        service.jobs.get.assert_called_once_with(actor,workspace,job)
        with patch.object(ai_api,'identity',return_value=actor) as identity:
            ai_api.cancel_voice_job(workspace,job,request)
            identity.assert_called_once_with(request,mutation=True)
        service.jobs.get.assert_called_with(actor,workspace,job,cancel=True)

    def test_diagnostic_rejects_free_text_and_unbounded_or_non_integer_metrics(self):
        data = dict(attempt_id=uuid4(), outcome="error", stage="starting", code="no_frames", audio_state="running",
                    elapsed_ms=5000, first_frame_ms=None, captured_ms=0)
        self.assertEqual(ai_api.VoiceDiagnosticInput(**data).code, "no_frames")
        for invalid in [dict(text="private message"), dict(audio="private audio"),
                        dict(code="arbitrary log line"), dict(captured_ms=180001),
                        dict(elapsed_ms=-1), dict(elapsed_ms=True), dict(first_frame_ms=1.5)]:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ai_api.VoiceDiagnosticInput(**(data | invalid))

    def test_transcription_keeps_legacy_payload_and_correlates_optional_attempt(self):
        workspace, actor, attempt = uuid4(), object(), uuid4()
        service = Mock()
        service.transcribe.return_value = {"text": "private transcription"}
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(ai_voice=service)))
        audio = base64.b64encode(b"\0\0" * 16000).decode()
        for attempt_id in [None, attempt]:
            model = ai_api.TranscribeInput(audio=audio, attempt_id=attempt_id)
            with patch.object(ai_api, "identity", return_value=actor) as identity, self.assertLogs(ai_api.VOICE_LOG.name, "INFO") as logs:
                result = ai_api.transcribe(workspace, model, request)
            identity.assert_called_once_with(request, mutation=True)
            self.assertEqual(result["text"], "private transcription")
            self.assertNotIn("private transcription", " ".join(logs.output))
            self.assertIn("status=200", logs.output[0])
            self.assertIn(f"attempt={attempt_id}", logs.output[0])

    def test_error_status_is_logged_without_provider_secrets(self):
        service = Mock()
        service.transcribe.side_effect = AuthError(503, "provider detail must not enter logs")
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(ai_voice=service)))
        model = ai_api.TranscribeInput(audio="AAAA", attempt_id=uuid4())
        with patch.object(ai_api, "identity", return_value=object()), self.assertLogs(ai_api.VOICE_LOG.name, "INFO") as logs:
            with self.assertRaises(AuthError):
                ai_api.transcribe(uuid4(), model, request)
        self.assertIn("status=503", logs.output[0])
        self.assertNotIn("provider detail", logs.output[0])

    def test_diagnostic_requires_mutation_identity(self):
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(ai_voice=Mock())))
        model = ai_api.VoiceDiagnosticInput(attempt_id=uuid4(), outcome="cancelled", stage="recording", code="none", audio_state="running", elapsed_ms=100, captured_ms=0)
        with patch.object(ai_api, "identity", side_effect=AuthError(403, "csrf")) as identity:
            with self.assertRaises(AuthError):
                ai_api.voice_diagnostic(uuid4(), model, request)
        identity.assert_called_once_with(request, mutation=True)
        request.app.state.ai_voice.diagnostic.assert_not_called()
