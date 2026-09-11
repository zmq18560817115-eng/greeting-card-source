"""Identity/import/sync safety tests; all databases temporary, all APIs mocked.

Run: python -m unittest discover -s tests -p test_employees.py -v
"""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from app import db, employees, feishu, sync


class EmployeeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "test.db")
        database.start()
        self.addCleanup(database.stop)
        db.init_db()
        self.get_user = self.mock_api("get_user", side_effect=lambda oid: self.remote(open_id=oid))
        self.get_department = self.mock_api("get_department", side_effect=lambda did: {
            "open_department_id": did, "name": {"d1": "研发部", "d2": "市场部", "d3": "研发"}.get(did, "未知部")})
        self.list_users = self.mock_api("list_users", return_value=[])
        self.list_scope = self.mock_api("list_scope_users", return_value=[])
        self.custom_attrs = self.mock_api("list_custom_attrs", return_value=[])
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Tests must never use the network"))
        network.start()
        self.addCleanup(network.stop)

    def mock_api(self, name, **kwargs):
        target = patch.object(feishu, name, create=True, **kwargs)
        mocked = target.start()
        self.addCleanup(target.stop)
        return mocked

    def remote(self, **changes):
        return {"open_id": "ou_one", "name": "张三", "department_ids": ["d1"],
                "status": {"is_resigned": False}, **changes}

    def employee(self, **changes):
        return employees.save_employee({"name": "张三", "department": "研发部", "feishu_open_id": "ou_one", **changes})["id"]

    def get(self, eid):
        return db.query_one("SELECT * FROM employees WHERE id=?", (eid,))

    def event(self, employee_id, status="confirmed", day="2026-09-12"):
        eid = db.execute("""INSERT INTO events(employee_id,event_type,event_date,trigger_at,status,
                            confirmed_by,confirmed_at,generation_token,delivery_uuid,employee_snapshot)
                            VALUES(?,'birthday',?,? ,?,'hr','yesterday','old-token','delivery-id','old-snapshot')""",
                         (employee_id, day, day + " 10:00:00", status))
        cid = db.execute("INSERT INTO cards(event_id,idx,file_path,status,employee_snapshot) VALUES(?,1,'old.png','ok','old-snapshot')", (eid,))
        db.execute("UPDATE events SET selected_card_id=? WHERE id=?", (cid, eid))
        if status == "pushed":
            db.execute("UPDATE events SET pushed_at='2026-09-01 10:00:00' WHERE id=?", (eid,))
            db.execute("INSERT INTO push_logs(event_id,card_id,status,message_id) VALUES(?,?,'success','message-1')", (eid, cid))
        return eid, cid

    def assert_invalidated(self, event_id, card_id, status="needs_regeneration"):
        ev = db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        self.assertEqual(ev["status"], status)
        for field in ("selected_card_id", "confirmed_by", "confirmed_at", "generation_token"):
            self.assertIsNone(ev[field], field)
        self.assertEqual(db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))["status"], "invalid")

    def test_exact_identity_returns_evidence_without_writes(self):
        eid = self.employee()
        before = self.get(eid)
        result = employees.validate_identity(before)
        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence"]["rule"], "unique_exact_name_and_open_id")
        self.get_user.assert_called_once_with("ou_one")
        self.get_department.assert_not_called()
        self.assertEqual(before, self.get(eid))

    def test_name_and_open_id_are_exact(self):
        eid = self.employee()
        for changes, code in [({"name": "张三丰"}, "name_mismatch"),
                              ({"name": "张三 "}, "name_mismatch"),
                              ({"open_id": "ou_other"}, "open_id_mismatch")]:
            with self.subTest(changes=changes):
                self.get_user.side_effect = None
                self.get_user.return_value = self.remote(**changes)
                with self.assertRaises(employees.IdentityError) as raised:
                    employees.validate_identity(self.get(eid))
                self.assertEqual(raised.exception.code, code)

    def test_department_difference_does_not_block_name_id_mapping(self):
        eid = self.employee(department="研发")
        self.assertTrue(employees.validate_identity(self.get(eid))["ok"])
        self.get_department.assert_not_called()

    def test_multiple_exact_department_names_accept_one_complete_match(self):
        eid = self.employee(department=" 产品部，市场部、研发部 ")
        self.assertTrue(employees.validate_identity(self.get(eid))["ok"])

    def test_department_permission_is_not_required_for_recipient_mapping(self):
        eid = self.employee()
        self.get_user.side_effect = None
        self.get_user.return_value = self.remote(department_ids=["d1", "d2"])
        self.get_department.side_effect = lambda did: ({"open_department_id": did, "name": "研发部"}
                                                     if did == "d1" else {"department_id": "d2", "name": "市场部"})
        self.assertTrue(employees.validate_identity(self.get(eid))["ok"])
        self.get_department.assert_not_called()

    def test_remote_error_or_resignation_fails_closed_and_is_read_only(self):
        eid = self.employee()
        before = self.get(eid)
        self.get_user.side_effect = RuntimeError("permission denied")
        with self.assertRaises(employees.IdentityError) as raised:
            employees.validate_identity(before)
        self.assertEqual(raised.exception.code, "user_unavailable")
        self.get_user.side_effect = None
        self.get_user.return_value = self.remote(status={"is_resigned": True})
        with self.assertRaises(employees.IdentityError):
            employees.validate_identity(before)
        self.assertEqual(before, self.get(eid))

    def test_live_verification_requires_explicit_boolean_resigned_status(self):
        eid = self.employee()
        self.get_user.side_effect = None
        for status in (None, {}, {"is_resigned": None}, {"is_resigned": "false"},
                       {"is_resigned": 0}, {"is_resigned": 1}, {"is_frozen": False}):
            with self.subTest(status=status):
                self.get_user.return_value = self.remote(status=status)
                with self.assertRaises(employees.IdentityError) as raised:
                    employees.validate_identity(self.get(eid))
                self.assertEqual(raised.exception.code, "invalid_user_status")
        user = self.remote()
        del user["status"]
        self.get_user.return_value = user
        with self.assertRaises(employees.IdentityError):
            employees.validate_identity(self.get(eid))

    def test_missing_remote_status_revokes_approval_and_generation_token(self):
        eid = self.employee()
        employees.verify_employee(eid)
        event_id, card_id = self.event(eid, "generating")
        self.get_user.side_effect = None
        self.get_user.return_value = self.remote(status={})
        result = employees.verify_employee(eid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_user_status")
        self.assertEqual(self.get(eid)["identity_status"], "failed")
        self.assert_invalidated(event_id, card_id)

    def test_missing_identity_or_inactive_employee_never_calls_remote(self):
        for changes in ({"active": 0}, {"name": ""}, {"feishu_open_id": ""}):
            with self.subTest(changes=changes):
                emp = {"active": 1, "name": "张三", "department": "研发部", "feishu_open_id": "ou_one", **changes}
                with self.assertRaises(employees.IdentityError):
                    employees.validate_identity(emp)
        self.get_user.assert_not_called()

    def test_verify_persists_snapshot(self):
        eid = self.employee(feishu_open_id=None)
        event_id, card_id = self.event(eid)
        result = employees.verify_employee(eid, "ou_one")
        self.assertTrue(result["ok"])
        emp = self.get(eid)
        self.assertEqual(emp["identity_status"], "verified")
        self.assertEqual(emp["feishu_open_id"], "ou_one")
        self.assertIsNotNone(emp["identity_verified_at"])
        self.assertEqual(json.loads(emp["identity_snapshot"]), result["evidence"])
        self.assert_invalidated(event_id, card_id)

    def test_verify_failure_revokes_previous_verification_cards_and_approval(self):
        eid = self.employee()
        employees.verify_employee(eid)
        event_id, card_id = self.event(eid)
        self.get_user.side_effect = RuntimeError("network unavailable")
        result = employees.verify_employee(eid)
        self.assertFalse(result["ok"])
        self.assertEqual(self.get(eid)["identity_status"], "unavailable")
        self.assertIsNone(self.get(eid)["identity_verified_at"])
        self.assert_invalidated(event_id, card_id)

    def test_feishu_permission_failure_keeps_specific_reason(self):
        eid = self.employee()
        self.get_user.side_effect = feishu.FeishuError(99991672, "缺少用户读取权限")
        result = employees.verify_employee(eid)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "user_unavailable")
        self.assertIn("99991672", result["error"])
        self.assertEqual(self.get(eid)["identity_status"], "unavailable")
        self.list_scope.side_effect = feishu.FeishuError(99991672, "缺少通讯录权限")
        self.assertIn("99991672", employees.match_employee(eid)["msg"])

    def test_failed_candidate_binding_preserves_old_open_id(self):
        eid = self.employee()
        self.get_user.side_effect = lambda oid: self.remote(open_id=oid, name="另一个人")
        result = employees.verify_employee(eid, "ou_wrong")
        self.assertFalse(result["ok"])
        self.assertEqual(self.get(eid)["feishu_open_id"], "ou_one")

    def test_candidate_owned_by_other_employee_is_rejected(self):
        self.employee()
        other = self.employee(name="李四", feishu_open_id="ou_two")
        self.get_user.side_effect = lambda oid: self.remote(open_id=oid, name="李四")
        result = employees.verify_employee(other, "ou_one")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "identifier_conflict")
        self.assertEqual(self.get(other)["feishu_open_id"], "ou_two")

    def test_verify_detects_local_edit_during_network_call(self):
        eid = self.employee()
        def remote_with_edit(oid):
            employees.save_employee({"id": eid, "name": "李四"})
            return self.remote(open_id=oid)
        self.get_user.side_effect = remote_with_edit
        with self.assertRaises(employees.EmployeeError) as raised:
            employees.verify_employee(eid)
        self.assertEqual(raised.exception.code, "employee_changed")
        self.assertEqual(self.get(eid)["identity_status"], "pending")
        self.assertEqual(self.get(eid)["name"], "李四")

    def test_verify_detects_push_started_during_network_call(self):
        eid = self.employee()
        event_id, card_id = self.event(eid)
        def remote_with_push(oid):
            db.execute("UPDATE events SET status='pushing' WHERE id=?", (event_id,))
            return self.remote(open_id=oid)
        self.get_user.side_effect = remote_with_push
        with self.assertRaises(employees.EmployeeError) as raised:
            employees.verify_employee(eid)
        self.assertEqual(raised.exception.code, "employee_pushing")
        self.assertEqual(db.query_one("SELECT status FROM cards WHERE id=?", (card_id,))["status"], "ok")

    def test_match_never_binds_even_one_eligible_candidate(self):
        eid = self.employee(feishu_open_id=None)
        self.list_scope.return_value = [self.remote()]
        before = self.get(eid)
        result = employees.match_employee(eid)
        self.assertTrue(result["ok"])
        self.assertFalse(result["requires_selection"])
        self.assertTrue(result["candidates"][0]["eligible"])
        self.get_department.assert_not_called()
        self.assertEqual(before, self.get(eid))

    def test_match_reports_ambiguous_and_ineligible_candidates(self):
        eid = self.employee(feishu_open_id=None)
        self.list_scope.return_value = [self.remote(open_id=oid) for oid in ("ou_a", "ou_b", "ou_c")]
        self.get_user.side_effect = lambda oid: self.remote(open_id=oid, department_ids=["d3"] if oid == "ou_c" else ["d1"])
        result = employees.match_employee(eid)
        self.assertTrue(result["ambiguous"])
        self.assertEqual(sum(c["eligible"] for c in result["candidates"]), 0)
        self.assertEqual(result["candidates"][2]["code"], "ambiguous_employee")

    def test_identity_edits_reset_verified_and_revoke_cards(self):
        for field, value in (("name", "李四"), ("department", "产品部"), ("feishu_open_id", "ou_changed"), ("feishu_user_id", "new-user")):
            with self.subTest(field=field):
                eid = self.employee(name="张三" + field, feishu_open_id="ou_" + field)
                db.execute("UPDATE employees SET identity_status='verified', identity_verified_at='yesterday' WHERE id=?", (eid,))
                event_id, card_id = self.event(eid)
                employees.save_employee({"id": eid, field: value})
                self.assertEqual(self.get(eid)["identity_status"], "verified" if field == "department" else "pending")
                if field != "department":
                    self.assertIsNone(self.get(eid)["identity_verified_at"])
                self.assert_invalidated(event_id, card_id)

    def test_other_profile_changes_revoke_cards(self):
        eid = self.employee()
        event_id, card_id = self.event(eid)
        employees.save_employee({"id": eid, "birth_date": "1992-08-10", "note": "更新备注"})
        self.assert_invalidated(event_id, card_id)

    def test_empty_update_preserves_dates_information_and_approval(self):
        eid = self.employee(join_date="2020-01-01", birth_date="1990-02-01", employee_no="E1", email="a@test", note="保留")
        self.event(eid)
        before = self.get(eid)
        result = employees.import_rows([{"open_id": "ou_one", "姓名": "张三", "部门": " ", "生日": None,
                                         "入职日期": "", "邮箱": None, "工号": "", "备注": ""}])
        self.assertTrue(result["ok"])
        self.assertEqual(before, self.get(eid))
        self.assertEqual(db.query_one("SELECT status FROM events")["status"], "confirmed")

    def test_manual_blank_name_department_rejected_import_blanks_preserved(self):
        eid = self.employee()
        before = self.get(eid)
        for field in ("name", "department"):
            for value in (None, "", "  "):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(employees.EmployeeError):
                        employees.save_employee({"id": eid, field: value})
        self.assertEqual(before, self.get(eid))
        self.assertTrue(employees.import_rows([{"id": eid, "name": "", "department": None}])["ok"])
        self.assertEqual(before, self.get(eid))

    def test_csv_header_mapping_and_physical_row_numbers(self):
        rows = csv.DictReader(io.StringIO("姓名,部门,飞书open_id,生日\n张三,研发部,ou_one,1991-01-02\n李四,研发部,ou_two,不合法\n王五,市场部,ou_three,1993/03/04\n"))
        result = employees.import_rows(rows)
        self.assertEqual((result["added"], result["total"]), (2, 3))
        self.assertEqual(result["errors"][0]["row"], 3)
        self.assertEqual(result["errors"][0]["code"], "invalid_date")
        self.assertEqual(result["errors"][0]["msg"], result["errors"][0]["error"])

    def test_xlsx_native_date_datetime_cells_and_explicit_row_numbers(self):
        result = employees.import_rows([(4, {"姓名": "张三", "部门": "研发部", "生日": date(1990, 3, 4),
                                            "入职日期": datetime(2020, 2, 3)}),
                                        (9, {"姓名": "李四", "入职日期": "2020-02-30"})])
        self.assertEqual(result["errors"][0]["row"], 9)
        row = db.query_one("SELECT * FROM employees")
        self.assertEqual(row["birth_date"], "1990-03-04")
        self.assertEqual(row["join_date"], "2020-02-03")

    def test_same_name_different_department_can_insert(self):
        first = self.employee(feishu_open_id=None)
        result = employees.import_rows([{"姓名": "张三", "部门": "市场部"}])
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(db.query("SELECT * FROM employees")), 2)
        self.assertEqual(self.get(first)["department"], "研发部")

    def test_same_name_same_department_without_id_is_not_merged(self):
        eid = self.employee(feishu_open_id=None)
        before = self.get(eid)
        result = employees.import_rows([{"姓名": "张三", "部门": "研发部", "邮箱": "someone@test", "open_id": "ou_new"}])
        self.assertEqual(result["errors"][0]["code"], "ambiguous_employee")
        self.assertEqual(before, self.get(eid))
        self.assertEqual(len(db.query("SELECT id FROM employees")), 1)

    def test_import_does_not_overwrite_conflicting_identity(self):
        eid = self.employee(employee_no="E1")
        before = self.get(eid)
        result = employees.import_rows([{"open_id": "ou_one", "姓名": "李四"},
                                        {"employee_no": "E1", "department": "研发"},
                                        {"employee_no": "E1", "open_id": "ou_wrong"}])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.get(eid)["department"], "研发")
        for key in ("name", "employee_no", "feishu_open_id"):
            self.assertEqual(before[key], self.get(eid)[key])

    def test_explicit_id_can_edit_but_identifier_collision_rolls_back(self):
        eid = self.employee(employee_no="E1")
        self.employee(name="李四", feishu_open_id="ou_two", employee_no="E2")
        before = self.get(eid)
        result = employees.import_rows([{"id": eid, "name": "名字已改", "employee_no": "E2"},
                                        {"open_id": "ou_one", "employee_no": "E2"}])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(before, self.get(eid))
        self.assertTrue(employees.import_rows([{"id": eid, "name": "张三新名"}])["ok"])

    def test_bad_rows_do_not_abort_later_rows(self):
        result = employees.import_rows([{"_row": 7, "部门": "研发部"}, {"姓名": "", "部门": ""},
                                        "not a row", {"name": "张三", "姓名": "李四"},
                                        {"姓名": "王五", "部门": "研发部"}])
        self.assertEqual((result["added"], result["skipped"], len(result["errors"])), (1, 1, 3))
        self.assertEqual(result["errors"][0]["row"], 7)

    def test_row_savepoint_rolls_back_employee_and_event_invalidation(self):
        eid = self.employee()
        self.event(eid)
        db.execute("CREATE TRIGGER reject_invalid_card BEFORE UPDATE ON cards BEGIN SELECT RAISE(ABORT, 'test conflict'); END")
        before = self.get(eid)
        result = employees.import_rows([{"id": eid, "name": "不应保存"}, {"name": "李四", "department": "市场部"}])
        self.assertEqual((result["added"], len(result["errors"])), (1, 1))
        self.assertEqual(before, self.get(eid))
        self.assertEqual(db.query_one("SELECT status FROM events")["status"], "confirmed")

    def test_infrastructure_failure_rolls_back_entire_import(self):
        original = employees._save
        calls = 0
        def save_or_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("disk failure")
            return original(*args, **kwargs)
        with patch.object(employees, "_save", side_effect=save_or_fail):
            with self.assertRaises(sqlite3.OperationalError):
                employees.import_rows([{"name": "张三"}, {"name": "李四"}])
        self.assertEqual(db.query("SELECT * FROM employees"), [])

    def test_pushing_blocks_save_import_deactivate_and_verify(self):
        eid = self.employee()
        self.event(eid, "pushing")
        before = self.get(eid)
        for operation in (lambda: employees.save_employee({"id": eid, "note": "change"}),
                          lambda: employees.deactivate([eid]), lambda: employees.verify_employee(eid)):
            with self.assertRaises(employees.EmployeeError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "employee_pushing")
        result = employees.import_rows([{"id": eid, "note": "change"}])
        self.assertEqual(result["errors"][0]["code"], "employee_pushing")
        self.assertEqual(before, self.get(eid))
        self.get_user.assert_not_called()

    def test_begin_immediate_precedes_push_check(self):
        eid = self.employee()
        statements, original = [], db.connect
        def traced_connection():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(db, "connect", side_effect=traced_connection):
            employees.save_employee({"id": eid, "note": "checked"})
        begin = statements.index("BEGIN IMMEDIATE")
        push_check = next(i for i, s in enumerate(statements) if "status='pushing'" in s)
        update = next(i for i, s in enumerate(statements) if s.startswith("UPDATE employees"))
        self.assertLess(begin, push_check)
        self.assertLess(push_check, update)

    def test_deactivate_batch_is_atomic_on_pushing_or_missing_id(self):
        first = self.employee()
        second = self.employee(name="李四", feishu_open_id="ou_two")
        self.event(first)
        self.event(second, "pushing")
        for ids in ([first, second], [first, 999]):
            with self.assertRaises(employees.EmployeeError):
                employees.deactivate(ids)
            self.assertEqual(self.get(first)["active"], 1)
            self.assertEqual(db.query_one("SELECT status FROM events WHERE employee_id=?", (first,))["status"], "confirmed")

    def test_deactivate_keeps_history_and_cancels_all_unsent_states(self):
        eid = self.employee()
        pushed, pushed_card = self.event(eid, "pushed")
        before = db.query_one("SELECT * FROM events WHERE id=?", (pushed,))
        pending = [self.event(eid, status, f"2026-10-{i:02}") for i, status in enumerate(
            ("confirmed", "ready", "failed", "generating", "skipped", "simulated"), 1)]
        result = employees.deactivate([eid, eid])
        self.assertEqual(result, {"ok": True, "ids": [eid], "disabled": 1, "cancelled": 5, "mode": "disabled"})
        self.assertEqual(self.get(eid)["active"], 0)
        self.assertEqual(before, db.query_one("SELECT * FROM events WHERE id=?", (pushed,)))
        self.assertEqual(db.query_one("SELECT status FROM cards WHERE id=?", (pushed_card,))["status"], "ok")
        self.assertEqual(len(db.query("SELECT * FROM push_logs")), 1)
        for event_id, card_id in pending:
            self.assert_invalidated(event_id, card_id, "skipped")
        self.assertEqual(employees.deactivate([eid])["cancelled"], 0)

    def test_delivery_unknown_preserved_through_edit_failure_and_deactivation(self):
        eid = self.employee()
        event_id, card_id = self.event(eid, "delivery_unknown")
        ev_before = db.query_one("SELECT * FROM events WHERE id=?", (event_id,))
        card_before = db.query_one("SELECT * FROM cards WHERE id=?", (card_id,))
        employees.save_employee({"id": eid, "note": "change"})
        self.get_user.side_effect = RuntimeError("unavailable")
        employees.verify_employee(eid)
        employees.deactivate([eid])
        self.assertEqual(self.get(eid)["active"], 0)
        self.assertEqual(ev_before, db.query_one("SELECT * FROM events WHERE id=?", (event_id,)))
        self.assertEqual(card_before, db.query_one("SELECT * FROM cards WHERE id=?", (card_id,)))

    def test_simulated_can_be_revoked_even_with_old_pushed_at(self):
        eid = self.employee()
        event_id, card_id = self.event(eid, "simulated")
        db.execute("UPDATE events SET pushed_at='yesterday' WHERE id=?", (event_id,))
        employees.save_employee({"id": eid, "name": "改名"})
        self.assert_invalidated(event_id, card_id)

    def test_sync_empty_or_partial_list_does_not_deactivate_missing_users(self):
        eid = self.employee()
        db.execute("UPDATE employees SET source='feishu' WHERE id=?", (eid,))
        self.list_users.return_value = []
        self.assertEqual(sync.sync_from_feishu(False)["disabled"], 0)
        self.list_users.return_value = [self.remote(open_id="ou_other", name="李四")]
        self.assertEqual(sync.sync_from_feishu(False)["disabled"], 0)
        self.assertEqual(self.get(eid)["active"], 1)

    def test_sync_list_failure_performs_no_writes(self):
        eid = self.employee()
        before = self.get(eid)
        def partial_failure():
            yield self.remote(status={"is_resigned": True})
            raise RuntimeError("list failed mid-page")
        self.list_users.side_effect = partial_failure
        with self.assertRaises(RuntimeError):
            sync.sync_from_feishu(False)
        self.assertEqual(before, self.get(eid))

    def test_sync_only_explicit_resigned_disables_and_cancels_events(self):
        eid = self.employee()
        event_id, card_id = self.event(eid)
        self.list_users.return_value = [self.remote(status={"is_resigned": "false"})]
        self.assertEqual(len(sync.sync_from_feishu(False)["errors"]), 1)
        self.assertEqual(self.get(eid)["active"], 1)
        self.list_users.return_value = [self.remote(status={"is_resigned": True})]
        self.assertEqual(sync.sync_from_feishu(False)["disabled"], 1)
        self.assert_invalidated(event_id, card_id, "skipped")
        self.get_department.assert_not_called()

    def test_sync_does_not_restore_inactive_or_overwrite_existing_dates(self):
        eid = self.employee(active=0, join_date="2020-01-01", birth_date="1990-01-01")
        before = self.get(eid)
        self.list_users.return_value = [self.remote(name="改名", join_time=1735689600)]
        self.assertEqual(sync.sync_from_feishu(False)["skipped"], 1)
        self.assertEqual(before, self.get(eid))

    def test_sync_preserves_department_names_and_automatically_maps_recipient(self):
        self.list_users.return_value = [self.remote(department_ids=["d1", "d2"])]
        result = sync.sync_from_feishu(False)
        self.assertEqual(result["added"], 1)
        emp = db.query_one("SELECT * FROM employees")
        self.assertEqual(set(employees.department_names(emp["department"])), {"研发部", "市场部"})
        self.assertEqual(emp["identity_status"], "verified")
        self.assertEqual(result["binding"]["matched"], 1)
        self.assertEqual(emp["source"], "feishu")

    def test_sync_name_department_conflict_preserves_local_and_revokes_approval(self):
        eid = self.employee(join_date="2020-01-01", email="keep@test")
        event_id, card_id = self.event(eid)
        employees.verify_employee(eid)
        self.list_users.return_value = [self.remote(name="李四", department_ids=["d2"])]
        result = sync.sync_from_feishu(False)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.get(eid)["name"], "张三")
        self.assertEqual(self.get(eid)["department"], "研发部")
        self.assertEqual(self.get(eid)["identity_status"], "failed")
        self.assert_invalidated(event_id, card_id)

    def test_sync_fills_only_blanks_preserving_manual_fields_and_source(self):
        eid = self.employee(join_date="2020-01-01", employee_no="local-E1", note="local", email="keep@test")
        self.list_users.return_value = [self.remote(join_time=1735689600, employee_no="remote-E1", email="remote@test", user_id="u1")]
        self.assertEqual(sync.sync_from_feishu(False)["updated"], 1)
        emp = self.get(eid)
        self.assertEqual((emp["join_date"], emp["employee_no"], emp["email"], emp["source"]),
                         ("2020-01-01", "local-E1", "keep@test", "local"))
        self.assertEqual(emp["feishu_user_id"], "u1")

    def test_sync_department_failure_does_not_block_name_id_mapping(self):
        eid = self.employee()
        self.get_department.side_effect = RuntimeError("department forbidden")
        self.list_users.return_value = [self.remote(), self.remote(name="李四", open_id="ou_two")]
        result = sync.sync_from_feishu(False)
        self.assertEqual((result["added"], result["disabled"], len(result["errors"])), (1, 0, 0))
        self.assertEqual(self.get(eid)["department"], "研发部")
        self.assertEqual(self.get(eid)["active"], 1)

    def test_sync_auto_binds_unique_name_to_existing_unbound_employee(self):
        eid = self.employee(feishu_open_id=None)
        self.list_users.return_value = [self.remote()]
        result = sync.sync_from_feishu(False)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.get(eid)["feishu_open_id"], "ou_one")
        self.assertEqual(self.get(eid)["identity_status"], "verified")
        self.assertEqual(len(db.query("SELECT id FROM employees")), 1)

    def test_sync_respects_pushing_lock_and_preserves_unknown_deliveries(self):
        eid = self.employee()
        event_id, _ = self.event(eid, "pushing")
        self.list_users.return_value = [self.remote(status={"is_resigned": True})]
        result = sync.sync_from_feishu(False)
        self.assertEqual(result["errors"][0]["code"], "employee_pushing")
        self.assertEqual(self.get(eid)["active"], 1)
        db.execute("UPDATE events SET status='delivery_unknown' WHERE id=?", (event_id,))
        result = sync.sync_from_feishu(False)
        self.assertEqual(result["disabled"], 1)
        self.assertEqual(db.query_one("SELECT status FROM events WHERE id=?", (event_id,))["status"], "delivery_unknown")

    def test_sync_conflicting_duplicate_open_ids_are_rejected(self):
        eid = self.employee()
        self.list_users.return_value = [self.remote(), self.remote(status={"is_resigned": True})]
        result = sync.sync_from_feishu(False)
        self.assertEqual(result["errors"][0]["code"], "conflicting_directory_rows")
        self.assertEqual(self.get(eid)["active"], 1)

    def test_sync_only_known_birthday_attribute_is_used(self):
        user = self.remote(custom_attrs=[{"id": "other", "value": {"text": "2020-01-01"}},
                                        {"id": "birth", "value": {"text": "1990-02-03"}}])
        self.assertIsNone(sync._extract_birthday(user, set()))
        self.assertEqual(sync._extract_birthday(user, {"birth"}), "1990-02-03")
        self.custom_attrs.side_effect = RuntimeError("no access")
        self.list_users.return_value = [user]
        sync.sync_from_feishu()
        self.assertIsNone(db.query_one("SELECT birth_date FROM employees")["birth_date"])

    def test_month_day_birthday_accepts_leap_day_without_fake_age(self):
        eid = self.employee(birth_date="02-29")
        self.assertEqual(self.get(eid)["birth_date"], "1896-02-29")
        with self.assertRaises(employees.EmployeeError):
            employees.save_employee({"id": eid, "join_date": "02-29"})


