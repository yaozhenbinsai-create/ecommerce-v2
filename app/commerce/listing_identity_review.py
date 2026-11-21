"""Explicit, evidence-bound review of an Amazon-assigned replacement ASIN.

The first identity stays immutable. No Amazon mutation or catalog offer is made.
"""
import re
import time
import uuid

from fastapi import HTTPException
from . import amazon, listing_identity as identity, store as s

FIELDS = ('item_name', 'brand', 'model_name', 'color', 'size', 'item_length_width',
          'item_weight', 'fabric_type', 'fill_material', 'cover')


def canonical(value, field=''):
    if isinstance(value, dict):
        return {key: canonical(item, field) for key, item in value.items()
                if key not in {'marketplace_id', 'language_tag'}}
    if isinstance(value, list):
        return [canonical(item, field) for item in value]
    if isinstance(value, str):
        value = ' '.join(value.split()).casefold()
        if field == 'item_name':
            value = re.sub(r'^generic\s+', '', value)
    return value


def context(c, ident):
    listing = s.get(c, 'listing', ident)
    target = s.get(c, 'target', listing['target_id'])
    version = listing.get('published_version')
    if target['environment'] != 'production' or not target.get('enabled') or not version or listing.get('paused'):
        raise HTTPException(409, '仅可核对已正式提交且未暂停的商品')
    if c.execute("SELECT 1 FROM commerce_jobs WHERE kind IN ('submit','availability') AND json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')", (ident,)).fetchone():
        raise HTTPException(409, '商品写入仍在处理或待核实，请先完成回查')
    material = s.by_key(c, 'material_version', f'{ident}:{version}')
    if not material:
        raise HTTPException(409, '缺少已提交资料版本')
    draft = material['draft']
    identity.assert_source_current(c, listing, target, draft)
    return listing, target, draft


def inspect(ident):
    with s.main().db() as c:
        listing, target, draft = context(c, ident)
        entries = []
        for sku in draft['skus']:
            from . import listing_release
            old = s.by_key(c, 'listing_identity', listing_release.identity_key(target,sku['seller_sku'],listing))
            receipt = s.by_key(c, 'submission', f"{ident}:{listing['published_version']}:{sku['seller_sku']}:False")
            if not old or not receipt or receipt.get('preview') or receipt.get('status') != 'accepted':
                raise HTTPException(409, '必须保留首个身份及同版正式提交成功回执')
            if sku.get('asin') or sku.get('gtin'):
                raise HTTPException(409, '已有目录标识的商品需单独核对，不能使用新货身份复核')
            if old.get('sku_id') != sku['sku_id'] or old.get('listing_id') != ident or any(
                    old.get(key) != target.get(key) for key in ('account_id', 'seller_id', 'marketplace_id')):
                raise HTTPException(409, '首个身份与当前商品或店铺作用域不符')
            entries.append((sku, old, receipt))
    rows = []
    all_asins = []
    for sku, old, receipt in entries:
        response = amazon.get_listing(target, sku['seller_sku'])
        current, error = identity.observed_asin(response['body'], target['marketplace_id'], sku['seller_sku'])
        if error or not current:
            raise HTTPException(409, error or '平台尚未返回当前商品编号')
        all_asins.append(current)
        if current == old['asin']:
            continue
        try:
            amazon.get_catalog_item(target, old['asin'])
        except amazon.PlatformError as exc:
            if exc.status != 404 or exc.unknown:
                raise
            old_evidence = {'http_status': 404, 'request_id': exc.request_id, 'errors': exc.errors}
        else:
            raise HTTPException(409, '旧目录仍存在，请先核对是否误绑定；不会自动替换')
        catalog = amazon.get_catalog_item(target, current)
        body = catalog['body']
        if body.get('asin') != current or not any(row.get('marketplaceId') == target['marketplace_id'] for row in body.get('summaries', [])):
            raise HTTPException(409, '目录响应与当前编号或站点不符')
        expected = receipt['request']['attributes']
        actual = body.get('attributes') or {}
        mismatches = [field for field in FIELDS if not expected.get(field) or not actual.get(field) or
                      any(row.get('marketplace_id') != target['marketplace_id'] for row in actual[field]) or
                      canonical(expected[field], field) != canonical(actual[field], field)]
        if mismatches:
            raise HTTPException(409, f"{sku['seller_sku']} 的新目录事实待核对：" + '、'.join(mismatches))
        summary = next(row for row in response['body']['summaries'] if row.get('marketplaceId') == target['marketplace_id'])
        submitted_image = (expected.get('main_product_image_locator') or [{}])[0].get('media_location', '')
        current_image = (summary.get('mainImage') or {}).get('link', '')
        if not submitted_image.startswith('https://') or not current_image.startswith('https://'):
            raise HTTPException(409, '缺少已提交或平台当前图片，不能完成图片核对')
        rows.append({'seller_sku': sku['seller_sku'], 'sku_id': sku['sku_id'],
                     'old_identity_id': old['id'], 'old_asin': old['asin'], 'asin': current,
                     'submission_id': receipt['id'], 'expected': {key: expected[key] for key in FIELDS},
                     'catalog_attributes': {key: actual[key] for key in FIELDS},
                     'submitted_image': submitted_image, 'current_image': current_image,
                     'listing_response': response, 'catalog_response': catalog, 'old_catalog': old_evidence})
    if len(set(all_asins)) != len(all_asins):
        raise HTTPException(409, '多个真实规格指向同一编号，请先核对变体身份')
    return {'listing_id': ident, 'version': listing['published_version'], 'target_scope': identity.target_scope(target),
            'archive_digest': draft['source']['archive_digest'], 'rows': rows}


