import datetime as dt
import unittest

from homelab_backup import schedule


class CronTests(unittest.TestCase):
    @staticmethod
    def brute_previous(spec, now, search_minutes):
        candidate = now.replace(second=0, microsecond=0)
        for _ in range(search_minutes + 1):
            if schedule.cron_matches(spec, candidate):
                return candidate
            candidate -= dt.timedelta(minutes=1)
        return None

    @staticmethod
    def brute_next(spec, now, search_minutes):
        candidate = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(search_minutes):
            if schedule.cron_matches(spec, candidate):
                return candidate
            candidate += dt.timedelta(minutes=1)
        return None

    def test_optimized_search_matches_minute_by_minute_reference(self):
        cases = (
            ('*/15 * * * *', dt.datetime(2026, 7, 13, 12, 7), 24 * 60),
            ('0 2,8,14,20 * * *', dt.datetime(2026, 7, 13, 12, 7), 24 * 60),
            ('0 0 * * mon', dt.datetime(2026, 7, 15, 12, 7), 8 * 24 * 60),
            ('0 0 29 2 *', dt.datetime(2024, 3, 1, 12, 7), 3 * 24 * 60),
        )
        for expression, now, window in cases:
            with self.subTest(expression=expression):
                spec = schedule.parse_cron(expression)
                self.assertEqual(
                    schedule.cron_previous(spec, now, window),
                    self.brute_previous(spec, now, window),
                )
                self.assertEqual(
                    schedule.cron_next(spec, now, window),
                    self.brute_next(spec, now, window),
                )

    def test_sunday_ranges_accept_seven_and_names(self):
        self.assertEqual(
            schedule.parse_cron('0 0 * * 5-7')['weekdays'],
            {0, 5, 6},
        )
        self.assertEqual(
            schedule.parse_cron('0 0 * * fri-sun')['weekdays'],
            {0, 5, 6},
        )

    def test_step_from_single_value_continues_to_field_maximum(self):
        self.assertEqual(
            schedule.parse_cron('5/10 * * * *')['minutes'],
            {5, 15, 25, 35, 45, 55},
        )

    def test_star_step_keeps_day_field_wildcard_semantics(self):
        spec = schedule.parse_cron('0 0 */1 * mon')
        tuesday = dt.datetime(2026, 7, 14, 0, 0)
        self.assertFalse(schedule.cron_matches(spec, tuesday))


if __name__ == '__main__':
    unittest.main()
