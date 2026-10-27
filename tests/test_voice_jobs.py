"""Long voice jobs, scope checks and configured limits without paid calls."""
import unittest
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

from test_ai_voice import service, ACTOR, WS, FakeClient
from center_messenger.ai_yandex import silence
from center_messenger.auth_security import AuthError


class VoiceJobTests(unittest.TestCase):
    def test_speechkit_connection_check_does_not_invoke_a_yandex_text_model(self):
        voice, _ = self.make(role='owner',model='deepseek-flash',deepseek=True)
        self.assertTrue(voice.verify(ACTOR,WS)['ok'])
        self.assertEqual([c[0] for c in voice.client.calls],['recognize'])
        self.assertEqual(voice.deepseek.calls,[])

    def make(self, **kwargs):
        voice, db = service(**kwargs)
        self.addCleanup(lambda: voice.jobs.pool.shutdown(wait=True, cancel_futures=True))
        return voice, db

    def test_long_audio_uses_selected_deepseek_and_roundtrips_job_result(self):
        voice, db = self.make(model='deepseek-flash', deepseek=True)
        result = voice.jobs.create(ACTOR, WS, silence(180), None)
        key = UUID(result['job_id'])
        voice.jobs.jobs[key]['future'].result(timeout=3)
        answer = voice.jobs.get(ACTOR, WS, key)
        self.assertEqual(answer['status'], 'done')
        self.assertEqual(answer['result']['seconds'], 180)
        self.assertEqual([c[0] for c in voice.client.calls], ['recognize'])
        self.assertEqual(voice.deepseek.calls[0][1], 'deepseek-flash')
        self.assertEqual(db.usage[-1]['s'], 180)
        # Retain a completed result for a retried poll until explicit release.
        self.assertEqual(voice.jobs.get(ACTOR, WS, key), answer)
        voice.jobs.get(ACTOR, WS, key, cancel=True)
        with self.assertRaises(AuthError): voice.jobs.get(ACTOR, WS, key)

    def test_recognized_text_is_available_while_the_agent_is_still_processing(self):
        normalizing, release = Event(), Event()

        class SlowNormalizer(FakeClient):
            def complete(self, *args, **kwargs):
                normalizing.set()
                release.wait(3)
                return super().complete(*args, **kwargs)

        voice, _ = self.make(client=SlowNormalizer())
        try:
            key = UUID(voice.jobs.create(ACTOR, WS, silence(2), None)['job_id'])
            self.assertTrue(normalizing.wait(3))
            partial = voice.jobs.get(ACTOR, WS, key)
            self.assertEqual(partial, {'status': 'recognized', 'result': {'text': 'ну это мы завтра придём'}})
            release.set()
            voice.jobs.jobs[key]['future'].result(timeout=3)
            finished = voice.jobs.get(ACTOR, WS, key)
            self.assertEqual(finished['status'], 'done')
            self.assertEqual(finished['result']['text'], 'Мы придём завтра.')
        finally:
            release.set()

    def test_four_operators_can_start_recognition_without_waiting_for_two_slow_jobs(self):
        release, all_started = Event(), Event()
        from threading import Lock
        count, lock = [0], Lock()

        class SlowRecognition(FakeClient):
            def recognize(self, *args, **kwargs):
                with lock:
                    count[0] += 1
                    if count[0] == 4:
                        all_started.set()
                release.wait(3)
                return super().recognize(*args, **kwargs)

        voice, _ = self.make(client=SlowRecognition(), agent=False)
        try:
            actors = [SimpleNamespace(user_id=uuid4()) for _ in range(4)]
            jobs = [UUID(voice.jobs.create(actor, WS, silence(1), None)['job_id']) for actor in actors]
            self.assertTrue(all_started.wait(3))
            release.set()
            for actor, key in zip(actors, jobs):
                voice.jobs.jobs[key]['future'].result(timeout=3)
                self.assertEqual(voice.jobs.get(actor, WS, key)['status'], 'done')
        finally:
            release.set()

    def test_custom_duration_is_enforced_before_paid_calls_and_queueing(self):
        voice, db = self.make()
        db.settings['voice_max_seconds'] = 45
        for method in (lambda: voice.jobs.create(ACTOR, WS, silence(46), None),
                       lambda: voice.transcribe(ACTOR, WS, silence(46))):
            with self.assertRaises(AuthError) as error: method()
            self.assertEqual(error.exception.status, 413)
        self.assertFalse(voice.client.calls)
        self.assertFalse(voice.jobs.jobs)

    def test_duration_is_saved_and_omission_preserves_it(self):
        voice, db = self.make(role='owner')
        fields = {'folder_id':db.settings['folder_id'], 'voice_enabled':True, 'voice_max_seconds':75}
        self.assertEqual(voice.save_settings(ACTOR, WS, fields)['voice_max_seconds'],75)
        fields.pop('voice_max_seconds')
        self.assertEqual(voice.save_settings(ACTOR, WS, fields)['voice_max_seconds'],75)
        for value in (0,14,181,30.5,True):
            with self.subTest(value=value), self.assertRaises(AuthError):
                voice.save_settings(ACTOR, WS, fields | {'voice_max_seconds':value})

    def test_job_cannot_be_read_or_cancelled_by_another_user_or_workspace(self):
        voice, _ = self.make()
        result = voice.jobs.create(ACTOR, WS, silence(2), None)
        key = UUID(result['job_id'])
        voice.jobs.jobs[key]['future'].result(timeout=3)
        for actor, workspace in ((SimpleNamespace(user_id=uuid4()), WS), (ACTOR, uuid4())):
            for cancel in (False,True):
                with self.assertRaises(AuthError) as error: voice.jobs.get(actor,workspace,key,cancel=cancel)
                self.assertEqual(error.exception.status,404)
        self.assertEqual(voice.jobs.get(ACTOR,WS,key)['status'],'done')
        voice.accounts.role='observer'
        with self.assertRaises(AuthError) as error: voice.jobs.get(ACTOR,WS,key)
        self.assertEqual(error.exception.status,403)

    def test_cancel_discards_late_result_and_two_job_limit_is_bounded(self):
        gate=Event()
        class Slow(FakeClient):
            def recognize(self, *args, **kwargs):
                gate.wait(3)
                return super().recognize(*args, **kwargs)
        voice, _ = self.make(client=Slow())
        try:
            one=UUID(voice.jobs.create(ACTOR,WS,silence(1),None)['job_id'])
            two=UUID(voice.jobs.create(ACTOR,WS,silence(1),None)['job_id'])
            with self.assertRaises(AuthError) as error: voice.jobs.create(ACTOR,WS,silence(1),None)
            self.assertEqual(error.exception.status,429)
            future=voice.jobs.jobs[one]['future']
            voice.jobs.get(ACTOR,WS,one,cancel=True)
            gate.set();future.result(timeout=3)
            self.assertNotIn(one,voice.jobs.jobs)
            voice.jobs.jobs[two]['future'].result(timeout=3)
        finally: gate.set()
