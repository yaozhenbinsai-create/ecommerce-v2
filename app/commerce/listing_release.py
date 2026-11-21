"""Explicit publication cycles, retirement barriers and immutable request snapshots."""
from __future__ import annotations

import copy
import hashlib
import time

from fastapi import HTTPException

from . import amazon, store as s


def cycle(listing):
    return int(listing.get('publication_cycle_id') or 0)


def identity_key(target, seller_sku, listing=None):
    base = f"{target['id']}:{seller_sku}"
    return base + f":cycle:{cycle(listing)}" if listing and cycle(listing) else base


def assert_action(listing, action):
    if listing.get('retirement_id') and action not in {'preview', 'refresh', 'readiness'}:
        raise HTTPException(409, '商品已删除，请先准备新的重新刊登草稿；原刊登不能恢复或重发')
    if action in {'pause', 'resume', 'continue'} and not listing.get('published_version'):
        raise HTTPException(409, '该商品尚未上架，暂停或恢复销售只适用于已发布商品')


def assert_job(listing, payload, action):
    assert_action(listing, action)
    if int(payload.get('publication_cycle_id') or 0) != cycle(listing):
        raise HTTPException(409, '任务属于旧刊登批次，已停止；请使用当前批次')


def begin_first_publication(c, listing):
    """An explicit merchant request may release an unused first-listing hold.

    Workers never call this. A retired, accepted, unknown or already published
    listing must retain its existing recovery/availability operation.
    """
    assert_action(listing, 'submit')
    if not listing.get('paused'):
        return listing
    if (listing.get('published_version') is not None or listing.get('published_skus')
            or listing.get('status') != 'not_submitted'):
        raise HTTPException(409, '商品已有平台提交或销售记录，请从商品页面核对并恢复销售')
    unsettled = c.execute("""SELECT 1 FROM commerce_jobs
        WHERE kind IN ('submit','availability') AND json_extract(payload,'$.listing_id')=?
        AND status IN ('queued','running','retry_wait','unknown') LIMIT 1""", (listing['id'],)).fetchone()
    receipt = c.execute("""SELECT 1 FROM commerce_records WHERE kind='submission'
        AND json_extract(data,'$.listing_id')=? AND COALESCE(json_extract(data,'$.preview'),0)=0
        AND json_extract(data,'$.status') IN ('accepted','sending','unknown') LIMIT 1""", (listing['id'],)).fetchone()
    if unsettled or receipt:
        raise HTTPException(409, '已有提交任务或平台回执待核对，保留暂停，不重复首次上架')
    current = s.save(c, 'listing', listing | {'paused': False})
    s.audit(c, 'first_publication_requested', listing['id'],
            s.dump({'previous_paused': True, 'publication_cycle_id': cycle(listing),
                    'draft_version': listing['draft_version']}))
    return current


def bind_payload(listing, payload):
    return {**payload, 'publication_cycle_id': cycle(listing)} if cycle(listing) else payload


def assert_preparation_available(c, listing, target):
    """Local field preparation is independent of the sale pause flag."""
    busy = c.execute("""SELECT 1 FROM commerce_jobs WHERE
        json_extract(payload,'$.listing_id')=? AND kind IN ('materials','submit')
        AND status IN ('queued','running','retry_wait','unknown')""", (listing['id'],)).fetchone()
    if busy:
        raise HTTPException(409, '商品资料或平台提交正在处理或待核实，请等待原任务结果')
    from . import listing_identity
    listing_identity.assert_source_current(c, listing, target, listing['draft'])


def fingerprint(value):
    return hashlib.sha256(s.dump(value).encode()).hexdigest()


def assert_accepted_request(receipt, body):
    if receipt and receipt.get('status') == 'accepted' and receipt.get('request') != body:
        raise HTTPException(409, '本版本已有回执与本次请求不同；原回执已保留，请保存为新版本后再提交')


def snapshot(listing, target, draft, bodies):
    from .listing_identity import target_scope
    return {'listing_id': listing['id'], 'version': listing['draft_version'],
            'publication_cycle_id': cycle(listing), 'target_scope': target_scope(target),
            'draft_sha256': fingerprint(draft), 'listing_draft_sha256': fingerprint(listing['draft']),
            'request_sha256': fingerprint(bodies),
            'checked_at': time.time()}


