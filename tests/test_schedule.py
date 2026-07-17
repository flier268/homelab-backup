import datetime as dt
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import TZPATH, ZoneInfo

from homelab_backup import schedule


class CronTests(unittest.TestCase):
    def test_previous_and_next_occurrences(self):
        cases = (
            ('*/15 * * * *', dt.datetime(2026, 7, 13, 12, 7),
             dt.datetime(2026, 7, 13, 12, 0), dt.datetime(2026, 7, 13, 12, 15)),
            ('0 2,8,14,20 * * *', dt.datetime(2026, 7, 13, 12, 7),
             dt.datetime(2026, 7, 13, 8, 0), dt.datetime(2026, 7, 13, 14, 0)),
            ('0 0 * * mon', dt.datetime(2026, 7, 15, 12, 7),
             dt.datetime(2026, 7, 13, 0, 0), dt.datetime(2026, 7, 20, 0, 0)),
            ('0 0 29 2 *', dt.datetime(2024, 3, 1, 12, 7),
             dt.datetime(2024, 2, 29, 0, 0), dt.datetime(2028, 2, 29, 0, 0)),
        )
        for expression, now, previous, following in cases:
            with self.subTest(expression=expression):
                spec = schedule.parse_cron(expression)
                self.assertEqual(schedule.cron_previous(spec, now), previous)
                self.assertEqual(schedule.cron_next(spec, now), following)

    def test_sunday_ranges_accept_seven_and_names(self):
        now = dt.datetime(2026, 7, 13, 12, 0)
        expected = dt.datetime(2026, 7, 17, 0, 0)
        for expression in ('0 0 * * 5-7', '0 0 * * fri-sun'):
            with self.subTest(expression=expression):
                spec = schedule.parse_cron(expression)
                self.assertEqual(schedule.cron_next(spec, now), expected)

    def test_step_from_single_value_continues_to_field_maximum(self):
        spec = schedule.parse_cron('5/10 * * * *')
        self.assertEqual(
            schedule.cron_next(spec, dt.datetime(2026, 7, 14, 12, 6)),
            dt.datetime(2026, 7, 14, 12, 15),
        )

    def test_star_step_keeps_day_field_wildcard_semantics(self):
        spec = schedule.parse_cron('0 0 */1 * mon')
        tuesday = dt.datetime(2026, 7, 14, 0, 0)
        self.assertEqual(
            schedule.cron_next(spec, tuesday),
            dt.datetime(2026, 7, 20, 0, 0),
        )

    def test_spring_forward_fixed_job_runs_after_skipped_interval(self):
        timezone = ZoneInfo('America/New_York')
        spec = schedule.parse_cron('30 2 * * *')

        before_jump = dt.datetime(2026, 3, 8, 1, 55, tzinfo=timezone)
        after_jump = dt.datetime(2026, 3, 8, 3, 5, tzinfo=timezone)
        expected = dt.datetime(2026, 3, 8, 3, 0, tzinfo=timezone)

        self.assertEqual(schedule.cron_next(spec, before_jump), expected)
        self.assertEqual(schedule.cron_previous(spec, after_jump), expected)

    def test_fall_back_fixed_job_is_not_repeated(self):
        timezone = ZoneInfo('America/New_York')
        spec = schedule.parse_cron('30 1 * * *')
        second_hour = dt.datetime(2026, 11, 1, 1, 45, tzinfo=timezone, fold=1)
        first_occurrence = dt.datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0)

        self.assertEqual(
            schedule.cron_previous(spec, second_hour),
            first_occurrence,
        )

    def test_fall_back_frequent_job_keeps_second_fold(self):
        timezone = ZoneInfo('America/New_York')
        spec = schedule.parse_cron('*/5 * * * *')
        second_hour = dt.datetime(2026, 11, 1, 1, 45, tzinfo=timezone, fold=1)

        previous = schedule.cron_previous(spec, second_hour)
        following = schedule.cron_next(spec, second_hour)

        self.assertEqual(previous, second_hour)
        self.assertEqual(previous.fold, 1)
        self.assertEqual(
            following,
            dt.datetime(2026, 11, 1, 1, 50, tzinfo=timezone, fold=1),
        )
        self.assertEqual(following.fold, 1)


class LocalTimezoneTests(unittest.TestCase):
    def test_local_timezone_keeps_transition_rules(self):
        timezone_file = next(
            (
                Path(root) / 'America' / 'New_York'
                for root in TZPATH
                if (Path(root) / 'America' / 'New_York').is_file()
            ),
            None,
        )
        if timezone_file is None:
            self.skipTest('system zoneinfo database is unavailable')

        with mock.patch.object(schedule, 'LOCALTIME_PATH', timezone_file):
            schedule.local_timezone.cache_clear()
            timezone = schedule.local_timezone()
        schedule.local_timezone.cache_clear()

        winter = dt.datetime(2026, 1, 1, tzinfo=timezone)
        summer = dt.datetime(2026, 7, 1, tzinfo=timezone)
        self.assertNotEqual(winter.utcoffset(), summer.utcoffset())


if __name__ == '__main__':
    unittest.main()
