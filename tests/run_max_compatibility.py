"""Pinned MAX behaviour gates, run against each proposed image.

No credentials, live CRM calls, production database or customer sends.
--database additionally uses a fresh isolated PostgreSQL with synthetic data.
"""
import sys
import os
from pathlib import Path
import tempfile
import unittest

CASES = (
    'test_message_sync.HistoryCursorTests',
    'test_message_sync.PageTests',
    'test_message_sync.LoaderPlanningTests',
    'test_native_session.SessionPolicyTests',
    'test_compose_recipient.ComposeRecipientTests',
    'test_native_priority.PriorityPolicyTests',
    'test_native_send_runtime.NativeSendRuntimeTests',
    'test_native_send_runtime.InlineSendTests',
    'test_history_audit.SourceAuditTests',
    'test_history_audit.BrowserAuditContextTests',
    'test_history_audit.HistoryNavigationRecoveryTests',
    'test_native_relink',
    'test_native_archive_relink',
    'test_max_container',
    'test_novofon_diagnostic.DiagnosticTests',
    'test_novofon_connection.NovofonServiceTests',
    'test_telephony_calls',
    'test_novofon_quota.QuotaTests',
)

DATABASE_CASES = (
    'test_novofon_quota_database.QuotaDatabaseTests',
    'test_novofon_connection.NovofonDatabaseTests',
    'test_telephony_database.TelephonyDatabaseTests',
    'test_native_archive_database.ArchiveDatabaseTests.test_empty_archive_redirect_is_invalidated_by_target_phone_change',
    'test_native_archive_database.ArchiveDatabaseTests.test_empty_archive_source_recovery_does_not_move_unproven_history_or_unknown_sends',
    'test_native_archive_database.ArchiveDatabaseTests.test_empty_archive_source_recovery_preserves_failed_sends_and_resolves_old_links',
    'test_native_session.NativeSessionTests',
    'test_native_priority.PrioritySessionTests',
    *('test_message_sync.MessageSyncTests.' + name for name in (
        'test_repeated_page_is_idempotent',
        'test_same_text_and_timestamp_are_distinct_source_messages',
        'test_two_crm_rows_cannot_claim_one_local_send',
        'test_history_pages_use_crm_order_for_equal_timestamps',
        'test_worker_persists_progress_instead_of_restoring_old_cursor',
        'test_a_failed_page_does_not_discard_other_clients_or_advance_its_cursor',
        'test_complete_archive_for_every_client_survives_new_bursts_and_worker_restarts',
        'test_same_account_reauthentication_retains_history_and_pause_but_not_live_activity',
    )),
)


def main():
    database = '--database' in sys.argv[1:]
    if database and not os.environ.get('CM_TEST_DATABASE_URL'):
        raise RuntimeError('Isolated database URL required')
    # Tests normally use repository-root/app/backend/migrations. In a built
    # image backend is directly under /app; adapt only fixture file paths.
    import test_message_sync as fixture
    with tempfile.TemporaryDirectory(prefix='max-check-fixture-') as directory:
        if Path('/app/backend/migrations').is_dir():
            fixture.ROOT = Path(directory)
            (fixture.ROOT / 'app').mkdir()
            (fixture.ROOT / 'app/backend').symlink_to('/app/backend')
        suite = unittest.defaultTestLoader.loadTestsFromNames(DATABASE_CASES if database else CASES)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    # Import failures, empty suites and unexpectedly skipped mandatory checks
    # must block deployment rather than turning into a green no-op.
    if not result.wasSuccessful() or result.testsRun < (30 if database else 100) or result.skipped:
        return 1
    print(f'MAX COMPATIBILITY OK: {result.testsRun} tests, no skips', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
