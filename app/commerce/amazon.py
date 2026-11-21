"""Amazon SP-API transport. No silent fallback to simulated responses."""
from __future__ import annotations
import json
import hashlib
import math
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from urllib.parse import quote
import httpx
from fastapi import HTTPException
from . import store as s
from .. import security_secrets
from app.error_log import record

TOKENS={}
LOCK=threading.Lock()
REGIONS={'NA':'sellingpartnerapi-na.amazon.com','EU':'sellingpartnerapi-eu.amazon.com','FE':'sellingpartnerapi-fe.amazon.com'}
US='ATVPDKIKX0DER'


class PlatformError(Exception):
    def __init__(self,message,status=0,retry_after=0,unknown=False,*,request_id='',errors=None):
        super().__init__(message);self.status=status;self.retry_after=retry_after;self.unknown=unknown
        self.request_id=request_id
        self.preview_media_timeout=False
        self.errors=[{'code':str(row.get('code',''))[:100],
                      'message':str(row.get('message',''))[:1000],
                      'details':str(row.get('details',''))[:2000]}
                     for row in (errors or [])[:30] if isinstance(row,dict)]


def secret_file():return s.main().DATA_ROOT/'commerce_credentials.json'


def _valid_credentials(data):
    return all(isinstance(key,str) and isinstance(row,dict) and
               all(field in {'client_id','client_secret','refresh_token','client_secret_expires_on'} and isinstance(value,str)
                   for field,value in row.items()) for key,row in data.items())


def credential_data(account_id):
    source='加密凭据文件'
    with s.main().db() as c:
        row=c.execute('SELECT credential_env FROM platform_accounts WHERE id=?',(account_id,)).fetchone()
    try:
        if row and row[0]:
            source='账号指定的环境变量'
            raw=os.environ.get(row[0])
            if not raw:return {},source,'指定环境变量尚未设置；不会回退使用本机旧凭据'
            data=json.loads(raw)
        else:
            all_data=security_secrets.load_json(secret_file(),_valid_credentials)
            data=all_data.get(str(account_id),{})
        if not isinstance(data,dict):raise ValueError()
    except (ValueError,OSError,security_secrets.SecretStorageError):return {},source,'凭据无法认证解密或格式无效，请核对密钥、服务账户和文件权限；不会回退到旧授权'
    return {key:value.strip() for key,value in data.items() if key in {'client_id','client_secret','refresh_token','client_secret_expires_on'} and isinstance(value,str)},source,''


def rotation_status(data):
    value = data.get('client_secret_expires_on', '')
    try:
        due = date.fromisoformat(value)
        days = (due - datetime.now(timezone.utc).date()).days
    except (ValueError, TypeError):
        return {'status':'unknown','expires_on':'','days_remaining':None,'message':'尚未登记Amazon控制台中的密钥轮换截止日，请核对后填写'}
    status = 'expired' if days <= 0 else 'due_soon' if days <= 30 else 'scheduled'
    message = ('已到登记的轮换截止日，请在Amazon控制台核对并更换密钥' if days <= 0 else
               f'距离密钥轮换截止日还有{days}天。换新后旧密钥7天失效；共用此应用的店铺都需更新。')
    return {'status':status,'expires_on':value,'days_remaining':days,'message':message}


def credential_status(account_id):
    data,source,error=credential_data(account_id)
    return {'source':source,'error':error,'rotation':rotation_status(data),
            'fields':{key:bool(data.get(key)) for key in ['client_id','client_secret','refresh_token']}}


def credentials(account_id):
    data,_,error=credential_data(account_id)
    if error:raise PlatformError(error,401)
    if not all(data.get(x) for x in ['client_id','client_secret','refresh_token']):raise PlatformError('请配置Amazon应用凭据和刷新令牌',401)
    return data


