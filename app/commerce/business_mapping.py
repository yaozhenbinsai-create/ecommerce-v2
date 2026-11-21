"""One business action; platform scope comes exclusively from platform accounts."""
import copy
import json
from datetime import date
from pathlib import Path
from fastapi import HTTPException
from . import store as s, categories, catalog, amazon, category_rules


def seed():
    return json.loads(Path(__file__).with_name('category_seed.json').read_text(encoding='utf-8'))


def business_entry(business, category):
    entries=seed()['categories']
    exact=next((e for e in entries if e['business_name']==business['name']),None)
    if exact:return exact
    name=business['name']
    # Only explicit business names are classified locally; do not guess from a
    # material or size, or label arbitrary products as pillows.
    matches=[]
    for names,code in [(('四件套',),'TEXTILE_SET_4'),(('三件套',),'TEXTILE_SET_3'),(('两件套','二件套'),'TEXTILE_SET_2'),
                       (('枕套',),'PILLOWCASE'),(('枕芯','枕头'),'BED_PILLOW'),(('床笠',),'FITTED_SHEET'),
                       (('床单',),'FLAT_SHEET'),(('被套',),'DUVET_COVER')]:
        if any(word in name for word in names):matches.append(next(e for e in entries if e['code']==code))
    return matches[0] if len(matches)==1 else None


def destinations(c):
    accounts=[dict(x) for x in c.execute('SELECT id,platform,marketplace,account_name FROM platform_accounts WHERE enabled=1 ORDER BY id')]
    targets=s.all_rows(c,'target');result=[]
    for account in accounts:
        configured=[t for t in targets if t['account_id']==account['id']]
        active=[t for t in configured if t.get('enabled')]
        if configured and not active:continue
        if active:
            for t in active:result.append({'account':account,'target':t,'marketplace_id':t['marketplace_id'],'site_name':t['name']})
        else:
            # Reuse the platform page's existing site, never ask for a duplicate.
            market=account['marketplace'].strip()
            if categories.platform_name(account['platform'])=='亚马逊' and market.lower() in {'美国','美国站','us','usa','amazon.com','united states'}:market=amazon.US
            result.append({'account':account,'target':None,'marketplace_id':market,'site_name':account['marketplace']})
    return result


def ensure_category(c,business):
    old=next((x for x in s.all_rows(c,'category') if business['id'] in x.get('business_ids',[])),None)
    if old:
        entry=business_entry(business,old)
        changes={'enabled':True}
        if len(old['business_ids'])==1:
            changes['name']=business['name']
            if entry:changes['path']=entry['path']
        if any(old.get(k)!=v for k,v in changes.items()):old=categories.save_category(c,old|changes,old['id'])
        return old
    entry=business_entry(business,None)
    return categories.save_category(c,{'code':'BUSINESS_'+str(business['id']),'name':business['name'],
        'path':entry['path'] if entry else business['name'],'business_ids':[business['id']],'enabled':True,
        'notes':'由业务配置一键映射自动建立'})


