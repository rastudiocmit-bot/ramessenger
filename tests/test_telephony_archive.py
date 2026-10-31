"""Night window, rolling age and persisted whole-call pacing."""
from datetime import datetime, timedelta, timezone
import unittest
from center_messenger.telephony_archive import ZONE, START, eligible, night, due, status


class ArchiveScheduleTests(unittest.TestCase):
    def local(self,value): return datetime.fromisoformat(value).replace(tzinfo=ZONE)
    def test_window_includes_midnight_but_excludes_eight_am(self):
        self.assertTrue(night(self.local('2026-10-31T00:00:00')))
        self.assertTrue(night(self.local('2026-10-31T07:59:59')))
        self.assertFalse(night(self.local('2026-10-31T08:00:00')))
        self.assertFalse(night(self.local('2026-10-31T23:59:59')))
    def test_utc_server_uses_moscow_midnight(self):
        self.assertTrue(night(datetime(2026,10,30,21,0,tzinfo=timezone.utc)))
        self.assertFalse(night(datetime(2026,10,31,5,0,tzinfo=timezone.utc)))
    def test_daytime_start_is_queued_for_the_next_midnight(self):
        self.assertEqual(due(self.local('2026-10-31T15:00:00')),self.local('2026-11-01T00:00:00'))
    def test_whole_call_starts_are_at_least_120_seconds_apart_after_restart(self):
        last=self.local('2026-10-31T01:00:00')
        self.assertEqual(due(last+timedelta(seconds=1),last),last+timedelta(seconds=120))
        self.assertEqual(due(last+timedelta(seconds=120),last),last+timedelta(seconds=120))
    def test_rate_boundary_never_extends_work_beyond_eight_am(self):
        last=self.local('2026-10-31T07:59:00')
        self.assertEqual(due(last+timedelta(seconds=5),last),self.local('2026-11-01T00:00:00'))
    def test_historical_range_is_inclusive_at_2020_start_and_strictly_older_than_a_week(self):
        now=self.local('2026-10-31T15:00:00')
        self.assertTrue(eligible(START,now));self.assertFalse(eligible(START-timedelta(seconds=1),now))
        self.assertTrue(eligible(now-timedelta(days=7,seconds=1),now))
        self.assertFalse(eligible(now-timedelta(days=7),now));self.assertFalse(eligible(now-timedelta(days=6),now))
    def test_pause_has_no_scheduled_start_and_the_ui_reports_the_exact_contract(self):
        value=status({'enabled':False},self.local('2026-10-31T15:00:00'))
        self.assertIsNone(value['next_at']);self.assertEqual(value['interval_seconds'],120)
        self.assertEqual((value['start_hour'],value['end_hour']),(0,8));self.assertEqual(value['timezone'],'Europe/Moscow')


if __name__=='__main__':unittest.main()
