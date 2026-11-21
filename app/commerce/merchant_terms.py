"""Lean merchant price/shipping terms; verified configuration is not buyer acceptance."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

from fastapi import HTTPException

from . import listing_release, shipping, store as s

PRICE_MODES = {'unspecified', 'separate', 'included'}


def read_evidence(check):
    path = Path(check['evidence_file']).resolve()
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != check['evidence_sha256']:
        raise HTTPException(409, '配送设置依据文件已变化')
    evidence = json.loads(content)
    if evidence.get('name') != check['template_name']:
        raise HTTPException(409, '配送设置依据对应另一模板')
    cells = {row['id']: row['regions'].split(', ') for row in evidence.get('cells', [])}
    for row in check['regions']:
        if cells.get(row['source_id']) != row['destinations']:
            raise HTTPException(409, '配送范围与后台核对依据不一致')
        text = next((text for text in evidence.get('rows', []) if text.startswith(', '.join(row['destinations']) + '\t')), '')
        if (f"${row['per_item_fee']} 每 商品" not in text or '$0.00 每个订单' not in text
                or row['transit'] not in text or '不免费配送' not in text):
            raise HTTPException(409, '收费或时效与后台核对依据不一致')
    return evidence


def declare_separate_prices(ident, expected):
    """Version only the authorized item-price declaration, retaining all values."""
    with s.transaction() as c:
        listing = s.get(c, 'listing', ident)
        target = s.get(c, 'target', listing['target_id'])
        draft = copy.deepcopy(listing['draft'])
        if (shipping.scope(target) != expected['target_scope'] or not listing.get('retirement_id')
                or listing.get('published_version') or listing.get('status') != 'not_submitted'
                or not listing.get('paused')):
            raise HTTPException(409, '计价说明只能应用于清单内已删除并保留暂停的商品')
        before = copy.deepcopy(draft)
        before.pop('price_shipping_mode', None)
        if listing_release.fingerprint(before) != expected['draft_without_mode_sha256']:
            raise HTTPException(409, '商品资料已变化，已停止计价说明更改')
        actual = [{'sku_id': row['sku_id'], 'seller_sku': row['seller_sku'], 'price': row['price']}
                  for row in draft['skus']]
        if actual != expected['skus']:
            raise HTTPException(409, '商品规格或价格与操作清单不一致')
        if draft.get('price_shipping_mode') == 'separate':
            return listing  # Local recovery after a completed, journaled save.
        if listing['revision'] != expected['revision'] or listing['draft_version'] != expected['draft_version']:
            raise HTTPException(409, '计价说明的资料版本已变化')
        validate_submission(c, target, {**draft, 'price_shipping_mode': 'separate'})
        draft['price_shipping_mode'] = 'separate'
        listing.update(draft=draft, draft_version=listing['draft_version'] + 1)
        s.put(c, 'material_version', f"{ident}:{listing['draft_version']}",
              {'listing_id': ident, 'version': listing['draft_version'], 'draft': draft,
               'change_reason': 'merchant_item_price_excludes_checkout_shipping'})
        s.audit(c, 'listing_price_shipping_declared', ident, 'separate; numeric prices retained')
        return s.save(c, 'listing', listing)


def validate_mode(draft):
    mode=draft.get('price_shipping_mode','unspecified')
    if not isinstance(mode,str) or mode not in PRICE_MODES:
        raise HTTPException(422, '商品价格与运费的计价方式无效')


def template_check(c, target, template_id, product_type=None):
    row = s.by_key(c, 'shipping_terms', f"{target['id']}:{template_id}")
    return row if (row and row.get('scope') == shipping.scope(target)
                   and row.get('currency') == target.get('currency')
                   and (product_type is None or row.get('product_type') == product_type)) else None


def validate_submission(c, target, draft):
    validate_mode(draft)
    if target.get('environment') != 'production' or draft.get('price_shipping_mode') != 'separate':
        return
    if draft.get('currency') != target.get('currency'):
        raise HTTPException(422, '商品价币种与店铺运费币种不一致，请核对当前站点计价')
    template_id = draft.get('shipping_group')
    checked = template_check(c, target, template_id, draft.get('product_type'))
    if not template_id or not checked or checked.get('fee_collection') != 'checkout_paid':
        raise HTTPException(422, '商品价不含运费，请先选择并核对本店结账收费的配送模板')


def record_check(ident, payload):
    """Audited operator readback from the merchant's actual shipping settings."""
    with s.transaction() as c:
        target = s.get(c, 'target', ident)
        if payload.get('scope') != shipping.scope(target):
            raise HTTPException(409, '配送核对记录的卖家或站点不一致')
        if payload.get('currency') != target.get('currency'):
            raise HTTPException(422, '配送收费币种与当前站点不一致')
        template_id = payload.get('template_id')
        cache = s.by_key(c, 'shipping_templates', shipping.cache_key(ident, payload.get('product_type', ''))) or {}
        selected = next((r for r in cache.get('templates', []) if r.get('id') == template_id), None)
        if cache.get('scope') != shipping.scope(target, payload.get('product_type')) or not selected:
            raise HTTPException(422, '核对模板不在本店当前商品类型的模板列表中')
        if payload.get('fee_collection') != 'checkout_paid' or not payload.get('evidence_sha256') or len(payload['evidence_sha256']) != 64:
            raise HTTPException(422, '请保存实际收费配置的核对依据')
        regions = payload.get('regions')
        if not isinstance(regions, list) or not regions:
            raise HTTPException(422, '请保存已核对的配送地区与收费规则')
        for row in regions:
            if not isinstance(row.get('destinations'), list) or not row['destinations']:
                raise HTTPException(422, '配送地区不能为空')
            s.money(row.get('per_item_fee'), True)
        saved = s.put(c, 'shipping_terms', f'{ident}:{template_id}', {
            'target_id': ident, 'scope': shipping.scope(target), 'template_id': template_id,
            'template_name': selected['name'], 'product_type': payload['product_type'],
            'fee_collection': 'checkout_paid', 'currency': payload['currency'],
            'regions': copy.deepcopy(regions), 'handling_days': payload.get('handling_days'),
            'international_options': payload.get('international_options'),
            'evidence_sha256': payload['evidence_sha256'], 'checked_at': time.time(),
            'source': 'merchant_shipping_settings_readback', 'carrier_quote': False,
            'buyer_acceptance': 'not_verified_by_this_record'})
        s.audit(c, 'shipping_terms_recorded', ident, s.dump({'template_id': template_id, 'evidence_sha256': payload['evidence_sha256']}))
        return saved


