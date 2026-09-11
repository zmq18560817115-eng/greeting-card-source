"""Atomic batch corrections with temporary employee records; no real Feishu calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, employees, feishu, main


class StaffBatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, key, value in ((db, "DB_PATH", Path(folder.name) / "batch.db"),
                                   (main, "ADMIN_TOKEN", ""), (feishu, "FEISHU_APP_ID", ""),
                                   (feishu, "FEISHU_APP_SECRET", "")):
            handle = patch.object(target, key, value)
            handle.start()
            self.addCleanup(handle.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network in tests"))
        network.start()
        self.addCleanup(network.stop)
        db.init_db()
        self.client = TestClient(main.app)
        self.ids = [employees.save_employee({"name": name, "department": "测试部门", "employee_no": str(index),
                                            "join_date": "2021-02-01", "birth_date": "2000-02-29", "email": "preserve@test"})["id"]
                    for index, name in enumerate(("离线甲", "离线乙", "离线丙"), 1)]

    def row(self, index):
        return db.query_one("SELECT * FROM employees WHERE id=?", (self.ids[index],))

    def change(self, index, **changes):
        row = self.row(index)
        return {"id": row["id"], "original": {key: row[key] for key in employees.EDIT_FIELDS}, "changes": changes}

    def submit(self, updates):
        return self.client.post("/api/employees/batch-update", json={"updates": updates})

    def event(self, index, state="confirmed"):
        return db.execute("""INSERT INTO events(employee_id,event_type,event_date,trigger_at,status)
                             VALUES(?,'birthday','2026-09-12','2026-09-12 10:00:00',?)""", (self.ids[index], state))

    def test_only_selected_rows_and_changed_fields_are_saved(self):
        response = self.submit([self.change(0, department="新部门"), self.change(1, department="新部门", employee_no="002")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        self.assertEqual(response.json()["binding"]["pending"], 2)
        self.assertEqual(self.row(0)["department"], "新部门")
        self.assertEqual(self.row(1)["employee_no"], "002")
        self.assertEqual(self.row(2)["department"], "测试部门")
        self.assertEqual(self.row(0)["email"], "preserve@test")
        self.assertEqual(self.row(0)["birth_date"], "2000-02-29")

    def test_invalid_date_or_blank_required_field_saves_nothing(self):
        for bad in ({"join_date": "2026-02-30"}, {"name": " "}, {"department": ""}, {"active": ""}):
            with self.subTest(bad=bad):
                response = self.submit([self.change(0, department="新部门"), self.change(1, **bad)])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.row(0)["department"], "测试部门")

    def test_batch_edit_birthday_accepts_month_day_but_join_date_requires_year(self):
        response = self.submit([self.change(0, birth_date='09-11')])
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.client.get('/api/employees').json()
        updated = next(row for row in rows if row['id'] == self.ids[0])
        self.assertEqual(updated['birth_date_display'], '09-11')
        response = self.submit([self.change(0, join_date='09-11')])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.row(0)['join_date'], '2021-02-01')

    def test_duplicate_employee_number_rolls_back_data_and_event_invalidations(self):
        event = self.event(0)
        response = self.submit([self.change(0, department="新部门"), self.change(1, employee_no="3")])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("工号", response.json()["detail"])
        self.assertEqual(self.row(0)["department"], "测试部门")
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (event,))["status"], "confirmed")

    def test_name_conflict_rolls_back_every_change(self):
        response = self.submit([self.change(0, department="新部门"), self.change(1, name="离线丙")])
        self.assertEqual(response.status_code, 409)
        self.assertIn("同名", response.json()["detail"])
        self.assertEqual(self.row(0)["department"], "测试部门")

    def test_concurrent_edit_is_rejected_without_overwriting_new_data(self):
        updates = [self.change(0, department="新部门"), self.change(1, birth_date="2001-01-01")]
        employees.save_employee({"id": self.ids[1], "department": "刚更新的部门"})
        response = self.submit(updates)
        self.assertEqual(response.status_code, 409)
        self.assertIn("资料已被修改", response.json()["detail"])
        self.assertEqual(self.row(0)["department"], "测试部门")
        self.assertEqual(self.row(1)["department"], "刚更新的部门")

    def test_sending_employee_prevents_partial_batch_save(self):
        self.event(1, "pushing")
        response = self.submit([self.change(0, department="新部门"), self.change(1, active=0)])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.row(0)["department"], "测试部门")
        self.assertEqual(self.row(1)["active"], 1)

    def test_departure_cancels_pending_event_but_preserves_sent_event(self):
        pending = self.event(0)
        sent = self.event(1, "pushed")
        self.assertEqual(self.submit([self.change(0, active=0), self.change(1, active=0)]).status_code, 200)
        self.assertEqual(self.row(0)["active"], 0)
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (pending,))["status"], "skipped")
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (sent,))["status"], "pushed")

    def test_clear_optional_fields_and_unchanged_rows(self):
        event = self.event(1)
        response = self.submit([self.change(0, employee_no="", birth_date="", join_date=""), self.change(1, department="测试部门")])
        self.assertEqual(response.json()["updated"], 1)
        self.assertEqual(response.json()["unchanged"], 1)
        for key in ("employee_no", "birth_date", "join_date"):
            self.assertIsNone(self.row(0)[key])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (event,))["status"], "confirmed")

    def test_duplicate_rows_unknown_fields_missing_snapshot_and_auth(self):
        valid = self.change(0, department="新部门")
        for updates in ([valid, valid], [{**valid, "changes": {"feishu_user_id": "user_bad"}}],
                        [{**valid, "original": {}}], [], None):
            self.assertEqual(self.submit(updates).status_code, 400)
        with patch.object(main, "ADMIN_TOKEN", "test-token"):
            self.assertEqual(self.submit([valid]).status_code, 401)

    def test_distinct_ids_can_be_corrected_per_row_and_revoke_pending_approval(self):
        event = self.event(0)
        response = self.submit([self.change(0, feishu_open_id="ou_first"), self.change(1, feishu_open_id="ou_second")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 2)
        self.assertEqual((self.row(0)["feishu_open_id"], self.row(1)["feishu_open_id"]), ("ou_first", "ou_second"))
        self.assertIsNone(self.row(2)["feishu_open_id"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (event,))["status"], "needs_regeneration")
        self.assertNotEqual(self.row(0)["identity_status"], "verified")

    def test_duplicate_id_rolls_back_the_entire_batch(self):
        event = self.event(0)
        response = self.submit([self.change(0, feishu_open_id="ou_same"), self.change(1, feishu_open_id="ou_same")])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.row(0)["feishu_open_id"])
        self.assertIsNone(self.row(1)["feishu_open_id"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (event,))["status"], "confirmed")

    def test_blank_invalid_and_stale_id_changes_are_rejected(self):
        for value in ("", "wrong_id", 42):
            response = self.submit([self.change(0, department="新部门"), self.change(1, feishu_open_id=value)])
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.row(0)["department"], "测试部门")
        updates = [self.change(0, department="新部门"), self.change(1, feishu_open_id="ou_proposed")]
        employees.save_employee({"id": self.ids[1], "feishu_open_id": "ou_newer"})
        response = self.submit(updates)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.row(0)["department"], "测试部门")
        self.assertEqual(self.row(1)["feishu_open_id"], "ou_newer")

    def test_old_batch_client_must_refresh_to_include_id_snapshot(self):
        update = self.change(0, department="新部门")
        del update["original"]["feishu_open_id"]
        response = self.submit([update])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("刷新", response.json()["detail"])
        self.assertEqual(self.row(0)["department"], "测试部门")
