"""Temporary poster files, local ASGI requests and no external services."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles

from app import access, main


class PrivateAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'poster.png').write_bytes(b'private-poster-bytes')
        self.mount = next(route for route in main.app.routes if getattr(route, 'path', '') == '/files')
        for item in (patch.object(main, 'ADMIN_TOKEN', 'private-admin-test'),
                     patch.object(self.mount, 'app', StaticFiles(directory=root)),
                     patch('requests.sessions.Session.request', side_effect=AssertionError('No network'))):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.headers = {'X-Admin-Token':'private-admin-test'}

    def test_anonymous_image_head_and_range_are_rejected(self):
        for method in ('get', 'head'):
            with self.subTest(method=method):
                response = getattr(self.client, method)('/files/poster.png', headers={'Range':'bytes=0-4'})
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(b'private-poster-bytes', response.content)
                self.assertIn('no-store', response.headers['cache-control'])

    def test_authenticated_preview_cookie_works_without_secret_in_url(self):
        response = self.client.get('/api/feishu/config', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie']
        self.assertNotIn('private-admin-test', cookie)
        for flag in ('HttpOnly', 'SameSite=strict', 'Path=/files', 'Max-Age=3600'):
            self.assertIn(flag, cookie)
        image = self.client.get('/files/poster.png')
        self.assertEqual(image.content, b'private-poster-bytes')
        self.assertIn('no-store', image.headers['cache-control'])
        self.assertEqual(self.client.get('/api/feishu/config').status_code, 401)

    def test_direct_header_access_and_wrong_token(self):
        self.assertEqual(self.client.get('/files/poster.png', headers={'X-Admin-Token':'wrong'}).status_code, 401)
        self.assertEqual(self.client.get('/files/poster.png', headers=self.headers).status_code, 200)
        # Even an admin URL query cannot become a shareable poster credential.
        self.assertEqual(self.client.get('/files/poster.png?token=private-admin-test').status_code, 401)

    def test_expired_tampered_or_rotated_sessions_fail(self):
        with patch.object(access.time, 'time', return_value=1000):
            token = access.issue_file_session('private-admin-test')
            self.assertTrue(access.valid_file_session(token, 'private-admin-test'))
            self.assertFalse(access.valid_file_session(token, 'rotated-secret'))
            self.assertFalse(access.valid_file_session(token+'x', 'private-admin-test'))
        with patch.object(access.time, 'time', return_value=4600):
            self.assertFalse(access.valid_file_session(token, 'private-admin-test'))
        self.client.cookies.set(access.COOKIE_NAME, token, path='/files')
        self.assertEqual(self.client.get('/files/poster.png').status_code, 401)

    def test_https_issues_secure_cookie_and_private_response(self):
        client = TestClient(main.app, base_url='https://testserver')
        self.addCleanup(client.close)
        response = client.get('/api/feishu/config', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Secure', response.headers['set-cookie'])
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
        self.assertEqual(client.get('/files/poster.png').status_code, 200)

    def test_local_only_debug_remains_available(self):
        with patch.object(main, 'ADMIN_TOKEN', ''):
            self.assertEqual(self.client.get('/files/poster.png').status_code, 200)

    def test_lan_binding_requires_admin_token(self):
        for host in ('0.0.0.0', '192.168.1.20', '10.0.0.20', '::'):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, 'ADMIN_TOKEN'):
                access.require_private_access(host, '')
        for host in ('127.0.0.1', 'localhost', '::1'):
            access.require_private_access(host, '')
        access.require_private_access('192.168.1.20', 'private-admin-test')


if __name__ == '__main__':
    unittest.main()