def map_destination(business,category,destination,read_official=False,cache=None):
    account=destination['account'];mid=destination['marketplace_id'];target=destination['target']
    platform=categories.platform_name(account['platform'])
    row={'platform':platform,'site_name':destination['site_name'] or '未设置站点','account_name':account['account_name'],'account_id':account['id']}
    if not mid:return row|{'status':'pending','message':'平台页面尚未设置站点'}
    with s.main().db() as c:
        existing=next((x for x in s.all_rows(c,'category_mapping') if x['category_id_local']==category['id'] and x['platform']==platform and x['marketplace_id']==mid),None)
    entry=business_entry(business,category)
    different=bool(platform=='亚马逊' and mid==amazon.US and entry and existing and existing.get('product_type')!=entry['product_type'])
    def outcome(mapping):
        if platform=='亚马逊' and read_official:
            mapping=category_rules.sync(mapping,target,cache)
        official=category_rules.state(mapping)
        return row|{'status':'matched','message':official['message'] if platform=='亚马逊' else '已自动关联',
                    'mapping_id':mapping['id'],'name':mapping['name'],'classification_complete':official['complete']}
    if existing and not different and existing.get('enabled') and existing['status']!='sample' and (existing.get('product_type') or existing.get('category_id')):
        return outcome(existing)
    fields=None
    if platform=='亚马逊' and mid==amazon.US and entry:
        fields={'product_type':entry['product_type'],'name':entry['name'],'category_path':entry['category_path'],
            'source':seed()['source'],'source_date':seed()['source_date'],'status':'reference','notes':entry.get('notes','')}
    elif platform=='亚马逊' and target and target['environment']=='production':
        try:
            found=amazon.search_product_types(target,business['name'],'itemName')
            choices=found['product_types']
            if not found['sample_only'] and len(choices)==1:
                fields={'product_type':choices[0]['name'],'name':business['name'],'category_path':'',
                    'source':'Amazon商品标题推荐；请求 '+found['request_id'],'source_date':date.today().isoformat(),'status':'manual'}
            else:return row|{'status':'pending','message':'平台未返回明确分类，暂未映射'}
        except (amazon.PlatformError,HTTPException):return row|{'status':'pending','message':'平台分类服务暂时不可用，可稍后再次一键映射'}
    if fields is None:
        message='该平台分类服务尚未接入' if platform!='亚马逊' else '当前沙箱不提供此类商品的真实分类，系统分类库暂未覆盖'
        return row|{'status':'pending','message':message}
    with s.transaction() as c:
        # Another account can share this platform/site mapping; reread before save.
        latest=next((x for x in s.all_rows(c,'category_mapping') if x['category_id_local']==category['id'] and x['platform']==platform and x['marketplace_id']==mid),None)
        if different:fields.update(category_id='',item_type_keyword='')
        payload={**(latest or {}),**fields,'category_id_local':category['id'],'platform':platform,'marketplace_id':mid,
                 'marketplace_name':destination['site_name'],'enabled':True}
        mapping=categories.save_mapping(c,payload,latest['id'] if latest else None)
    return outcome(mapping)


def apply_to_products(business_id,category):
    with s.transaction() as c:
        ids=[]
        for product in s.all_rows(c,'product'):
            if product.get('business_id')!=business_id:continue
            ids.append(product['id'])
            if product.get('category_id')!=category['id']:
                product['category_id']=category['id'];product['fact_revision']=product.get('fact_revision',0)+1;s.save(c,'product',product)
        listings=[x for x in s.all_rows(c,'listing') if x['product_id'] in ids]
    updated=[]
    for listing in listings:
        old=listing.get('draft',{})
        if not old or listing.get('published_version') is not None or listing.get('status')!='not_submitted':continue
        if old.get('product_type') and not old.get('classification'):continue
        with s.main().db() as c:
            active=c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(listing['id'],)).fetchone()
            if active:continue
            target=s.get(c,'target',listing['target_id'])
            account=c.execute('SELECT enabled FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
            if not target.get('enabled') or not account or not account[0]:continue
            draft=copy.deepcopy(old)
            found=categories.apply_defaults(c,s.get(c,'product',listing['product_id']),target,draft)
        if found['mapping'] and draft!=old:
            try:
                catalog.refresh_archive_fields(listing['id']);updated.append(listing['id'])
            except HTTPException as exc:
                if exc.status_code!=409:raise
    return updated


def run(job):
    output=[];cache={}
    for bid in job['payload']['business_ids']:
        with s.transaction() as c:
            record=c.execute('SELECT * FROM businesses WHERE id=?',(bid,)).fetchone()
            if not record:continue
            business=dict(record)
            if not business['enabled']:continue
            category=ensure_category(c,business);scope=destinations(c)
            platforms=list(dict.fromkeys(categories.platform_name(d['account']['platform']) for d in scope))
            c.execute('UPDATE businesses SET platforms=?,platform=? WHERE id=?',(s.dump(platforms),platforms[0] if platforms else '',bid))
        rows=[map_destination(business,category,d,job['payload'].get('read_official',False),cache) for d in scope]
        updated=apply_to_products(bid,category)
        all_platforms={r['platform'] for r in rows}
        success={p for p in all_platforms if all(r['status']=='matched' for r in rows if r['platform']==p)}
        complete=bool(rows) and all(r.get('classification_complete') for r in rows)
        result={'business_id':bid,'name':business['name'],'platform_count':len(all_platforms),'matched_platforms':len(success),
                'destinations':rows,'status':'completed' if complete else 'partial',
                'message':(f'已关联 {len(success)}/{len(all_platforms)} 个平台 · '+('官方分类已补齐' if complete else '分类待补充')) if rows else '平台页面暂无启用的平台站点',
                'drafts_updated':updated}
        with s.transaction() as c:s.put(c,'business_mapping',str(bid),result)
        output.append(result)
    return {'businesses':output,'count':len(output)}