def assert_snapshot(listing, target, draft, saved, bodies=None):
    from .listing_identity import target_scope
    if (listing['draft_version'] != saved['version'] or cycle(listing) != saved['publication_cycle_id']
            or target_scope(target) != saved['target_scope'] or fingerprint(draft) != saved['draft_sha256']
            or fingerprint(listing['draft']) != saved['listing_draft_sha256']
            or (bodies is not None and fingerprint(bodies) != saved['request_sha256'])):
        raise HTTPException(409, '预检后的商品、售价、配送或刊登批次已变化，请重新预检')


def apply_cycle_names(listing, draft):
    names = listing.get('publication_sku_names') or {}
    if not names:
        return draft
    for row in draft.get('skus', []):
        if str(row['sku_id']) not in names:
            raise HTTPException(409, '商品规格已变化，请重新核对刊登批次')
        row['seller_sku'] = names[str(row['sku_id'])]
    if draft.get('variation_theme'):
        draft['parent_sku'] = names['parent']
    return draft


def prepare(ident, payload):
    """Prepare a local draft only, after independently reading old SKU absence."""
    from . import catalog, listing_retirement, listing_identity
    strategy = payload.get('sku_strategy', 'retain')
    if strategy not in {'retain', 'new'}:
        raise HTTPException(422, '请选择沿用卖家SKU或新建卖家SKU')
    with s.main().db() as c:
        listing = s.get(c, 'listing', ident)
        target = s.get(c, 'target', listing['target_id'])
        if listing['revision'] != payload.get('revision'):
            raise HTTPException(409, '商品已变化，请刷新后重试')
        retirement = s.get(c, 'listing_retirement', listing.get('retirement_id'), required=False)
        if not retirement or listing.get('published_version') or listing.get('status') != 'not_submitted':
            raise HTTPException(409, '此操作只用于已确认删除的未上架商品')
        listing_identity.assert_target_scope(target, listing['draft'])
        entries = [row for row in retirement['plan']['items'] if row['listing_id'] == ident]
        if not entries or {row['seller_sku'] for row in entries if not row['parent']} != {row['seller_sku'] for row in listing['draft']['skus']}:
            raise HTTPException(409, '原删除清单与商品不一致')
    absence = {row['seller_sku']: listing_retirement.read_presence(target, row['seller_sku']) for row in entries}
    if any(not row['absent'] for row in absence.values()):
        raise HTTPException(409, '原SKU或父体仍可读取，保留未上架状态，待目录删除确认后再准备')
    with s.transaction() as c:
        current = s.get(c, 'listing', ident)
        actual_target = s.get(c, 'target', current['target_id'])
        if current['revision'] != listing['revision'] or listing_identity.target_scope(actual_target) != listing_identity.target_scope(target):
            raise HTTPException(409, '目录核查期间商品或店铺已变化')
        active = c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')", (ident,)).fetchone()
        if active:
            raise HTTPException(409, '相关任务尚未结束，请先核对原任务')
        draft = copy.deepcopy(current['draft'])
        record = s.put(c, 'listing_publication_cycle', f"{ident}:{current['draft_version'] + 1}", {
            'listing_id': ident, 'target_scope': listing_identity.target_scope(target),
            'previous_retirement_id': current['retirement_id'], 'previous_draft': copy.deepcopy(draft),
            'previous_version': current['draft_version'], 'sku_strategy': strategy,
            'absence_evidence': absence, 'status': 'prepared_not_published', 'prepared_at': time.time()})
        names = {}
        for row in draft['skus']:
            if strategy == 'new':
                row['seller_sku'] = f"{row['seller_sku'][:18]}-{row['sku_id']}-R{record['id']}"
            names[str(row['sku_id'])] = row['seller_sku']
        if draft.get('variation_theme'):
            if strategy == 'new':
                draft['parent_sku'] = f"{draft['parent_sku'][:20]}-R{record['id']}"
            names['parent'] = draft['parent_sku']
        current.update(draft=draft, draft_version=current['draft_version'] + 1,
                       publication_cycle_id=record['id'], publication_sku_names=names,
                       previous_retirement_id=current['retirement_id'], status='not_submitted', paused=False,
                       published_version=None, published_skus={}, platform_observed={}, sku_results={},
                       preview_results={}, last_error='', buyable_since=None)
        current.pop('retirement_id', None)
        current.pop('retirement_verified_at', None)
        current.pop('detail_publication_id', None)
        for field in ('platform_parent_observed', 'checked_at', 'previous_observed_at',
                      'preflight_results', 'identifier_check', 'publication_snapshot',
                      'packaging_projection', 'media_exclusions', 'platform_issues'):
            current.pop(field, None)
        current['issues'] = catalog.issues(draft, target)
        current['draft_status'] = 'needs_fields' if current['issues'] else 'ready'
        s.put(c, 'material_version', f"{ident}:{current['draft_version']}",
              {'listing_id': ident, 'version': current['draft_version'], 'draft': draft, 'publication_cycle_id': record['id']})
        s.audit(c, 'relisting_draft_prepared', ident, s.dump({'publication_cycle_id': record['id'], 'sku_strategy': strategy}))
        return s.save(c, 'listing', current)


