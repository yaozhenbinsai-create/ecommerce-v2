import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import security_secrets
from app.commerce import access, account_setup, secure_auth as auth


class TokenParser(HTMLParser):
    token = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('id') == 'setup-csrf':
            self.token = attrs['value']


class AccountSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'local_auth.json').write_text('{"required":true}', encoding='utf-8')
        env = patch.dict(os.environ, {'ECOMMERCE2_DATA_DIR': str(self.root), 'SHOP_SECURITY_MODE': '0', 'SHOP_PUBLIC_MODE': '0'})
        env.start(); self.addCleanup(env.stop)
        backend = patch.object(security_secrets, '_backend', return_value=security_secrets._AESGCM(os.urandom(32)))
        backend.start(); self.addCleanup(backend.stop)
        for name in ('PENDING',):
            state = patch.dict(getattr(account_setup, name), {}, clear=True)
            state.start(); self.addCleanup(state.stop)
        state = patch.dict(access.SESSIONS, {}, clear=True)
        state.start(); self.addCleanup(state.stop)
        failures = patch.object(account_setup, 'FAILURES', [])
        failures.start(); self.addCleanup(failures.stop)
        audit = patch.object(access, '_audit')
        self.audit = audit.start(); self.addCleanup(audit.stop)
        self.app = FastAPI()
        access.install(self.app)
        @self.app.get('/')
        def home(): return {'business_data': 'private'}
        @self.app.get('/api/probe')
        def probe(): return {'business_data': 'private'}
        @self.app.get('/healthz')
        def health(): return {'status': 'ok'}
        self.client = self.new_client()
        self.password = 'Independent!Pass9087'

    def new_client(self, peer='127.0.0.1'):
        return TestClient(self.app, base_url='http://127.0.0.1:8000', client=(peer, 45000))

    def begin(self, client=None, username='ADMIN'):
        client = client or self.client
        page = client.get('/commerce-setup')
        self.assertEqual(page.status_code, 200, page.text)
        parser = TokenParser(); parser.feed(page.text)
        headers = {'origin': 'http://127.0.0.1:8000', 'x-setup-csrf': parser.token}
        response = client.post('/commerce-setup/begin', json={'username': username}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), headers

    def finish(self, info, headers, client=None, **changes):
        payload = {'username': 'ADMIN', 'password': self.password, 'confirm_password': self.password,
                   'otp': auth.totp_code(info['secret'], int(time.time() // 30)), 'recovery_saved': True, **changes}
        return (client or self.client).post('/commerce-setup/finish', json=payload, headers=headers)

    def test_first_visit_protects_business_and_exposes_only_setup_and_minimal_health(self):
        auth.validate_configuration()
        response = self.client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/commerce-setup')
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        self.assertEqual(self.client.get('/healthz').json(), {'status': 'ok'})
        self.assertTrue(self.client.get('/api/auth/session').json()['setup_required'])
        self.assertFalse(auth.store_path().exists())

    def test_remote_peer_and_forwarded_headers_cannot_use_local_http_exception(self):
        remote = self.new_client('192.0.2.4')
        for path in ('/commerce-setup', '/api/probe', '/healthz', '/commerce-login'):
            self.assertEqual(remote.get(path, headers={'host': 'localhost:8000', 'x-forwarded-for': '127.0.0.1',
                                                       'x-forwarded-proto': 'https'}).status_code, 403)
        self.assertEqual(self.client.get('/commerce-setup', headers={'host': 'attacker.example'}).status_code, 403)
        with patch.dict(os.environ, {'SHOP_PUBLIC_MODE': '1'}):
            self.assertEqual(self.client.get('/commerce-setup').status_code, 503)

    def test_csrf_exact_origin_and_cookie_are_required(self):
        info, headers = self.begin()
        for origin in ('null', 'http://evil.example', 'http://127.0.0.1:9000', 'https://127.0.0.1:8000', ''):
            response = self.finish(info, dict(headers, origin=origin))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.finish(info, dict(headers, **{'x-setup-csrf': 'wrong'})).status_code, 403)
        self.assertEqual(self.finish(info, headers, client=self.new_client()).status_code, 403)
        self.assertFalse(auth.store_path().exists())

    def test_cross_site_embeds_cannot_exhaust_first_setup_capacity(self):
        for destination in ('iframe', 'image', 'empty', 'document'):
            response = self.client.get('/commerce-setup', headers={'sec-fetch-site': 'cross-site', 'sec-fetch-dest': destination})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(account_setup.PENDING, {})
        response = self.client.get('/commerce-setup', headers={'sec-fetch-site': 'cross-site', 'sec-fetch-dest': 'document', 'sec-fetch-user': '?1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(account_setup.PENDING), 1)

    def test_enrollment_then_mfa_login_logout_and_protected_status(self):
        info, headers = self.begin()
        response = self.finish(info, headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(auth.store_path().read_bytes().startswith(security_secrets.MAGIC))
        self.assertNotIn(self.password.encode(), auth.store_path().read_bytes())
        self.assertFalse(auth.bootstrap_allowed())
        self.assertEqual(self.client.get('/commerce-setup').status_code, 403)
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        page = self.client.get('/commerce-login?setup=complete')
        self.assertIn('name="otp"', page.text)
        self.assertIn('下一组验证码', page.text)
        self.assertEqual(self.client.post('/commerce-login', data={'username': 'ADMIN', 'password': self.password, 'otp': ''}).status_code, 401)
        # A recovery code can be used once without waiting for the next real TOTP interval.
        logged = self.client.post('/commerce-login', data={'username': 'ADMIN', 'password': self.password, 'otp': info['recovery_codes'][0]}, follow_redirects=False)
        self.assertEqual(logged.status_code, 303)
        self.assertIn('httponly', logged.headers['set-cookie'].lower())
        self.assertIn('samesite=strict', logged.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/api/probe').status_code, 200)
        self.assertEqual(self.client.get('/api/auth/session').json()['username'], 'ADMIN')
        self.client.post('/commerce-logout', follow_redirects=False)
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        self.assertFalse(self.client.get('/api/auth/session').json()['authenticated'])
        self.assertEqual(account_setup.PENDING, {})
        self.audit.assert_any_call('auth.first_admin', 'success', 'ADMIN')

    def test_refresh_reuses_pending_secret_and_does_not_reset_limit_or_expiry(self):
        first, headers = self.begin()
        record = next(iter(account_setup.PENDING.values()))
        expires = record['expires']; record['attempts'] = 2
        second, headers_again = self.begin()
        self.assertEqual(first['secret'], second['secret'])
        self.assertEqual(first['recovery_codes'], second['recovery_codes'])
        self.assertEqual(headers, headers_again)
        self.assertEqual(record['expires'], expires)
        self.assertEqual(record['attempts'], 2)
        record['expires'] = time.time() - 1
        self.assertEqual(self.finish(first, headers).status_code, 403)

    def test_client_cannot_supply_role_seed_or_skip_confirming_mfa(self):
        info, headers = self.begin()
        self.assertEqual(self.finish(info, headers, role='admin').status_code, 422)
        self.assertEqual(self.finish(info, headers, recovery_saved=False).status_code, 422)
        self.assertEqual(self.finish(info, headers, password='weak', confirm_password='weak').status_code, 422)
        self.assertEqual(self.finish(info, headers, otp='abcdef').status_code, 422)
        self.assertFalse(auth.store_path().exists())

    def test_only_one_of_two_competing_browser_enrollments_can_succeed(self):
        other = self.new_client()
        info, headers = self.begin()
        other_info, other_headers = self.begin(other, 'ADMIN')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.finish, info, headers)
            second = pool.submit(self.finish, other_info, other_headers, other, username='ADMIN')
            statuses = [first.result().status_code, second.result().status_code]
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(auth.status()['total_user_count'], 1)

    def test_audit_failure_does_not_create_admin_and_request_body_is_bounded(self):
        info, headers = self.begin()
        with patch.object(access, '_audit', side_effect=OSError('unavailable')):
            self.assertEqual(self.finish(info, headers).status_code, 503)
        self.assertFalse(auth.store_path().exists())
        response = self.client.post('/commerce-setup/finish', content=json.dumps({'password': 'a' * 9000}),
                                    headers={**headers, 'content-type': 'application/json'})
        self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
