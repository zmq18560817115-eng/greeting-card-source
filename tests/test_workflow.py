"""离线业务回归。临时SQLite和模拟飞书，绝不向真实员工发送消息。"""
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from fastapi.testclient import TestClient
from PIL import Image

from app import db, employees, feishu, main, pipeline, push, recovery, scheduler, templates


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(feishu, "FEISHU_APP_ID", "cli_workflow_test"),
            patch.object(db, "DB_PATH", self.root / "test.db"),
            patch.object(pipeline, "CARD_DIR", self.root / "cards"),
            patch.object(main, "OUTPUT_DIR", self.root),
            patch.object(main, "ADMIN_TOKEN", ""),
            patch("requests.sessions.Session.request", side_effect=AssertionError("离线测试不得访问真实飞书")),
            patch.object(feishu, "list_scope_users", return_value=[self.remote_user("ou_test")]),
            patch.object(feishu, "get_user", side_effect=self.remote_user),
            patch.object(feishu, "get_department", return_value={"open_department_id": "od_sales", "name": "市场中心"}),
            patch.object(feishu, "upload_image", return_value="image_test"),
            patch.object(feishu, "send_notice", return_value="notice_test"),
            patch.object(feishu, "send_full_card", return_value="message_test"),
            patch.object(push, "DRY_RUN", False),
            # Retain regression coverage for historical two-message deliveries.
            patch.object(push, "DELIVERY_MODE", "notice_then_full_card"),
            patch.object(scheduler.auto_schedule, "prepare", return_value={}),
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

    def test_app_change_or_legacy_approval_blocks_both_messages(self):
        eid, card = self.prepare()
        for app_id in ('cli_another_app', None):
            with self.subTest(app_id=app_id):
                self.client.post(f'/api/events/{eid}/select', json={'card_id':card['id']})
                self.confirm(eid)
                db.execute('UPDATE events SET confirmed_app_id=? WHERE id=?', (app_id, eid))
                result = push.push_event(eid, force=True)
                self.assertEqual(result['status'], 'blocked')
                self.assertIn('当前飞书应用', result['msg'])
                feishu.send_notice.assert_not_called()
                feishu.send_full_card.assert_not_called()

    def test_legacy_birthday_age_requires_regeneration_before_confirm_or_send(self):
        eid, _ = self.prepare()
        db.execute('UPDATE events SET years=31 WHERE id=?', (eid,))
        response = self.client.post(f'/api/events/{eid}/confirm', json={'operator': 'test'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('生日不计算年龄', response.json()['detail'])
        db.execute('UPDATE events SET years=NULL WHERE id=?', (eid,))
        self.confirm(eid)
        db.execute('UPDATE events SET years=31 WHERE id=?', (eid,))
        result = push.push_event(eid, force=True)
        self.assertEqual(result['status'], 'blocked')
        feishu.upload_image.assert_not_called()
        feishu.send_notice.assert_not_called()
        feishu.send_full_card.assert_not_called()

    def test_force_cannot_send_before_the_birthday_date(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        with patch.object(push, 'now', return_value='2026-09-09 12:00:00'):
            result = push.push_event(eid, force=True)
        self.assertFalse(result['ok'])
        self.assertIn('未到贺卡日期', result['msg'])
        self.assertEqual(db.query_one('SELECT status FROM events WHERE id=?', (eid,))['status'], 'confirmed')
        feishu.send_notice.assert_not_called()
        feishu.send_full_card.assert_not_called()

    def test_push_rechecks_actual_month_day_even_for_confirmed_legacy_events(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        db.execute("UPDATE events SET event_date='2026-09-11' WHERE id=?", (eid,))
        with patch.object(push, 'now', return_value='2026-09-11 12:00:00'):
            result = push.push_event(eid, force=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('贺卡日期', result['msg'])
        feishu.send_notice.assert_not_called()
        feishu.send_full_card.assert_not_called()

    def test_new_remote_same_name_blocks_before_notice_and_before_full_card(self):
        eid, card = self.prepare()
        self.confirm(eid)
        original = self.remote_user('ou_test')
        duplicates = [original, self.remote_user('ou_other')]
        with patch.object(feishu, 'list_scope_users', return_value=duplicates):
            self.assertEqual(push.push_event(eid, force=True)['status'], 'blocked')
        feishu.send_notice.assert_not_called()
        feishu.send_full_card.assert_not_called()
        self.client.post(f'/api/events/{eid}/select', json={'card_id':card['id']})
        self.confirm(eid)
        with patch.object(feishu, 'list_scope_users', side_effect=[[original], [original], duplicates]):
            self.assertEqual(push.push_event(eid, force=True)['status'], 'blocked')
        feishu.send_notice.assert_called_once()
        feishu.send_full_card.assert_not_called()

    def test_unapproved_force_send_is_rejected(self):
        eid, _ = self.prepare()
        result = push.push_event(eid, force=True)
        self.assertFalse(result["ok"])
        feishu.send_full_card.assert_not_called()

    def test_complete_workflow_and_no_duplicate_even_force(self):
        eid, card = self.prepare()
        self.confirm(eid)
        order = []
        def notice(*args, **kwargs):
            order.append("notice")
            self.assertIsNone(db.query_one("SELECT pushed_at FROM events WHERE id=?", (eid,))["pushed_at"])
            return "notice_test"
        def full(*args, **kwargs):
            order.append("full")
            saved = db.query_one("SELECT * FROM events WHERE id=?", (eid,))
            self.assertEqual(saved["notice_message_id"], "notice_test")
            self.assertEqual(saved["status"], "pushing")
            self.assertIsNotNone(saved["delivery_started_at"])
            return "message_test"
        feishu.send_notice.side_effect = notice
        feishu.send_full_card.side_effect = full
        self.assertTrue(push.push_event(eid, force=True)["ok"])
        self.assertTrue(push.push_event(eid, force=True)["already_sent"])
        self.assertEqual(feishu.send_full_card.call_count, 1)
        self.assertEqual(feishu.send_full_card.call_args.args[0], "ou_test")
        self.assertTrue(feishu.send_full_card.call_args.kwargs["uuid"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "pushed")
        self.assertTrue(db.query_one("SELECT recipient_snapshot FROM push_logs")["recipient_snapshot"])
        self.assertEqual(order, ["notice", "full"])
        feishu.send_notice.assert_called_once()
        self.assertNotEqual(feishu.send_notice.call_args.kwargs["uuid"], feishu.send_full_card.call_args.kwargs["uuid"])
        self.assertEqual([row["status"] for row in db.query("SELECT status FROM push_logs ORDER BY id")], ["notice_success", "success"])

    def test_notice_timeout_stops_full_card_and_disallows_retry(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_notice.side_effect = TimeoutError("回执超时")
        result = push.push_event(eid, force=True)
        self.assertEqual(result["status"], "delivery_unknown")
        self.assertIn("简短通知发送结果待确认", result["msg"])
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_notice.assert_called_once()
        feishu.send_full_card.assert_not_called()

    def test_notice_rejection_retries_same_notice_uuid_before_full_card(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_notice.side_effect = feishu.FeishuError(230002, "无发信权限", definitive=True)
        self.assertEqual(push.push_event(eid, force=True)["status"], "failed")
        first_uuid = feishu.send_notice.call_args.kwargs["uuid"]
        feishu.send_full_card.assert_not_called()
        feishu.send_notice.side_effect = None
        self.assertTrue(push.push_event(eid, force=True)["ok"])
        self.assertEqual(feishu.send_notice.call_args.kwargs["uuid"], first_uuid)
        feishu.send_full_card.assert_called_once()

    def test_crash_between_messages_recovers_without_repeating_notice(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        real_validate = employees.validate_identity
        def interrupted(emp):
            if db.query_one("SELECT notice_message_id FROM events WHERE id=?", (eid,))["notice_message_id"]:
                raise SystemExit("模拟进程在第二条请求前退出")
            return real_validate(emp)
        with patch.object(employees, "validate_identity", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                push.push_event(eid, force=True)
        self.assertIsNone(db.query_one("SELECT delivery_started_at FROM events WHERE id=?", (eid,))["delivery_started_at"])
        with patch.object(recovery, "_pid_alive", return_value=False):
            self.assertEqual(recovery.recover_interrupted_jobs()["failed"], 1)
        self.assertTrue(push.push_event(eid, force=True)["ok"])
        feishu.send_notice.assert_called_once()
        feishu.send_full_card.assert_called_once()

    def test_recipient_departure_after_notice_stops_full_card(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        def notice(*args, **kwargs):
            feishu.get_user.side_effect = lambda oid: {**self.remote_user(oid), "status": {"is_resigned": True}}
            return "notice_test"
        feishu.send_notice.side_effect = notice
        result = push.push_event(eid, force=True)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("简短通知已发送", result["msg"])
        feishu.send_full_card.assert_not_called()

    def test_changed_app_cannot_resume_second_message(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_full_card.side_effect = feishu.FeishuError(230002, "无发信权限", definitive=True)
        push.push_event(eid, force=True)
        feishu.send_full_card.reset_mock()
        with patch.object(feishu, "FEISHU_APP_ID", "different_app"):
            self.assertEqual(push.push_event(eid, force=True)["status"], "blocked")
        feishu.send_notice.assert_called_once()
        feishu.send_full_card.assert_not_called()

    def test_departure_cancels_scheduled_event(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        employees.deactivate([self.emp["id"]])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "skipped")
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_full_card.assert_not_called()
        self.assertEqual(push.due_events(), [])

    def test_department_change_invalidates_old_card(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        employees.save_employee({"id": self.emp["id"], "department": "研发中心"})
        event = db.query_one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(event["status"], "needs_regeneration")
        self.assertIsNone(event["selected_card_id"])
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_full_card.assert_not_called()

    def test_remote_identity_changed_before_send_blocks(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.get_user.side_effect = lambda oid: {**self.remote_user(oid), "name": "另一位员工"}
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "blocked")
        feishu.upload_image.assert_not_called()
        feishu.send_full_card.assert_not_called()

    def test_remote_departure_during_upload_blocks(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        def upload(_):
            feishu.get_user.side_effect = lambda oid: {**self.remote_user(oid), "status": {"is_resigned": True}}
            return "image_test"
        feishu.upload_image.side_effect = upload
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        feishu.send_full_card.assert_not_called()

    def test_timeout_is_unknown_and_never_automatically_resent(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_full_card.side_effect = TimeoutError("回执超时")
        self.assertEqual(push.push_event(eid, force=True)["status"], "delivery_unknown")
        self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(feishu.send_full_card.call_count, 1)
        self.assertEqual(push.due_events(), [])

    def test_expired_event_cannot_be_sent_even_force(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        with patch.object(push, "now", return_value="2026-09-11 00:01:00"):
            self.assertFalse(push.push_event(eid, force=True)["ok"])
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (eid,))["status"], "expired")
        feishu.send_full_card.assert_not_called()

    def test_definite_rejection_can_retry_after_fix(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        feishu.send_full_card.side_effect = feishu.FeishuError(230002, "无发信权限", definitive=True)
        self.assertEqual(push.push_event(eid, force=True)["status"], "failed")
        first_uuid = feishu.send_full_card.call_args.kwargs["uuid"]
        self.assertIsNone(db.query_one("SELECT pushed_at FROM events WHERE id=?", (eid,))["pushed_at"])
        feishu.send_full_card.side_effect = None
        self.assertTrue(push.push_event(eid, force=True)["ok"])
        feishu.send_notice.assert_called_once()
        self.assertEqual(feishu.send_full_card.call_args.kwargs["uuid"], first_uuid)

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

    def test_weekly_submission_failure_is_reported_and_remains_retryable(self):
        with patch.object(pipeline._async_pool, "submit", side_effect=RuntimeError("关闭中")):
            with self.assertRaisesRegex(ValueError, "提交失败"):
                pipeline.run_weekly_async((date(2026, 9, 10), date(2026, 9, 10)))
        pending = pipeline._pending_generation(date(2026, 9, 10), date(2026, 9, 10))
        self.assertEqual(len(pending), 2)

    def test_weekly_render_failure_is_present_in_task_result(self):
        with patch.object(pipeline._async_pool, "submit", side_effect=lambda work: work()), \
                patch.object(pipeline, "generate_for_event", return_value=0):
            _, task = pipeline.run_weekly_async((date(2026, 9, 10), date(2026, 9, 10)))
        self.assertEqual(task["generated"], 0)
        self.assertEqual(len(task["errors"]), 2)

    def test_health_reports_missing_birthday_and_loaded_delivery_flow(self):
        db.execute("UPDATE employees SET birth_date=NULL WHERE id=?", (self.emp["id"],))
        with patch.object(main.feishu_config, "check_connection", return_value={'ok':False, 'msg':'缺少读取用户组权限'}):
            result = self.client.get('/api/health').json()
        self.assertEqual(result['missing_fields'], {'birthday':1, 'join_date':0, 'feishu_id':0})
        self.assertEqual(result['delivery_flow'], 'compact_link')
        self.assertEqual(result['recipient_rule'], 'unique_exact_name_and_open_id')
        self.assertEqual(result['feishu'], {'ok':False, 'error':'缺少读取用户组权限'})

    def test_scheduler_sends_only_confirmed_due_tasks_once(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        db.execute("UPDATE events SET trigger_at='2026-09-10 13:00:00' WHERE id=?", (eid,))
        scheduler.push_job()
        feishu.send_notice.assert_not_called()
        with patch.object(push, 'now', return_value='2026-09-10 13:01:00'):
            scheduler.push_job()
            scheduler.push_job()
        feishu.send_notice.assert_called_once()
        feishu.send_full_card.assert_called_once()
        self.assertEqual(db.query_one('SELECT status FROM events WHERE id=?', (eid,))['status'], 'pushed')

    def test_concurrent_clicks_send_once_and_block_employee_edit(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        started, finish = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            started.set()
            self.assertTrue(finish.wait(5))
            return "message_test"
        feishu.send_full_card.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(push.push_event, eid, force=True)
            self.assertTrue(started.wait(5))
            self.assertFalse(push.push_event(eid, force=True)["ok"])
            with self.assertRaises(employees.EmployeeError):
                employees.deactivate([self.emp["id"]])
            finish.set()
            self.assertTrue(future.result()["ok"])
        self.assertEqual(feishu.send_full_card.call_count, 1)

    def test_dry_run_is_not_marked_delivered(self):
        eid, _ = self.prepare()
        self.confirm(eid)
        with patch.object(push, "DRY_RUN", True):
            self.assertTrue(push.push_event(eid, force=True)["dry_run"])
        event = db.query_one("SELECT * FROM events WHERE id=?", (eid,))
        self.assertEqual(event["status"], "simulated")
        self.assertIsNone(event["pushed_at"])
        feishu.upload_image.assert_not_called()
        feishu.send_full_card.assert_not_called()

        feishu.send_notice.assert_not_called()

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

    def test_review_fields_show_service_years_without_birthday_age(self):
        eid, _ = self.prepare()
        response = self.client.get(f"/api/events/{eid}")
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        self.assertEqual(view["event_type"], "birthday")
        self.assertEqual((view["years"], view["anniversary_years"]), (None, 3))
        self.assertEqual(view["employee"]["employee_no"], "E001")
        self.assertEqual(view["employee"]["join_date"], "2023-09-10")
        self.assertEqual(view["employee"]["birth_date_display"], "09-10")
        self.assertFalse(view["is_pushed"])
        self.assertIsNone(view["actual_push_at"])
        self.assertEqual(view["planned_push_at"], view["trigger_at"])
        employees.save_employee({"id": self.emp["id"], "join_date": "2022年9月10日", "anniversary_years": 999})
        changed = self.client.get(f"/api/events/{eid}").json()
        self.assertEqual(changed["anniversary_years"], 4)
        self.assertIn("重新生成", changed["exception_hint"])
        listing = self.client.get("/api/employees").json()
        self.assertIn("anniversary_years", listing[0])
        self.assertNotIn("anniversary_years", db.query_one("SELECT * FROM employees WHERE id=?", (self.emp["id"],)))

    def test_review_exception_and_delivery_state_are_consistent_in_list_and_detail(self):
        eid, _ = self.prepare()
        db.execute("UPDATE events SET status='delivery_unknown',last_error='ReadTimeout: timed out' WHERE id=?", (eid,))
        detail = self.client.get(f"/api/events/{eid}").json()
        item = next(row for row in self.client.get("/api/events").json() if row["id"] == eid)
        self.assertEqual(detail["exception_hint"], item["exception_hint"])
        self.assertIn("暂勿重新发送", detail["exception_hint"])
        self.assertIsNone(detail["is_pushed"])
        self.assertIsNone(detail["actual_push_at"])
        self.assertEqual(detail["last_error"], "ReadTimeout: timed out")

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

    def test_preview_keeps_automatic_fields_without_reading_employees_or_saving(self):
        with patch.object(main.compose, "load_config", return_value=self.cfg), patch.object(templates, "save_config") as save, \
                patch.object(main, "query_one", side_effect=AssertionError("preview must not read employees")), \
                patch.object(main.compose, "render", wraps=main.compose.render) as render:
            response = self.client.post("/api/templates/preview", json={
                "employee_id": self.emp["id"], "event_date": "invalid", "years": 999,
                "templates": {"birthday": {"layers": [{"text": "{name} {department} {birth_date} {date}"}]}}})
            self.assertEqual(response.status_code, 200, response.text)
            for url in response.json()["images"].values():
                self.assertTrue((self.root / url.split("/")[-1]).is_file())
            for call in render.call_args_list:
                for field in ("name", "department", "birth_date", "date", "years", "event_date"):
                    self.assertEqual(call.args[1][field], "{" + field + "}")
            self.assertEqual(response.json()["sizes"]["birthday"], {"width": 400, "height": 500})
            save.assert_not_called()

    def test_background_upload_preview_then_save_only_replaces_selected_template(self):
        original = self.root / "original.png"
        with Image.new("RGB", (400, 500), "red") as image:
            image.save(original)
        self.cfg["templates"]["birthday"]["base_image"] = str(original)
        self.cfg["canvas"] = {"width": 600, "height": 700}
        config_path = self.root / "templates.json"
        templates.save_config(self.cfg, config_path)
        before = config_path.read_bytes()
        stream = io.BytesIO()
        with Image.new("RGB", (100, 100), "green") as image:
            image.save(stream, "PNG")
        with patch.object(templates, "TEMPLATE_CONFIG", config_path), \
                patch.object(templates, "TEMPLATE_DIR", self.root / "uploads"), \
                patch.object(main.compose, "load_config", side_effect=lambda: templates.load_config()):
            response = self.client.post("/api/templates/birthday/background?fit=cover",
                                        files={"file": ("updated.png", stream.getvalue(), "image/png")})
            self.assertEqual(response.status_code, 200, response.text)
            info = response.json()
            self.assertEqual((info["width"], info["height"]), (400, 500))
            self.assertEqual(config_path.read_bytes(), before)
            edits = {"templates": {"birthday": {"base_image": info["base_image"]}}}
            preview = self.client.post("/api/templates/preview", json=edits)
            self.assertEqual(preview.status_code, 200, preview.text)
            with Image.open(self.root / preview.json()["images"]["birthday"].split("/")[-1]) as image:
                self.assertEqual(image.getpixel((0, 0)), (0, 128, 0))
            self.assertEqual(config_path.read_bytes(), before)
            saved = self.client.post("/api/templates", json=edits)
            self.assertEqual(saved.status_code, 200, saved.text)
            cfg = templates.load_config()
            self.assertEqual(cfg["templates"]["birthday"]["base_image"], info["base_image"])
            self.assertEqual(cfg["templates"]["anniversary"], templates.validate_config(self.cfg)["templates"]["anniversary"])
            self.assertEqual(cfg["templates"]["birthday"]["layers"], templates.validate_config(self.cfg)["templates"]["birthday"]["layers"])
            with Image.open(original) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))

    def test_background_upload_rejects_invalid_files_keys_modes_and_unauthorized_requests(self):
        stream = io.BytesIO()
        with Image.new("RGB", (100, 100)) as image:
            image.save(stream, "PNG")
        target = self.root / "uploads"
        with patch.object(main.compose, "load_config", return_value=self.cfg), \
                patch.object(templates, "TEMPLATE_DIR", target), patch.object(templates, "save_config") as save:
            for url, raw in (("birthday/background", b"not an image"),
                             ("missing/background", stream.getvalue()),
                             ("birthday/background?fit=stretch", stream.getvalue())):
                response = self.client.post("/api/templates/" + url, files={"file": ("photo.png", raw, "image/png")})
                self.assertEqual(response.status_code, 400, response.text)
            with patch.object(main, "ADMIN_TOKEN", "test-only-token"):
                response = self.client.post("/api/templates/birthday/background", files={"file": ("photo.png", stream.getvalue())})
                self.assertEqual(response.status_code, 401)
            save.assert_not_called()
        self.assertFalse(target.exists())

    def test_legacy_database_migration_is_repeatable(self):
        db.init_db()
        db.init_db()
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM employees")["c"], 1)
        self.assertEqual(self.emp["identity_status"], "pending")


if __name__ == "__main__":
    unittest.main()
