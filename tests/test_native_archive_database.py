"""Isolated PostgreSQL acceptance for exact-source empty-archive recovery."""
import os
import json
from datetime import timedelta
import unittest
from uuid import uuid4
from sqlalchemy import text
import test_message_sync as fixture
from center_messenger.chat_api import ChatReader
from center_messenger.chat_store import refresh_unread


@unittest.skipUnless(os.environ.get('CM_TEST_DATABASE_URL'), 'isolated PostgreSQL URL required')
class ArchiveDatabaseTests(unittest.TestCase):
    setUpClass = fixture.MessageSyncTests.__dict__['setUpClass']
    tearDownClass = fixture.MessageSyncTests.__dict__['tearDownClass']
    setUp = fixture.MessageSyncTests.setUp
    tearDown = fixture.MessageSyncTests.tearDown
    add_customer = fixture.MessageSyncTests.add_customer
    customer_phone = fixture.MessageSyncTests.customer_phone
    row = fixture.MessageSyncTests.row
    project = fixture.MessageSyncTests.project

    def archived_source_fixture(self):
        current_contact = uuid4()
        self.add_customer(current_contact, 1, 8)
        self.customer_phone(8)
        for branch, record in ((1,'52'),(2,'53')):
            self.project(self.row(record, branch=branch, customer=7, body='Original incoming'), branch=branch)
            self.project(self.row(record, branch=branch, customer=8, body='Original incoming'), branch=branch)
        self.db.execute(text("UPDATE alfa_customers SET status='archive',phones='[]'::jsonb "
            "WHERE workspace_id=:w AND run_id=:r AND external_id=7"), {'w':self.workspace,'r':self.run})
        self.db.execute(text("UPDATE messages SET payload=payload||CAST(:payload AS jsonb) "
            "WHERE workspace_id=:w AND payload->>'customer_id'='7'"),
            {'w':self.workspace,'payload':json.dumps({'crm_recipient_stale':True})})
        old = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:c"),
            {'w':self.workspace,'c':self.contact}).scalar_one()
        new = self.db.execute(text("SELECT id FROM conversations WHERE workspace_id=:w AND contact_id=:c"),
            {'w':self.workspace,'c':current_contact}).scalar_one()
        command, outgoing = uuid4(),uuid4()
        self.db.execute(text("INSERT INTO send_commands(workspace_id,id,conversation_id,client_request_id,payload,payload_hash,"
            "state,last_error_code) VALUES(:w,:id,:c,:request,'{}',:hash,'failed','recipient_missing')"),
            {'w':self.workspace,'id':command,'c':old,'request':str(uuid4()),'hash':'a'*64})
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,send_command_id,direction,actor_type,text,payload,occurred_at) "
            "VALUES(:w,:id,:c,:command,'out','human','Saved failed reply',CAST(:payload AS jsonb),:at)"),
            {'w':self.workspace,'id':outgoing,'c':old,'command':command,
             'payload':json.dumps({'failed':True}),'at':self.at+timedelta(minutes=1)})
        refresh_unread(self.db,self.workspace,old)
        return old,new,current_contact,command

    def test_empty_archive_source_recovery_preserves_failed_sends_and_resolves_old_links(self):
        from center_messenger import native_archive_relink as archive
        old,new,contact,command = self.archived_source_fixture()
        original = dict(self.db.execute(text("SELECT id,text FROM messages WHERE workspace_id=:w"),
            {'w':self.workspace}).all())
        # Another conversation of this contact is not proof for another merge.
        other,other_message = uuid4(),uuid4()
        self.db.execute(text("INSERT INTO conversations(workspace_id,id,connection_id,contact_id,external_chat_id) "
            "VALUES(:w,:id,:channel,:contact,'separate-source')"),
            {'w':self.workspace,'id':other,'channel':self.channel,'contact':self.contact})
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,occurred_at) "
            "VALUES(:w,:id,:c,'in','contact','Unrelated history',:at)"),
            {'w':self.workspace,'id':other_message,'c':other,'at':self.at})
        self.assertTrue(archive.recover(self.db,self.service,self.workspace,self.channel,self.snapshot,old))
        saved = self.db.execute(text("SELECT conversation_id,state FROM send_commands WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':command}).one()
        self.assertEqual(tuple(saved),(new,'failed'))
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM messages WHERE workspace_id=:w AND conversation_id=:c"),
            {'w':self.workspace,'c':old}).scalar_one(),0)
        self.assertEqual(self.db.execute(text("SELECT conversation_id FROM messages WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':other_message}).scalar_one(),other)
        actual = dict(self.db.execute(text("SELECT id,text FROM messages WHERE workspace_id=:w"),{'w':self.workspace}).all())
        self.assertTrue(all(actual[key]==value for key,value in original.items()))
        self.assertEqual(archive.canonical_alias(self.db,self.workspace,self.channel,self.snapshot,self.contact),new)
        self.assertEqual(ChatReader._family_conversation(self.db,self.workspace,old)['contact_id'],contact)
        self.assertEqual(ChatReader._conversation(self.db,self.workspace,old)['id'],new)
        target = self.service.send_target(self.workspace,self.channel,new)
        self.assertEqual((target['branch'],target['customer']),(1,8))
        # Repetition cannot duplicate messages or retry the failed command.
        self.assertFalse(archive.recover(self.db,self.service,self.workspace,self.channel,self.snapshot,old))

    def test_empty_archive_source_recovery_does_not_move_unproven_history_or_unknown_sends(self):
        from center_messenger import native_archive_relink as archive
        old,new,contact,command = self.archived_source_fixture()
        self.db.execute(text("UPDATE send_commands SET state='unknown' WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':command})
        self.assertFalse(archive.recover(self.db,self.service,self.workspace,self.channel,self.snapshot,old))
        self.db.execute(text("UPDATE send_commands SET state='failed' WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':command})
        self.db.execute(text("INSERT INTO messages(workspace_id,id,conversation_id,direction,actor_type,text,payload,occurred_at) "
            "VALUES(:w,:id,:c,'in','contact','Unproven older incoming',CAST(:p AS jsonb),:at)"),
            {'w':self.workspace,'id':uuid4(),'c':old,'at':self.at-timedelta(minutes=1),
             'p':json.dumps({'branch_id':1,'crm_record_id':'51','customer_id':7,'customer_branch_id':1})})
        self.assertFalse(archive.recover(self.db,self.service,self.workspace,self.channel,self.snapshot,old))
        self.assertEqual(self.db.execute(text("SELECT conversation_id FROM send_commands WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':command}).scalar_one(),old)
        self.assertEqual(self.db.execute(text("SELECT count(*) FROM outbox_events WHERE workspace_id=:w AND topic=:t"),
            {'w':self.workspace,'t':archive.TOPIC}).scalar_one(),0)

    def test_empty_archive_redirect_is_invalidated_by_target_phone_change(self):
        from center_messenger import native_archive_relink as archive
        old,new,contact,command = self.archived_source_fixture()
        self.assertTrue(archive.recover(self.db,self.service,self.workspace,self.channel,self.snapshot,old))
        self.customer_phone(8,phone='+79002222222')
        self.assertIsNone(archive.canonical_alias(self.db,self.workspace,self.channel,self.snapshot,self.contact))
        self.assertEqual(self.db.execute(text("SELECT state FROM send_commands WHERE workspace_id=:w AND id=:id"),
            {'w':self.workspace,'id':command}).scalar_one(),'failed')