def save_credentials(account_id,data):
    if not isinstance(data,dict) or any(k not in {'client_id','client_secret','refresh_token','client_secret_expires_on'} or not isinstance(v,str) for k,v in data.items()):raise HTTPException(422,'授权字段必须为文本，且仅允许三项LWA参数和密钥轮换截止日')
    expiry = data.get('client_secret_expires_on', '').strip()
    if expiry:
        try:
            if date.fromisoformat(expiry).isoformat() != expiry:raise ValueError()
        except ValueError:raise HTTPException(422,'密钥轮换截止日须为YYYY-MM-DD') from None
    with LOCK:
        try:all_data=security_secrets.load_json(secret_file(),_valid_credentials)
        except security_secrets.SecretStorageError:raise HTTPException(503,'凭据无法读取或解密，请修复后再保存，原文件未覆盖') from None
        old=all_data.get(str(account_id),{})
        if data.get('client_secret','').strip() and data['client_secret'].strip() != old.get('client_secret'):
            old.pop('client_secret_expires_on', None)
        if expiry:old['client_secret_expires_on']=expiry
        for key in ['client_id','client_secret','refresh_token']:
            if data.get(key,'').strip():old[key]=data[key].strip()
        all_data[str(account_id)]=old
        try:security_secrets.save_json(secret_file(),all_data,_valid_credentials)
        except security_secrets.SecretStorageError:raise HTTPException(503,'凭据加密保存失败，原文件已保留；请检查主密钥及文件权限') from None
        TOKENS.pop(account_id,None)


def token(account_id,force=False):
    with LOCK:
        cred=credentials(account_id)
        fingerprint=hashlib.sha256(s.dump(cred).encode()).hexdigest()
        cached=TOKENS.get(account_id)
        if cached and len(cached)>2 and cached[2]==fingerprint and not force and cached[1]>time.time()+90:return cached[0]
        try:r=httpx.post('https://api.amazon.com/auth/o2/token',data={'grant_type':'refresh_token',**{k:cred[k] for k in ['client_id','client_secret','refresh_token']}},timeout=30)
        except httpx.RequestError as exc:
            record('amazon.auth_connection_failed', exc, account_id=account_id)
            raise PlatformError('Amazon授权服务连接失败',503) from exc
        if not r.is_success:
            record('amazon.auth_rejected', account_id=account_id, http_status=r.status_code)
            try:error=r.json().get('error','')
            except (ValueError,AttributeError):error=''
            hint={'invalid_grant':'店铺授权已失效或令牌不属于此应用，请在Amazon重新自授权并更新Refresh Token',
                  'invalid_client':'应用密钥无效或已过期，请核对Client ID、Client Secret及轮换截止日'}.get(error,'请核对应用凭据与卖家自授权')
            raise PlatformError('Amazon授权失败：'+hint+'（HTTP '+str(r.status_code)+'）',r.status_code)
        try:
            body=r.json()
            if not isinstance(body.get('access_token'),str) or not body['access_token']:raise ValueError()
            lifetime=int(body.get('expires_in',3600))
            if not 0 < lifetime <= 86400:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise PlatformError('Amazon授权响应格式无效，请稍后重试',503) from None
        TOKENS[account_id]=(body['access_token'],time.time()+lifetime,fingerprint)
        return body['access_token']


