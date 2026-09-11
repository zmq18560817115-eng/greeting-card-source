"""Configuration checks use temporary files, mocked Feishu, and no employee data."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import feishu, feishu_config, main, settings


class FeishuConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ".env"
        for target in (
            patch.object(feishu_config, "ENV_PATH", self.path),
            patch.object(feishu, "FEISHU_APP_ID", ""),
            patch.object(feishu, "FEISHU_APP_SECRET", ""),
            patch.object(settings, "FEISHU_APP_ID", ""),
            patch.object(settings, "FEISHU_APP_SECRET", ""),
            patch.object(feishu, "_token", {"value": "old-token", "expire_at": 100}),
            patch.object(main, "ADMIN_TOKEN", "test-admin"),
            patch.dict(os.environ),
            patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.client = TestClient(main.app)
        self.headers = {"X-Admin-Token": "test-admin"}

    def test_save_updates_runtime_and_preserves_unrelated_local_settings(self):
        self.path.write_text("# Keep local settings\nDRY_RUN=true\nADMIN_TOKEN=keep\nFEISHU_APP_ID=old\nFEISHU_APP_ID=duplicate\n", encoding="utf-8")
        result = feishu_config.save_config("cli_example", "secret-for-test")
        content = self.path.read_text(encoding="utf-8")
        self.assertIn("DRY_RUN=true\nADMIN_TOKEN=keep\n", content)
        self.assertEqual(content.count("FEISHU_APP_ID="), 1)
        self.assertEqual((feishu.FEISHU_APP_ID, settings.FEISHU_APP_ID), ("cli_example", "cli_example"))
        self.assertEqual(os.environ["FEISHU_APP_SECRET"], "secret-for-test")
        self.assertEqual(feishu._token, {"value": None, "expire_at": 0})
        self.assertTrue(result["configured"])
        self.assertNotIn("secret-for-test", json.dumps(result))

    def test_blank_secret_preserves_saved_secret_only_for_same_app(self):
        feishu_config.save_config("cli_example", "secret-for-test")
        feishu_config.save_config("cli_example", "")
        self.assertEqual(feishu.FEISHU_APP_SECRET, "secret-for-test")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            feishu_config.save_config("cli_other", "")
        self.assertEqual(self.path.read_bytes(), before)

    def test_credentials_cannot_change_during_recipient_operation(self):
        @feishu.in_application
        def check():
            with self.assertRaisesRegex(ValueError, '正在读取通讯录或执行推送'):
                feishu_config.save_config('cli_example', 'secret-for-test')
            self.assertFalse(self.path.exists())
        check()
        feishu_config.save_config('cli_example', 'secret-for-test')
        self.assertEqual(feishu.FEISHU_APP_ID, 'cli_example')

    def test_missing_group_permission_cannot_report_connection_ready(self):
        with patch.object(feishu, 'tenant_access_token', return_value='test-token'), \
             patch.object(feishu, '_get', side_effect=[{'data': {'group_ids':['g1']}},
                feishu.FeishuError(99991672, 'contact:group:readonly')]):
            result = feishu_config.check_connection()
        self.assertFalse(result['ok'])
        self.assertIn('读取用户组', result['msg'])

    def test_reject_invalid_or_multiline_credentials_without_writing(self):
        for app_id, secret in (("invalid", "secret"), ("cli_example", ""),
                               ("cli_example", "secret\nDRY_RUN=false"), (None, "secret")):
            with self.subTest(app_id=app_id):
                with self.assertRaises(ValueError):
                    feishu_config.save_config(app_id, secret)
                self.assertFalse(self.path.exists())

    def test_failed_file_replace_preserves_original_file_and_runtime(self):
        self.path.write_text("DRY_RUN=true\n", encoding="utf-8")
        with patch.object(feishu_config.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                feishu_config.save_config("cli_example", "secret-for-test")
        self.assertEqual(self.path.read_text(), "DRY_RUN=true\n")
        self.assertEqual(feishu.FEISHU_APP_ID, "")
        self.assertEqual(list(self.path.parent.glob(".env.*.tmp")), [])

    def test_config_endpoints_require_admin_and_never_return_secret(self):
        payload = {"app_id": "cli_example", "app_secret": "secret-for-test"}
        for method, url in (("get", "/api/feishu/config"), ("post", "/api/feishu/config"), ("post", "/api/feishu/check")):
            response = getattr(self.client, method)(url, **({"json": payload} if method == "post" else {}))
            self.assertEqual(response.status_code, 401)
        self.assertFalse(self.path.exists())
        response = self.client.post("/api/feishu/config", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("secret-for-test", response.text)
        response = self.client.get("/api/feishu/config", headers=self.headers)
        self.assertTrue(response.json()["secret_configured"])
        self.assertNotIn("secret-for-test", response.text)

    def test_missing_credentials_explains_where_to_connect(self):
        result = feishu_config.check_connection()
        self.assertFalse(result["ok"])
        self.assertIn("飞书连接", result["msg"])

    def test_valid_token_does_not_hide_directory_permission_failure(self):
        with patch.object(feishu, "tenant_access_token", return_value="test-token"), \
             patch.object(feishu, "_get", side_effect=feishu.FeishuError(99991672, "permission denied")):
            result = feishu_config.check_connection()
        self.assertFalse(result["ok"])
        self.assertIn("99991672", result["msg"])

    def test_directory_check_reads_real_api_contract_without_returning_employee_ids(self):
        with patch.object(feishu, "tenant_access_token", return_value="test-token"), \
             patch.object(feishu, "_get", return_value={"data": {"department_ids": ["od_example"]}}) as get:
            result = feishu_config.check_connection()
        self.assertTrue(result["ok"])
        self.assertNotIn("od_example", json.dumps(result))
        self.assertEqual(get.call_args.args[0], "/open-apis/contact/v3/scopes")
        self.assertEqual(get.call_args.args[1]["user_id_type"], "open_id")

    def test_empty_scope_is_not_reported_as_ready(self):
        with patch.object(feishu, "tenant_access_token", return_value="test-token"), \
             patch.object(feishu, "_get", return_value={"data": {"user_ids": [], "department_ids": []}}):
            result = feishu_config.check_connection()
        self.assertFalse(result["ok"])
        self.assertIn("授权范围为空", result["msg"])

    def test_connection_errors_redact_configured_credentials(self):
        with patch.object(feishu, "FEISHU_APP_SECRET", "private-test-secret"):
            result = feishu.connection_error(feishu.FeishuError(123, "private-test-secret old-token"))
        self.assertNotIn("private-test-secret", result)
        self.assertNotIn("old-token", result)


if __name__ == "__main__":
    unittest.main()
