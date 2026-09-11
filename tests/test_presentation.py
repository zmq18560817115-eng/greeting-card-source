"""Review-field semantics: completed service years, delivery truth and readable errors."""
import unittest
from datetime import date

from app.dates import completed_years_since
from app import presentation


class ReviewPresentationTests(unittest.TestCase):
    def test_completed_years_use_full_anniversaries(self):
        for joined, day, expected in (
            ("2021-02-01", "2026-01-31", 4),
            ("2021-02-01", "2026-02-01", 5),
            ("2021-02-01", "2026-02-02", 5),
            ("2026-02-01", "2026-02-01", 0),
            ("2027-02-01", "2026-02-01", 0),
            (None, "2026-02-01", None),
            ("not-a-date", "2026-02-01", None),
            ("2021-02-01", "not-a-date", None),
        ):
            with self.subTest(joined=joined, day=day):
                self.assertEqual(completed_years_since(joined, day), expected)

    def test_leap_day_matches_existing_anniversary_policy(self):
        for day, expected in (("2025-02-27", 0), ("2025-02-28", 1),
                              ("2028-02-28", 3), ("2028-02-29", 4)):
            with self.subTest(day=day):
                self.assertEqual(completed_years_since(date(2024, 2, 29), day), expected)

    def test_unknown_birth_year_is_not_displayed_as_real_year(self):
        self.assertEqual(presentation.birthday_display("1896-02-29"), "02-29")
        self.assertEqual(presentation.birthday_display("1995-09-10"), "1995-09-10")
        self.assertIsNone(presentation.birthday_display(None))

    def test_delivery_never_confuses_planned_simulated_or_uncertain_with_sent(self):
        for status, state, sent, actual in (
            ("ready", "not_sent", False, None),
            ("confirmed", "not_sent", False, None),
            ("failed", "not_sent", False, None),
            ("pushing", "sending", None, None),
            ("delivery_unknown", "unknown", None, None),
            ("simulated", "simulated", False, None),
            ("pushed", "sent", True, "2026-09-10 12:00:00"),
        ):
            with self.subTest(status=status):
                event = {"status": status, "trigger_at": "2026-09-10 10:00:00",
                         "pushed_at": "2026-09-10 12:00:00" if status in ("pushed", "simulated", "delivery_unknown") else None}
                fields = presentation.delivery_fields(event)
                self.assertEqual((fields["delivery_state"], fields["is_pushed"], fields["actual_push_at"]), (state, sent, actual))
                self.assertEqual(fields["planned_push_at"], event["trigger_at"])

    def test_unknown_delivery_notice_does_not_suggest_blind_retry(self):
        event = {"status": "delivery_unknown", "last_error": "ReadTimeout: timed out"}
        before = dict(event)
        notice = presentation.event_notice(event, {})
        self.assertIn("可能已收到", notice)
        self.assertIn("暂勿重新发送", notice)
        self.assertEqual(event, before)

    def test_english_errors_get_chinese_guidance_and_known_chinese_stays_useful(self):
        cases = (("[飞书 99991672] Access denied. scopes are required", "权限"),
                 ("[飞书 123] invalid app secret", "应用凭据"),
                 ("HTTPSConnectionPool: Max retries exceeded", "网络"),
                 ("ReadTimeout: timed out", "先核实"),
                 ("Too many requests", "过于频繁"),
                 ("未知部门", "未知部门"),
                 ("[飞书 123] unexpected backend response", "技术详情"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertIn(expected, presentation.friendly_error(raw))


if __name__ == "__main__":
    unittest.main()
