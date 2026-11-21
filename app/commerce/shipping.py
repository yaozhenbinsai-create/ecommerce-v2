"""Seller and product-type scoped Amazon shipping template discovery.

The product type definition determines whether a template is required. The
cached hint guides editing; the full schema validates actual listing data.
"""
from __future__ import annotations

import copy
import re
import time

from fastapi import HTTPException

from . import amazon, store as s
from .category_rules import walk


PRODUCT_TYPE = re.compile(r'^[A-Z][A-Z0-9_]{0,99}$')
CACHE_MAX_AGE = 24 * 60 * 60


def scope(target, product_type=None):
    result = {k: target.get(k) for k in ('account_id', 'seller_id', 'marketplace_id', 'environment', 'region')}
    if product_type is not None:
        result['product_type'] = product_type
    return result


def cache_key(ident, product_type):
    return f'{ident}:{product_type}'


def _product_types(c, ident):
    return sorted({draft['product_type'] for row in s.all_rows(c, 'listing')
                   if row.get('target_id') == ident
                   for draft in [row.get('draft') or {}]
                   if isinstance(draft.get('product_type'), str)
                   and PRODUCT_TYPE.fullmatch(draft['product_type'])})


def _type(c, ident, product_type, *, required=False):
    if product_type:
        if not isinstance(product_type, str) or not PRODUCT_TYPE.fullmatch(product_type):
            raise HTTPException(422, 'Amazon产品类型无效')
        return product_type
    types = _product_types(c, ident)
    if len(types) == 1:
        return types[0]
    if required:
        raise HTTPException(422, '请先选择商品实际的Amazon产品类型')
    return ''


def _dereference(node, root, seen=()):
    if not isinstance(node, dict):
        return node
    ref = node.get('$ref', '')
    if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
        return node
    value = root
    try:
        for part in ref[2:].split('/'):
            value = value[part.replace('~1', '/').replace('~0', '~')]
    except (KeyError, TypeError):
        return node
    return _dereference(value, root, seen + (ref,))


def _requires_shipping(node, root, seen=None):
    node = _dereference(node, root)
    if not isinstance(node, dict):
        return False
    seen = seen if seen is not None else set()
    if id(node) in seen:
        return False
    seen = seen | {id(node)}
    if 'merchant_shipping_group' in (node.get('required') or []):
        return True
    if any(_requires_shipping(node.get(key), root, seen) for key in ('then', 'else')):
        return True
    if any(_requires_shipping(child, root, seen) for child in (node.get('dependentSchemas') or {}).values()):
        return True
    return any(_requires_shipping(child, root, seen) for key in ('allOf', 'anyOf', 'oneOf')
               for child in node.get(key, []) if isinstance(child, dict))


def requirement(schema):
    """Classify the schema hint; conditional rules need full JSON validation.

    None means the decision depends on other listing attributes. It is not an
    exemption from the merchant shipping group requirement.
    """
    if not isinstance(schema, dict):
        raise HTTPException(422, '平台属性表不完整，无法判断配送模板要求')
    root = _dereference(schema, schema)
    if not isinstance(root, dict) or not isinstance(root.get('properties'), dict):
        raise HTTPException(422, '平台属性表不完整，无法判断配送模板要求')
    if 'merchant_shipping_group' in (root.get('required') or []):
        return {'status': 'required', 'required': True}
    for node in root.get('allOf', []):
        node = _dereference(node, schema)
        if isinstance(node, dict) and 'merchant_shipping_group' in (node.get('required') or []):
            return {'status': 'required', 'required': True}
    conditional = any(_requires_shipping(node, schema) for key in ('allOf', 'anyOf', 'oneOf')
                      for node in root.get(key, []) if isinstance(node, dict))
    conditional |= any(_requires_shipping(root.get(key), schema) for key in ('then', 'else'))
    if conditional:
        return {'status': 'conditional', 'required': None}
    present = any('merchant_shipping_group' in (node.get('properties') or {})
                  for node in walk(root, schema))
    return {'status': 'optional', 'required': False} if present else {'status': 'absent', 'required': False}


