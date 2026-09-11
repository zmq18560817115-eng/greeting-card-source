"""Personal review and delivery use isolated records and mocked Feishu only."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import compose, db, employees, feishu, main, pipeline, push


class PersonReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = {"canvas": {"width": 200, "height": 300}, "vars": {}, "templates": {
            key: {"ai": {"enabled": False}, "layers": []} for key in ("birthday", "anniversary")}}
        changes = [(db, "DB_PATH", self.root / "review.db"), (pipeline, "CARD_DIR", self.root / "cards"),
                   (main, "ADMIN_TOKEN", ""), (feishu, "FEISHU_APP_ID", "cli_person_test"),
                   (push, "DRY_RUN", False), (pipeline, "_async_tasks", {})]
        for target, attr, value in changes:
            handle = patch.object(target, attr, value)
            handle.start()
            self.addCleanup(handle.stop)
        self.mock("requests.sessions.Session.request", side_effect=AssertionError("No real network"))
        self.mock("app.main.now", return_value="2026-09-11 12:00:00")
        self.mock("app.push.now", return_value="2026-09-11 12:00:00")
        self.mock("app.compose.load_config", return_value=cfg)
        self.mock("app.pipeline._async_pool.submit", side_effect=lambda fn, *args: fn(*args))
        self.upload = self.mock("app.feishu.upload_image", return_value="image_person_test")
        self.notice = self.mock("app.feishu.send_notice", return_value="notice_person_test")
        self.full = self.mock("app.feishu.send_full_card", return_value="full_person_test")
        db.init_db()
        self.client = TestClient(main.app)
        self.first = employees.save_employee({"name": "个人甲", "department": "研发", "feishu_open_id": "ou_first",
                                             "join_date": "2023-09-11", "birth_date": "09-11"})["employee"]
        self.second = employees.save_employee({"name": "个人乙", "department": "研发", "feishu_open_id": "ou_second",
                                              "join_date": "2024-09-11", "birth_date": "1990-09-11"})["employee"]
        remote = [{"name": emp["name"], "open_id": emp["feishu_open_id"], "status": {"is_resigned": False}}
                  for emp in (self.first, self.second)]
        self.mock("app.feishu.list_scope_users", return_value=remote)
        self.mock("app.feishu.get_user", side_effect=lambda oid: next(row for row in remote if row["open_id"] == oid))

    def mock(self, target, **kwargs):
        handle = patch(target, **kwargs)
        result = handle.start()
        self.addCleanup(handle.stop)
        return result

    def generate(self, employee=None):
        return self.client.post(f"/api/employees/{(employee or self.first)['id']}/generate-today", json={})

    def test_person_generation_never_picks_up_another_employees_pending_events(self):
        pipeline.scan_cycle((date(2026, 9, 11), date(2026, 9, 11)), employee_id=self.second["id"])
        before = db.query("SELECT * FROM events WHERE employee_id=?", (self.second["id"],))
        response = self.generate()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["created"], result["total"]), (2, 2))
        task = pipeline.async_task(result["task_id"])
        self.assertEqual(task["generated"], 2, task)
        rows = db.query("SELECT * FROM events WHERE employee_id=?", (self.first["id"],))
        self.assertEqual({row["id"] for row in rows}, set(task["events"]))
        self.assertEqual({row["event_type"]: row["years"] for row in rows}, {"birthday": None, "anniversary": 3})
        self.assertEqual(before, db.query("SELECT * FROM events WHERE employee_id=?", (self.second["id"],)))
        self.assertFalse(db.query("SELECT * FROM cards WHERE event_id IN (SELECT id FROM events WHERE employee_id=?)", (self.second["id"],)))
        self.upload.assert_not_called()
        self.notice.assert_not_called()
        self.full.assert_not_called()

    def test_event_query_filters_exact_employee_and_keeps_date_scope(self):
        pipeline.scan_cycle((date(2026, 9, 11), date(2026, 9, 11)))
        response = self.client.get(f"/api/events?employee_id={self.first['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["employee_id"] for row in response.json()}, {self.first["id"]})
        self.assertEqual(len(response.json()), 2)
        with patch.object(main, "next_cycle", return_value=(date(2026, 9, 14), date(2026, 9, 20))):
            self.assertEqual(self.client.get(f"/api/events?employee_id={self.first['id']}&scope=next").json(), [])
        self.assertEqual(self.client.get('/api/events?employee_id=99999').json(), [])
        self.assertEqual(self.client.get('/api/events?employee_id=0').status_code, 400)

    def test_repeat_person_generation_preserves_existing_posters(self):
        self.generate()
        before_events = db.query("SELECT * FROM events")
        before_cards = db.query("SELECT * FROM cards")
        self.assertEqual(self.generate().json()["total"], 0)
        self.assertEqual(before_events, db.query("SELECT * FROM events"))
        self.assertEqual(before_cards, db.query("SELECT * FROM cards"))

    def test_only_the_selected_and_confirmed_event_sends_both_messages(self):
        self.generate()
        eid = db.query_one("SELECT id FROM events WHERE employee_id=? AND event_type='birthday'", (self.first["id"],))["id"]
        card = db.query_one("SELECT * FROM cards WHERE event_id=?", (eid,))
        self.assertEqual(self.client.post(f"/api/events/{eid}/select", json={"card_id": card["id"]}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/events/{eid}/confirm", json={"operator": "test"}).status_code, 200)
        result = self.client.post(f"/api/events/{eid}/push", json={"force": True, "operator": "test"}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.notice.call_args.args[0], "ou_first")
        self.assertEqual(self.full.call_args.args[0], "ou_first")
        self.assertEqual(self.notice.call_count, 1)
        self.assertEqual(self.full.call_count, 1)
        self.assertEqual(self.generate().json()["total"], 0)
        self.assertEqual(self.notice.call_count, 1)
        remaining = db.query("SELECT status FROM events WHERE id<>?", (eid,))
        self.assertEqual([row["status"] for row in remaining], ["ready"])

    def test_wrong_day_and_incomplete_or_inactive_employee_never_create_events(self):
        with patch.object(main, "now", return_value="2026-09-12 12:00:00"):
            response = self.generate().json()
            self.assertIsNone(response["task_id"])
            self.assertIn("不是生日或入职周年", response["msg"])
        for changes in ({"birth_date": None, "join_date": None}, {"active": 0}):
            with db.tx() as conn:
                conn.execute("UPDATE employees SET " + ",".join(key + "=?" for key in changes) + " WHERE id=?", [*changes.values(), self.first["id"]])
            self.assertEqual(self.generate().status_code, 400)
        self.assertEqual(self.client.post('/api/employees/99999/generate-today', json={}).status_code, 404)
        self.assertEqual(db.query("SELECT * FROM events"), [])

    def test_person_generation_requires_auth_and_rejects_invalid_ids(self):
        with patch.object(main, "ADMIN_TOKEN", "test-only-token"):
            self.assertEqual(self.generate().status_code, 401)
            self.assertEqual(self.client.get(f"/api/events?employee_id={self.first['id']}").status_code, 401)
        for value in (0, -1):
            self.assertEqual(self.client.post(f'/api/employees/{value}/generate-today', json={}).status_code, 400)
        for value in (False, 0, -1, "1"):
            with self.assertRaises(ValueError):
                pipeline.run_weekly_async(employee_id=value)
        self.assertEqual(db.query("SELECT * FROM events"), [])
