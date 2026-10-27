"""Task due-time presets and settings normalisation (no database needed)."""

from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/backend/src"))

from center_messenger.tasks import STUDIO_DEFAULTS, _personal, _studio, presets  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


class PresetTests(unittest.TestCase):
    def at(self, *args):
        return datetime(*args, tzinfo=MSK)

    def test_end_of_day_today_during_working_hours(self):
        result = presets(self.at(2026, 10, 27, 14, 0), MSK, _studio({}))  # Tuesday
        self.assertEqual(result["end_of_day"], "2026-10-27T20:00:00+03:00")
        self.assertEqual(result["tomorrow_morning"], "2026-10-28T10:00:00+03:00")
        self.assertEqual(result["default"], result["end_of_day"])

    def test_late_evening_moves_to_next_working_day(self):
        result = presets(self.at(2026, 10, 27, 19, 45), MSK, _studio({}))
        self.assertEqual(result["end_of_day"], "2026-10-28T20:00:00+03:00")

    def test_saturday_evening_skips_sunday(self):
        result = presets(self.at(2026, 10, 31, 21, 0), MSK, _studio({}))  # Saturday, Sunday is off
        self.assertEqual(result["tomorrow_morning"], "2026-11-02T10:00:00+03:00")
        self.assertEqual(result["end_of_day"], "2026-11-02T20:00:00+03:00")

    def test_early_morning_means_this_morning(self):
        result = presets(self.at(2026, 10, 27, 7, 30), MSK, _studio({}))
        self.assertEqual(result["tomorrow_morning"], "2026-10-27T10:00:00+03:00")

    def test_two_hours_never_runs_past_the_working_day(self):
        studio = _studio({"default_due": "two_hours"})
        result = presets(self.at(2026, 10, 27, 19, 0), MSK, studio)
        self.assertEqual(result["two_hours"], "2026-10-27T20:00:00+03:00")
        self.assertEqual(result["default"], result["two_hours"])


class SettingsTests(unittest.TestCase):
    def test_studio_defaults_and_cleaning(self):
        self.assertEqual(_studio({}), STUDIO_DEFAULTS)
        cleaned = _studio({"day_start": "21:00", "day_end": "08:00", "work_days": [9, "x"], "visibility": "bad", "extra": 1})
        self.assertEqual((cleaned["day_start"], cleaned["day_end"]), ("10:00", "20:00"))
        self.assertEqual(cleaned["work_days"], STUDIO_DEFAULTS["work_days"])
        self.assertEqual(cleaned["visibility"], "all")
        self.assertNotIn("extra", cleaned)

    def test_personal_choices(self):
        self.assertEqual(_personal({"press": "menu", "reminders": 0})["press"], "menu")
        self.assertIs(_personal({"reminders": 0})["reminders"], False)
        self.assertEqual(_personal({"press": "nope"})["press"], "split")


if __name__ == "__main__":
    unittest.main()
