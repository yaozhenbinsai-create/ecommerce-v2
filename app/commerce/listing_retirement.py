"""Explicit seller-listing retirement with remote evidence and retained history.

This operator workflow never creates listings or deletes product source records.
"""
import hashlib
import json
import time

from fastapi import HTTPException
from . import amazon, store as s


def digest(value):
    return hashlib.sha256(s.dump(value).encode()).hexdigest()


def scope(target):
    return {k: target[k] for k in ('id', 'account_id', 'seller_id', 'marketplace_id', 'region', 'environment')}


def validate(c, plan):
    target = s.get(c, 'target', plan['target']['id'])
    if scope(target) != plan['target'] or target['environment'] != 'production':
        raise HTTPException(409, '删除目标已改变')
    settings = s.by_key(c, 'settings', 'global') or {}
    if not settings.get('live_writes_enabled') or not settings.get('listing_automation_paused'):
        raise HTTPException(409, '请保持正式写入开启、自动上架暂停')
    entries = plan['items']
    if not entries or len({x['seller_sku'] for x in entries}) != len(entries):
        raise HTTPException(409, '删除清单为空或 SKU 重复')
    listings = {}
    for ident in sorted({x['listing_id'] for x in entries}):
        listing = s.get(c, 'listing', ident)
        if listing['target_id'] != target['id']:
            raise HTTPException(409, '刊登不属于删除目标')
        children = {x['seller_sku'] for x in listing['draft']['skus']}
        selected = {x['seller_sku'] for x in entries if x['listing_id'] == ident and not x['parent']}
        if children != selected:
            raise HTTPException(409, '必须明确选择整组销售规格，不能遗漏或添加 SKU')
        parents = {x['seller_sku'] for x in entries if x['listing_id'] == ident and x['parent']}
        expected = {listing['draft']['parent_sku']} if listing['draft'].get('variation_theme') and len(children) > 1 else set()
        if parents != expected:
            raise HTTPException(409, '父体范围与实际变体组不一致')
        listings[ident] = listing
    return target, listings