def parent_observation(c, listing, target, response):
    from .listing_identity import observed_asin, target_scope
    name = listing['draft']['parent_sku']
    body = response.get('body') or {}
    asin, error = observed_asin(body, target['marketplace_id'], name)
    if error or response.get('http_status') != 200 or not response.get('request_id'):
        return {'status': 'unverified', 'message': error or '父体结果尚未核实'}
    key = identity_key(target, name, listing)
    old = s.by_key(c, 'listing_parent_identity', key)
    if not asin:
        return {'status': 'processing'}
    if old and (old['asin'] != asin or old['target_scope'] != target_scope(target)):
        return {'status': 'identity_conflict', 'asin': asin, 'message': '父体目录身份与此前确认结果不一致'}
    if not old:
        old = s.put(c, 'listing_parent_identity', key, {'listing_id': listing['id'], 'seller_sku': name,
            'publication_cycle_id': cycle(listing), 'target_scope': target_scope(target), 'asin': asin,
            'request_id': response['request_id'], 'checked_at': time.time()})
    return {'status': 'confirmed', 'asin': asin, 'record_id': old['id'], 'request_id': response['request_id']}


def verify_parent(listing, target, draft, attributes=None, definition=None):
    if target.get('environment') != 'production' or not draft.get('variation_theme'):
        return
    name = draft['parent_sku']
    published = (listing.get('published_skus') or {}).get(name)
    if published is None and not listing.get('published_version') and not cycle(listing):
        return  # Original first-publication flow retains its existing identifier checks.
    try:
        response = amazon.get_listing(target, name)
    except amazon.PlatformError as exc:
        if published is None and not listing.get('published_version') and exc.status == 404 and exc.request_id and not exc.unknown:
            return
        raise
    with s.main().db() as c:
        old = s.by_key(c, 'listing_parent_identity', identity_key(target, name, listing))
        receipt = s.by_key(c, 'submission', f"{listing['id']}:{listing['draft_version']}:{name}:False")
    # A partial first submission can have accepted the parent before any child
    # existed. Adopt only the exact accepted request, never a coincidental SKU.
    if (not old and receipt and receipt.get('status') == 'accepted'
            and int(receipt.get('publication_cycle_id') or 0) == cycle(listing)
            and receipt.get('request', {}).get('attributes')
            and all((response.get('body') or {}).get('attributes', {}).get(k) == v
                    for k, v in receipt['request']['attributes'].items())):
        with s.transaction() as c:
            current = s.get(c, 'listing', listing['id'])
            if cycle(current) != cycle(listing) or current['draft_version'] != listing['draft_version']:
                raise HTTPException(409, '父体核对期间刊登批次已变化')
            proof = parent_observation(c, listing, target, response)
            if proof.get('status') == 'confirmed':
                old = s.get(c, 'listing_parent_identity', proof['record_id'])
    if not old:
        raise HTTPException(409, '父体SKU已存在或原编号未核实，请先刷新并核对父体身份')
    from .listing_identity import observed_asin, target_scope
    asin, error = observed_asin(response.get('body') or {}, target['marketplace_id'], name)
    if error or not response.get('request_id') or response.get('http_status') != 200 or asin != old['asin'] or old['target_scope'] != target_scope(target):
        raise HTTPException(409, '父体目录身份已变化，已停止本批次提交')
    if attributes is not None:
        names = ((definition or {}).get('propertyGroups') or {}).get('product_identity', {}).get('propertyNames') or []
        names = set(names) | {'item_name', 'brand', 'manufacturer', 'model_name', 'model_number', 'part_number'}
        observed = (response.get('body') or {}).get('attributes') or {}
        if any(attributes.get(key) != observed.get(key) for key in names if key in attributes):
            raise HTTPException(409, '父体身份字段将发生变化；当前父体规则不支持ASIN绑定，已停止提交，请单独处理父体身份字段')
