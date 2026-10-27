"""Task due-time presets and settings normalisation (no database needed)."""

from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/backend/src"))

from center_messenger.tasks import STUDIO_DEFAULTS, _personal, _studio, presets  # noqa: E402
from center_messenger.task_speech import link, parse  # noqa: E402

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


class SpeechTests(unittest.TestCase):
    now = datetime(2026, 10, 27, 14, 0, tzinfo=MSK)  # Tuesday
    people = [{"id": "olga", "name": "Ольга Ким"}, {"id": "maria", "name": "Мария Петрова"}]

    def test_due_assignee_kind_and_title(self):
        result = parse("Записать Мишу на отработку в четверг, сделать до завтра до обеда, поручи Ольге",
                       self.now, MSK, _studio({}), self.people)
        self.assertEqual(result["title"], "Записать Мишу на отработку в четверг")
        self.assertEqual(result["due_at"].isoformat(), "2026-10-28T13:00:00+03:00")
        self.assertEqual((result["kind"], result["assignee_id"]), ("makeup", "olga"))

    def test_trigger_word_and_relative_time(self):
        result = parse("Задача: перезвонить Анне Смирновой через 2 часа", self.now, MSK, _studio({}))
        self.assertEqual(result["title"], "Перезвонить Анне Смирновой")
        self.assertEqual(result["due_at"].isoformat(), "2026-10-27T16:00:00+03:00")
        self.assertEqual(result["kind"], "call")

    def test_bare_hour_in_the_afternoon_and_weekday(self):
        self.assertEqual(parse("напомни проверить оплату в 5", self.now, MSK, _studio({}))["due_at"].hour, 17)
        friday = parse("в пятницу к вечеру уточнить расписание", self.now, MSK, _studio({}))["due_at"]
        self.assertEqual(friday.isoformat(), "2026-10-30T18:00:00+03:00")

    def test_no_time_leaves_the_default_to_the_studio(self):
        self.assertIsNone(parse("проверить остаток по абонементу", self.now, MSK, _studio({}))["due_at"])

    def test_link_prefers_unanswered_client_messages(self):
        newest_first = [{"direction": "in", "text": "И справку"}, {"direction": "in", "text": "Можно в четверг?"},
                        {"direction": "out", "text": "Добрый день"}]
        self.assertEqual([m["text"] for m in link(newest_first)], ["Можно в четверг?", "И справку"])
        promise = [{"direction": "out", "text": "Уточню у администратора"}, {"direction": "in", "text": "Спасибо"}]
        self.assertEqual(link(promise), [promise[0]])
        self.assertEqual(link([{"direction": "out", "text": "Хорошего дня"}]), [])


if __name__ == "__main__":
    unittest.main()
