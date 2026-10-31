"""Call identity, durable pagination, media bounds and process ownership. Offline."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import asyncio
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/backend/src'))
from center_messenger.auth_security import AuthError
from center_messenger.novofon_client import AuditError
from center_messenger.telephony_model import normalize,call_view
from center_messenger.telephony_import import TelephonyImport,page_complete,LIMIT
from center_messenger import telephony_audio as audio

NOW=datetime(2026,10,31,0,0,tzinfo=timezone.utc)


def source(**extra):
    return {'id':123,'communication_id':124,'start_time':'2026-10-31 03:00:00',
        'finish_time':'2026-10-31 03:01:00','direction':'in','is_lost':False,
        'contact_phone_number':'8 (999) 123-45-67','talk_duration':40,'total_duration':60,
        'employees':[{'employee_full_name':'Synthetic operator'}],
        'call_records':['synthetic-record-token'],**extra}


class CallModelTests(unittest.TestCase):
    def setUp(self):
        self.args={'workspace':uuid4(),'account':'17','connection':uuid4(),'zone':'Europe/Moscow'}

    def test_id_uses_account_and_source_not_phone_text_or_timestamp(self):
        a,_=normalize(source(),**self.args)
        b,_=normalize(source(contact_phone_number='+79990000000'),**self.args)
        c,_=normalize(source(id=125),**self.args)
        d,_=normalize(source(),**(self.args|{'account':'18'}))
        self.assertEqual(a['id'],b['id']);self.assertNotEqual(a['id'],c['id']);self.assertNotEqual(a['id'],d['id'])

    def test_phone_timezone_and_missed_facts_are_normalized(self):
        row,url=normalize(source(is_lost=True,talk_duration=0),**self.args)
        self.assertEqual(row['phone'],'+79991234567');self.assertEqual(row['started_at'],NOW)
        self.assertTrue(row['missed']);self.assertIn('/124/',url)

    def test_private_urls_and_numbers_are_absent_from_public_bubble(self):
        row,url=normalize(source(),**self.args)
        view=call_view(row|{'recording_ciphertext':'encrypted','version':1})
        self.assertTrue(view['recording_available']);self.assertNotIn(url,json.dumps(view));self.assertNotIn(row['phone'],json.dumps(view))
        self.assertFalse(call_view(row|{'recording_ciphertext':'encrypted','version':1},audio_available=False)['recording_available'])

    def test_invalid_facts_cannot_be_published_as_a_partial_page(self):
        for data in (source(id=None),source(direction='unknown'),source(is_lost='false'),
                     source(talk_duration=True),source(start_time='broken'),source(finish_time='2026-10-30 01:00:00')):
            with self.subTest(data=data),self.assertRaises(AuditError):normalize(data,**self.args)

    def test_unknown_or_hidden_phone_is_retained_without_a_false_match(self):
        row,_=normalize(source(contact_phone_number='anonymous'),**self.args)
        self.assertIsNone(row['phone'])


class PageTests(unittest.TestCase):
    def test_full_page_without_a_total_needs_another_request(self):
        self.assertFalse(page_complete([{}]*100,{},0));self.assertTrue(page_complete([],{},100))
        self.assertTrue(page_complete([{}]*100,{'total_items':100},0))

    def test_truncated_page_with_a_larger_total_does_not_complete_the_window(self):
        self.assertFalse(page_complete([{}]*30,{'total_items':100},0))
        with self.assertRaises(AuditError):page_complete([],{'total_items':100},30)

    def make_job(self):
        return {'workspace':uuid4(),'token':uuid4(),
            'settings':{'connection_id':uuid4(),'key_ciphertext':'encrypted','version':1},
            'state':{'settings_version':None,'account_id':None,'timezone':None,'recent':{},'archive':{},
                     'archive_done':False,'recent_due':NOW,'history_days':7,'poll_seconds':60,'attempts':0,'version':1}}

    def importer(self,rows,meta=None):
        client=Mock();client.read.side_effect=[([{'app_id':17,'timezone':'Europe/Moscow'}],{}),(rows,meta or {})]
        importer=TelephonyImport(Mock(),Mock(open=Mock(return_value={'api_key':'synthetic-api-token'})),lambda _:client,lambda:NOW)
        importer.commit=Mock();return importer,client

    def test_page_is_validated_before_a_cursor_or_calls_are_committed(self):
        importer,_=self.importer([source(),source(id=None)])
        with self.assertRaises(AuditError):importer.process(self.make_job())
        importer.commit.assert_not_called()

    def test_recent_calls_precede_archive_and_the_account_timezone_is_used(self):
        importer,client=self.importer([source()]);job=self.make_job();importer.process(job)
        params=client.read.call_args.kwargs
        self.assertEqual(params['date_till'],'2026-10-31 03:00:00');self.assertFalse(params['include_ongoing_calls'])
        self.assertEqual(len(importer.commit.call_args.args[6]),1)
        self.assertTrue(importer.commit.call_args.args[-1])
        self.assertFalse(importer.commit.call_args.args[5])

    def test_duplicate_source_ids_are_rejected_not_counted_twice(self):
        importer,_=self.importer([source(),source()])
        with self.assertRaisesRegex(AuditError,'duplicate'):importer.process(self.make_job())
        importer.commit.assert_not_called()


class AudioTests(unittest.TestCase):
    def test_range_is_bounded_and_multi_ranges_are_rejected(self):
        self.assertEqual(audio.requested_range('bytes=10-'),(10,10+audio.CHUNK-1))
        for value in ('bytes=1-0','bytes=-500','bytes=0-1,4-5','garbage'):
            with self.assertRaises(AuthError):audio.requested_range(value)

    def test_dns_private_redirect_targets_and_credentials_are_rejected(self):
        resolver=lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))]
        for url in ('https://app.novofon.ru/token','https://evil.test/token','https://user@app.novofon.ru/token'):
            with self.assertRaises(AuthError):audio.location(url,resolver)

    def response(self,body,status=206,**headers):
        response=Mock(status=status);data={'Content-Type':'audio/wav','Content-Length':str(len(body)),
            'Content-Range':f'bytes 0-{len(body)-1}/{len(body)}',**headers}
        response.getheader.side_effect=lambda key,default=None:data.get(key,default)
        response.read.return_value=body
        conn=Mock();conn.getresponse.return_value=response
        return conn

    def test_audio_reads_only_a_validated_range_and_hides_provider_urls(self):
        body=b'RIFF'+b'\0'*4+b'WAVE'+b'\0'*12;conn=self.response(body)
        resolver=lambda *a,**k:[(2,1,6,'',('93.184.216.34',443))]
        factory=Mock(return_value=conn)
        result,headers,status=audio.fetch('https://app.novofon.ru/system/media/wav/1/token/',resolver=resolver,connection_factory=factory)
        self.assertEqual(result,body);self.assertEqual(status,206);self.assertNotIn('token',str(headers))
        self.assertEqual(factory.call_args.args,('app.novofon.ru','93.184.216.34'))
        self.assertEqual(conn.request.call_args.kwargs['headers']['Range'],f'bytes=0-{audio.CHUNK-1}')
        conn.close.assert_called_once()

    def test_html_or_an_oversized_body_is_never_returned_as_audio(self):
        resolver=lambda *a,**k:[(2,1,6,'',('93.184.216.34',443))]
        for conn in (self.response(b'<html>not audio</html>'),self.response(b'RIFF',**{'Content-Length':str(audio.CHUNK+1)})):
            with self.assertRaises(AuthError):audio.fetch('https://app.novofon.ru/audio',resolver=resolver,connection_factory=lambda *a:conn)

    def test_safari_two_byte_discovery_range_is_not_mistaken_for_truncated_audio(self):
        resolver=lambda *a,**k:[(2,1,6,'',('93.184.216.34',443))]
        conn=self.response(b'RI',**{'Content-Range':'bytes 0-1/2000'})
        body,headers,status=audio.fetch('https://app.novofon.ru/audio','bytes=0-1',resolver=resolver,connection_factory=lambda *a:conn)
        self.assertEqual(body,b'RI');self.assertEqual(headers['Content-Range'],'bytes 0-1/2000')


class WorkerTests(unittest.TestCase):
    def worker(self):
        from center_messenger.config import Settings
        from center_messenger.telephony_worker import create_worker
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        key=Path(directory.name)/'key';key.write_text('a'*64)
        return create_worker(Settings(runtime_role='telephony',telephony_worker_key_file=key))

    def request(self,app,path,method='POST',key=None,payload=b'{}'):
        # Exercise the actual ASGI application and middleware, as the MAX
        # worker gate already does. Production deliberately has no httpx2
        # test dependency; do not import TestClient in the pinned image gate.
        async def request():
            headers=[(b'content-type',b'application/json'),(b'content-length',str(len(payload)).encode())]
            if key is not None:headers.append((b'x-cm-telephony-key',key.encode()))
            scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
                   'method':method,'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
                   'headers':headers,'client':('internal',1),'server':('telephony-worker',8002)}
            messages=[];delivered=False
            async def receive():
                nonlocal delivered
                if delivered:await asyncio.Event().wait()
                delivered=True
                return {'type':'http.request','body':payload,'more_body':False}
            async def send(message):messages.append(message)
            await asyncio.wait_for(app(scope,receive,send),timeout=5)
            starts=[m for m in messages if m['type']=='http.response.start']
            self.assertEqual(len(starts),1)
            self.assertTrue(any(m['type']=='http.response.body' and not m.get('more_body',False) for m in messages))
            return starts[0]['status'],b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
        return asyncio.run(request())

    def test_worker_scheduler_registers_only_telephony_not_max_or_crm(self):
        from center_messenger.telephony_worker import build_worker_loader
        loader=build_worker_loader(Mock())
        self.assertEqual(set(loader.steps),{'novofon.calls'})
        self.assertEqual(loader.limits,{'messages':1,'maintenance':1,'media':1})

    def test_worker_rejects_other_runtime_roles_and_untrusted_requests(self):
        from center_messenger.config import Settings
        from center_messenger.telephony_worker import create_worker
        for role in ('api','max','combined'):
            with self.subTest(role=role),self.assertRaises(ValueError):create_worker(Settings(runtime_role=role))
        app=self.worker();app.state.db_engine=Mock()
        for key in (None,'wrong-key','b'*64):
            for path,method in (('/recording','POST'),('/api/workspaces/unknown/chats','POST'),('/assets/chat.js','GET')):
                with self.subTest(path=path,key=key):
                    self.assertEqual(self.request(app,path,method,key)[0],403)
        app.state.db_engine.begin.assert_not_called()
        status,body=self.request(app,'/health/live','GET')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['role'],'telephony')
        self.assertEqual(self.request(app,'/health/ready','GET')[0],503)

    def test_authenticated_worker_has_no_public_chat_ai_or_static_routes(self):
        app=self.worker();app.state.db_engine=Mock()
        for path in ('/api/workspaces/unknown/chats','/api/ai/agents','/assets/chat.js','/docs','/openapi.json'):
            with self.subTest(path=path):self.assertEqual(self.request(app,path,'GET','a'*64)[0],404)
        app.state.db_engine.begin.assert_not_called()

    def test_authenticated_recording_request_keeps_schema_and_body_limits(self):
        app=self.worker();app.state.db_engine=Mock()
        self.assertEqual(self.request(app,'/recording',key='a'*64)[0],422)
        self.assertEqual(self.request(app,'/recording',key='a'*64,payload=b'{' + b' '*1024 + b'}')[0],413)
        app.state.db_engine.begin.assert_not_called()


if __name__=='__main__':unittest.main()
