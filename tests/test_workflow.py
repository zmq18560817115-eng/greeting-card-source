"""离线业务回归。临时SQLite和模拟飞书，绝不向真实员工发送消息。"""
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app import db, employees, feishu, main, pipeline, push, templates


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(db, "DB_PATH", self.root / "test.db"),
            patch.object(pipeline, "CARD_DIR", self.root / "cards"),
            patch.object(main, "OUTPUT_DIR", self.root),
            patch.object(main, "ADMIN_TOKEN", ""),
            patch.object(feishu, "get_user", side_effect=self.remote_user),
            patch.object(feishu, "get_department", return_value={"open_department_id": "od_sales", "name": "市场中心"}),
            patch.object(feishu, "upload_image", return_value="image_test"),
            patch.object(feishu, "send_card", return_value="message_test"),
            patch.object(push, "DRY_RUN", False),
            patch.object(push, "now", return_value="2026-09-10 12:00:00"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)
        db.init_db()
        self.client = TestClient(main.app)
        self.emp = employees.save_employee({
            "name": "测试员工", "department": "市场中心", "employee_no": "E001",
            "feishu_open_id": "ou_test", "join_date": "2023-09-10", "birth_date": "1995-09-10",
        })["employee"]
        self.cfg = {"canvas": {"width": 400, "height": 500}, "vars": {"company": "测试公司"},
                    "templates": {key: {"label": key, "ai": {"enabled": False},
                    "layers": [{"type": "text", "text": "{name} {department}", "xy": [20, 30],
                                "size": 24, "min_size": 12, "font": "auto", "max_width": 360}]}
                    for key in ("birthday", "anniversary")}}

    def remote_user(self, open_id):
        return {"open_id": open_id, "name": "测试员工", "department_ids": ["od_sales"],
                "status": {"is_resigned": False, "is_frozen": False}}

    def prepare(self):
        ids, _, _ = pipeline.scan_cycle((date(2026, 9, 10), date(2026, 9, 10)))
        eid = ids[0]
        self.assertEqual(pipeline.generate_for_event(eid, cfg=self.cfg), 1)
        card = db.query_one("SELECT * FROM cards WHERE event_id=?", (eid,))
        response = self.client.post(f"/api/events/{eid}/select", json={"card_id": card["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return eid, card

    def confirm(self, eid):
        response = self.client.post(f"/api/events/{eid}/confirm", json={"operator": "test"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_unapproved_force_send_is_rejected(self):
        eid, _ = self.prepare()
        result = push.push_event(eid, force=True)
        self.assertFalse(result["ok"])
        feishu.send_card.assert_not_called()

    def test_complete_workflow_and_no_duplicate_even_force(self):
        eid, card = self.prepare()
        self.confirm(eid)
        self.assertTrue(push.push_event(eid, force=True)["ok"])
        self.assertTrue(push.push_event(eid, force=True)["already_sent"])
        self.assertEqual(feishu.send_card.call_count, 1)
        self.assertEqual(feishu.send_card.call_args.args[0], "ou_test")
        self.assertTrue(feishu.send_card.call_args.kwargs["uuid"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "pushed")
        self.assertTrue(db.query_one("SELECT recipient_snapshot FROM push_logs")["recipient_snapshot"])

    def test_departure_cancels_scheduled_event(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        employees.deactivate([self.emp["id"]])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "skipped")
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_card.assert_not_called()
        self.assertEqual(push.due_events(), [])

    def test_department_change_invalidates_old_card(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        employees.save_employee({"id": self.emp["id"], "department": "研发中心"})
        event = db.query_one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(event["status"], "needs_regeneration")
        self.assertIsNone(event["selected_card_id"])
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_card.assert_not_called()

    def test_remote_identity_changed_before_send_blocks(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.get_user.side_effect = lambda oid: {**self.remote_user(oid), "name": "另一位员工"}
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "blocked")
        feishu.upload_image.assert_not_called()
        feishu.send_card.assert_not_called()

    def test_remote_departure_during_upload_blocks(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        def upload(_):
            feishu.get_user.side_effect = lambda oid: {**self.remote_user(oid), "status": {"is_resigned": True}}
            return "image_test"
        feishu.upload_image.side_effect = upload
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_card.assert_not_called()

    def test_timeout_is_unknown_and_never_automatically_resent(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_card.side_effect = TimeoutError("回执超时")
        self.assertEqual(push.push_event(eid, force=True)["status"], "delivery_unknown")
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(feishu.send_card.call_count, 1)
        self.assertEqual(push.due_events(), [])

    def test_expired_event_cannot_be_sent_even_force(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        with patch.object(push, "now", return_value="2026-09-11 00:01:00"):
            self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "expired")
        feishu.send_card.assert_not_called()

    def test_definite_rejection_can_retry_after_fix(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_card.side_effect = feishu.FeishuError(230002, "无发信权限", definitive=True)
        self.assertEqual(push.push_event(eid, force=True)["status"], "failed")
        feishu.send_card.side_effect = None
        self.assertTrue(push.push_event(eid, force=True)["ok"])

    def test_scan_then_weekly_still_generates(self):
        cycle = (date(2026, 9, 10), date(2026, 9, 10))
        pipeline.scan_cycle(cycle)
        with patch.object(pipeline.compose, "load_config", return_value=self.cfg):
            result = pipeline.run_weekly(cycle)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["generated"], 2)

    def test_submit_failure_releases_generation(self):
        ids, _, _ = pipeline.scan_cycle((date(2026, 9, 10), date(2026, 9, 10)))
        with patch.object(pipeline.compose, "load_config", return_value=self.cfg), patch.object(pipeline._async_pool, "submit", side_effect=RuntimeError("关闭中")):
            with self.assertRaises(RuntimeError):
                pipeline.submit_generate(ids[0])
        event = db.query_one("SELECT * FROM events WHERE id=?", (ids[0],))
        self.assertEqual(event["status"], "gen_failed")
        self.assertIsNone(event["generation_token"])

    def test_concurrent_clicks_send_once_and_block_employee_edit(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        started, finish = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            started.set()
            self.assertTrue(finish.wait(5))
            return "message_test"
        feishu.send_card.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(push.push_event, eid, force=True)
            self.assertTrue(started.wait(5))
            self.assertFalse(push.push_event(eid, force=True)["ok"])
            with self.assertRaises(employees.EmployeeError):
                employees.deactivate([self.emp["id"]])
            finish.set()
            self.assertTrue(future.result()["ok"])
        self.assertEqual(feishu.send_card.call_count, 1)

    def test_dry_run_is_not_marked_delivered(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        with patch.object(push, "DRY_RUN", True):
            self.assertTrue(push.push_event(eid, force=True)["dry_run"])
        event = db.query_one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(event["status"], "simulated")
        self.assertIsNone(event["pushed_at"])
        feishu.upload_image.assert_not_called()
        feishu.send_card.assert_not_called()

    def test_cross_event_card_is_rejected(self):
        eid, _ = self.prepare()
        other = db.query_one("SELECT * FROM events WHERE id<>?", (eid,))
        pipeline.generate_for_event(other["id"], cfg=self.cfg)
        card = db.query_one("SELECT * FROM cards WHERE event_id=?", (other["id"],))
        result = self.client.post(f"/api/events/{eid}/select", json={"card_id": card["id"]})
        self.assertEqual(result.status_code, 400)

    def test_edit_during_generation_cannot_revive_old_card(self):
        ids, _, _ = pipeline.scan_cycle((date(2026, 9, 10), date(2026, 9, 10)))
        real_render = pipeline.compose.render
        def render(*args, **kwargs):
            result = real_render(*args, **kwargs)
            employees.save_employee({"id": self.emp["id"], "name": "新姓名"})
            return result
        with patch.object(pipeline.compose, "render", side_effect=render):
            self.assertEqual(pipeline.generate_for_event(ids[0], cfg=self.cfg), 0)
        event = db.query_one("SELECT * FROM events WHERE id=?", (ids[0],))
        self.assertEqual(event["status"], "needs_regeneration")

    def test_csv_reports_invalid_rows_without_name_overwrite(self):
        raw = "姓名,工号,部门,生日\n测试员工,,研发中心,1996-03-02\n新员工,E003,研发中心,2026-99-40\n测试员工,E001,市场中心,\n"
        result = self.client.post("/api/employees/import", files={"file": ("staff.csv", raw.encode("utf-8"), "text/csv")})
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(data["added"], 1)
        self.assertEqual(data["updated"], 1)
        self.assertEqual(data["errors"][0]["row"], 3)
        self.assertEqual(db.query_one("SELECT birth_date FROM employees WHERE id=?", (self.emp["id"],))["birth_date"], "1995-09-10")

    def test_csv_chinese_dates_import_and_invalid_row_remains_rejected(self):
        raw = ("姓名,工号,部门,入职日期,生日\n"
               "测试员工,E001,市场中心,2021年2月1日,1995年9月10日\n"
               "日期导入测试,E_DATE,研发中心,2026年8月3日,2月29日\n"
               "无效日期测试,E_INVALID,研发中心,2021年2月29日,1990年1月1日\n")
        for encoding, added, updated in (("utf-8-sig", 1, 1), ("gb18030", 0, 2)):
            with self.subTest(encoding=encoding):
                response = self.client.post("/api/employees/import",
                    files={"file": ("staff.csv", raw.encode(encoding), "text/csv")})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual((result["total"], result["added"], result["updated"]), (3, added, updated))
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual((result["errors"][0]["row"], result["errors"][0]["code"]), (4, "invalid_date"))
                self.assertIn("入职日期", result["errors"][0]["error"])
                self.assertEqual(db.query_one("SELECT COUNT(*) AS n FROM employees")["n"], 2)
                saved = db.query_one("SELECT * FROM employees WHERE employee_no='E_DATE'")
                self.assertEqual((saved["join_date"], saved["birth_date"]), ("2026-08-03", "1896-02-29"))
                self.assertEqual(db.query_one("SELECT join_date FROM employees WHERE id=?", (self.emp["id"],))["join_date"], "2021-02-01")

    def test_xlsx_and_duplicate_headers(self):
        import openpyxl
        workbook = openpyxl.Workbook()
        workbook.active.append(["姓名", "工号", "部门", "生日", "入职日期"])
        workbook.active.append(["新员工", "E009", "研发中心", date(1990, 2, 1), "2021年2月1日"])
        raw = io.BytesIO()
        workbook.save(raw)
        response = self.client.post("/api/employees/import", files={"file": ("staff.xlsx", raw.getvalue())})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["added"], 1)
        self.assertEqual(db.query_one("SELECT join_date FROM employees WHERE employee_no='E009'")["join_date"], "2021-02-01")
        response = self.client.post("/api/employees/import", files={"file": ("staff.csv", "姓名,name\n甲,乙".encode())})
        self.assertEqual(response.status_code, 400)

    def test_preview_uses_real_employee_and_does_not_save(self):
        with patch.object(main.compose, "load_config", return_value=self.cfg), patch.object(templates, "save_config") as save:
            response = self.client.post("/api/templates/preview", json={
                "employee_id": self.emp["id"], "event_date": "2026-09-10", "years": 3,
                "templates": {"birthday": {"layers": [{"text": "{name} {department} {birth_date} {date}"}]}}})
            self.assertEqual(response.status_code, 200, response.text)
            for url in response.json()["images"].values():
                self.assertTrue((self.root / url.split("/")[-1]).is_file())
            save.assert_not_called()

    def test_legacy_database_migration_is_repeatable(self):
        db.init_db()
        db.init_db()
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM employees")["c"], 1)
        self.assertEqual(self.emp["identity_status"], "pending")


if __name__ == "__main__":
    unittest.main()
