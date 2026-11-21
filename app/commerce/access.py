"""Public deployment protection. Local UI stays local; image tunnel is separate."""
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
import json
import re
import threading
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from fastapi import Request
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment,FileSystemLoader,select_autoescape
from app.commerce import secure_auth
from app import security_secrets

SESSIONS={}
FAILURES={}
_LOGIN_BUDGET=threading.BoundedSemaphore(2)
AUTH_TEMPLATES=Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'templates'),autoescape=select_autoescape(['html']))


def _session_path():
    return secure_auth.store_path().parent / 'security_sessions.enc'


def _valid_sessions(data):
    return (isinstance(data,dict) and all(
        isinstance(sid,str) and re.fullmatch(r'[a-f0-9]{64}',sid)
        and isinstance(entry,dict) and isinstance(entry.get('username'),str)
        and isinstance(entry.get('revision'),str)
        and type(entry.get('expiry')) in (int,float)
        and type(entry.get('last_seen')) in (int,float)
        for sid,entry in data.items()))


def _load_sessions():
    restored=security_secrets.load_json(_session_path(),_valid_sessions)
    SESSIONS.update(restored)


def _save_sessions():
    security_secrets.save_json(_session_path(),SESSIONS,_valid_sessions)


def _renew_cookie(response,request,token):
    response.set_cookie('commerce_session',token,httponly=True,secure=request.url.scheme=='https',
                        samesite='strict',max_age=secure_auth.SESSION_MAX_AGE)
    return response


def _audit(event, outcome, actor=None):
    from app.security_audit import emit
    emit(event, outcome, actor=actor)


def _secure_headers(response, request=None):
    response.headers['Cache-Control']='no-store'
    # Cache only successful resource reads after the existing access checks.
    # Private images revalidate on every use, so logout and permission changes
    # still take effect before a cached image can be reused.
    if request is not None and request.method in {'GET','HEAD'} and response.status_code in {200,206,304}:
        path=request.url.path
        if path.startswith('/static/') and path.endswith(('.css','.js')) and request.query_params.get('v'):
            response.headers['Cache-Control']='private, max-age=86400'
        elif (path.startswith('/image-previews/')
                or path.startswith(('/uploads/','/design-outputs/')) and path.lower().endswith(('.png','.jpg','.jpeg','.webp','.gif','.avif','.bmp','.tif','.tiff'))
                or re.fullmatch(r'/api/commerce/media/file/[a-f0-9]{64}\.jpg',path)):
            response.headers['Cache-Control']='private, no-cache'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    # Native form POSTs can acquire Origin: null under no-referrer.
    # Keep the same-origin value needed by CSRF checks; disclose nothing cross-origin.
    response.headers['Referrer-Policy']='same-origin'
    response.headers['Strict-Transport-Security']='max-age=31536000'
    return response


def _expired_response(request):
    if request.url.path.startswith('/api/') or request.url.path=='/commerce-session-activity':
        return JSONResponse({'detail':'请重新登录'},status_code=401)
    return RedirectResponse('/commerce-login',status_code=303)


def users():
    configured=json.loads(os.environ.get('SHOP_USERS_JSON','{}'))
    configured[os.environ.get('SHOP_ADMIN_USER','admin')]={'password':os.environ.get('SHOP_ADMIN_PASSWORD',''),'role':'admin'}
    return configured


def local_host(value):
    try:
        parsed=urlparse('//'+value)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:return False
        host=parsed.hostname
        parsed.port  # Reject malformed ports along with malformed hostnames.
        return host=='localhost' or bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:return False


def local_connection(request):
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback
                    and local_host(request.headers.get('host','')))
    except ValueError:return False


def local_login_mode():
    return (os.environ.get('SHOP_PUBLIC_MODE')!='1' and os.environ.get('SHOP_SECURITY_MODE')!='1'
            and secure_auth.local_required())