def begin(plan):
    key = digest(plan)
    with s.transaction() as c:
        target, listings = validate(c, plan)
        existing = s.by_key(c, 'listing_retirement', key)
        if existing:
            return existing
        ids = set(listings)
        workflows = [r for r in s.all_rows(c, 'listing_workflow') if r.get('listing_id') in ids]
        workflow_ids = {r['id'] for r in workflows}
        owned_jobs = []
        for row in c.execute('SELECT * FROM commerce_jobs'):
            payload = json.loads(row['payload'])
            if payload.get('listing_id') in ids or payload.get('workflow_id') in workflow_ids:
                if row['status'] in {'running', 'unknown'}:
                    raise HTTPException(409, '相关任务正在执行或结果待核实，尚未删除任何刊登')
                if row['status'] in {'queued', 'retry_wait', 'paused'}:
                    owned_jobs.append(row['id'])
        publications = [r for r in s.all_rows(c, 'detail_publication') if r.get('listing_id') in ids]
        allocations = [r for r in s.all_rows(c, 'allocation') if r.get('listing_id') in ids]
        if any(r.get('quantity', 0) and r.get('policy') != 'amazon_max' for r in allocations):
            raise HTTPException(409, '存在实际库存承诺，需先核对库存释放方式')
        record = s.put(c, 'listing_retirement', key, {'plan': plan, 'status': 'removing', 'started_at': time.time(),
            'before': {'listings': list(listings.values()), 'workflows': workflows,
                       'publications': publications, 'allocations': allocations}, 'cancelled_job_ids': owned_jobs})
        for ident, listing in listings.items():
            listing.update(paused=True, retirement_id=record['id'], checked_at=time.time())
            s.save(c, 'listing', listing)
            s.audit(c, 'listing_retirement_started', ident, s.dump({'retirement_id': record['id'], 'reason': plan['reason']}))
        for job_id in owned_jobs:
            c.execute("UPDATE commerce_jobs SET status='cancelled',error=?,next_at=0,lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      ('商户要求删除刊登并逐项重做；原任务记录保留', job_id))
        for row in workflows:
            row.update(status='cancelled', stage='cancelled', message='商户要求删除刊登并逐项重做；资料与历史保留', retirement_id=record['id'])
            s.save(c, 'listing_workflow', row)
        for row in publications:
            row.update(status='cancelled', next_at=0, retirement_id=record['id'])
            s.save(c, 'detail_publication', row)
        return record


def read_presence(target, sku):
    try:
        response = amazon.get_listing(target, sku)
    except amazon.PlatformError as exc:
        if exc.status == 404 and exc.request_id and not exc.unknown:
            return {'absent': True, 'http_status': 404, 'request_id': exc.request_id,
                    'errors': exc.errors, 'checked_at': time.time()}
        raise
    return {'absent': False, 'checked_at': time.time(), 'response': response}


def delete_one(target, entry):
    """Exactly one explicit seller/SKU delete; transport errors are never replayed here."""
    presence = read_presence(target, entry['seller_sku'])
    if presence['absent']:
        return {'already_absent': True, 'presence': presence}
    body = presence['response']['body']
    summaries = [r for r in body.get('summaries', []) if r.get('marketplaceId') == target['marketplace_id']]
    if body.get('sku') != entry['seller_sku'] or len(summaries) != 1 or summaries[0].get('asin') != entry['current_asin']:
        raise HTTPException(409, '删除前 SKU/ASIN 发生变化，已停止：' + entry['seller_sku'])
    response = amazon.request(target, 'DELETE', amazon.listing_path(target, entry['seller_sku']),
                              {'marketplaceIds': target['marketplace_id']})
    if response['body'].get('status') != 'ACCEPTED' or any(i.get('severity') == 'ERROR' for i in response['body'].get('issues', [])):
        raise HTTPException(409, '亚马逊未接受删除：' + entry['seller_sku'])
    return response


def complete(plan, evidence, receipts, *, allow_parent_processing=False):
    key = digest(plan)
    skus = {r['seller_sku'] for r in plan['items']}
    required = {r['seller_sku'] for r in plan['items'] if not allow_parent_processing or not r['parent']}
    if set(evidence) != skus or any(not evidence[sku].get('absent') or evidence[sku].get('http_status') != 404 or not evidence[sku].get('request_id')
                                  or not 0 <= time.time() - evidence[sku].get('checked_at', 0) < 300 for sku in required):
        raise HTTPException(409, '必须取得全部 SKU 的近期官方不存在证据，不能提前标记未上架')
    pending_parents = [r['seller_sku'] for r in plan['items'] if r['parent'] and not evidence[r['seller_sku']].get('absent')]
    if pending_parents and (set(receipts) != skus or any(r.get('status') != 'accepted' for r in receipts.values())):
        raise HTTPException(409, '父体尚未完成时，必须保留全部已接受的删除请求回执')
    with s.transaction() as c:
        _, listings = validate(c, plan)
        record = s.by_key(c, 'listing_retirement', key)
        if not record:
            raise HTTPException(409, '缺少删除操作记录')
        if record['status'] == 'completed':
            return record
        for ident, listing in listings.items():
            listing.update(status='not_submitted', published_version=None, published_skus=[], sku_results={},
                           platform_observed={r['seller_sku']: {'status': 'not_submitted', 'retirement_id': record['id'],
                               'deletion_verified': evidence[r['seller_sku']]} for r in plan['items'] if r['listing_id'] == ident and not r['parent']},
                           buyable_since=None, checked_at=time.time(), last_error='', paused=True,
                           retirement_verified_at=time.time())
            s.save(c, 'listing', listing)
            s.audit(c, 'listing_retirement_completed', ident, s.dump({'retirement_id': record['id'], 'seller_skus': sorted(listing['platform_observed'])}))
        for allocation in s.all_rows(c, 'allocation'):
            if allocation.get('listing_id') in listings:
                allocation.update(quantity=0, retirement_id=record['id'])
                s.save(c, 'allocation', allocation)
        record.update(status='parents_processing' if pending_parents else 'completed',
                      completed_at=None if pending_parents else time.time(), sales_unlisted_at=time.time(),
                      pending_parent_skus=pending_parents, absence_evidence=evidence, delete_receipts=receipts)
        return s.save(c, 'listing_retirement', record)


def archive_retired_refreshes(plan):
    """Retain, cancel and acknowledge only obsolete reads after verified sales deletion."""
    with s.transaction() as c:
        _, listings = validate(c, plan)
        retirement = s.by_key(c, 'listing_retirement', digest(plan))
        if not retirement or retirement['status'] not in {'completed', 'parents_processing'}:
            raise HTTPException(409, '尚未确认销售刊登删除，不能归档同步报错')
        if any(row.get('published_version') or not row.get('retirement_verified_at') for row in listings.values()):
            raise HTTPException(409, '系统商品尚未重置为未上架')
        archived = retirement.get('archived_refreshes', [])
        archived_ids = {r['job']['id'] for r in archived}
        for job_row in c.execute("SELECT * FROM commerce_jobs WHERE kind='refresh' AND status IN ('queued','retry_wait','paused')").fetchall():
            payload = json.loads(job_row['payload'])
            if payload.get('listing_id') not in listings or job_row['id'] in archived_ids:
                continue
            result = json.loads(job_row['result'])
            if job_row['status'] == 'paused' and result.get('platform_error', {}).get('http_status') != 404:
                continue
            notice = s.by_key(c, 'notice', 'job:' + str(job_row['id']))
            archived.append({'job': dict(job_row), 'notice': notice})
            c.execute("UPDATE commerce_jobs SET status='cancelled',error=?,next_at=0,lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      ('销售刊登已确认删除；旧同步任务和原报错已保留归档', job_row['id']))
            if notice:
                notice['read'] = True
                s.save(c, 'notice', notice)
            s.audit(c, 'retired_listing_refresh_archived', payload['listing_id'], s.dump({'job_id': job_row['id'], 'retirement_id': retirement['id']}))
        retirement['archived_refreshes'] = archived
        return s.save(c, 'listing_retirement', retirement)
