"""Offline evidence, tenancy-envelope and isolation checks; no customer traffic."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from pydantic import ValidationError
from center_messenger import phone_care_model as model
from center_messenger.auth_security import AuthError
from center_messenger.phone_care_bridge import CareBridge, MAX_RESPONSE
from center_messenger.phone_care_protocol import BoardInput, PatchInput, TaskInput, CampaignMemberInput, HistoryInput

NOW = datetime(2026,11,1,9,0,tzinfo=timezone.utc)


def call(**values):
    return {'id':uuid4(),'account_id':'17','phone':'+79990000000','direction':'in','missed':False,
            'started_at':NOW,'finished_at':NOW+timedelta(minutes=1),'talk_seconds':40,'total_seconds':60,
            'recording_ciphertext':'synthetic-encrypted'} | values


async def http(app, path, *, data=None, key=None, method='POST', headers=()):
    body=json.dumps(data or {}).encode();values=[(b'content-type',b'application/json'),(b'content-length',str(len(body)).encode()),*headers]
    if key: values.append((b'x-cm-telephony-key',key.encode()))
    scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1','method':method,
           'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'','headers':values,
           'client':('synthetic',1),'server':('test',80)}
    messages=[];delivered=False
    async def receive():
        nonlocal delivered
        if delivered:await asyncio.Event().wait()
        delivered=True;return {'type':'http.request','body':body,'more_body':False}
    async def send(message):messages.append(message)
    await asyncio.wait_for(app(scope,receive,send),5)
    return next(m['status'] for m in messages if m['type']=='http.response.start'),b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body').decode()


class EvidenceTests(unittest.TestCase):
    def test_phone_dedup_is_bound_to_workspace_account_and_hidden_call(self):
        w=uuid4();a=call();self.assertEqual(model.family_id(w,'17',a['phone']),model.family_id(w,'17',a['phone']))
        for other in (model.family_id(uuid4(),'17',a['phone']),model.family_id(w,'18',a['phone'])):
            self.assertNotEqual(model.family_id(w,'17',a['phone']),other)
        self.assertNotEqual(model.family_id(w,'17',None,uuid4()),model.family_id(w,'17',None,uuid4()))

    def test_literal_quote_time_and_unconfirmed_speaker_are_preserved(self):
        row=call();raw='[00:25] Канал 2 (роль не подтверждена): Я уточню свободные места по робототехнике и перезвоню завтра до обеда.'
        result=model.analyze(row,raw,run=uuid4())
        self.assertIn('robotics',result['interests'])
        for source in result['signals']:
            self.assertIn(source['quote'],raw);self.assertEqual(source['channel'],2);self.assertEqual(source['offset_seconds'],25)
            self.assertNotIn('assignee_id',source)
        self.assertEqual(next(x for x in result['signals'] if x['kind']=='callback')['suggested_due_at'],'2026-11-02T13:00:00+03:00')

    def test_negation_does_not_become_a_callback_promise(self):
        value=model.analyze(call(),'Я не перезвоню. Мы не уточним. Пробное занятие не согласовали.')
        self.assertFalse(any(x['kind'] in ('callback','followup') for x in value['signals']))
        self.assertEqual(next(x['kind'] for x in value['signals']),'trial')

    def test_paid_provider_is_not_required_and_no_role_or_trial_completion_is_inferred(self):
        row=call();w=uuid4();value=model.merge_case(None,row,model.analyze(row,'Хочу записаться на пробное занятие.'),workspace=w,now=NOW)
        self.assertEqual(value['stage'],'contact');self.assertEqual(value['journey'],'unknown')
        self.assertEqual(model.metrics([model.case_summary(value)],now=NOW)['new_journey_count'],0)

    def test_answered_call_resolves_contact_only_not_other_promises(self):
        w=uuid4();missed=call(missed=True,talk_seconds=0,started_at=NOW-timedelta(hours=2));one=model.merge_case(None,missed,model.analyze(missed),workspace=w,now=NOW)
        talking=call(started_at=NOW-timedelta(hours=1));two=model.merge_case(one,talking,model.analyze(talking,'Я уточню и отвечу завтра до обеда.'),workspace=w,now=NOW)
        self.assertEqual(next(x['state'] for x in two['signals'] if x['kind']=='missed'),'contacted')
        self.assertEqual(next(x['state'] for x in two['signals'] if x['kind']=='followup'),'proposed')
        self.assertEqual(two['status'],'open')

    def test_resolved_history_does_not_reopen_but_new_calls_do(self):
        w=uuid4();old=call(started_at=NOW-timedelta(days=2),missed=True,talk_seconds=0)
        value=model.merge_case(None,old,model.analyze(old),workspace=w,now=NOW)
        value.update(status='resolved',resolved_at=NOW.isoformat());value['signals'][0]['state']='reviewed'
        value=model.merge_case(value,old,model.analyze(old),workspace=w,now=NOW)
        self.assertEqual(value['status'],'resolved')
        new=call(started_at=NOW+timedelta(hours=1),missed=True,talk_seconds=0)
        value=model.merge_case(value,new,model.analyze(new),workspace=w,now=NOW+timedelta(hours=1))
        self.assertEqual(value['status'],'open')

    def test_reprocessing_replaces_proposals_but_keeps_confirmed_task_source(self):
        w=uuid4();row=call();one=model.merge_case(None,row,model.analyze(row,'Я перезвоню завтра.'),workspace=w,now=NOW)
        one['signals'][0].update(state='confirmed',task_id=str(uuid4()))
        new=model.merge_case(one,row,model.analyze(row,'Больше вопросов нет.'),workspace=w,now=NOW)
        self.assertEqual(new['signals'][0]['task_id'],one['signals'][0]['task_id'])
        fresh=model.merge_case(None,row,model.analyze(row,'Я перезвоню завтра.'),workspace=w,now=NOW)
        changed=model.merge_case(fresh,row,model.analyze(row,'Больше вопросов нет.'),workspace=w,now=NOW)
        self.assertEqual(changed['signals'],[])

    def test_repeat_calls_produce_review_not_an_assertion_that_issue_is_unresolved(self):
        w=uuid4();value=None
        for n in range(3):
            row=call(started_at=NOW+timedelta(hours=n));value=model.merge_case(value,row,model.analyze(row),workspace=w,now=NOW)
        signals=[x for x in value['signals'] if x['kind']=='repeat'];self.assertEqual(len(signals),1)
        self.assertEqual(signals[0]['quote'],'');self.assertIn('проверить',signals[0]['title'])

    def test_truncated_analysis_is_visible_and_has_no_fabricated_quote(self):
        result=model.analyze(call(),'Синтетический фрагмент',truncated=True)
        self.assertFalse(result['complete']);self.assertEqual(result['signals'][-1]['kind'],'review');self.assertEqual(result['signals'][-1]['quote'],'')

    def test_initial_archive_import_does_not_generate_urgent_callbacks_from_old_calls(self):
        row=call(started_at=NOW-timedelta(days=30),missed=True,talk_seconds=0)
        value=model.merge_case(None,row,model.analyze(row,'Я перезвоню завтра.'),workspace=uuid4(),now=NOW)
        self.assertEqual(value['status'],'observed');self.assertTrue(all(x['state']=='historical' for x in value['signals']))
        self.assertEqual(model.case_summary(value)['pending'],0)

    def test_conversion_counts_only_explicitly_reached_stages(self):
        value={'status':'resolved','journey':'new','stage':'subscription','reached_stages':['contact','subscription'],'reasons':[],'interests':[]}
        result=model.metrics([value],now=NOW)
        self.assertEqual(result['confirmed_stages']['subscription'],1);self.assertEqual(result['confirmed_stages']['trial_attended'],0)


class ProtocolTests(unittest.TestCase):
    def test_versions_dates_and_nullable_fields_are_strict(self):
        for kwargs in ({'expected_version':True},{'due_at':'2026-11-01T12:00:00'},{'status':None},{'label':'bad\0text'}):
            data={'case_id':uuid4(),'expected_version':1}|kwargs
            with self.assertRaises(ValidationError):PatchInput(**data)
        self.assertIsNone(PatchInput(case_id=uuid4(),expected_version=1,due_at=None).due_at)

    def test_page_cursors_and_intervals_cannot_be_ambiguous(self):
        with self.assertRaises(ValidationError):HistoryInput(case_id=uuid4(),before_id=uuid4())
        with self.assertRaises(ValidationError):BoardInput(since=NOW,until=NOW-timedelta(days=1))
        with self.assertRaises(ValidationError):BoardInput(offset=True)
        with self.assertRaises(ValidationError):CampaignMemberInput(campaign_id=uuid4(),contact_id=uuid4(),phone='+79990000000',due_at='2026-11-01T12:00:00')


class ServicePermissionTests(unittest.TestCase):
    def test_recording_audit_uses_a_writable_transaction_without_granting_observer_mutations(self):
        from center_messenger.phone_care import FamilyCare
        scopes=[];db=Mock();row={'id':uuid4(),'phone':'+79990000000','connected':True,'recording_ciphertext':'opaque'}
        db.execute.return_value.mappings.return_value.one_or_none.return_value=row
        @contextmanager
        def transaction(actor,**options):scopes.append(options);yield db
        accounts=SimpleNamespace(transaction=transaction,_workspace_chat=lambda *_:{'role':'observer','sections':['messages']})
        service=FamilyCare(accounts);service.case=Mock(return_value={'account_id':'17','phone':row['phone']})
        data={'case_id':uuid4(),'call_id':row['id'],'recording':True}
        self.assertEqual(service.call(SimpleNamespace(user_id=uuid4()),uuid4(),data),{'authorized':True})
        self.assertEqual(scopes[-1],{'snapshot':False})
        with self.assertRaises(AuthError):
            with service.enter(None,uuid4(),write=True):pass
        with service.enter(None,uuid4()):pass
        self.assertEqual(scopes[-1],{'snapshot':True})

    def test_staff_without_tasks_access_cannot_create_a_task_from_a_call(self):
        from center_messenger.phone_care import FamilyCare
        @contextmanager
        def transaction(*_,**__):yield Mock()
        service=FamilyCare(SimpleNamespace(transaction=transaction,_workspace_chat=lambda *_:{'role':'operator','sections':['messages']}))
        with self.assertRaises(AuthError) as raised:service.task(None,uuid4(),{})
        self.assertEqual(raised.exception.status,403)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        key=Path(self.temp.name)/'key';key.write_text('a'*64);self.config=SimpleNamespace(telephony_worker_key_file=key)
        self.actor=SimpleNamespace(user_id=uuid4(),session_id=uuid4(),auth_epoch=1)
        @contextmanager
        def transaction(*args,**kwargs):yield Mock()
        self.accounts=SimpleNamespace(transaction=transaction,_workspace_chat=Mock())

    def bridge(self,status=200,value=None,raw=None):
        conn=Mock();conn.getresponse.return_value=SimpleNamespace(status=status,read=Mock(return_value=raw if raw is not None else json.dumps(value or {'contract':'phone-family-care-v1','cases':[]}).encode()))
        return CareBridge(self.accounts,self.config,connection_factory=Mock(return_value=conn)),conn

    def test_actor_and_key_are_internal_and_responses_are_version_fenced(self):
        bridge,conn=self.bridge();result=bridge.call(self.actor,uuid4(),'board',{})
        body=json.loads(conn.request.call_args.args[2]);self.assertEqual(body['actor']['user_id'],str(self.actor.user_id));self.assertNotIn('a'*64,str(body))
        self.assertEqual(result['contract'],'phone-family-care-v1');conn.close.assert_called_once()
        bad,_=self.bridge(value={'contract':'other'})
        with self.assertRaises(AuthError):bad.call(self.actor,uuid4(),'board',{})

    def test_old_worker_missing_route_is_not_mistaken_for_an_empty_queue(self):
        bridge,_=self.bridge(404,{'detail':'Not Found'})
        with self.assertRaises(AuthError) as error:bridge.call(self.actor,uuid4(),'board',{})
        self.assertEqual(error.exception.status,503)

    def test_oversized_and_invalid_responses_fail_closed(self):
        for raw in (b'x'*(MAX_RESPONSE+1),b'<html>not json</html>'):
            bridge,conn=self.bridge(raw=raw)
            with self.assertRaises(AuthError):bridge.call(self.actor,uuid4(),'board',{})
            conn.close.assert_called_once()


class WorkerRouteTests(unittest.TestCase):
    def setUp(self):
        from center_messenger.config import Settings
        from center_messenger.telephony_worker import create_worker
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);key=Path(self.temp.name)/'key';key.write_text('a'*64)
        self.app=create_worker(Settings(runtime_role='telephony',telephony_worker_key_file=key));self.app.state.family_care=Mock();self.app.state.family_care.board.return_value={'cases':[]}
        self.payload={'contract':'phone-family-care-v1','workspace':str(uuid4()),'actor':{'user_id':str(uuid4()),'session_id':str(uuid4()),'auth_epoch':0},'action':'board','data':{}}

    def test_worker_rejects_browser_and_arbitrary_dispatch_before_service(self):
        for key in (None,'wrong'):
            self.assertEqual(asyncio.run(http(self.app,'/care',data=self.payload,key=key))[0],403)
        self.payload['action']='__dict__';status,body=asyncio.run(http(self.app,'/care',data=self.payload,key='a'*64))
        self.assertEqual(status,422);self.assertNotIn(self.payload['actor']['session_id'],body);self.app.state.family_care.board.assert_not_called()

    def test_private_request_runs_the_typed_action_with_session_identity(self):
        status,body=asyncio.run(http(self.app,'/care',data=self.payload,key='a'*64))
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['contract'],'phone-family-care-v1')
        actor=self.app.state.family_care.board.call_args.args[0];self.assertEqual(str(actor.session_id),self.payload['actor']['session_id'])

    def test_private_validation_never_echoes_source_text(self):
        self.payload.update(action='patch',data={'case_id':str(uuid4()),'expected_version':True,'note':'synthetic-private-quote'})
        status,body=asyncio.run(http(self.app,'/care',data=self.payload,key='a'*64))
        self.assertEqual(status,422);self.assertNotIn('synthetic-private-quote',body)
