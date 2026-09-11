"""Saving stays responsive while identity work runs separately; no real contacts."""
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import bindings, db, employees, main


class BindingTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target, key, value in ((db, "DB_PATH", Path(temporary.name) / "save.db"),
                                   (bindings, "_binding_tasks", {}), (main, "ADMIN_TOKEN", "")):
            handle = patch.object(target, key, value)
            handle.start()
            self.addCleanup(handle.stop)
        handle = patch("requests.sessions.Session.request", side_effect=AssertionError("No real network"))
        handle.start()
        self.addCleanup(handle.stop)
        db.init_db()
        self.client = TestClient(main.app)
        self.employee = employees.save_employee({"name": "离线保存", "department": "测试部门", "feishu_open_id": "ou_saved"})["employee"]

    def save(self, **changes):
        return self.client.post("/api/employees?defer_binding=true", json={"id": self.employee["id"], **changes})

    def test_save_returns_while_remote_binding_is_still_waiting(self):
        entered, release = threading.Event(), threading.Event()
        expected = {"matched": 1, "pending": 0, "errors": [], "msg": "对应完成"}

        def slow_bind(ids):
            self.assertEqual(ids, [self.employee["id"]])
            entered.set()
            self.assertTrue(release.wait(5), "Test did not release directory read")
            return expected

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(bindings, "_binding_pool", pool), \
                patch.object(bindings, "auto_bind", side_effect=slow_bind):
            try:
                result = self.save(employee_no="001").json()
                self.assertTrue(result["ok"])
                self.assertTrue(entered.wait(1))
                task_id = result["binding"]["task_id"]
                self.assertEqual(self.client.get(f"/api/binding-tasks/{task_id}").json()["status"], "running")
                self.assertEqual(db.query_one("SELECT employee_no FROM employees WHERE id=?", (self.employee["id"],))["employee_no"], "001")
            finally:
                release.set()
        completed = self.client.get(f"/api/binding-tasks/{task_id}").json()
        self.assertEqual((completed["status"], completed["matched"]), ("done", 1))

    def test_binding_failure_or_worker_failure_does_not_turn_save_into_error(self):
        with patch.object(bindings._binding_pool, "submit", side_effect=lambda fn: fn()), \
                patch.object(bindings, "auto_bind", side_effect=RuntimeError("private-secret-must-not-be-returned")):
            response = self.save(employee_no="002")
        self.assertEqual(response.status_code, 200)
        task = self.client.get('/api/binding-tasks/' + response.json()["binding"]["task_id"]).json()
        self.assertEqual(task["status"], "failed")
        self.assertIn("资料已保存", task["msg"])
        self.assertNotIn("private-secret", str(task))
        with patch.object(bindings._binding_pool, "submit", side_effect=RuntimeError("worker unavailable")):
            response = self.save(employee_no="003")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["binding"]["status"], "failed")
        self.assertEqual(db.query_one("SELECT employee_no FROM employees WHERE id=?", (self.employee["id"],))["employee_no"], "003")

    def test_batch_only_queues_changed_ids_after_atomic_commit(self):
        original = {key: self.employee[key] for key in employees.EDIT_FIELDS}
        calls = []
        with patch.object(bindings._binding_pool, "submit", side_effect=lambda fn: calls.append(fn)), \
                patch.object(bindings, "auto_bind", return_value={"matched": 0, "pending": 1, "errors": [], "msg": "等待飞书"}) as remote:
            response = self.client.post('/api/employees/batch-update?defer_binding=true', json={"updates": [
                {"id": self.employee["id"], "original": original, "changes": {"department": "新部门"}}]})
            self.assertEqual(response.status_code, 200, response.text)
            remote.assert_not_called()
            self.assertEqual(db.query_one("SELECT department FROM employees WHERE id=?", (self.employee["id"],))["department"], "新部门")
            calls[0]()
            remote.assert_called_once_with([self.employee["id"]])

    def test_invalid_save_and_unauthenticated_requests_never_queue_work(self):
        with patch.object(bindings._binding_pool, "submit") as submit:
            self.assertEqual(self.save(feishu_open_id="invalid").status_code, 400)
            with patch.object(main, "ADMIN_TOKEN", "test-only-token"):
                self.assertEqual(self.save(employee_no="004").status_code, 401)
                self.assertEqual(self.client.get('/api/binding-tasks/anything').status_code, 401)
            submit.assert_not_called()
        self.assertEqual(self.client.get('/api/binding-tasks/missing').status_code, 404)

    def test_queue_limit_and_empty_batch_preserve_saved_data(self):
        bindings._binding_tasks.update({str(i): {"status": "running"} for i in range(20)})
        with patch.object(bindings._binding_pool, "submit") as submit:
            result = self.save(employee_no="005").json()
            self.assertTrue(result["ok"])
            self.assertEqual(result["binding"]["status"], "failed")
            self.assertEqual(bindings.submit_auto_bind([])["status"], "done")
            submit.assert_not_called()
