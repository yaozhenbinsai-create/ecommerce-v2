"""Account-level authorization settings, independent of marketplace creation."""
import hashlib
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from . import amazon, store as s


def revision(account):
    return hashlib.sha256(s.dump({k:account[k] for k in ['id','platform','seller_id','credential_env','updated_at']}).encode()).hexdigest()


def read(ident):
    with s.main().db() as c:
        row=c.execute('SELECT * FROM platform_accounts WHERE id=?',(ident,)).fetchone()
        if not row:raise HTTPException(404,'平台账号不存在')
        account=dict(row)
        targets=[t for t in s.all_rows(c,'target') if t['account_id']==ident and t['environment']=='production']
    supported=account['platform']=='亚马逊'
    status=amazon.credential_status(ident) if supported else {'fields':{},'source':'未接入','error':'此平台授权适配器尚未接入'}
    return {'account_id':ident,'account_name':account['account_name'],'platform':account['platform'],
            'seller_id':account['seller_id'],'credential_env':account['credential_env'],'revision':revision(account),
            'mode':'environment' if account['credential_env'] else 'file','supported':supported,'credentials':status,
            'targets':[{'id':t['id'],'name':t['name'],'enabled':t['enabled'],'verified':bool(t.get('verified')) and t.get('seller_id')==account['seller_id']} for t in targets]}


def save(ident,payload):
    allowed={'revision','seller_id','mode','credential_env','client_id','client_secret','refresh_token','client_secret_expires_on'}
    if not isinstance(payload,dict) or set(payload)-allowed or any(not isinstance(v,str) for v in payload.values()):
        raise HTTPException(422,'授权参数格式无效')
    mode=payload.get('mode');seller=payload.get('seller_id','').strip();env=payload.get('credential_env','').strip()
    if mode not in {'file','environment'}:raise HTTPException(422,'请选择凭据配置方式')
    if not seller or len(seller)>160:raise HTTPException(422,'请填写有效的 Seller ID')
    if mode=='environment' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,159}',env):raise HTTPException(422,'请填写有效的凭据环境变量名称')
    with s.transaction() as c:
        row=c.execute('SELECT * FROM platform_accounts WHERE id=?',(ident,)).fetchone()
        if not row:raise HTTPException(404,'平台账号不存在')
        if row['platform']!='亚马逊':raise HTTPException(422,'此平台授权适配器尚未接入')
        if payload.get('revision')!=revision(row):raise HTTPException(409,'账号配置已变化，请重新打开后保存')
        if mode=='file':
            credentials={k:payload.get(k,'').strip() for k in ['client_id','client_secret','refresh_token']}
            # Switching from an environment must explicitly supply a complete new authorization.
            if row['credential_env'] and not all(credentials.values()):raise HTTPException(422,'切换到页面配置时请完整填写三项授权参数')
            credentials['client_secret_expires_on']=payload.get('client_secret_expires_on','')
            amazon.save_credentials(ident,credentials)
        c.execute('UPDATE platform_accounts SET seller_id=?,credential_env=?,authorized=0,updated_at=? WHERE id=?',
                  (seller,env if mode=='environment' else '',datetime.now(timezone.utc).isoformat(),ident))
        for target in s.all_rows(c,'target'):
            if target['account_id']==ident:
                target.update(verified=False,connection={},last_sync_at=None);s.save(c,'target',target)
    with amazon.LOCK:amazon.TOKENS.pop(ident,None)
    return {'saved':True,'message':'账号授权配置已保存，请选择地区并验证连接。'}