def install(app):
    if secure_auth.enabled():
        _load_sessions()
    @app.middleware('http')
    async def access(request:Request,call_next):
        from app.tunnel_proxy import prepare_request
        rejected = prepare_request(request)
        if rejected is not None:
            return _secure_headers(rejected)
        public=os.environ.get('SHOP_PUBLIC_MODE')=='1'
        public_secure=os.environ.get('SHOP_SECURITY_MODE')=='1'
        from app import public_media
        if public_media.allowed(request):
            return await call_next(request)
        try:
            secured=secure_auth.enabled()
            local_login=local_login_mode()
        except (secure_auth.AccountError,secure_auth.security_secrets.SecretStorageError):
            return _secure_headers(JSONResponse({'detail':'登录配置不可用，请管理员检查服务器配置'},status_code=503))
        if public and not public_secure:
            return JSONResponse({'detail':'公网模式需要安全账号和 HTTPS 配置'},status_code=503)
        if secured:
            if local_login and not local_connection(request):
                return _secure_headers(JSONResponse({'detail':'本机登录只允许从当前电脑的回环地址访问'},status_code=403))
            if request.url.scheme!='https' and not local_login:
                return JSONResponse({'detail':'安全模式只接受 HTTPS；请使用配置的 HTTPS 地址'},status_code=400)
            # Login validation runs after the bounded login admission check.
            # Other requests must not block the event loop behind password hashing.
            if not (request.method=='POST' and request.url.path in {'/commerce-login','/commerce-password','/shareholder-login','/shareholder-password'}):
                try:await run_in_threadpool(secure_auth.validate_configuration)
                except (secure_auth.AccountError,secure_auth.security_secrets.SecretStorageError):
                    return _secure_headers(JSONResponse({'detail':'安全账号未就绪，请管理员检查服务器配置'},status_code=503))
        # Loopback binding alone does not prevent a browser from reaching this
        # unauthenticated local app through a DNS-rebinding domain.
        if not public and not secured and not local_host(request.headers.get('host','')):
            return JSONResponse({'detail':'本地模式仅允许通过 localhost 或回环地址访问'},status_code=400)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin=request.headers.get('origin')
            if (origin and (urlparse(origin).netloc!=request.headers.get('host') or
                            (secured and urlparse(origin).scheme!=request.url.scheme))) or request.headers.get('sec-fetch-site')=='cross-site':
                return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
        request.state.commerce_role=None if secured else 'admin'
        request.state.commerce_username=None
        if request.method in {'GET','HEAD'} and request.url.path in {'/static/ui.css','/static/account-auth.css','/static/account-setup.js','/static/admin-activate.js'}:
            return _secure_headers(await call_next(request),request) if secured else await call_next(request)
        if secured and request.method in {'GET','HEAD'} and request.url.path=='/healthz':
            return _secure_headers(await call_next(request))
        traditional=secure_auth.password_only()
        if traditional and request.url.path.startswith('/commerce-setup'):
            return _secure_headers(RedirectResponse('/commerce-login',status_code=303)) if request.method in {'GET','HEAD'} else _secure_headers(JSONResponse({'detail':'注册与验证器绑定已关闭'},status_code=410))
        if request.url.path in {'/admin-activate','/admin-activate/finish'}:
            from app.commerce import user_management
            return _secure_headers(await call_next(request)) if await run_in_threadpool(user_management.activation_available,request) else _secure_headers(JSONResponse({'detail':'超级管理员初始密码入口已关闭'},status_code=403))
        if traditional and await run_in_threadpool(secure_auth.pending_owner):
            if request.url.path=='/api/auth/session':
                return _secure_headers(JSONResponse({'authenticated':False,'local_only':True,'setup_required':False,'activation_pending':True,'password_only':True}))
            if request.url.path.startswith('/api/') or request.method not in {'GET','HEAD'}:
                return _secure_headers(JSONResponse({'detail':'请先设置超级管理员初始密码'},status_code=401))
            return _secure_headers(RedirectResponse('/admin-activate',status_code=303))
        if request.url.path in {'/commerce-setup','/commerce-setup/begin','/commerce-setup/finish'}:
            from app.commerce import account_setup
            return _secure_headers(await call_next(request)) if await run_in_threadpool(account_setup.available,request) else _secure_headers(JSONResponse({'detail':'首次管理员设置不可用；已有账号请登录，恢复账号需由管理员处理'},status_code=403))
        if secured and local_login and await run_in_threadpool(secure_auth.bootstrap_allowed):
            if request.url.path=='/api/auth/session':
                return _secure_headers(JSONResponse({'authenticated':False,'local_only':True,'setup_required':True}))
            if request.url.path.startswith('/api/') or request.method not in {'GET','HEAD'}:
                return _secure_headers(JSONResponse({'detail':'请先在当前电脑完成管理员设置'},status_code=401))
            return _secure_headers(RedirectResponse('/commerce-setup',status_code=303))
        if request.url.path=='/api/auth/session':
            entry=SESSIONS.get(hashlib.sha256(request.cookies.get('commerce_session','').encode()).hexdigest(),{})
            try:account=await run_in_threadpool(secure_auth.session_account,entry) if secured else None
            except secure_auth.security_secrets.SecretStorageError:return _secure_headers(JSONResponse({'detail':'安全账号存储不可用'},status_code=503))
            if account and not entry.get('csrf'):
                entry['csrf']=secrets.token_urlsafe(32)
                _save_sessions()
            csrf=entry.get('csrf') if account else None
            return _secure_headers(JSONResponse({'authenticated':bool(account),'local_only':not public_secure,'setup_required':False,'password_only':traditional,
                                                 'idle_timeout_seconds':secure_auth.IDLE_TIMEOUT if secured else None,
                                                 **(account or {}),**({'csrf_token':csrf} if account else {})}))
        if secured and request.url.path not in {'/commerce-login','/commerce-password','/shareholder-login','/shareholder-password'}:
            sid=hashlib.sha256(request.cookies.get('commerce_session','').encode()).hexdigest()
            entry=SESSIONS.get(sid,{})
            try:account=await run_in_threadpool(secure_auth.session_account,entry)
            except secure_auth.security_secrets.SecretStorageError:
                return _secure_headers(JSONResponse({'detail':'安全凭据存储暂不可用'},status_code=503))
            if not account:
                if SESSIONS.pop(sid,None) is not None:
                    _save_sessions()
                return _secure_headers(_expired_response(request))
            request.state.commerce_role=account['role']
            request.state.commerce_username=account['username']
            mfa_paths = {'/commerce-mfa', '/api/auth/mfa/begin', '/api/auth/mfa/finish', '/static/mfa-enrollment.js'}
            if account.get('mfa_enrollment_required'):
                if request.url.path not in mfa_paths | {'/commerce-logout'}:
                    response = (JSONResponse({'detail':'请先绑定验证器', 'redirect':'/commerce-mfa'}, status_code=403)
                                if request.url.path.startswith('/api/') or request.method not in {'GET','HEAD'}
                                else RedirectResponse('/commerce-mfa', status_code=303))
                    return _secure_headers(response)
            if request.url.path in mfa_paths:
                return _secure_headers(await call_next(request))
            if request.method=='POST' and request.url.path=='/commerce-session-activity':
                now=time.time()
                entry['last_seen']=now
                entry['expiry']=now+secure_auth.SESSION_MAX_AGE
                _save_sessions()
                response=JSONResponse({'idle_timeout_seconds':secure_auth.IDLE_TIMEOUT})
                return _secure_headers(_renew_cookie(response,request,request.cookies['commerce_session']))
            if request.headers.get('sec-fetch-dest')=='document':
                now=time.time()
                entry['last_seen']=now
                entry['expiry']=now+secure_auth.SESSION_MAX_AGE
                _save_sessions()
                request.state.renew_session_token=request.cookies['commerce_session']
            role=account['role'];path=request.url.path
            admin_read=(path in {'/docs','/redoc','/openapi.json','/api/models','/api/users','/user-management','/api/accounts','/api/commerce/settings'}
                        or any(path.startswith(prefix) for prefix in ('/api/security','/api/commerce/security','/api/models/','/api/users/','/api/openai-account/'))
                        or path.endswith('/credentials')
                        # FastAPI integer parameters also accept +1, 1.0 and
                        # whitespace; authorization must cover every route value.
                        or re.fullmatch(r'/api/commerce/(accounts|targets)/[^/]+/(connection|integration)/?',path))
            if role!='admin' and admin_read:
                return _secure_headers(JSONResponse({'detail':'该操作需要管理员账号'},status_code=403))
            if request.method in {'GET','HEAD'} and path in {'/static/security-session.js','/static/account-session.js'}:
                return _secure_headers(await call_next(request),request)
            if role=='production':
                if path=='/':
                    page=(Path(__file__).resolve().parents[1]/'templates/production.html').read_text(encoding='utf-8')
                    response=HTMLResponse(page)
                    if getattr(request.state,'renew_session_token',None):_renew_cookie(response,request,request.state.renew_session_token)
                    return _secure_headers(response)
                if request.method in {'GET','HEAD'} and path=='/static/production.js':
                    return _secure_headers(await call_next(request),request)
                allowed=request.method=='GET' and (path=='/api/commerce/production-queue' or re.fullmatch(r'/api/commerce/export/production/\d+',path))
                allowed=allowed or request.method=='POST' and (re.fullmatch(r'/api/commerce/production/\d+/(start|complete|stop)',path) or path=='/commerce-logout')
                if not allowed:return _secure_headers(JSONResponse({'detail':'生产账号只能访问生产任务'},status_code=403))
            elif role=='operator' and request.method not in {'GET','HEAD'}:
                if not path.startswith('/api/commerce/') or path=='/api/commerce/settings' or path.endswith('/credentials'):
                    if path!='/commerce-logout':return _secure_headers(JSONResponse({'detail':'该操作需要管理员账号'},status_code=403))
        response=await call_next(request)
        if getattr(request.state,'renew_session_token',None):
            _renew_cookie(response,request,request.state.renew_session_token)
        return _secure_headers(response,request) if secured else response

    @app.get('/commerce-login',response_class=HTMLResponse)
    def login_page(request:Request):
        if secure_auth.enabled():
            message=('初始密码已设置，请使用账号和密码登录。' if secure_auth.password_only() else '管理员已创建。请等待认证器显示下一组验证码后登录。') if request.query_params.get('setup')=='complete' else ''
            return _secure_form(False,local_only=local_login_mode(),message=message)
        return '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>织造台登录</title><style>body{font:16px system-ui;background:#f2f3ed;color:#304c43;display:grid;place-items:center;min-height:90vh}form{background:white;border-radius:18px;padding:40px;width:300px}input,button{box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ddd;border-radius:8px}button{background:#335c4e;color:white}</style><link rel="stylesheet" href="/static/ui.css?v=20260909-2"><body class="login-page"><form method="post"><h1>织造台</h1><p>电商经营管理</p><input name="username" placeholder="用户名" autocomplete="username" required><input type="password" name="password" placeholder="密码" autocomplete="current-password" required><button class="primary">登录</button></form></html>'''

    @app.post('/commerce-login')
    async def login(request:Request):
        if secure_auth.enabled():
            return await _secure_login(request)
        key=request.client.host if request.client else 'unknown'
        failed=[t for t in FAILURES.get(key,[]) if time.time()-t<900]
        if len(failed)>=10:return HTMLResponse('尝试次数过多，请15分钟后再试',status_code=429)
        form=await request.form();account=users().get(str(form.get('username','')),{})
        password=account.get('password','')
        if not password or not hmac.compare_digest(str(form.get('password','')).encode('utf-8'),password.encode('utf-8')):
            failed.append(time.time());FAILURES[key]=failed
            return HTMLResponse('用户名或密码不正确',status_code=401)
        FAILURES.pop(key,None)
        for sid in list(SESSIONS):
            if SESSIONS[sid]['expiry']<time.time():SESSIONS.pop(sid,None)
        token=secrets.token_urlsafe(32);SESSIONS[hashlib.sha256(token.encode()).hexdigest()]={'expiry':time.time()+43200,'role':account['role']}
        response=RedirectResponse('/',status_code=303);response.set_cookie('commerce_session',token,httponly=True,secure=os.environ.get('SHOP_PUBLIC_MODE')=='1',samesite='strict',max_age=43200)
        return response

    @app.post('/commerce-logout')
    def logout(request:Request):
        SESSIONS.pop(hashlib.sha256(request.cookies.get('commerce_session','').encode()).hexdigest(),None)
        if secure_auth.enabled():_save_sessions()
        if secure_auth.enabled():_audit('auth.logout','success',getattr(request.state,'commerce_username',None))
        response=RedirectResponse('/commerce-login',status_code=303);response.delete_cookie('commerce_session');return response

    @app.get('/commerce-password',response_class=HTMLResponse)
    def password_page():
        if not secure_auth.enabled():return HTMLResponse('安全模式未启用',status_code=404)
        return _secure_form(True,local_only=local_login_mode())

    @app.post('/commerce-password')
    async def password_change(request:Request):
        if not secure_auth.enabled():return HTMLResponse('安全模式未启用',status_code=404)
        return await _secure_login(request,change_password=True)

    @app.get('/shareholder-login',response_class=HTMLResponse)
    def shareholder_login_page():
        if not secure_auth.enabled() or not secure_auth.shareholder_password_login_enabled():
            return HTMLResponse('股东登录未启用',status_code=404)
        return _secure_form(False,shareholder_login=True)

    @app.post('/shareholder-login')
    async def shareholder_login(request:Request):
        if not secure_auth.enabled() or not secure_auth.shareholder_password_login_enabled():
            return HTMLResponse('股东登录未启用',status_code=404)
        return await _secure_login(request,shareholder_login=True)

    @app.get('/shareholder-password',response_class=HTMLResponse)
    def shareholder_password_page():
        if not secure_auth.enabled() or not secure_auth.shareholder_password_login_enabled():
            return HTMLResponse('股东登录未启用',status_code=404)
        return _secure_form(True,shareholder_login=True)

    @app.post('/shareholder-password')
    async def shareholder_password_change(request:Request):
        if not secure_auth.enabled() or not secure_auth.shareholder_password_login_enabled():
            return HTMLResponse('股东登录未启用',status_code=404)
        return await _secure_login(request,change_password=True,shareholder_login=True)

    from app.commerce import account_setup
    account_setup.install(app)
    from app.commerce import user_management
    user_management.install(app)
    from app.commerce import mfa_enrollment
    mfa_enrollment.install(app)


