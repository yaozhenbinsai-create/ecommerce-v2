"""Resolve one-click listing scope from enabled platform configuration."""
import hashlib
from fastapi import HTTPException
from . import environment, store as s


def plan(c):
    accounts={a['id']:dict(a) for a in c.execute('SELECT id,account_name,platform FROM platform_accounts WHERE enabled=1')}
    targets=sorted([t for t in s.all_rows(c,'target') if t.get('enabled') and environment.allowed(t['environment']) and t['account_id'] in accounts],key=lambda t:t['id'])
    rows=sorted([r for r in s.all_rows(c,'listing') if r['target_id'] in {t['id'] for t in targets}],key=lambda r:r['id'])
    if not rows:raise HTTPException(422,'启用的账号地区暂无商品资料，请先生成生产资料')
    selection=[{'listing_id':r['id'],'revision':r['revision']} for r in rows]
    groups=[{'target_id':t['id'],'account_name':accounts[t['account_id']]['account_name'],'name':t['name'],
             'environment':t['environment'],'listing_ids':[r['id'] for r in rows if r['target_id']==t['id']]} for t in targets if any(r['target_id']==t['id'] for r in rows)]
    settings=s.by_key(c,'settings','global') or {}
    fingerprint=hashlib.sha256(s.dump({'selection':selection,'targets':targets,'accounts':accounts,'live_writes_enabled':settings.get('live_writes_enabled',False)}).encode()).hexdigest()
    return {'fingerprint':fingerprint,'selection':selection,'groups':groups,'listing_count':len(rows),
            'overwrite_count':sum(bool(r.get('published_version') or r.get('published_skus') or r.get('platform_observed')) for r in rows)}


def request(c,payload):
    from .routes import ListingBatch,queue_listing_batch
    intent=payload.model_dump();previous=s.by_key(c,'listing_batch',payload.request_id)
    if previous:
        if previous.get('all_intent')!=intent:raise HTTPException(409,'该批次编号已用于另一批上架')
        return previous
    current=plan(c)
    if current['fingerprint']!=payload.fingerprint:raise HTTPException(409,'商品资料或账号地区配置已变化，请重新点击一键上架并确认')
    if current['overwrite_count'] and not payload.overwrite:raise HTTPException(409,'已有平台商品，请先确认是否覆盖原来数据')
    for row in current['selection']:
        active=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('materials','submit','availability') AND status IN ('queued','running','retry_wait','unknown') AND json_extract(payload,'$.listing_id')=?",(row['listing_id'],)).fetchone()
        if active:raise HTTPException(409,'商品有正在处理或待核实的任务，请完成后再一键上架')
    result=queue_listing_batch(c,ListingBatch(request_id=payload.request_id,listings=current['selection']))
    result['all_intent']=intent
    return s.save(c,'listing_batch',result)
