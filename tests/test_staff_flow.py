"""Six-column imports and automatic binding in an isolated database; no network."""
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import openpyxl
from fastapi.testclient import TestClient

from app import bindings, db, employees, feishu, main, staff_template


class StaffFlowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, attr, value in [(db, "DB_PATH", Path(folder.name) / "staff.db"),
                                    (main, "ADMIN_TOKEN", ""), (feishu, "FEISHU_APP_ID", ""),
                                    (feishu, "FEISHU_APP_SECRET", "")]:
            mock = patch.object(target, attr, value)
            mock.start()
            self.addCleanup(mock.stop)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No real network"))
        network.start()
        self.addCleanup(network.stop)
        db.init_db()
        self.client = TestClient(main.app)

    def employee(self, **changes):
        return employees.save_employee({"name": "测试姓名甲", "department": "本地部门", "employee_no": "001", **changes})["id"]

    def remote(self, **changes):
        return {"name": "测试姓名甲", "open_id": "ou_test", "status": {"is_resigned": False}, **changes}

    def row(self, eid):
        return db.query_one("SELECT * FROM employees WHERE id=?", (eid,))

    def test_excel_download_is_blank_six_columns_and_authenticated(self):
        response = self.client.get("/api/employees/template.xlsx")
        self.assertEqual(response.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertEqual(list(wb.active.values), [staff_template.HEADERS])
        self.assertEqual(wb.active.column_dimensions["C"].number_format, "@")
        self.assertEqual(wb.active["E1"].comment.text[:6], "填写完整日期")
        self.assertEqual(wb.active.data_validations.dataValidation[0].formula1, '"在职,离职"')
        wb.close()
        with patch.object(main, "ADMIN_TOKEN", "test-secret"):
            self.assertEqual(self.client.get("/api/employees/template.xlsx").status_code, 401)

    def test_excel_roundtrip_preserves_zeroes_dates_status_without_feishu(self):
        wb = openpyxl.load_workbook(io.BytesIO(staff_template.build()))
        wb.active.append(["测试姓名甲", "部门甲", "001", date(2021, 2, 1), date(2000, 2, 29), "在职"])
        wb.active.append(["测试姓名乙", "部门乙", "002", "2023年3月1日", "1990年12月1日", "离职"])
        stream = io.BytesIO()
        wb.save(stream)
        wb.close()
        response = self.client.post("/api/employees/import", files={"file": ("staff.xlsx", stream.getvalue())})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["binding"]["pending"], 1)
        self.assertIn("连接飞书", result["binding"]["msg"])
        rows = db.query("SELECT * FROM employees ORDER BY id")
        self.assertEqual((rows[0]["employee_no"], rows[0]["join_date"], rows[0]["birth_date"]), ("001", "2021-02-01", "2000-02-29"))
        self.assertEqual(rows[1]["active"], 0)
        self.assertIsNone(rows[0]["feishu_open_id"])

    def test_unique_name_binds_without_department_or_second_network_read(self):
        eid = self.employee()
        with patch.object(feishu, "get_user") as get_user, patch.object(feishu, "get_department") as get_department:
            result = bindings.auto_bind([eid], users=[self.remote()])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(self.row(eid)["feishu_open_id"], "ou_test")
        self.assertEqual(self.row(eid)["department"], "本地部门")
        get_user.assert_not_called()
        get_department.assert_not_called()

    def test_remote_same_name_and_conflicting_id_are_not_guessed(self):
        for users in ([self.remote(), self.remote(open_id="ou_other")],
                      [self.remote(), self.remote(name="测试姓名乙")],
                      [self.remote(name="测试姓名甲乙")]):
            with self.subTest(users=users):
                eid = self.employee()
                result = bindings.auto_bind([eid], users=users)
                self.assertEqual(result["matched"], 0)
                self.assertEqual(result["pending"], 1)
                self.assertIsNone(self.row(eid)["feishu_open_id"])
                db.execute("DELETE FROM employees WHERE id=?", (eid,))

    def test_local_same_names_cannot_share_or_split_feishu_identity(self):
        first = self.employee()
        second = self.employee(employee_no="002", department="另一个部门")
        result = bindings.auto_bind(users=[self.remote()])
        self.assertEqual((result["matched"], result["pending"]), (0, 2))
        self.assertIsNone(self.row(first)["feishu_open_id"])
        self.assertIsNone(self.row(second)["feishu_open_id"])

    def test_existing_id_is_not_silently_replaced(self):
        eid = self.employee(feishu_open_id="ou_old")
        result = bindings.auto_bind([eid], users=[self.remote()])
        self.assertEqual(result["matched"], 0)
        self.assertIn("冲突", result["errors"][0]["msg"])
        self.assertEqual(self.row(eid)["feishu_open_id"], "ou_old")

    def test_connection_failure_does_not_undo_employee_save(self):
        with patch.object(feishu, "FEISHU_APP_ID", "test-id"), patch.object(feishu, "FEISHU_APP_SECRET", "test-secret"), \
                patch.object(feishu, "list_users", side_effect=RuntimeError("timeout")):
            response = self.client.post("/api/employees", json={"name": "测试姓名甲", "department": "部门甲", "active": 1})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["binding"]["pending"], 1)
        self.assertIsNotNone(self.row(result["id"]))

    def test_save_and_import_invoke_automatic_binding(self):
        with patch.object(feishu, "FEISHU_APP_ID", "test-id"), patch.object(feishu, "FEISHU_APP_SECRET", "test-secret"), \
                patch.object(feishu, "list_users", return_value=[self.remote()]) as directory:
            response = self.client.post("/api/employees", json={"name": "测试姓名甲", "department": "部门甲", "employee_no": "001"})
            self.assertEqual(response.json()["binding"]["matched"], 1)
            raw = "姓名,部门,工号,入职日期,出生年月,在职状态\n测试姓名甲,部门乙,001,2021年2月1日,2000-02-29,在职\n".encode("utf-8")
            imported = self.client.post("/api/employees/import", files={"file": ("staff.csv", raw)}).json()
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["updated"], 1)
        self.assertEqual(imported["binding"]["matched"], 1)
        self.assertEqual(directory.call_count, 2)
        self.assertEqual(self.row(response.json()["id"])["department"], "部门乙")
