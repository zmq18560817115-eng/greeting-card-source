"""Calendar rules are independent of Feishu, system time and production data."""
from datetime import date
import unittest

from app import dates, employees, pipeline


class GreetingDateTests(unittest.TestCase):
    def hits(self, employee, start, end=None):
        return dates.match_employee(employee, date.fromisoformat(start), date.fromisoformat(end or start))

    def test_birth_year_never_affects_trigger_or_produces_age(self):
        for birthday in ('09-11', '9-11', '1990-09-11', '2000-09-11', '2090-09-11', '1900-09-11'):
            with self.subTest(birthday=birthday):
                hits = self.hits({'birth_date': birthday}, '2026-09-10', '2026-09-12')
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]['event_date'], '2026-09-11')
                self.assertEqual(hits[0]['event_type'], 'birthday')
                self.assertIsNone(hits[0]['years'])
                self.assertTrue(hits[0]['trigger_at'].startswith('2026-09-11 '))

    def test_leap_day_birthday_does_not_move_to_february_28_or_march_1(self):
        for birthday in ('02-29', '2月29日', '2000-02-29', '1896-02-29'):
            with self.subTest(birthday=birthday):
                self.assertEqual(self.hits({'birth_date': birthday}, '2026-02-28', '2026-03-01'), [])
                hits = self.hits({'birth_date': birthday}, '2028-02-28', '2028-03-01')
                self.assertEqual([h['event_date'] for h in hits], ['2028-02-29'])

    def test_anniversary_requires_valid_complete_join_date_and_one_full_year(self):
        for joined in (None, '', '09-11', '9月11日', '2026-09', '202611', '2026-02-30', '2026-09-11', '2027-09-11'):
            with self.subTest(joined=joined):
                self.assertEqual(self.hits({'join_date': joined}, '2026-09-11'), [])
        for joined, years in (('2025-09-11', 1), ('2021-09-11', 5)):
            hits = self.hits({'join_date': joined}, '2026-09-10', '2026-09-12')
            self.assertEqual([(h['event_type'], h['event_date'], h['years']) for h in hits],
                             [('anniversary', '2026-09-11', years)])
        self.assertIsNone(dates.completed_years_since('09-11', '2026-09-11'))
        with self.assertRaises(employees.EmployeeError):
            employees._date('202611', 'join_date')

    def test_leap_day_anniversary_only_triggers_on_actual_anniversary_day(self):
        employee = {'join_date': '2024-02-29'}
        self.assertEqual(self.hits(employee, '2025-02-28', '2025-03-01'), [])
        hits = self.hits(employee, '2028-02-28', '2028-03-01')
        self.assertEqual([(h['event_date'], h['years']) for h in hits], [('2028-02-29', 4)])

    def test_year_boundary_matches_only_month_and_day(self):
        employee = {'birth_date': '12-31', 'join_date': '2024-01-01'}
        hits = self.hits(employee, '2026-12-30', '2027-01-02')
        self.assertEqual([(h['event_type'], h['event_date'], h['years']) for h in hits],
                         [('birthday', '2026-12-31', None), ('anniversary', '2027-01-01', 3)])

    def test_birthday_and_anniversary_can_share_date_without_sharing_years(self):
        hits = self.hits({'birth_date': '1995-09-11', 'join_date': '2023-09-11'}, '2026-09-11')
        self.assertEqual([(h['event_type'], h['years']) for h in hits], [('birthday', None), ('anniversary', 3)])

    def test_invalid_birthday_is_not_guessed(self):
        for birthday in (None, '', '02-30', '13-01', '00-11', '2026-09', 'not-a-date'):
            with self.subTest(birthday=birthday):
                self.assertEqual(self.hits({'birth_date': birthday}, '2026-01-01', '2026-12-31'), [])

    def test_poster_context_never_reuses_legacy_birthday_age(self):
        emp = {'name': '测试', 'birth_date': '1995-09-11', 'join_date': '2023-09-11'}
        context = pipeline.build_context({'event_type': 'birthday', 'event_date': '2026-09-11', 'years': 31}, emp)
        self.assertEqual(context['years'], '')
        self.assertEqual(context['birth_date'], '09-11')
        context = pipeline.build_context({'event_type': 'anniversary', 'event_date': '2026-09-11', 'years': 3}, emp)
        self.assertEqual((context['years'], context['join_date']), (3, '2023-09-11'))


if __name__ == '__main__':
    unittest.main()