def _secure_form(change_password,*,local_only=False,error='',message='',shareholder_login=False):
    return AUTH_TEMPLATES.get_template('login.html').render(change_password=change_password,local_only=local_only,error=error,message=message,
        password_only=not secure_auth.mfa_required(), mfa_migration=secure_auth.password_only() and secure_auth.mfa_required(), shareholder_login=shareholder_login,
        password_minimum=secure_auth.PASSWORD_MIN_LENGTH)


def _login_error(request,message,status,change_password=False,shareholder_login=False):
    return HTMLResponse(_secure_form(change_password,local_only=local_login_mode(),error=message,shareholder_login=shareholder_login),status_code=status)


async def _secure_login(request,change_password=False,shareholder_login=False):
    try:
        if int(request.headers.get('content-length','0'))>8192:
            return HTMLResponse('请求过大',status_code=413)
    except ValueError:return HTMLResponse('请求无效',status_code=400)
    # Enforce a bound even for streamed requests without Content-Length.
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>8192:return HTMLResponse('请求过大',status_code=413)
    from urllib.parse import parse_qs
    if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
        return HTMLResponse('表单格式无效',status_code=415)
    try:form=parse_qs(body.decode('utf-8'),max_num_fields=8)
    except (UnicodeError,ValueError):return HTMLResponse('表单格式无效',status_code=400)
    value=lambda key: form.get(key,[''])[0]
    username=value('username')
    if change_password and value('new_password')!=value('confirm_password'):
        return _login_error(request,'两次输入的新密码不一致',400,change_password,shareholder_login)
    if not _LOGIN_BUDGET.acquire(blocking=False):
        return HTMLResponse('登录服务繁忙，请稍后重试',status_code=503,headers={'Retry-After':'2'})
    def authenticate_checked():
        try:secure_auth.validate_configuration()
        except secure_auth.AccountError:
            _audit('auth.login','unconfigured')
            return None,'unconfigured'
        # A failed audit must not silently change the user's password. Keep the
        # audit and password work in the same worker, before issuing a session.
        if change_password:_audit('auth.password_change_requested','requested',username)
        try:
            account,reason=secure_auth.authenticate(username,value('password'),value('otp'),request.client.host if request.client else 'unknown',
                                                    new_password=value('new_password') if change_password else None,
                                                    shareholder_login=shareholder_login)
        except secure_auth.AccountError:
            _audit('auth.password_change','policy_rejected')
            raise
        if account:
            _audit('auth.password_change' if change_password else 'auth.login','recovery_code' if account['recovery_used'] else 'success',account['username'])
        else:
            _audit('auth.login',reason)
        return account,reason
    task=asyncio.create_task(run_in_threadpool(authenticate_checked))
    def finished(result):
        _LOGIN_BUDGET.release()
        if not result.cancelled():result.exception()  # Retrieve errors even after the requesting client disconnects.
    task.add_done_callback(finished)
    try:
        # A cancelled HTTP request cannot release capacity while its hash work is still running.
        account,reason=await asyncio.shield(task)
    except secure_auth.AccountError as exc:
        return _login_error(request,str(exc),400,change_password,shareholder_login)
    except secure_auth.security_secrets.SecretStorageError:
        return HTMLResponse('安全凭据存储暂不可用，请联系管理员',status_code=503)
    except OSError:
        message='账号审计存储暂不可用，' + ('请联系管理员核对密码变更状态' if change_password else '请联系管理员处理后重试登录')
        return _login_error(request,message,503,change_password,shareholder_login)
    if not account:
        messages={'unconfigured':('安全账号未就绪，请管理员检查服务器配置',503),
                  'locked':('尝试次数过多，账号或当前来源已锁定30分钟',429),
                  'change_required':('首次使用或密码已重置，请点击下方“更改密码”设置自己的新密码后登录',403),
                  'expired':('密码已到期，请返回登录页选择“更改或更新到期密码”，并使用新的验证码',403)}
        message,status=messages.get(reason,('账号或密码不正确' if shareholder_login or not secure_auth.mfa_required() else '用户名、密码或验证码不正确；已使用的验证码不能重复登录',401))
        if reason=='change_required' and shareholder_login:message=f'请点击“更改密码”，设置至少{secure_auth.PASSWORD_MIN_LENGTH}位且含大小写字母、数字和特殊字符的新密码。'
        elif reason=='change_required' and secure_auth.mfa_required():message=f'请点击“更改密码”，设置至少{secure_auth.PASSWORD_MIN_LENGTH}位且含大小写字母、数字和特殊字符的新密码。未绑定验证器时验证码留空。'
        if reason=='expired' and secure_auth.password_only():message='密码已到期，请点击下方“更改密码”更新后登录'
        return _login_error(request,message,status,change_password,shareholder_login)
    now=time.time()
    for sid in list(SESSIONS):
        if SESSIONS[sid].get('expiry',0)<=now or now-SESSIONS[sid].get('last_seen',0)>=secure_auth.IDLE_TIMEOUT:
            SESSIONS.pop(sid,None)
    old=hashlib.sha256(request.cookies.get('commerce_session','').encode()).hexdigest()
    SESSIONS.pop(old,None)
    token=secrets.token_urlsafe(32)
    lifetime = 600 if account.get('mfa_enrollment_required') else secure_auth.SESSION_MAX_AGE
    SESSIONS[hashlib.sha256(token.encode()).hexdigest()]={**account,'expiry':now+lifetime,'last_seen':now}
    try:_save_sessions()
    except security_secrets.SecretStorageError:
        SESSIONS.pop(hashlib.sha256(token.encode()).hexdigest(),None)
        return HTMLResponse('登录会话暂无法保存，请联系管理员检查存储',status_code=503)
    response=RedirectResponse('/commerce-mfa' if account.get('mfa_enrollment_required') else '/',status_code=303)
    return _renew_cookie(response,request,token)


def validate_configuration():
    secure_auth.validate_configuration()
