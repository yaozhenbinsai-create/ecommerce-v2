"""Closed account administration: one pre-provisioned owner, no public signup."""
import asyncio
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from . import secure_auth as auth, account_setup

PENDING = {}
LOCK = threading.RLock()
COOKIE = 'weaving_owner_activation'


def activation_available(request):
    from . import access
    return access.local_login_mode() and access.local_connection(request) and bool(auth.pending_owner())


def _same_origin(request):
    if request.headers.get('origin') != f"{request.url.scheme}://{request.headers.get('host','')}":
        raise HTTPException(403, '请求来源不匹配，请刷新当前页面')


def _owner(request):
    if not auth.enabled() or not auth.password_only() or getattr(request.state, 'commerce_role', None) != 'admin' or not getattr(request.state, 'commerce_username', None):
        raise HTTPException(403, '只有超级管理员可以管理用户')
    return request.state.commerce_username


def _csrf(request):
    from . import access
    _same_origin(request)
    entry = access.SESSIONS.get(hashlib.sha256(request.cookies.get('commerce_session', '').encode()).hexdigest(), {})
    token = entry.get('csrf')
    if not token or not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), token.encode()):
        raise HTTPException(403, '操作令牌无效，请刷新页面')


async def _change(operation, actor, action):
    from . import access
    if not access._LOGIN_BUDGET.acquire(blocking=False):
        raise HTTPException(503, '账号服务繁忙，请稍后重试')
    def execute():
        access._audit('auth.' + operation + '_requested', 'requested', actor)
        action()
        access._audit('auth.' + operation, 'success', actor)
    task = asyncio.create_task(run_in_threadpool(execute))
    def completed(result):
        access._LOGIN_BUDGET.release()
        if not result.cancelled():result.exception()
    task.add_done_callback(completed)
    try:
        await asyncio.shield(task)
    except auth.AccountError as exc:
        raise HTTPException(422, str(exc)) from None
    except (OSError, auth.security_secrets.SecretStorageError):
        raise HTTPException(503, '账号或审计存储未完成，请刷新核对状态后再操作') from None


def install(app):
    @app.get('/admin-activate', response_class=HTMLResponse)
    def activate_page(request: Request):
        from . import access
        destination=request.headers.get('sec-fetch-dest','')
        if destination not in {'','document'} or (request.headers.get('sec-fetch-site')=='cross-site' and request.headers.get('sec-fetch-user')!='?1'):
            raise HTTPException(403,'请在本机直接打开此页面')
        with LOCK:
            now=time.time()
            for key, record in list(PENDING.items()):
                if record['expires']<=now:PENDING.pop(key,None)
            token=request.cookies.get(COOKIE,'')
            key=hashlib.sha256(token.encode()).hexdigest()
            record=PENDING.get(key)
            if not record:
                if len(PENDING)>=32:raise HTTPException(429,'打开的设置页面过多，请稍后重试')
                token=secrets.token_urlsafe(32);key=hashlib.sha256(token.encode()).hexdigest()
                record={'csrf':secrets.token_urlsafe(32),'expires':now+600};PENDING[key]=record
            response=HTMLResponse(access.AUTH_TEMPLATES.get_template('admin-activate.html').render(username=auth.pending_owner(),csrf_token=record['csrf']))
            response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/admin-activate',max_age=max(1,int(record['expires']-now)))
            return account_setup._headers(response)

    @app.post('/admin-activate/finish')
    async def activate_finish(request: Request):
        _same_origin(request)
        key=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
        with LOCK:
            record=PENDING.get(key)
            if not record or record['expires']<=time.time() or not hmac.compare_digest(request.headers.get('x-setup-csrf','').encode(),record['csrf'].encode()):
                raise HTTPException(403,'初始密码页面已过期，请刷新重试')
        data=await account_setup._payload(request)
        if set(data)!={'password','confirm_password'} or any(not isinstance(v,str) for v in data.values()):
            raise HTTPException(422,'请填写两次密码')
        if data['password']!=data['confirm_password']:raise HTTPException(422,'两次密码不一致')
        await _change('owner_activated',auth.pending_owner(),lambda:auth.activate_owner(data['password']))
        with LOCK:PENDING.clear()
        response=JSONResponse({'redirect':'/commerce-login?setup=complete'})
        response.delete_cookie(COOKIE,path='/admin-activate')
        return response

    @app.get('/user-management', response_class=HTMLResponse)
    def user_page(request: Request):
        from . import access
        _owner(request)
        return access.AUTH_TEMPLATES.get_template('user-management.html').render(password_minimum=auth.PASSWORD_MIN_LENGTH)

    @app.get('/api/users')
    def users(request: Request):
        return {'users':auth.managed_users(_owner(request))}

    @app.post('/api/users',status_code=201)
    async def create_user(request: Request):
        actor=_owner(request);_csrf(request)
        data=await account_setup._payload(request)
        if set(data)!={'username','password','role'} or any(not isinstance(v,str) for v in data.values()):
            raise HTTPException(422,'账号资料无效')
        await _change('user_created',actor,lambda:auth.manage_user(actor,data['username'],create=True,password=data['password'],role=data['role']))
        return {'saved':True}

    @app.patch('/api/users/{username}')
    async def change_user(username: str,request: Request):
        actor=_owner(request);_csrf(request)
        data=await account_setup._payload(request)
        if not data or set(data)-{'role','disabled','password'} or ('role' in data and not isinstance(data['role'],str)) or ('password' in data and not isinstance(data['password'],str)) or ('disabled' in data and type(data['disabled']) is not bool):
            raise HTTPException(422,'账号变更资料无效')
        await _change('user_changed',actor,lambda:auth.manage_user(actor,username,**data))
        return {'saved':True}