def readiness(ident):
    with s.main().db() as c:
        listing = s.get(c, 'listing', ident)
        target = s.get(c, 'target', listing['target_id'])
        draft = listing.get('draft') or {}
        template_id = draft.get('shipping_group')
        check = template_check(c, target, template_id, draft.get('product_type'))
        items = [{'seller_sku': r['seller_sku'], 'item_price': r.get('price'),
                  'currency': draft.get('currency'), 'published': bool(listing.get('published_version'))}
                 for r in draft.get('skus', [])]
        return {'listing_id': ident, 'revision': listing['revision'], 'status': listing['status'],
                'publication_cycle_id': listing_release.cycle(listing),
                'retired': bool(listing.get('retirement_id')),
                'can_prepare_relisting': bool(listing.get('retirement_id')) and not listing.get('published_version'),
                'price_shipping_mode': draft.get('price_shipping_mode', 'unspecified'),
                'handling_days': draft.get('handling_days'), 'shipping_template_id': template_id,
                'shipping_check': check, 'items': items,
                'packaging_rule': '已有包装值优先；确实必填的缺项取产品对应值；非必填缺项不拦截。',
                'buyer_acceptance': ('当前商品未上架，目的地能否下单需在重新刊登后验证。'
                                     if not listing.get('published_version') and not listing.get('published_skus')
                                     else '买家能否下单需按目的地单独验证，本页展示本地参数。'),
                'message': '展示本地刊登状态、商品价和配送配置；正式提交前会重新核对身份与平台必填字段。'}
