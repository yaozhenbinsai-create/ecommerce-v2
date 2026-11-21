"""Account/marketplace bindings and explicit batch destinations."""
from fastapi import HTTPException
from . import store as s,locales,environment

# Official store identifiers and endpoint regions, checked 2026-09-12:
# https://developer-docs.amazon/sp-api/lang-en_US/docs/store-identifiers
# https://developer-docs.amazon/sp-api/lang-en_US/docs/sp-api-endpoints
MARKETPLACES = [dict(zip(('marketplace_id','name','region','currency'), row), language=locales.DEFAULTS[row[0]],language_mode='marketplace') for row in [
    ('ATVPDKIKX0DER','美国','NA','USD'), ('A2EUQ1WTGCTBG2','加拿大','NA','CAD'),
    ('A1AM78C64UM0Y8','墨西哥','NA','MXN'), ('A2Q3Y263D00KWC','巴西','NA','BRL'),
    ('A1F83G8C2ARO7P','英国','EU','GBP'), ('A1PA6795UKMFR9','德国','EU','EUR'),
    ('A13V1IB3VIYZZH','法国','EU','EUR'), ('APJ6JRA9NG5V4','意大利','EU','EUR'),
    ('A1RKKUPIHCS9HS','西班牙','EU','EUR'), ('A1805IZSGTT6HS','荷兰','EU','EUR'),
    ('AMEN7PMS3EDWL','比利时','EU','EUR'), ('A28R8C7NBKEWEA','爱尔兰','EU','EUR'),
    ('A2NODRKZP88ZB9','瑞典','EU','SEK'), ('A1C3SOZRARQ6R3','波兰','EU','PLN'),
    ('A2VIGQ35RCS4UG','阿联酋','EU','AED'), ('A17E79C6D8DWNP','沙特阿拉伯','EU','SAR'),
    ('A21TJRUUN4KGV','印度','EU','INR'), ('A33AVAJ2PDY3EV','土耳其','EU','TRY'),
    ('ARBP9OOSHTCHU','埃及','EU','EGP'), ('AE08WJ6YKNBMC','南非','EU','ZAR'),
    ('A1VC38T7YXB528','日本','FE','JPY'), ('A39IBJ37TRP1C6','澳大利亚','FE','AUD'),
    ('A19VAU5U5O7RUS','新加坡','FE','SGD'),
]]


def configuration():
    with s.main().db() as c:
        accounts=[dict(a) for a in c.execute('SELECT id,account_name,platform,enabled FROM platform_accounts ORDER BY id')]
        rows=s.all_rows(c,'target')
        for target in rows:
            target['runtime_available']=environment.allowed(target['environment'])
            try:target['effective_language']=locales.resolve(target);target['language_name']=locales.LANGUAGES[target['effective_language']]
            except HTTPException:target['language_name']='待配置'
        return {'accounts':accounts,'targets':rows,'marketplaces':MARKETPLACES,'languages':locales.options()}


def options(account, accounts, targets):
    choices={m['marketplace_id']:m for m in MARKETPLACES} if account['platform']=='亚马逊' else {}
    platform_ids={a['id'] for a in accounts if a['platform']==account['platform']}
    for target in targets:
        if target['account_id'] in platform_ids:
            choices.setdefault(target['marketplace_id'],{k:target[k] for k in ('marketplace_id','name','region','currency','language')})
    return choices


def save_bindings(payload):
    """Replace only explicitly scoped accounts in one environment, atomically."""
    from .environment import require
    require(payload.environment)
    environment=payload.environment
    account_ids=set(payload.account_ids)
    selections={row.account_id:set(row.marketplace_ids) for row in payload.selections}
    if len(selections)!=len(payload.selections) or not set(selections)<=account_ids:
        raise HTTPException(422,'账号选择重复或超出编辑范围')
    with s.transaction() as c:
        accounts=[dict(a) for a in c.execute('SELECT * FROM platform_accounts')]
        indexed={a['id']:a for a in accounts}
        if any(aid not in indexed or not indexed[aid]['enabled'] for aid in account_ids):
            raise HTTPException(422,'所选账号不存在或已停用，请刷新')
        targets=s.all_rows(c,'target')
        existing=[t for t in targets if t['account_id'] in account_ids and t['environment']==environment]
        if {t['id']:t['revision'] for t in existing}!=payload.revisions:
            raise HTTPException(409,'账号地区配置已更新，请关闭并重新打开后保存')
        desired={}
        for aid in account_ids:
            account=indexed[aid]
            selected=selections.get(aid,set())
            if selected and environment!='mock' and account['platform']!='亚马逊':
                raise HTTPException(422,f"{account['account_name']}：该平台适配器尚未接入，仅支持本地模拟")
            choices=options(account,accounts,targets)
            for mid in selected:
                if mid not in choices:raise HTTPException(422,'未知地区，请先添加自定义地区')
                desired[(aid,mid)]=choices[mid]
        result=[]
        for t in existing:
            enabled=(t['account_id'],t['marketplace_id']) in desired
            account=indexed[t['account_id']]
            changed=t.get('seller_id')!=account['seller_id']
            if t.get('enabled')!=enabled or (enabled and changed):
                t['enabled']=enabled
                if enabled and changed:t.update(seller_id=account['seller_id'],verified=False,connection={},last_sync_at=None)
                t=s.save(c,'target',t,t['revision'])
            result.append(t)
        for (aid,mid),market in desired.items():
            key=f'{aid}:{mid}:{environment}'
            # Reuse the existing binding, including settings, receipts and identity.
            if s.by_key(c,'target',key):continue
            account=indexed[aid]
            result.append(s.put(c,'target',key,market|{'account_id':aid,'account_name':account['account_name'],
                'seller_id':account['seller_id'],'environment':environment,'enabled':True,
                'include_recipient':False,'auto_sync_orders':False,'orders_since':''}))
        s.audit(c,'target_bindings_saved',None,s.dump({'environment':environment,'accounts':sorted(account_ids),'selected':len(desired)}))
        return {'targets':result,'selected_count':len(desired)}


def selected_targets(c, ids):
    if not isinstance(ids,list) or not ids or len(ids)>500 or any(type(i) is not int or i<1 for i in ids):
        raise HTTPException(422,'请明确勾选本批账号和地区')
    active={a[0] for a in c.execute('SELECT id FROM platform_accounts WHERE enabled=1')}
    indexed={t['id']:t for t in s.all_rows(c,'target')}
    if any(i not in indexed or not indexed[i].get('enabled') or indexed[i]['account_id'] not in active for i in ids):
        raise HTTPException(422,'所选账号或地区不存在、已停用，请刷新后重新选择')
    for i in ids:environment.require(indexed[i]['environment'])
    return [indexed[i] for i in dict.fromkeys(ids)]
