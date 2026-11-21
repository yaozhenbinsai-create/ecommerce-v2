"""On-demand, source-backed import reconciliation for the authenticated UI."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from fastapi import HTTPException

from app.commerce import store as s
from app.supplier_batch_ops import batch, products
from app.supplier_fact_notes import notices
from app.supplier_variants import family_anchors, field_records, parsing_session, regions


def inspect(c, ident):
    imported = batch(c, ident)
    snapshot = json.loads(imported['workbook_snapshot'])
    mappings = json.loads(imported['field_mapping'])
    families = products(c, ident)
    found = [sku for product in families for sku in s.product_skus(c, product['id'])]
    by_source = {}
    for sku in found:
        by_source.setdefault(sku.get('source_variant_id'), []).append(sku)
    errors, rows, source_gaps, derived = [], [], Counter(), Counter()
    expected_ids, matched, expected_families = set(), 0, 0
    expected_variants = {}
    with parsing_session():
        for sheet in snapshot.get('sheets', []):
            name = sheet['name']
            anchors = family_anchors(snapshot, mappings, name, [])
            expected_families += len(anchors)
            from app.supplier_identity import rows_for_source
            source_bundle = dict(imported, workbook_snapshot=snapshot, field_mapping=mappings,
                                 extracted_images=json.loads(imported['extracted_images']))
            for anchor in anchors:
                variants = rows_for_source(c, {'import_id': ident, 'sheet': name, 'anchor_row': anchor}, imported=source_bundle)
                expected_variants.update({v['source_variant_id']: v for v in variants})
            for region in regions(snapshot, mappings, name):
                headers = {m['column_index']: m['source_column'] for m in region['mappings']}
                for label, column in region['headers'].items():
                    headers.setdefault(column, label.split(' / ')[-1])
                mapped = {m['column_index'] for m in region['mappings']}
                controls = {i for i,h in headers.items() if h in {'记录类型', '行类型', 'record_type'}}
                for row_number, values in region['data_rows'].items():
                    if not any(v not in (None, '') for v in values):
                        continue
                    identity = f'{ident}:{name}:{row_number}'
                    expected_ids.add(identity)
                    choices = by_source.get(identity, [])
                    if len(choices) != 1:
                        errors.append({'sheet': name, 'row': row_number, 'message': '实际规格缺失或重复', 'count': len(choices)})
                        continue
                    sku = choices[0]
                    product = next((p for p in families if p['id'] == sku['product_id']), {})
                    expected_anchor = max((a for a in anchors if a <= row_number), default=None)
                    if (product.get('supplier_source') or {}).get('anchor_row') != expected_anchor:
                        errors.append({'sheet': name, 'row': row_number, 'message': '规格所属商品分组不一致'})
                    raw = sku.get('source_fields') or {}
                    additional = []
                    from app.commerce.product_archive import field_value
                    expected = expected_variants.get(identity, {})
                    for field, fact in expected.get('structured_facts', {}).items():
                        if field_value(sku, field)[1] in {'manual', 'cleared', 'not_applicable'}:
                            continue
                        actual = (sku.get('structured_facts') or {}).get(field) or {}
                        if any(actual.get(k) != fact.get(k) for k in ('value', 'raw', 'status', 'qualifiers')):
                            errors.append({'sheet': name, 'row': row_number, 'column': fact.get('source', {}).get('column', field),
                                           'message': '结构化字段、备注参数或限定说明与来源不一致'})
                    for col, label in headers.items():
                        value = values[col] if col < len(values) else None
                        if value in (None, ''):
                            source_gaps[label] += 1
                        elif col not in mapped | controls:
                            if raw.get(label) != value:
                                errors.append({'sheet': name, 'row': row_number, 'column': label, 'message': '非空附加列未完整保留'})
                            additional.append({'label': label, 'value': value, 'sheet': name, 'row': row_number,
                                               'status': 'raw_only', 'message': '原样保留，用途未归类；未作为自动设计或平台参数'})
                    for fact in field_records(snapshot, mappings, name, row_number):
                        if fact['field'] in {'image', 'image_url'}:
                            continue
                        if str(raw.get(fact['column'], '')).strip() != str(fact['raw']).strip():
                            errors.append({'sheet': name, 'row': row_number, 'column': fact['column'], 'message': '原表字段未完整保留'})
                        else:
                            matched += 1
                    additions = []
                    for field, fact in (sku.get('structured_facts') or {}).items():
                        if (fact.get('source') or {}).get('kind') == 'supplier_note':
                            source = fact['source']
                            additions.append({'field': field, 'value': fact.get('value'), 'source': source})
                            derived[field] += 1
                    issues = notices(sku)
                    issues.extend({'field': q.get('field'), 'label': q.get('field'), 'kind': 'conflict',
                                   'message': q.get('message'), 'source': {'sheet': name, 'row': row_number}}
                                  for q in sku.get('source_parse_issues', []))
                    rows.append({'product_id': product.get('id'), 'product_name': product.get('name'),
                                 'sku_id': sku['id'], 'sku': sku.get('sku'), 'name': sku.get('name'),
                                 'sheet': name, 'row': row_number, 'notices': issues,
                                 'additional_fields': additional,
                                 'note_facts': additions, 'price': sku.get('price'), 'currency': sku.get('currency'),
                                 'image_count': len(sku.get('source_images') or [])})
    for sku in found:
        if sku.get('source_variant_id') not in expected_ids:
            errors.append({'sku': sku.get('sku'), 'message': '存在不属于本次商品区域的额外规格'})
    if len(families) != expected_families:
        errors.append({'message': '实际商品数与原表分组数不一致'})
    workbook_ok = False
    try:
        workbook_ok = hashlib.sha256(Path(imported['stored_path']).read_bytes()).hexdigest() == imported['file_hash']
    except OSError:
        pass
    if not workbook_ok:
        errors.append({'message': '原始工作簿缺失或文件校验不一致'})
    images = json.loads(imported['extracted_images'])
    image_urls = {image['url'] for image in images}
    from app.simple_design_flow import local_path
    for url in image_urls:
        try:
            if not local_path(url, source=True).is_file():
                raise OSError()
        except (OSError, ValueError, HTTPException):
            errors.append({'message': '原表图片文件缺失', 'url': url})
    for sku in found:
        # Explicitly uploaded/corrected later images have their own history; only
        # pristine imports must reconcile entirely to this workbook's images.
        if sku.get('design_reference'):
            continue
        expected_images = set(expected_variants.get(sku.get('source_variant_id'), {}).get('source_images', []))
        actual_images = set(sku.get('source_images') or [])
        if expected_images != actual_images & image_urls:
            errors.append({'sku': sku.get('sku'), 'message': '规格图片来源需核对'})
    return {'import_id': ident, 'filename': imported['filename'], 'status': 'passed' if not errors else 'failed',
            'products': len(families), 'skus': len(found), 'expected_products': expected_families,
            'expected_skus': len(expected_ids), 'matched_source_fields': matched,
            'images': len(images), 'workbook_byte_identical': workbook_ok, 'errors': errors,
            'note_fact_counts': dict(derived), 'note_facts': sum(derived.values()),
            'source_blank_counts': dict(source_gaps), 'rows_with_notices': sum(bool(r['notices']) for r in rows),
            'notice_counts': dict(Counter(v['label'] for r in rows for v in r['notices'])),
            'rows': rows, 'archived': imported['status'] == '已归档'}