def binding(review):
    return {key: review[key] for key in ('listing_id', 'version', 'target_scope', 'archive_digest')} | {
        'rows': [{key: row[key] for key in ('seller_sku', 'sku_id', 'old_identity_id', 'old_asin', 'asin',
                                           'submission_id', 'expected', 'catalog_attributes', 'submitted_image', 'current_image')}
                 for row in review['rows']]}


def prepare(ident, actor=''):
    review = inspect(ident)
    if not review['rows']:
        raise HTTPException(409, '当前未发现需要核对的编号变化')
    with s.transaction() as c:
        context(c, ident)
        saved = s.put(c, 'listing_identity_review', uuid.uuid4().hex,
                      review | {'status': 'pending', 'actor': actor, 'observed_at': time.time()})
    return saved


def confirm(ident, review_id, confirmed, reason, actor=''):
    if confirmed is not True or len(reason.strip()) < 8:
        raise HTTPException(422, '请核对规格与图片并填写确认依据')
    with s.main().db() as c:
        prior = s.get(c, 'listing_identity_review', review_id)
    if prior['listing_id'] != ident or prior['status'] != 'pending' or time.time() - prior['observed_at'] > 900:
        raise HTTPException(409, '核对已过期或已处理，请重新读取平台资料')
    fresh = inspect(ident)
    if binding(prior) != binding(fresh):
        raise HTTPException(409, '平台身份或事实在核对期间已变化，请重新核对')
    with s.transaction() as c:
        listing, target, draft = context(c, ident)
        current = s.get(c, 'listing_identity_review', review_id)
        if current['status'] != 'pending' or listing['published_version'] != fresh['version'] or draft['source']['archive_digest'] != fresh['archive_digest'] or identity.target_scope(target) != fresh['target_scope']:
            raise HTTPException(409, '本地资料在核对期间已变化')
        for row in fresh['rows']:
            key = resolution_key(row['old_identity_id'], row['asin'], fresh['archive_digest'])
            if not s.by_key(c, 'listing_identity_resolution', key):
                s.put(c, 'listing_identity_resolution', key, fresh | row | {
                    'rows': [], 'status': 'confirmed', 'review_id': review_id, 'actor': actor,
                    'reason': reason.strip(), 'confirmed_at': time.time()})
        current.update(status='confirmed', actor=actor, reason=reason.strip(), confirmed_at=time.time())
        s.save(c, 'listing_identity_review', current)
        s.audit(c, 'amazon_asin_reassignment_reviewed', ident, s.dump({'review_id': review_id, 'actor': actor, 'reason': reason.strip()}))
        return s.enqueue(c, 'refresh', f'identity-review:{review_id}', {'listing_id': ident})


def resolution_key(old_id, asin, digest):
    return f'{old_id}:{asin}:{digest}'


def resolved(c, target, listing, sku, old, asin):
    version = (listing.get('published_skus') or {}).get(sku['seller_sku']) or listing.get('published_version')
    material = s.by_key(c, 'material_version', f"{listing['id']}:{version}") or {}
    digest = (material.get('draft', {}).get('source') or {}).get('archive_digest')
    if not digest:
        return None
    resolution = s.by_key(c, 'listing_identity_resolution', resolution_key(old['id'], asin, digest))
    if not resolution or resolution.get('status') != 'confirmed' or resolution.get('target_scope') != identity.target_scope(target) or resolution.get('sku_id') != sku['sku_id'] or resolution.get('listing_id') != listing['id']:
        return None
    from . import product_archive
    product = s.get(c, 'product', listing['product_id'])
    selected = material['draft']['source'].get('selected_sku_ids') or [item['sku_id'] for item in material['draft']['skus']]
    return resolution if digest == product_archive.fact_digest(c, product, selected) else None
