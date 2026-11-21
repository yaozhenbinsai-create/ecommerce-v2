"""Owner-confirmed corrected artwork inputs, separate from supplier evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from . import store as s, media


def source_path(c, url):
    from app.simple_design_flow import local_path
    if not url.startswith('/api/commerce/media/file/'):
        return local_path(url, source=True)
    key = url.rsplit('/', 1)[-1]
    media.path(key)  # Validate the key before looking up its original bytes.
    asset = s.by_key(c, 'media', key.removesuffix('.jpg')) or {}
    digest = asset.get('original_sha256', '')
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise HTTPException(409, '参考图原始字节记录缺失')
    root = (s.main().DATA_ROOT / 'commerce_originals').resolve()
    path = root / digest
    if not path.is_file() or path.is_symlink():
        raise HTTPException(409, '参考图原始字节缺失')
    return path


def parents(c, sku):
    product = s.get(c, 'product', sku['product_id'])
    rows = []
    for row in c.execute("""SELECT t.*,r.project_id FROM design_simple_tasks t
            JOIN design_simple_runs r ON r.id=t.run_id
            WHERE r.project_id=? AND t.kind IN ('image','master') AND t.status='completed'
            ORDER BY t.id DESC""", (product.get('project_id'),)):
        payload, result = json.loads(row['payload']), json.loads(row['result'])
        if (payload.get('product') or {}).get('sku_id') != sku['id']:
            continue
        rows.append({'task_id': row['id'], 'run_id': row['run_id'], 'url': result.get('url'),
                     'sha256': result.get('sha256'), 'key': row['task_key'], 'kind': row['kind']})
    return rows


def bind(sku_id, revision, asset, parent_task_id, basis, actor):
    from app.simple_design_flow import local_path, usable_result
    if not isinstance(basis, str) or not 10 <= len(basis.strip()) <= 4000:
        raise HTTPException(422, '请记录实货状态和修图授权依据，不能把修图当实拍')
    with s.transaction() as c:
        sku = s.get(c, 'sku', sku_id)
        product = s.get(c, 'product', sku['product_id'])
        if sku['revision'] != revision:
            raise HTTPException(409, '商品档案已更新，请重新打开')
        if product.get('brand_status') != 'unbranded' or str(product.get('brand', '')).lower() != 'generic':
            raise HTTPException(422, '此入口仅用于已确认实货无品牌的商品；请先核对实际品牌状态')
        parent = next((row for row in parents(c, sku) if row['task_id'] == parent_task_id), None)
        if not parent:
            raise HTTPException(422, '修正依据必须是本商品、本规格已有的完整设计图片')
        row = c.execute('SELECT result FROM design_simple_tasks WHERE id=?', (parent_task_id,)).fetchone()
        result = json.loads(row['result'])
        if not usable_result(result):
            raise HTTPException(409, '原设计的生成依据不完整，不能用作修正来源')
        parent_path = local_path(parent['url'], pid=product['project_id'])
        if hashlib.sha256(parent_path.read_bytes()).hexdigest() != parent['sha256']:
            raise HTTPException(409, '原设计文件与记录不一致')
        source_paths = list(sku.get('source_images') or [])
        source_hashes = [hashlib.sha256(source_path(c, url).read_bytes()).hexdigest() for url in source_paths]
        previous_reference = sku.get('design_reference') or {}
        if (previous_reference.get('asset_id') == asset['id'] and
                previous_reference.get('parent_task_id') == parent_task_id and
                previous_reference.get('basis') == basis.strip()):
            return sku  # One reviewed input is registered once, never a new run.
        if source_hashes != (result.get('generation') or {}).get('source_sha256'):
            raise HTTPException(409, '原设计对应的参考输入已变化，请核对当前版本后修正')
        active = c.execute("""SELECT 1 FROM design_simple_tasks t JOIN design_simple_runs r ON r.id=t.run_id
            WHERE r.project_id=? AND t.status='running' LIMIT 1""", (product['project_id'],)).fetchone()
        uncertain = c.execute("""SELECT 1 FROM commerce_jobs WHERE status IN ('running','unknown')
            AND json_extract(payload,'$.listing_id') IN
            (SELECT id FROM commerce_records WHERE kind='listing' AND json_extract(data,'$.product_id')=?) LIMIT 1""",
            (product['id'],)).fetchone()
        if active or uncertain:
            raise HTTPException(409, '该商品有在途设计或平台请求，请等结果明确后更新参考图')
        # The input is a corrected artwork, never a manufacturer photo or receipt.
        uploaded = s.get(c, 'media', asset['id'])
        raw = source_path(c, uploaded['url'])
        if hashlib.sha256(raw.read_bytes()).hexdigest() != uploaded['original_sha256']:
            raise HTTPException(409, '修正参考图原始字节与上传记录不一致')
        before = copy.deepcopy(sku)
        declaration = {'kind': 'ai_corrected_design_reference', 'asset_id': asset['id'],
            'url': uploaded['url'], 'original_sha256': uploaded['original_sha256'],
            'parent_task_id': parent_task_id, 'parent_run_id': parent['run_id'],
            'parent_url': parent['url'], 'parent_sha256': parent['sha256'],
            'retained_source_images': source_paths, 'retained_source_sha256': source_hashes,
            'basis': basis.strip(), 'registered_by': str(actor),
            'registered_at': datetime.now(timezone.utc).isoformat(),
            'boundary': 'AI修正设计参考图；供应商原图另行保留；不是厂家实拍、实际包装照片或GTIN证明'}
        sku['design_reference'] = declaration
        # Prior supplier references stay inspectable; only active design input changes.
        sku['source_images'] = [uploaded['url']]
        sku['source_image_references'] = copy.deepcopy(sku.get('source_image_references') or []) + [declaration]
        # Provenance belongs to the reference descriptor, never customer copy
        # or a physical feature printed in a generated product illustration.
        sku['fact_revision'] = before.get('fact_revision', 0) + 1
        s.put(c, 'archive_revision', f"sku:{sku_id}:{before['revision']}",
              {'kind': 'sku', 'record_id': sku_id, 'snapshot': before, 'actor': str(actor)})
        saved = s.save(c, 'sku', sku, expected=revision)
        s.audit(c, 'corrected_design_reference_bound', sku_id, s.dump(declaration))
        return saved
