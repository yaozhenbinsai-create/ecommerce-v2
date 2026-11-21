"""Authenticated one-time MFA migration. Password-only sessions cannot reach business data."""
import hashlib
import secrets
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from . import access, account_setup, secure_auth as auth, user_management

PENDING = {}
LOCK = threading.RLock()


def session(request):
    sid = hashlib.sha256(request.cookies.get('commerce_session', '').encode()).hexdigest()
    entry = access.SESSIONS.get(sid, {})
    account = auth.session_account(entry)
    if not account or not account.get('mfa_enrollment_required'):
        raise HTTPException(403, '当前账号不需要首次绑定，请重新登录')
    return sid, entry


def install(app):
    @app.get('/commerce-mfa', response_class=HTMLResponse)
    def page(request: Request):
        _, entry = session(request)
        csrf = entry.setdefault('csrf', secrets.token_urlsafe(32))
        return account_setup._headers(HTMLResponse(access.AUTH_TEMPLATES.get_template('mfa-enrollment.html').render(
            username=entry['username'], csrf=csrf)))

    @app.post('/api/auth/mfa/begin')
    def begin(request: Request):
        sid, entry = session(request)
        user_management._csrf(request)
        with LOCK:
            for key, value in list(PENDING.items()):
                if value['expires'] <= time.time():
                    PENDING.pop(key, None)
            if sid not in PENDING:
                if len(PENDING) >= 100:
                    raise HTTPException(429, '绑定请求过多，请稍后重试')
                PENDING[sid] = {'secret': auth.new_totp_secret(), 'codes': auth.new_recovery_codes(),
                                'expires': min(entry['expiry'], time.time() + 600)}
            record = PENDING[sid]
            return account_setup._headers(JSONResponse({'secret': record['secret'], 'recovery_codes': record['codes']}))

    @app.post('/api/auth/mfa/finish')
    async def finish(request: Request):
        sid, entry = session(request)
        user_management._csrf(request)
        payload = await account_setup._payload(request)
        if set(payload) != {'otp', 'recovery_saved'} or not isinstance(payload['otp'], str) or payload['recovery_saved'] is not True:
            raise HTTPException(422, '请填写验证码并确认恢复码已安全保存')
        with LOCK:
            record = PENDING.get(sid)
            if not record or record['expires'] <= time.time():
                raise HTTPException(403, '绑定已过期，请重新登录')
        await user_management._change('mfa_enrolled', entry['username'], lambda: auth.enroll_mfa(
            entry['username'], entry['revision'], record['secret'], payload['otp'], record['codes']))
        with LOCK:
            PENDING.pop(sid, None)
        access.SESSIONS.pop(sid, None)
        access._save_sessions()
        response = JSONResponse({'redirect': '/commerce-login', 'message': '绑定成功，请使用下一组验证码登录'})
        response.delete_cookie('commerce_session')
        return account_setup._headers(response)