def required(target, product_type=None, *, connection=None):
    """Return True/False/None from a current seller/product-type cache."""
    if target.get('environment') == 'mock':
        from .order_platforms import ADAPTERS
        if connection is not None:
            account = connection.execute('SELECT platform FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
        else:
            with s.main().db() as c:
                account = c.execute('SELECT platform FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
        adapter = ADAPTERS.get(account['platform']) if account else None
        return adapter.shipping_template_required if adapter else False
    if target.get('environment') != 'production' or not product_type:
        return None
    if connection is not None:
        cached = s.by_key(connection, 'shipping_templates', cache_key(target['id'], product_type)) or {}
    else:
        with s.main().db() as c:
            cached = s.by_key(c, 'shipping_templates', cache_key(target['id'], product_type)) or {}
    if cached.get('scope') != scope(target, product_type) or cached.get('status') not in {'ready', 'empty'}:
        return None
    if time.time() - cached.get('checked_at', 0) > CACHE_MAX_AGE:
        return None
    return (cached.get('requirement') or {}).get('required')


def options(schema):
    root = _dereference(schema, schema) if isinstance(schema, dict) else None
    if not isinstance(root, dict) or not isinstance(root.get('properties'), dict):
        raise HTTPException(422, '平台属性表不完整，无法读取配送模板')
    found = {}
    for node in walk(root, schema):
        attribute = (node.get('properties') or {}).get('merchant_shipping_group')
        if not isinstance(attribute, dict):
            continue
        for entry in walk(attribute, schema):
            value = (entry.get('properties') or {}).get('value')
            if not isinstance(value, dict):
                continue
            for rule in walk(value, schema):
                names = rule.get('enumNames') or []
                for index, ident in enumerate(rule.get('enum') or []):
                    if not isinstance(ident, str) or not ident.strip():
                        continue
                    found.setdefault(ident, {'id': ident, 'name': str(names[index]) if index < len(names) else ident})
    return list(found.values())


def _store_default(target):
    selected = target.get('shipping_store_default') or {}
    return selected if selected.get('scope') == scope(target) else {}


def _default(target, product_type):
    defaults = target.get('shipping_defaults') or {}
    selected = defaults.get(product_type) or {}
    store_default = _store_default(target)
    if store_default:
        if selected.get('override_store_default'):
            return selected if selected.get('scope') == scope(target, product_type) else {}
        return store_default | {'scope': scope(target, product_type), 'selection_scope': 'store'}
    if selected.get('scope') == scope(target, product_type):
        return selected
    if product_type in defaults:
        return {}
    legacy = target.get('shipping_default') or {}
    if (product_type == 'PRODUCT' or target.get('environment') == 'mock') and legacy.get('scope') == scope(target):
        return legacy
    return {}


def default_value(target, product_type=None):
    if not product_type:
        return _default(target, 'PRODUCT').get('id', '') if target.get('environment') == 'mock' else ''
    return _default(target, product_type).get('id', '')


def read(ident, product_type=None):
    with s.main().db() as c:
        target = s.get(c, 'target', ident)
        types = _product_types(c, ident)
        product_type = _type(c, ident, product_type)
        cached = (s.by_key(c, 'shipping_templates', cache_key(ident, product_type)) or {}) if product_type else {}
    valid = bool(product_type) and cached.get('scope') == scope(target, product_type)
    report = cached if valid else {}
    status = report.get('status', 'pending' if product_type else 'needs_product_type')
    stale = status in {'ready', 'empty'} and time.time() - report.get('checked_at', 0) > CACHE_MAX_AGE
    if stale:
        status = 'stale'
    selected = _default(target, product_type) if product_type else {}
    return {'target_id': ident, 'environment': target['environment'], 'product_type': product_type,
            'product_types': types, 'requirement': (report.get('requirement') if not stale else None)
                                                 or {'status': 'unknown', 'required': None},
            'required': required(target, product_type) if product_type else None,
            'status': status, 'templates': report.get('templates', []),
            'default': selected, 'default_scope': selected.get('selection_scope', 'product_type'),
            'store_default': _store_default(target),
            'revision': target['revision'], 'cache_revision': report.get('revision'),
            'checked_at': report.get('checked_at'),
            'message': '模板列表已过期，请重新读取' if stale else report.get('message',
                       '尚未读取此商品类型的店铺配送模板' if product_type
                       else '请先选择商品实际的Amazon产品类型')}


def refresh(ident, product_type=None):
    with s.main().db() as c:
        target = s.get(c, 'target', ident)
        product_type = _type(c, ident, product_type, required=True)
    report = {'scope': scope(target, product_type), 'checked_at': time.time(),
              'product_type': product_type, 'templates': [], 'requirement': {'status': 'unknown', 'required': None}}
    if target['environment'] != 'production':
        report.update(status='sample_only' if target['environment'] == 'sandbox' else 'unsupported',
                      message='当前环境不提供真实店铺配送模板；正式目标授权后再读取')
    else:
        try:
            from .order_platforms import resolve
            adapter = resolve(target)
            if not hasattr(adapter, 'shipping_templates'):
                raise HTTPException(422, '该平台尚未提供配送模板读取接口')
            result = adapter.shipping_templates(target, product_type)
            templates = result['templates']
            report.update(status='ready' if templates else 'empty', templates=templates,
                          requirement=result['requirement'], request_id=result.get('request_id', ''),
                          message='已读取此商品类型的店铺配送模板' if templates else
                                  '平台未返回此商品类型可用的配送模板；请检查卖家后台的配送设置与授权')
        except (amazon.PlatformError, HTTPException) as exc:
            report.update(status='unavailable', message=str(exc) if isinstance(exc, amazon.PlatformError) else exc.detail)
    with s.transaction() as c:
        current = s.get(c, 'target', ident)
        if scope(current, product_type) != scope(target, product_type):
            raise HTTPException(409, '读取期间上架目标已变化，请重试')
        s.put(c, 'shipping_templates', cache_key(ident, product_type), report)
    return read(ident, product_type)


def validate_selected(target, product_type, template_id):
    """Fail closed for a manually entered template on a production target."""
    if not template_id or target.get('environment') != 'production':
        return
    with s.main().db() as c:
        cached = s.by_key(c, 'shipping_templates', cache_key(target['id'], product_type)) or {}
    if cached.get('scope') != scope(target, product_type) or cached.get('status') not in {'ready', 'empty'}:
        raise HTTPException(422, '请先读取当前店铺和商品类型的真实配送模板')
    if time.time() - cached.get('checked_at', 0) > CACHE_MAX_AGE:
        raise HTTPException(422, '配送模板列表已过期，请重新读取')
    if template_id not in {v['id'] for v in cached.get('templates', [])}:
        raise HTTPException(422, '配送模板不在当前店铺和商品类型返回的列表中')


def ensure_selected(target, product_type, template_id):
    """Read-only refresh on cache miss/staleness, then verify a chosen ID."""
    if not template_id or target.get('environment') != 'production':
        return
    try:
        validate_selected(target, product_type, template_id)
        return
    except HTTPException:
        report = refresh(target['id'], product_type)
        if report['status'] not in {'ready', 'empty'}:
            raise HTTPException(422, report['message'])
    validate_selected(target, product_type, template_id)


def choose(ident, payload):
    from . import catalog
    updated, skipped = [], []
    with s.transaction() as c:
        target = s.get(c, 'target', ident)
        product_type = _type(c, ident, payload.get('product_type'), required=True)
        if payload['revision'] != target['revision']:
            raise HTTPException(409, '上架目标已变化，请刷新')
        selected = {}
        if payload['template_id']:
            cached = s.by_key(c, 'shipping_templates', cache_key(ident, product_type)) or {}
            if (target['environment'] != 'production' or cached.get('status') != 'ready'
                    or cached.get('scope') != scope(target, product_type)):
                raise HTTPException(422, '请先读取当前店铺和商品类型的真实配送模板')
            if time.time() - cached.get('checked_at', 0) > CACHE_MAX_AGE:
                raise HTTPException(422, '配送模板列表已过期，请重新读取')
            if cached.get('revision') != payload.get('cache_revision'):
                raise HTTPException(409, '模板列表已变化，请刷新')
            selected = next((v for v in cached['templates'] if v['id'] == payload['template_id']), None)
            if selected is None:
                raise HTTPException(422, '配送模板不在当前店铺和商品类型返回的列表中')
            selected = selected | {'scope': scope(target, product_type)}
        defaults = copy.deepcopy(target.get('shipping_defaults') or {})
        apply_all = payload.get('apply_all_product_types', False)
        if apply_all:
            if not selected:
                raise HTTPException(422, '请先选择已读取的真实配送模板，再设为店铺全部商品默认')
            target['shipping_store_default'] = selected | {'scope': scope(target),
                                                          'confirmed_product_type': product_type}
            for entry in defaults.values():
                entry.pop('override_store_default', None)
        elif _store_default(target):
            selected = selected | {'override_store_default': True,
                                   'scope': scope(target, product_type)}
        defaults[product_type] = selected
        target['shipping_defaults'] = defaults
        s.save(c, 'target', target)
        if selected and payload['fill_missing_drafts']:
            for listing in s.all_rows(c, 'listing'):
                draft = listing.get('draft') or {}
                if (listing['target_id'] != ident or draft.get('product_type') != product_type
                        or draft.get('shipping_group')):
                    continue
                active = c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? "
                                   "AND status IN ('queued','running','retry_wait','unknown')", (listing['id'],)).fetchone()
                if active or listing['status'] != 'not_submitted' or listing.get('published_version') is not None:
                    skipped.append(listing['id'])
                    continue
                draft = copy.deepcopy(draft)
                draft['shipping_group'] = selected['id']
                listing['draft'] = draft
                listing['draft_version'] += 1
                listing['issues'] = catalog.issues(draft)
                listing['draft_status'] = 'needs_fields' if listing['issues'] else 'ready'
                s.put(c, 'material_version', f"{listing['id']}:{listing['draft_version']}",
                      {'listing_id': listing['id'], 'version': listing['draft_version'], 'draft': draft})
                s.save(c, 'listing', listing)
                updated.append(listing['id'])
        s.audit(c, 'shipping_default_saved', ident, s.dump({'product_type': product_type,
                                                              'apply_all_product_types': apply_all,
                                                              'template_id': payload['template_id'],
                                                              'drafts_updated': updated}))
    return read(ident, product_type) | {'drafts_updated': updated, 'drafts_skipped': skipped}
