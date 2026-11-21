"""Import-scoped pricing and recoverable pre-design batch cleanup."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, Request
from app.commerce import costing, store as s


def batch(c, ident):
    row = c.execute('SELECT * FROM supplier_imports WHERE id=?', (ident,)).fetchone()
    if not row:
        raise HTTPException(404, '导入批次不存在')
    return dict(row)


def active(c, ident):
    value = batch(c, ident)
    if value['status'] == '已归档':
        raise HTTPException(409, '该批次已归档，请查看历史或重新上传原文件')
    return value


def products(c, ident):
    return [s.decode(r) for r in c.execute("SELECT * FROM commerce_records WHERE kind='product' "
            "AND json_extract(data,'$.supplier_source.import_id')=? ORDER BY id", (ident,))]


def pricing(c, ident):
    batch(c, ident)
    saved = s.by_key(c, 'supplier_pricing_rule', str(ident))
    return {'import_id': ident, 'revision': saved['revision'] if saved else 0,
            'rule': costing.sale_rule({'supplier_source': {'import_id': ident}}, c)}


def save_pricing(c, ident, payload, actor):
    active(c, ident)
    current = pricing(c, ident)
    if payload.get('revision') != current['revision']:
        raise HTTPException(409, '批次定价已更新，请刷新后重试')
    try:
        multiplier = Decimal(str(payload['multiplier']))
        freight = Decimal(str(payload['freight_reserve']))
        if not multiplier.is_finite() or not 0 < multiplier <= 100:
            raise ValueError()
        if not freight.is_finite() or not 0 <= freight <= 999999 or freight != freight.quantize(Decimal('.01')):
            raise ValueError()
        currency = str(payload.get('freight_currency', '')).strip().upper()
        if freight and currency not in costing.KNOWN_CURRENCIES:
            raise ValueError()
    except (KeyError, ValueError, InvalidOperation):
        raise HTTPException(422, '请输入有效的供货倍率、非负物流预留和对应币种') from None
    authorization = str(payload.get('authorization', '')).strip()
    if not authorization or len(authorization) > 1000:
        raise HTTPException(422, '请填写此次批次定价的依据，最长1000字')
    rule = {'policy': 'supplier_cost_multiplier_plus_reserve', 'multiplier': str(multiplier),
            'freight_reserve': s.money(freight), 'freight_currency': currency if freight else '',
            'authorization': authorization, 'actor': actor, 'scope': {'import_id': ident}}
    if current['rule'] == rule:
        return current
    s.put(c, 'supplier_pricing_revision', f"{ident}:{current['revision']}", current)
    s.put(c, 'supplier_pricing_rule', str(ident), {'import_id': ident, 'rule': rule})
    affected = products(c, ident)
    for product in affected:
        costing.apply(c, product)
    s.audit(c, 'supplier_pricing_saved', ident, s.dump({'actor': actor, 'rule': rule, 'products': len(affected)}))
    return pricing(c, ident) | {'products_repriced': len(affected)}


def archive_preview(c, ident):
    imported = batch(c, ident)
    history = s.by_key(c, 'supplier_batch_archive', str(ident))
    if imported['status'] == '已归档':
        return {'import_id': ident, 'archived': True, 'history': history,
                'products': len((history or {}).get('products', [])), 'skus': len((history or {}).get('skus', []))}
    found = products(c, ident)
    pids = {p['id'] for p in found}
    skus = [sku for p in found for sku in s.product_skus(c, p['id'])]
    sids = {sku['id'] for sku in skus}
    errors = []
    if imported['status'] in {'AI排队中', 'AI解析中', '等待重试'}:
        errors.append('AI解析尚未结束')
    if any(p.get('project_id') for p in found) or c.execute(
            'SELECT 1 FROM design_projects WHERE source_file LIKE ? LIMIT 1', (f'导入#{ident} ·%',)).fetchone():
        errors.append('已存在设计，不能使用导入阶段清理')
    permitted = {'product', 'sku', 'archive_revision', 'supplier_product_link', 'supplier_archive_evidence',
                 'supplier_sku_evidence', 'product_category_link', 'product_type_lookup', 'supplier_batch_archive'}
    def refers(value):
        if isinstance(value, dict):
            return any((key == 'product_id' and isinstance(v, int) and v in pids) or
                       (key == 'sku_id' and isinstance(v, int) and v in sids) or refers(v) for key, v in value.items())
        return isinstance(value, list) and any(refers(v) for v in value)
    for record in c.execute('SELECT kind,data FROM commerce_records'):
        if record['kind'] not in permitted and not record['kind'].startswith('archived_') and refers(json.loads(record['data'])):
            errors.append('已有关联业务记录：' + record['kind'])
    for job in c.execute('SELECT kind,status,payload FROM commerce_jobs'):
        if refers(json.loads(job['payload'])) and (job['kind'] != 'import_classification' or job['status'] not in {'completed', 'failed', 'cancelled'}):
            errors.append('仍有关联任务，请完成或停止后再清理')
    for sku in skus:
        for table in ('commerce_stock', 'commerce_stock_balances', 'commerce_stock_events'):
            if c.execute(f'SELECT 1 FROM {table} WHERE sku_id=? LIMIT 1', (sku['id'],)).fetchone():
                errors.append('已有关联库存记录，需保留在经营流程')
    identities = [(v['id'], v['revision']) for v in found + skus]
    token = hashlib.sha256(s.dump([ident, imported['status'], imported['updated_at'], identities]).encode()).hexdigest()
    return {'import_id': ident, 'archived': False, 'products': len(found), 'skus': len(skus),
            'product_ids': sorted(pids), 'sku_ids': sorted(sids), 'token': token, 'blockers': sorted(set(errors))}


def archive(c, ident, payload, actor):
    preview = archive_preview(c, ident)
    if preview['archived']:
        return preview
    if preview['blockers']:
        raise HTTPException(409, '；'.join(preview['blockers']))
    if payload.get('token') != preview['token']:
        raise HTTPException(409, '批次内容已改变，请重新查看清理范围')
    imported = batch(c, ident)
    history = {'import_id': ident, 'actor': actor, 'previous_status': imported['status'],
               'file_hash': imported['file_hash'], 'products': preview['product_ids'], 'skus': preview['sku_ids'],
               'reason': str(payload.get('reason') or '导入检查后归档重导')[:1000]}
    for kind, ids in [('product', history['products']), ('sku', history['skus'])]:
        c.executemany('UPDATE commerce_records SET kind=? WHERE id=? AND kind=?',
                      [('archived_' + kind, item_id, kind) for item_id in ids])
    s.put(c, 'supplier_batch_archive', str(ident), history)
    c.execute("UPDATE supplier_imports SET status='已归档',updated_at=CURRENT_TIMESTAMP WHERE id=?", (ident,))
    s.audit(c, 'supplier_batch_archived', ident, s.dump(history))
    return archive_preview(c, ident)


def restore(c, ident, actor):
    imported = batch(c, ident)
    history = s.by_key(c, 'supplier_batch_archive', str(ident))
    if imported['status'] != '已归档' or not history:
        raise HTTPException(409, '该批次未归档')
    for row in c.execute("SELECT id FROM supplier_imports WHERE file_hash=? AND id!=? AND status!='已归档'", (imported['file_hash'], ident)):
        if products(c, row['id']):
            raise HTTPException(409, '同一文件已有当前商品；请先归档当前批次，避免重复商品')
    for kind, ids in [('product', history['products']), ('sku', history['skus'])]:
        for item_id in ids:
            s.get(c, 'archived_' + kind, item_id)
            c.execute('UPDATE commerce_records SET kind=? WHERE id=?', (kind, item_id))
    c.execute('UPDATE supplier_imports SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (history['previous_status'], ident))
    s.audit(c, 'supplier_batch_restored', ident, actor)
    return {'import_id': ident, 'restored': True}


def install(app, main):
    @app.get('/api/supplier-imports/{ident}/inspection')
    def get_inspection(ident: int):
        from app.supplier_inspection import inspect
        with main.db() as c:
            active(c, ident)
            return inspect(c, ident)

    @app.get('/api/supplier-imports/{ident}/pricing')
    def get_pricing(ident: int):
        with main.db() as c:
            return pricing(c, ident)

    @app.patch('/api/supplier-imports/{ident}/pricing')
    def set_pricing(ident: int, payload: dict, request: Request):
        with s.transaction() as c:
            return save_pricing(c, ident, payload, getattr(request.state, 'commerce_username', ''))

    @app.get('/api/supplier-imports/{ident}/archive')
    def get_archive(ident: int):
        with main.db() as c:
            result = archive_preview(c, ident)
            if result['archived']:
                history = result['history']
                result['records'] = {'products': [s.get(c, 'archived_product', n) for n in history['products']],
                                     'skus': [s.get(c, 'archived_sku', n) for n in history['skus']]}
            return result

    @app.post('/api/supplier-imports/{ident}/archive')
    def post_archive(ident: int, payload: dict, request: Request):
        with s.transaction() as c:
            return archive(c, ident, payload, getattr(request.state, 'commerce_username', ''))

    @app.post('/api/supplier-imports/{ident}/restore')
    def post_restore(ident: int, request: Request):
        with s.transaction() as c:
            return restore(c, ident, getattr(request.state, 'commerce_username', ''))
