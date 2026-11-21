"""One-time first-admin enrollment, available only on the actual loopback host.

No default password. The human owns the password, authenticator and recovery codes.
Pending secrets are memory-only, bounded, short-lived, never logged or put in URLs.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.commerce import secure_auth

PENDING = {}
FAILURES = []
LOCK = threading.RLock()
TTL = 600
COOKIE = 'weaving_first_setup'


def available(request):
    from . import access
    return access.local_login_mode() and access.local_connection(request) and secure_auth.bootstrap_allowed()


def _headers(response):
    response.headers.update({'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                             'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                             'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
    return response


def _prune():
    now = time.time()
    for key, record in list(PENDING.items()):
        if record['expires'] <= now and not record.get('busy'):
            PENDING.pop(key, None)
    FAILURES[:] = [stamp for stamp in FAILURES if now - stamp < 1800]


def _session(request, *, csrf=True):
    token = request.cookies.get(COOKIE, '')
    key = hashlib.sha256(token.encode()).hexdigest()
    with LOCK:
        _prune()
        record = PENDING.get(key)
        if not token or not record or record['expires'] <= time.time():
            raise HTTPException(403, '设置页面已过期，请刷新后重新开始；旧的绑定密钥不会生效')
        if csrf:
            expected_origin = f"{request.url.scheme}://{request.headers.get('host','')}"
            if (request.headers.get('origin') != expected_origin
                    or not hmac.compare_digest(request.headers.get('x-setup-csrf', '').encode('utf-8'), record['csrf'].encode('ascii'))):
                raise HTTPException(403, '设置请求来源不匹配，请在当前电脑刷新页面后重试')
        return key, record


async def _payload(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(415, '请使用设置页面提交')
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    raise HTTPException(413, '设置资料过长')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except TimeoutError:
        raise HTTPException(408, '提交超时，请重试') from None
    except (ValueError, UnicodeError):
        raise HTTPException(400, '设置资料格式无效') from None


def install(app):
    @app.get('/commerce-setup', response_class=HTMLResponse)
    def page(request: Request):
        from . import access
        # A hostile website must not fill the pending pool with hidden images,
        # frames or speculative fetches. A human-activated top-level link is OK.
        destination = request.headers.get('sec-fetch-dest', '')
        activated_link = destination == 'document' and request.headers.get('sec-fetch-user') == '?1'
        if destination not in {'', 'document'} or (request.headers.get('sec-fetch-site') == 'cross-site' and not activated_link):
            raise HTTPException(403, '请在当前电脑直接打开管理员设置页面')
        with LOCK:
            _prune()
            token = request.cookies.get(COOKIE, '')
            key = hashlib.sha256(token.encode()).hexdigest()
            record = PENDING.get(key)
            if not record:
                if len(PENDING) >= 32:
                    raise HTTPException(429, '设置页面打开过多，请稍后重试')
                token = secrets.token_urlsafe(32)
                key = hashlib.sha256(token.encode()).hexdigest()
                record = {'csrf': secrets.token_urlsafe(32), 'expires': time.time() + TTL, 'attempts': 0}
                PENDING[key] = record
            response = HTMLResponse(access.AUTH_TEMPLATES.get_template('account-setup.html').render(csrf_token=record['csrf']))
            response.set_cookie(COOKIE, token, httponly=True, secure=request.url.scheme == 'https',
                                samesite='strict', max_age=max(1, int(record['expires'] - time.time())), path='/commerce-setup')
            return _headers(response)

    @app.post('/commerce-setup/begin')
    async def begin(request: Request):
        key, record = _session(request)
        data = await _payload(request)
        username = data.get('username')
        if set(data) != {'username'} or username != 'ADMIN':
            raise HTTPException(422, '首个管理员账号须为 ADMIN')
        with LOCK:
            _prune()
            if PENDING.get(key) is not record or record['expires'] <= time.time():
                raise HTTPException(403, '设置页面已过期，请刷新后重新开始')
            if record.get('busy') or record.get('attempts', 0) >= 5 or len(FAILURES) >= 10:
                raise HTTPException(429, '设置正在处理或尝试次数过多，请稍后重试')
            if record.get('username') and record['username'] != username:
                raise HTTPException(409, '本次绑定已关联之前的用户名，请使用同一用户名完成设置')
            if not record.get('secret'):
                record.update(username=username, secret=secure_auth.new_totp_secret(), codes=secure_auth.new_recovery_codes())
            uri = ('otpauth://totp/' + quote('织造台:' + username, safe='') + '?secret=' + record['secret']
                   + '&issuer=' + quote('织造台', safe='') + '&algorithm=SHA1&digits=6&period=30')
            return _headers(JSONResponse({'secret': record['secret'], 'otpauth_uri': uri,
                                          'recovery_codes': record['codes'], 'expires_in': max(1, int(record['expires'] - time.time()))}))

    @app.post('/commerce-setup/finish')
    async def finish(request: Request):
        from . import access
        key, record = _session(request)
        data = await _payload(request)
        required = {'username', 'password', 'confirm_password', 'otp', 'recovery_saved'}
        if (set(data) != required or any(not isinstance(data.get(k), str) for k in required - {'recovery_saved'})
                or data['recovery_saved'] is not True):
            raise HTTPException(422, '请完整填写设置资料，并确认已安全保存恢复码')
        if data['password'] != data['confirm_password']:
            raise HTTPException(422, '两次输入的密码不一致')
        with LOCK:
            _prune()
            if PENDING.get(key) is not record or record['expires'] <= time.time():
                raise HTTPException(403, '设置页面已过期，请刷新后重新开始')
            if not record.get('secret') or data['username'] != record.get('username'):
                raise HTTPException(403, '请先完成账号资料步骤')
            if record.get('busy') or record['attempts'] >= 5 or len(FAILURES) >= 10:
                raise HTTPException(429, '尝试次数过多，请稍后重试')
            if not access._LOGIN_BUDGET.acquire(blocking=False):
                raise HTTPException(503, '登录服务繁忙，请稍后重试')
            record['busy'] = True
            record['attempts'] += 1

        def enroll():
            success = False
            try:
                access._audit('auth.first_admin_requested', 'requested', data['username'])
                secure_auth.first_admin(data['username'], data['password'], record['secret'], data['otp'], record['codes'])
                success = True
                access._audit('auth.first_admin', 'success', data['username'])
            finally:
                with LOCK:
                    if success:
                        PENDING.clear()
                    else:
                        FAILURES.append(time.time())
                    record['busy'] = False

        task = asyncio.create_task(run_in_threadpool(enroll))
        def completed(result):
            access._LOGIN_BUDGET.release()
            if not result.cancelled():
                result.exception()
        task.add_done_callback(completed)
        try:
            await asyncio.shield(task)
        except secure_auth.AccountError as exc:
            raise HTTPException(422, str(exc)) from None
        except (secure_auth.security_secrets.SecretStorageError, OSError):
            raise HTTPException(503, '安全存储或审计未完成；请刷新页面核对账号状态，不要重复创建账号') from None
        response = JSONResponse({'redirect': '/commerce-login?setup=complete'})
        response.delete_cookie(COOKIE, path='/commerce-setup')
        return _headers(response)
