import os
import asyncio
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import security_secrets
from app.commerce import access, secure_auth as auth


class SecureAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'
        self.env = patch.dict(os.environ, {'ECOMMERCE2_DATA_DIR': str(self.root), 'SHOP_SECURITY_MODE': '1', 'SHOP_PUBLIC_MODE': '1'})
        self.env.start(); self.addCleanup(self.env.stop)
        backend = security_secrets._AESGCM(os.urandom(32))
        mock_backend = patch.object(security_secrets, '_backend', return_value=backend)
        mock_backend.start(); self.addCleanup(mock_backend.stop)
        self.current = int(time.time() // 30) * 30
        self.clock = patch.object(auth.time, 'time', return_value=self.current).start()
        self.addCleanup(patch.stopall)
        self.events = patch.object(access, '_audit').start()
        self.state = patch.dict(access.SESSIONS, {}, clear=True)
        self.state.start(); self.addCleanup(self.state.stop)
        self.password = 'Independent!Pass9087'
        self.secret = auth.new_totp_secret()
        self.codes = auth.new_recovery_codes()
        auth.add_user('ADMIN', self.password, 'admin', self.secret, self.otp(), recovery_codes=self.codes)
        self.advance(30)
        app = FastAPI()
        access.install(app)
        @app.get('/api/probe')
        def probe(): return {'ok': True}
        @app.get('/healthz')
        def health(): return {'status': 'ok'}
        @app.get('/api/models')
        def models(): return {'sensitive': True}
        @app.post('/api/commerce/settings')
        def settings(): return {'saved': True}
        @app.get('/api/commerce/production-queue')
        def queue(): return []
        self.client = TestClient(app, base_url='https://shop.example')

    def otp(self, secret=None):
        return auth.totp_code(secret or self.secret, self.current // 30)

    def advance(self, seconds):
        self.current += seconds
        self.clock.return_value = self.current

    def login(self, **fields):
        return self.client.post('/commerce-login', data={'username': 'ADMIN', 'password': self.password, 'otp': self.otp(), **fields}, follow_redirects=False)

    def test_https_cannot_be_forged_with_headers_and_public_is_fail_closed(self):
        http = TestClient(self.client.app, base_url='http://shop.example')
        self.assertEqual(http.get('/healthz', headers={'x-forwarded-proto': 'https'}).status_code, 400)
        self.assertEqual(self.client.get('/healthz').json(), {'status': 'ok'})
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        with patch.dict(os.environ, {'SHOP_SECURITY_MODE': '0'}):
            self.assertEqual(self.client.get('/healthz').status_code, 503)
        auth.store_path().unlink()
        self.assertEqual(self.client.get('/api/probe').status_code, 503)

    def test_password_alone_does_not_authenticate_and_cookie_is_hardened(self):
        self.assertEqual(self.login(otp='').status_code, 401)
        self.assertEqual(self.login(otp='١٢٣٤٥٦').status_code, 401)
        good = self.login()
        self.assertEqual(good.status_code, 303)
        cookie = good.headers['set-cookie'].lower()
        self.assertIn('secure', cookie); self.assertIn('httponly', cookie); self.assertIn('samesite=strict', cookie)
        self.assertEqual(self.client.get('/api/probe').status_code, 200)
        self.assertEqual(self.client.get('/api/probe').headers['cache-control'], 'no-store')

    def test_totp_replay_and_recovery_codes_are_single_use(self):
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(otp=self.codes[0]).status_code, 303)
        self.assertEqual(self.login(otp=self.codes[0]).status_code, 401)
        self.events.assert_any_call('auth.login', 'recovery_code', 'ADMIN')

    def test_ten_failed_attempts_lock_account_across_sources_for_thirty_minutes(self):
        for i in range(10):
            account, reason = auth.authenticate('ADMIN', 'wrong', '000000', 'source-' + str(i))
            self.assertIsNone(account); self.assertEqual(reason, 'invalid')
        self.assertEqual(auth.authenticate('ADMIN', self.password, self.otp(), 'fresh-source')[1], 'locked')
        self.advance(1799)
        self.assertEqual(auth.authenticate('ADMIN', self.password, self.otp(), 'fresh-source')[1], 'locked')
        self.advance(1)
        self.assertEqual(auth.authenticate('ADMIN', self.password, self.otp(), 'fresh-source')[1], 'ok')

    def test_background_requests_do_not_defeat_idle_timeout(self):
        self.assertEqual(self.login().status_code, 303)
        self.advance(auth.IDLE_TIMEOUT - 1)
        self.assertEqual(self.client.get('/api/probe').status_code, 200)
        self.advance(1)
        self.assertEqual(self.client.get('/api/probe').status_code, 401)

    def test_activity_renews_session_but_cross_origin_cannot(self):
        self.assertEqual(self.login().status_code, 303)
        self.advance(auth.IDLE_TIMEOUT - 50)
        self.assertEqual(self.client.post('/commerce-session-activity', headers={'origin': 'https://evil.example'}).status_code, 403)
        renewed=self.client.post('/commerce-session-activity', headers={'origin': 'https://shop.example'})
        self.assertEqual(renewed.status_code, 200)
        self.assertIn(f'max-age={auth.SESSION_MAX_AGE}', renewed.headers['set-cookie'].lower())
        self.advance(auth.IDLE_TIMEOUT - 50)
        self.assertEqual(self.client.get('/api/probe').status_code, 200)

    def test_session_survives_restart_and_logout_revokes_saved_session(self):
        self.assertEqual(self.login().status_code, 303)
        token=self.client.cookies.get('commerce_session')
        self.assertTrue((self.root / 'security_sessions.enc').read_bytes().startswith(security_secrets.MAGIC))
        access.SESSIONS.clear()
        access._load_sessions()
        self.assertEqual(self.client.get('/api/probe').status_code, 200)
        self.assertEqual(self.client.post('/commerce-logout', follow_redirects=False).status_code, 303)
        access.SESSIONS.clear()
        access._load_sessions()
        self.assertNotIn(__import__('hashlib').sha256(token.encode()).hexdigest(),access.SESSIONS)

    def test_regular_activity_keeps_session_past_initial_thirty_days(self):
        self.assertEqual(self.login().status_code, 303)
        for _ in range(6):
            self.advance(6 * 86400)
            response=self.client.post('/commerce-session-activity')
            self.assertEqual(response.status_code, 200)
        access.SESSIONS.clear()
        access._load_sessions()
        self.assertEqual(self.client.get('/api/probe').status_code, 200)

    def test_page_navigation_also_renews_browser_cookie(self):
        self.assertEqual(self.login().status_code, 303)
        self.advance(6 * 86400)
        response=self.client.get('/api/probe', headers={'sec-fetch-dest':'document'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'max-age={auth.SESSION_MAX_AGE}', response.headers['set-cookie'].lower())

    def test_role_changes_and_password_changes_revoke_existing_sessions(self):
        other = auth.new_totp_secret()
        auth.add_user('operator', self.password, 'operator', other, self.otp(other))
        self.advance(30)
        self.assertEqual(self.login(username='operator', otp=self.otp(other)).status_code, 303)
        self.assertEqual(self.client.get('/api/models').status_code, 403)
        self.assertEqual(self.client.post('/api/commerce/settings', json={}).status_code, 403)
        auth.update_user('operator', role='production')
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        self.advance(30)
        self.assertEqual(self.login(username='operator', otp=self.otp(other)).status_code, 303)
        self.assertEqual(self.client.get('/api/commerce/production-queue').status_code, 200)
        self.assertEqual(self.client.get('/api/probe').status_code, 403)
        auth.update_user('operator', password='FreshDifferent!9087', incident_reset=True)
        self.assertEqual(self.client.get('/api/commerce/production-queue').status_code, 401)

    def test_password_policy_history_minimum_age_and_last_admin_guard(self):
        auth.validate_password('ADMIN', 'Aa1!xyzwlong')
        for weak in ['LongButNoNumbers!', 'longbutnocapital!123', 'LongNoSpecial123', 'AdminIncluded!123', 'Aa1!xyz']:
            with self.subTest(password=weak), self.assertRaises(auth.AccountError):
                auth.validate_password('ADMIN', weak)
        with self.assertRaises(auth.AccountError):
            auth.update_user('ADMIN', password='NewPassword!12345')
        auth.update_user('ADMIN', password='NewPassword!12345', incident_reset=True)
        with self.assertRaises(auth.AccountError):
            auth.update_user('ADMIN', password=self.password, incident_reset=True)
        with self.assertRaises(auth.AccountError):
            auth.update_user('ADMIN', disabled=True)

    def test_expired_password_can_be_changed_with_fresh_mfa(self):
        self.advance(auth.PASSWORD_MAX_AGE)
        self.assertEqual(self.login().status_code, 403)
        self.advance(30)
        self.assertEqual(self.client.get('/commerce-password').status_code, 200)
        result = self.client.post('/commerce-password', data={'username': 'ADMIN', 'password': self.password, 'otp': self.otp(),
            'new_password': 'Replacement!9087', 'confirm_password': 'Replacement!9087'}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertEqual(self.client.get('/api/probe').status_code, 200)

    def test_secrets_are_encrypted_and_tampering_fails_closed(self):
        raw = auth.store_path().read_bytes()
        for secret in [self.password, self.secret, self.codes[0]]:
            self.assertNotIn(secret.encode(), raw)
        auth.store_path().write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        self.assertEqual(self.client.get('/api/probe').status_code, 503)

    def test_mfa_reset_revokes_old_codes_and_sessions(self):
        self.assertEqual(self.login().status_code, 303)
        new_secret = auth.new_totp_secret()
        new_codes = auth.new_recovery_codes()
        auth.update_user('ADMIN', totp_secret=new_secret, enrollment_code=self.otp(new_secret), recovery_codes=new_codes, incident_reset=True)
        self.assertEqual(self.client.get('/api/probe').status_code, 401)
        self.advance(30)
        self.assertEqual(self.login(otp=self.codes[0]).status_code, 401)
        self.assertEqual(self.login(otp=new_codes[0]).status_code, 303)

    def test_totp_matches_rfc6238_sha1_vector(self):
        # RFC 6238's 8-digit 94287082 becomes 287082 in a 6-digit authenticator.
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        self.assertEqual(auth.totp_code(secret, 59 // 30), '287082')

    def test_login_budget_rejects_without_queueing_hash_work(self):
        acquired=[]
        try:
            for _ in range(2):
                self.assertTrue(access._LOGIN_BUDGET.acquire(blocking=False)); acquired.append(True)
            with patch.object(auth,'authenticate') as authenticate:
                self.assertEqual(self.login().status_code,503)
                authenticate.assert_not_called()
        finally:
            for _ in acquired:access._LOGIN_BUDGET.release()

    def test_hash_work_runs_off_event_loop(self):
        release=threading.Event()
        def delayed_auth(*args,**kwargs):
            release.wait(2)
            return None,'invalid'
        async def scenario():
            async def receive():
                return {'type':'http.request','body':b'username=owner&password=example&otp=123456','more_body':False}
            request=Request({'type':'http','method':'POST','path':'/commerce-login','scheme':'https','query_string':b'',
                             'server':('shop.example',443),'client':('testclient',123),
                             'headers':[(b'content-type',b'application/x-www-form-urlencoded')]},receive)
            start=time.monotonic()
            job=asyncio.create_task(access._secure_login(request))
            try:
                await asyncio.sleep(.05)
                self.assertLess(time.monotonic()-start,.5,'Password work blocked the ASGI event loop')
            finally:
                release.set()
                await job
        with patch.object(auth,'authenticate',side_effect=delayed_auth):
            asyncio.run(scenario())

    def test_nonregular_lock_path_fails_closed(self):
        lock_path=auth.store_path().with_suffix('.lock')
        lock_path.unlink();lock_path.mkdir()
        with self.assertRaises(security_secrets.SecretStorageError):
            with auth.locked_store():pass

    def test_cli_audit_failure_prevents_account_mutation(self):
        from tools import security_accounts
        with patch.object(security_accounts.sys.stdin,'isatty',return_value=True),patch.object(security_accounts.sys.stdout,'isatty',return_value=True),\
                patch.object(security_accounts,'emit',side_effect=OSError('test audit unavailable')),patch.object(auth,'update_user') as update:
            self.assertEqual(security_accounts.main(['set-role','--username','ADMIN','--role','operator']),1)
            update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