def request(target,method,path,params=None,body=None,*,non_mutating=False):
    from .environment import require_production
    require_production(target['environment'])
    with s.main().db() as c:account=c.execute('SELECT enabled,seller_id,platform FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
    if not account or not account['enabled']:raise PlatformError('平台同步中的账号已停用',400)
    if account['platform']!='亚马逊':raise PlatformError('此账号当前平台不支持 Amazon 接口，请核对配置',400)
    if account['seller_id']!=target.get('seller_id'):raise PlatformError('卖家标识已变化，请重新保存该上架目标',400)
    host=REGIONS[target.get('region','NA')]
    aid=target['account_id']
    result_unknown=method not in {'GET','HEAD'} and not non_mutating
    for attempt in range(2):
        headers={'x-amz-access-token':token(aid,attempt==1),'User-Agent':'WeavingShop/1.0 (Language=Python)','Content-Type':'application/json'}
        try:r=httpx.request(method,'https://'+host+path,params=params,json=body,headers=headers,timeout=50)
        except httpx.RequestError as exc:
            record('amazon.connection_failed', exc, account_id=aid, target_id=target.get('id'), method=method,
                   endpoint=host, path=path, marketplace_id=target.get('marketplace_id'), attempt=attempt+1,
                   result_unknown=result_unknown)
            raise PlatformError('Amazon连接中断，写入结果需核实' if result_unknown else 'Amazon连接中断，请稍后重试',
                                503,unknown=result_unknown) from exc
        if r.status_code==401 and attempt==0:continue
        if not r.is_success:
            try:failure=r.json()
            except ValueError:failure=None
            errors=failure.get('errors',[]) if isinstance(failure,dict) else []
            if not isinstance(errors,list):errors=[]
            record('amazon.http_error', account_id=aid, target_id=target.get('id'), method=method, endpoint=host,
                   path=path, marketplace_id=target.get('marketplace_id'), http_status=r.status_code,
                   platform_request_id=r.headers.get('x-amzn-RequestId'), attempt=attempt+1,
                   errors=[{'code':row.get('code','')} for row in errors if isinstance(row,dict)])
            codes=', '.join(v['code'][:100] for v in errors[:5] if isinstance(v,dict) and isinstance(v.get('code'),str))
            try:delay=float(r.headers.get('Retry-After',0))
            except ValueError:delay=0
            if not math.isfinite(delay) or delay<0:delay=0
            raise PlatformError(f'Amazon HTTP {r.status_code} {codes}',r.status_code,delay,
                                result_unknown and (r.status_code==408 or r.status_code>=500),
                                request_id=r.headers.get('x-amzn-RequestId',''),errors=errors)
        try:
            result=r.json() if r.content else {}
            if not isinstance(result,dict):raise ValueError()
        except ValueError as exc:
            record('amazon.invalid_response', exc, account_id=aid, method=method, path=path,
                   http_status=r.status_code, platform_request_id=r.headers.get('x-amzn-RequestId'))
            # A successful write with an unreadable receipt may already have
            # changed the platform. Preserve that uncertainty for reconciliation.
            raise PlatformError('Amazon响应无法解析，请核对平台状态' if result_unknown else 'Amazon响应无法解析，请稍后重试',
                                503,unknown=result_unknown)
        return {'body':result,'request_id':r.headers.get('x-amzn-RequestId',''),'http_status':r.status_code}
    raise PlatformError('Amazon授权失败',401)


def connect(target):
    result=request(target,'GET','/sellers/v1/marketplaceParticipations')
    markets=result['body'].get('payload',[])
    return {'marketplaces':markets,'request_id':result['request_id'],'verified_at':time.time()}


def search_product_types(target,query='',mode='keywords'):
    from .environment import require_production
    require_production(target['environment'])
    if mode not in {'keywords','itemName'}:raise HTTPException(422,'查询模式无效')
    params={'marketplaceIds':target['marketplace_id']}
    if query.strip():params[mode]=query.strip()
    result=request(target,'GET','/definitions/2020-09-01/productTypes',params)
    return {'product_types':result['body'].get('productTypes',[]),'sample_only':False,
            'request_id':result['request_id'],'message':'Amazon站点查询结果'}


def definition(target,product_type,parentage='NONE',requirements='LISTING'):
    from .environment import require_production
    require_production(target['environment'])
    if not product_type:raise HTTPException(422,'请填写Amazon产品类型')
    result=request(target,'GET','/definitions/2020-09-01/productTypes/'+quote(product_type,safe=''),
      {'sellerId':target.get('seller_id',''),'marketplaceIds':target['marketplace_id'],'requirements':requirements,'requirementsEnforced':'ENFORCED','productTypeVersion':'LATEST','parentageLevel':parentage})
    descriptor=result['body'];link=descriptor.get('schema',{}).get('link',{}).get('resource','')
    selected_scope=definition_scope(target,product_type,parentage,requirements)
    archive_scope={key:selected_scope[key] for key in ('account_id','seller_id','marketplace_id','product_type','parentage','requirements')}
    checked_at=datetime.now(timezone.utc).isoformat()
    schema=download_json(link)
    return {'product_type':product_type,'schema':schema,'descriptor':descriptor,'fetched_at':time.time(),
            'scope':selected_scope,'archive_scope':archive_scope,'checked_at':checked_at,
            'schema_sha256':hashlib.sha256(s.dump(schema).encode()).hexdigest(),'request_id':result['request_id']}


def definition_scope(target,product_type,parentage='NONE',requirements='LISTING'):
    return {**{key:target.get(key) for key in ('account_id','seller_id','marketplace_id','environment','region')},
            'product_type':product_type,'parentage':parentage,'requirements':requirements}


def definition_matches(definition,target,product_type,parentage='NONE',requirements='LISTING'):
    if not isinstance(definition,dict) or definition.get('scope')!=definition_scope(target,product_type,parentage,requirements):
        return False
    descriptor=definition.get('descriptor') or {}
    return (descriptor.get('productType',product_type)==product_type and
            (not descriptor.get('marketplaceIds') or target['marketplace_id'] in descriptor['marketplaceIds']))


def assert_definition_scope(definition,target,product_type,parentage='NONE',requirements='LISTING'):
    if not definition_matches(definition,target,product_type,parentage,requirements):
        raise HTTPException(409,'商品规则的账号、卖家、站点或请求作用域已变化，请重新读取当前平台规则')


def download_json(url):
    from .network import public_stream
    try:
        with public_stream('GET',url,timeout=45) as r:
            if not r.is_success:raise PlatformError('平台规则文档下载失败',r.status_code)
            content=bytearray()
            for part in r.iter_bytes():
                content.extend(part)
                if len(content)>8*1024*1024:raise PlatformError('平台规则文件过大',400)
        return json.loads(content)
    except (httpx.RequestError,ValueError):raise PlatformError('平台规则文档读取失败',503)


def listing_path(target,sku):
    return '/listings/2021-08-01/items/'+quote(target['seller_id'],safe='')+'/'+quote(sku,safe='')


def get_listing(target,sku):
    return request(target,'GET',listing_path(target,sku),{'marketplaceIds':target['marketplace_id'],'includedData':'summaries,issues,attributes,fulfillmentAvailability,offers,relationships'})


def search_catalog_identifier(target, identifier):
    """Search the selected marketplace by an exact UPC-A or EAN-13."""
    from .identifiers import valid_gtin
    if not valid_gtin(identifier):
        raise HTTPException(422,'目录检索需要有效的 UPC-A 或 EAN-13')
    return request(target,'GET','/catalog/2022-04-01/items',{
        'marketplaceIds':target['marketplace_id'], 'identifiers':identifier,
        'identifiersType':'EAN' if len(identifier)==13 else 'UPC',
        'includedData':'summaries,attributes,identifiers,productTypes', 'pageSize':20})


def get_catalog_item(target, asin):
    return request(target,'GET','/catalog/2022-04-01/items/'+quote(asin,safe=''),
                   {'marketplaceIds':target['marketplace_id'],
                    'includedData':'summaries,attributes,identifiers,productTypes'})


def put_listing(target,sku,body,preview=False):
    params={'marketplaceIds':target['marketplace_id']}
    if preview:params['mode']='VALIDATION_PREVIEW'
    for attempt in range(3):
        try:return request(target,'PUT',listing_path(target,sku),params,body,non_mutating=preview)
        except PlatformError as exc:
            # A documented media-fetch timeout in non-mutating validation may
            # succeed once Amazon's fetch/cache recovers. Never replay a write,
            # another 400, a mixed rejection, or an ambiguous result.
            media_timeout=bool(exc.errors) and all(
                error.get('code')=='InvalidInput' and
                'Media Download Timed Out from Server' in error.get('message','')
                for error in exc.errors)
            safe_preview_timeout=preview and exc.status==400 and not exc.unknown and media_timeout
            if safe_preview_timeout and attempt==2:
                exc.preview_media_timeout=True
                raise
            if not (safe_preview_timeout and attempt<2):raise
            record('amazon.listing_media_preview_retry', target_id=target.get('id'),
                   seller_sku=sku, platform_request_id=exc.request_id, attempt=attempt+1,
                   payload_sha256=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest())
            time.sleep(attempt+1)


def restrictions(target,asin):
    if not isinstance(asin,str) or not re.fullmatch(r'[A-Z0-9]{10}',asin):
        raise HTTPException(422,'销售限制查询需要真实且有效的 ASIN；无 ASIN 商品请在 Seller Central 办理豁免')
    return request(target,'GET','/listings/2021-08-01/restrictions',{'asin':asin,'sellerId':target['seller_id'],
       'marketplaceIds':target['marketplace_id'],'conditionType':'new_new'})['body']




def patch_listing(target,sku,product_type,patches):
    return request(target,'PATCH',listing_path(target,sku),{'marketplaceIds':target['marketplace_id']},{'productType':product_type,'patches':patches})


def orders(target,cursor=None,before=None):
    params={'marketplaceIds':target['marketplace_id'],'lastUpdatedAfter':cursor or '2026-09-01T00:00:00Z','includedData':'FULFILLMENT,PROCEEDS,PACKAGES,CANCELLATION',
            'fulfillmentStatuses':'UNSHIPPED,PARTIALLY_SHIPPED','fulfilledBy':'MERCHANT'}
    if target.get('include_recipient'):
        from .customer_pii import require
        require()
        params['includedData']+=',RECIPIENT'
    if before:params['lastUpdatedBefore']=before
    rows=[];tokens=set()
    while True:
        r=request(target,'GET','/orders/2026-01-01/orders',params)['body']
        rows.extend(r.get('orders',[]))
        next_token=r.get('pagination',{}).get('nextToken') or r.get('paginationToken') or r.get('nextToken')
        if not next_token:break
        if next_token in tokens or len(tokens)>=100:raise PlatformError('订单分页令牌重复或页数过多，批次未推进',400)
        tokens.add(next_token)
        if len(rows)>2000:raise PlatformError('订单批次超过2000条，请缩小首次同步时间范围',400)
        params['paginationToken']=next_token
    return rows


def get_order(target,order_id,*,include_recipient=False):
    included = 'PACKAGES,FULFILLMENT,PROCEEDS'
    if include_recipient and target.get('include_recipient'):
        from .customer_pii import require
        require()
        included += ',RECIPIENT'
    result = request(target,'GET','/orders/2026-01-01/orders/'+quote(order_id,safe=''),{'includedData':included})['body']
    if 'RECIPIENT' not in included:
        result.pop('recipient', None)
    return result


def confirm_shipment(target,order_id,package):
    return request(target,'POST','/orders/v0/orders/'+quote(order_id,safe='')+'/shipmentConfirmation',body={'marketplaceId':target['marketplace_id'],'packageDetail':package})


def transactions(target,after):
    params={'postedAfter':after};rows=[];tokens=set()
    while True:
        body=request(target,'GET','/finances/2024-06-19/transactions',params)['body'].get('payload',{})
        rows.extend(body.get('transactions',[]))
        if not body.get('nextToken'):break
        if body['nextToken'] in tokens or len(tokens)>=100:raise PlatformError('交易分页令牌重复或页数过多',400)
        tokens.add(body['nextToken'])
        params={'nextToken':body['nextToken']}
        if len(rows)>10000:raise PlatformError('交易批次过大，请缩小时间范围',400)
    return rows