class DateParsingTests(unittest.TestCase):
    def test_chinese_full_dates_and_existing_formats(self):
        for raw, expected in (
            ("2021年2月1日", "2021-02-01"),
            ("2026年8月3日", "2026-08-03"),
            (" 2024 年 2 月 29 日 ", "2024-02-29"),
            ("2021年02月01日 00:00:00", "2021-02-01"),
            ("2021-02-01", "2021-02-01"),
            ("2021/2/1", "2021-02-01"),
            ("2021.2.1", "2021-02-01"),
            ("20210201", "2021-02-01"),
        ):
            for field in ("join_date", "birth_date"):
                with self.subTest(raw=raw, field=field):
                    self.assertEqual(employees._date(raw, field), expected)

    def test_chinese_month_day_only_allowed_for_birthdays(self):
        for raw, expected in (("2月1日", "1900-02-01"), ("2 月 29 日", "1896-02-29")):
            with self.subTest(raw=raw):
                self.assertEqual(employees._date(raw, "birth_date"), expected)
                with self.assertRaises(employees.EmployeeError) as raised:
                    employees._date(raw, "join_date")
                self.assertEqual(raised.exception.code, "invalid_date")
                self.assertIn("必须包含年份", str(raised.exception))

    def test_chinese_dates_still_reject_impossible_or_malformed_dates(self):
        for raw in ("2021年2月29日", "2024年2月30日", "2021年13月1日",
                    "2021年0月1日", "2021年2月0日", "2021年2月1日其他",
                    "2021年2月1日 25:00:00", "2月30日"):
            for field, label in (("join_date", "入职日期"), ("birth_date", "生日")):
                with self.subTest(raw=raw, field=field):
                    with self.assertRaises(employees.EmployeeError) as raised:
                        employees._date(raw, field)
                    self.assertEqual(raised.exception.code, "invalid_date")
                    self.assertIn(label, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
