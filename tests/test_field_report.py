"""Field diagnostics use a temporary database and mocked directory responses."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import db, feishu, field_report, main, sync


class FieldReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for target in (
            patch.object(db, 'DB_PATH', Path(temporary.name) / 'test.db'),
            patch.object(feishu, 'FEISHU_APP_ID', 'cli_test_fields'),
            patch.object(main, 'ADMIN_TOKEN', 'test-admin'),
            patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')),
        ):
            target.start()
            self.addCleanup(target.stop)
        db.init_db()
        db.execute("INSERT INTO employees(name,department,active) VALUES('本地名字','保留部门',1)")
        db.execute("INSERT INTO employees(name,department,active) VALUES('离职名字','部门',0)")
        self.client = TestClient(main.app)
        self.headers = {'X-Admin-Token': 'test-admin'}

    def fields(self, result):
        return {field['key']: field for field in result['fields']}

    def test_local_mapping_counts_active_staff_without_network_or_private_values(self):
        result = field_report.local_report()
        self.assertEqual(result['local_total'], 1)
        fields = self.fields(result)
        self.assertEqual(fields['department']['local_filled'], 1)
        self.assertEqual(fields['employee_no']['local_filled'], 0)
        self.assertIsNone(result['remote_total'])
        self.assertIsNone(fields['department']['remote'])
        self.assertNotIn('本地名字', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('保留部门', json.dumps(result, ensure_ascii=False))

    def test_field_check_distinguishes_absent_empty_and_available_without_writing(self):
        users = [
            {'open_id': 'ou_one', 'name': '远程名字', 'employee_no': '',
             'department_ids': ['od-one'], 'join_time': 1735689600, 'status': {'is_resigned': False}},
            {'open_id': 'ou_two', 'name': '另一名字', 'employee_no': 'secret-number',
             'department_ids': ['od-one'], 'join_time': 'bad-date', 'status': {}},
        ]
        before = db.query('SELECT * FROM employees')
        with patch.object(feishu, 'list_users', return_value=users), \
             patch.object(feishu, 'list_custom_attrs', return_value=[]), \
             patch.object(feishu, 'get_department', return_value={'open_department_id': 'od-one', 'name': '远程部门'}) as department:
            result = field_report.check_fields()
        fields = self.fields(result)
        self.assertTrue(result['ok'])
        self.assertEqual(result['remote_total'], 2)
        self.assertEqual(fields['employee_no']['remote']['empty'], 1)
        self.assertEqual(fields['employee_no']['remote']['available'], 1)
        self.assertEqual(fields['join_date']['remote']['invalid'], 1)
        self.assertEqual(fields['department']['remote']['available'], 2)
        self.assertEqual(fields['active']['remote']['not_returned'], 1)
        self.assertEqual(fields['birth_date']['remote']['unavailable'], 2)
        department.assert_called_once()
        self.assertEqual(db.query('SELECT * FROM employees'), before)
        for value in ('远程名字', '远程部门', 'secret-number', 'ou_one'):
            self.assertNotIn(value, json.dumps(result, ensure_ascii=False))

    def test_unreadable_department_is_reported_without_claiming_missing_id(self):
        with patch.object(feishu, 'list_users', return_value=[{'department_ids': ['od-one']}, {}]), \
             patch.object(feishu, 'list_custom_attrs', return_value=[]), \
             patch.object(feishu, 'get_department', side_effect=feishu.FeishuError(99991672, 'denied')):
            result = field_report.check_fields()
        counts = self.fields(result)['department']['remote']
        self.assertEqual((counts['unreadable'], counts['not_returned'], counts['available']), (1, 1, 0))

    def test_birthday_only_uses_identified_custom_field(self):
        users = [{'custom_attrs': [{'id': 'other', 'value': {'text': '1990-02-03'}}]},
                 {'custom_attrs': [{'id': 'birth', 'value': {'text': '1990年2月3日'}}]},
                 {'custom_attrs': [{'id': 'birth', 'value': {'text': 'invalid'}}]}]
        with patch.object(feishu, 'list_users', return_value=users), \
             patch.object(feishu, 'list_custom_attrs', return_value=[{'id': 'birth', 'name': '出生日期'}]):
            result = field_report.check_fields()
        counts = self.fields(result)['birth_date']['remote']
        self.assertEqual((counts['not_returned'], counts['available'], counts['invalid']), (1, 1, 1))

    def test_incomplete_directory_failure_keeps_remote_counts_unknown(self):
        with patch.object(feishu, 'list_users', side_effect=feishu.FeishuError(99991672, 'contact:group:readonly')), \
             patch.object(feishu, 'list_custom_attrs') as attrs:
            result = field_report.check_fields()
        self.assertFalse(result['ok'])
        self.assertIsNone(result['remote_total'])
        self.assertTrue(all(field['remote'] is None for field in result['fields']))
        self.assertIn('99991672', result['error'])
        attrs.assert_not_called()

    def test_diagnostic_endpoints_require_admin(self):
        self.assertEqual(self.client.get('/api/feishu/fields').status_code, 401)
        self.assertEqual(self.client.post('/api/feishu/fields/check').status_code, 401)
        self.assertEqual(self.client.get('/api/feishu/fields', headers=self.headers).status_code, 200)
        with patch.object(feishu, 'list_users', return_value=[]), patch.object(feishu, 'list_custom_attrs', return_value=[]):
            response = self.client.post('/api/feishu/fields/check', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['remote_total'], 0)

    def test_sync_reports_department_failure_once_and_keeps_existing_data(self):
        users = [{'name': '同步名字', 'open_id': 'ou_one', 'department_ids': ['od-one'], 'status': {'is_resigned': False}},
                 {'name': '同步名字二', 'open_id': 'ou_two', 'department_ids': ['od-one'], 'status': {'is_resigned': False}}]
        with patch.object(feishu, 'list_users', return_value=users), \
             patch.object(feishu, 'get_department', side_effect=feishu.FeishuError(99991672, 'denied')), \
             patch.object(sync.bindings, 'auto_bind', return_value={}):
            result = sync.sync_from_feishu(False)
        self.assertEqual(result['added'], 2)
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('部门未同步', result['warnings'][0])
        self.assertEqual(db.query_one("SELECT department FROM employees WHERE name='本地名字'")['department'], '保留部门')


if __name__ == '__main__':
    unittest.main()
