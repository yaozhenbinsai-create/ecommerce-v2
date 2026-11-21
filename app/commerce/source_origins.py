"""Carry a supplier batch or row origin into product and SKU records."""
from __future__ import annotations

import copy

from app import supplier_origin
from . import store as s


def apply(c, product):
    row = c.execute('SELECT * FROM design_projects WHERE id=?',
                    (product.get('project_id'),)).fetchone()
    if not row:
        return product
    project = dict(row)
    found = supplier_origin.project_origin(project, c)
    if found.get('status') == 'ready' and (product.get('origin') or {}).get('source', {}).get('kind') != 'manual':
        if product.get('origin') != found:
            product = s.save(c, 'product', product | {'origin': found})
    for sku in s.all_rows(c, 'sku'):
        if sku['product_id'] != product['id']:
            continue
        if (sku.get('origin') or {}).get('source', {}).get('kind') == 'manual':
            continue
        variant_origin = supplier_origin.project_origin(project, c, sku['name'])
        if variant_origin.get('status') == 'ready' and sku.get('origin') != variant_origin:
            s.save(c, 'sku', sku | {'origin': copy.deepcopy(variant_origin)})
    return product


def backfill(import_id):
    changed_products, changed_skus, skipped = [], [], []
    with s.transaction() as c:
        for product in s.all_rows(c, 'product'):
            row = c.execute('SELECT source_file FROM design_projects WHERE id=?',
                            (product.get('project_id'),)).fetchone()
            if not row or not str(row[0]).startswith(f'导入#{import_id} ·'):
                continue
            listings = [item for item in s.all_rows(c, 'listing') if item['product_id'] == product['id']]
            if any(c.execute("""SELECT 1 FROM commerce_jobs WHERE
                       json_extract(payload,'$.listing_id')=? AND
                       status IN ('queued','running','retry_wait','unknown')""",
                       (item['id'],)).fetchone() for item in listings):
                skipped.append(product['id'])
                continue
            before_skus = {item['id']: item.get('origin') for item in s.all_rows(c, 'sku')
                           if item['product_id'] == product['id']}
            updated = apply(c, product)
            if updated.get('origin') != product.get('origin'):
                changed_products.append(product['id'])
            for item in s.all_rows(c, 'sku'):
                if item['id'] in before_skus and item.get('origin') != before_skus[item['id']]:
                    changed_skus.append(item['id'])
        s.audit(c, 'supplier_origin_backfilled', import_id,
                s.dump({'products': changed_products, 'skus': changed_skus, 'skipped': skipped}))
    return {'products_updated': changed_products, 'skus_updated': changed_skus,
            'skipped_active_products': skipped}
