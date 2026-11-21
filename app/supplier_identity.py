"""Link supplier rows to stable local SKUs without treating supplier codes as Seller SKUs."""
from __future__ import annotations

import json
import re

from app.supplier_variants import derive_family_variants, region_for, field_records


SOURCE = re.compile(r'导入#(\d+).*?·\s*([^·]+)!R(\d+)\s*$')
ALIASES = {
    'supplier_sku': {'供应商SKU', '供应商sku', '供货编码', '供货货号', '供应商货号', '货号'},
    'gtin_upc': {'GTIN', 'UPC', 'EAN', '条码', '商品条码', 'GTIN/UPC/EAN'},
    'asin': {'ASIN', '亚马逊ASIN', 'Amazon ASIN'},
}


def rows_for_project(c, project):
    match = SOURCE.search(str(project.get('source_file') or ''))
    if not match:
        return []
    return rows_for_source(c, {'import_id': int(match[1]), 'sheet': match[2].strip(),
                              'anchor_row': int(match[3])}, project.get('product_name') or '')


def rows_for_source(c, source, fallback_name='', *, imported=None):
    import_id, sheet_name, anchor = int(source['import_id']), source['sheet'], int(source['anchor_row'])
    if imported is None:
        record = c.execute('SELECT workbook_snapshot,field_mapping,extracted_images FROM supplier_imports WHERE id=?',
                           (import_id,)).fetchone()
        if not record:
            return []
        imported = dict(id=import_id, workbook_snapshot=json.loads(record[0] or '{}'),
                        field_mapping=json.loads(record[1] or '[]'), extracted_images=json.loads(record[2] or '[]'))
    if imported['id'] != import_id:
        raise ValueError('Supplier source identity mismatch')
    snapshot = imported.get('workbook_snapshot') or {}
    mappings = imported.get('field_mapping') or []
    region = region_for(snapshot, mappings, sheet_name, anchor)
    if not region:
        return []
    rows = region['data_rows']
    if not rows:
        return []
    headers = region['headers']; resolved = region['mappings']
    images = imported.get('extracted_images') or []
    _, variants = derive_family_variants(snapshot, mappings, sheet_name, anchor,
                                        fallback_name)
    family_rows = {variant.get('source_row') for variant in variants}
    result = []
    for variant in variants:
        number = variant.get('source_row')
        if not number or number not in rows:
            continue
        values = rows[number]
        facts = field_records(snapshot, mappings, sheet_name, number)
        def field(name):
            mapped = [r['raw'] for r in facts if r['field'] == name]
            columns = [m.get('source_column') for m in resolved if m.get('standard_field') == name]
            columns += [h for h in headers if h in ALIASES.get(name, set())]
            found = list(mapped)
            for column in dict.fromkeys(columns):
                index = headers.get(column)
                if index is not None and index < len(values) and values[index] not in (None, ''):
                    found.append(values[index])
            if name == 'gtin_upc' and any(not isinstance(value, str) for value in found):
                return ''
            strings = [str(value).strip() for value in found]
            return strings[0] if len(set(strings)) == 1 else ''
        gtin_columns = [m.get('source_column') for m in resolved if m.get('standard_field') == 'gtin_upc']
        gtin_columns += [h for h in headers if h in ALIASES['gtin_upc']]
        numeric_gtin = any(headers.get(column) is not None and headers[column] < len(values)
                           and isinstance(values[headers[column]], (int, float))
                           for column in gtin_columns)
        structured = {}
        for name in {r['field'] for r in facts if r['field'] not in {'image', 'image_url'}}:
            found = [r for r in facts if r['field'] == name]
            unique = {r['value'] for r in found}
            first = found[0]
            structured[name] = {'value': first['value'] if len(unique) == 1 else '',
                                'raw': first['raw'] if len(unique) == 1 else [r['raw'] for r in found],
                                'status': ('inherited' if first.get('inherited') else 'source') if len(unique) == 1 else 'conflict',
                                'source': {'kind': 'supplier_cell', 'import_id': import_id, 'sheet': sheet_name,
                                           'row': first['row'], 'column': first['column'], 'column_index': first['column_index']}}
        source_images = list(dict.fromkeys(str(image.get('url')) for image in images
                                           if image.get('sheet') == sheet_name and image.get('row') == number and image.get('url')))
        references = []
        for fact in facts:
            if fact['field'] not in {'image', 'image_url'}:
                continue
            match = re.fullmatch(r'(?:款式|同款)(?:参考)?图见\s*([A-Z]{1,3})([1-9]\d*)', str(fact['raw']).strip(), re.I)
            if not match or int(match[2]) not in family_rows:
                continue
            column = 0
            for letter in match[1].upper():
                column = column * 26 + ord(letter) - ord('A') + 1
            for image in images:
                cell = re.fullmatch(r'R(\d+)C(\d+)', str(image.get('cell', '')))
                image_column = image.get('column') or (int(cell[2]) if cell else None)
                if (image.get('sheet') == sheet_name and image.get('row') == int(match[2])
                        and image_column == column and str(image.get('url', '')).startswith('/uploads/')):
                    source_images.append(image['url'])
                    references.append({'kind': 'supplier_family_image_reference', 'import_id': import_id,
                                       'sheet': sheet_name, 'row': number, 'column': fact['column'],
                                       'reference_cell': match[1].upper() + match[2], 'url': image['url']})
        source_images = list(dict.fromkeys(source_images))
        from app.supplier_fact_notes import enrich
        structured = enrich(structured)
        result.append({**variant, 'source_variant_id': f'{import_id}:{sheet_name}:{number}',
                       'import_id': import_id, 'sheet': sheet_name,
                       'supplier_sku': field('supplier_sku'), 'gtin': field('gtin_upc'),
                       'asin': field('asin').upper(),
                       'gtin_issue': ('原表条码是 Excel 数值，可能已丢失前导零，请提供文本原码'
                                      if numeric_gtin else ''),
                       'structured_facts': structured,
                       'normalized_fields': {k: v['value'] for k, v in structured.items() if v['status'] != 'conflict'},
                       'source_images': source_images,
                       'source_image_references': references,
                       'source_parse_issues': region['issues'] + [
                           {'field': k, 'message': '同一字段存在冲突的原表值', 'sheet': sheet_name, 'row': number}
                           for k, v in structured.items() if v['status'] == 'conflict'],
                       'raw_fields': {h: values[i] for h, i in headers.items()
                                      if i < len(values) and values[i] not in (None, '')}})
    if result:
        for row in result:
            # Only an explicit merged source cell establishes shared physical
            # facts. One populated sibling cell does not describe blank SKUs.
            row['inherited_fields'] = {
                fact['source']['column']: fact['raw'] for fact in row['structured_facts'].values()
                if fact['status'] == 'inherited'}
        # Country and place are one sourced unit. A variant with any origin
        # evidence keeps its own unit; never fill its missing city from another
        # country or combine unrelated cells into an apparent origin.
        from app.supplier_origin import split_origin
        origin_fields={'origin','origin_country','origin_place'}
        donors=[row for row in result if origin_fields & row['structured_facts'].keys()]
        origins=[]
        for donor in donors:
            facts=donor['structured_facts']
            if any(facts[name].get('status')=='conflict' for name in origin_fields & facts.keys()):
                origins.append(None)
                continue
            origin=split_origin(*(facts.get(name,{}).get('value','') for name in
                                  ('origin_country','origin_place','origin')))
            origins.append((origin.get('country',''),origin.get('place','')))
        if donors and None not in origins and len(set(origins))==1:
            donor=max(donors,key=lambda row:len(origin_fields & row['structured_facts'].keys()))
            for row in result:
                if origin_fields & row['structured_facts'].keys():continue
                for name in origin_fields & donor['structured_facts'].keys():
                    evidence=donor['structured_facts'][name]
                    row['structured_facts'][name]=dict(evidence,status='inherited')
                    row['normalized_fields'][name]=evidence['value']
                    column=evidence.get('source',{}).get('column')
                    if column and column in donor['raw_fields']:
                        row['inherited_fields'][column]=donor['raw_fields'][column]
    return result


def match_variant(rows, sku):
    if sku.get('supplier_sku'):
        found=[r for r in rows if r.get('supplier_sku')==sku['supplier_sku'] and
               (not sku.get('color') or r.get('color')==sku['color']) and
               (not sku.get('size') or not r.get('size') or r['size']==sku['size'])]
        if len(found)==1:return found[0]
    source_id = sku.get('source_variant_id')
    if source_id:
        found = [r for r in rows if r['source_variant_id'] == source_id]
        if found and sku.get('supplier_sku') and found[0].get('supplier_sku')!=sku['supplier_sku']:
            return None
    else:
        found = [r for r in rows if r['name'] == sku.get('name')]
        if len(found) != 1:
            found = [r for r in rows if r.get('color') and r['color'] == sku.get('color')]
    return found[0] if len(found) == 1 else None


def apply(c, product):
    from app.commerce import store as s
    if product.get('supplier_source'):
        rows = rows_for_source(c, product['supplier_source'], product.get('name', ''))
    else:
        project_row = c.execute('SELECT * FROM design_projects WHERE id=?',
                               (product.get('project_id'),)).fetchone()
        if not project_row:
            return product
        rows = rows_for_project(c, dict(project_row))
    if not rows:
        return product
    existing = [sku for sku in s.all_rows(c, 'sku') if sku['product_id'] == product['id']]
    matched = [match_variant(rows, sku) for sku in existing]
    if any(row is None for row in matched) or len({row['source_variant_id'] for row in matched if row}) != len(matched):
        from fastapi import HTTPException
        raise HTTPException(409, '已有本地 SKU 与导入变体无法唯一对应；已保留原资料，请核对商品身份')
    assigned = {row['source_variant_id'] for row in matched}
    used_keys = {sku['variant_key'] for sku in existing}
    for source in rows:
        if source['source_variant_id'] in assigned:
            continue
        number = next(n for n in range(1, 10001) if f'p{n}' not in used_keys)
        key = f'p{number}'
        used_keys.add(key)
        sku = s.put(c, 'sku', f"{product['id']}:{key}", {
            'product_id': product['id'], 'variant_key': key,
            'sku': f"W{product['id']:05d}-{key.upper()}",
            'name': source.get('name') or product['name'],
            'color': source.get('color') or '', 'size': '', 'facts': '',
            'components': product.get('components') or [],
            'cost': product.get('cost'), 'price': product.get('price'),
            'cost_currency': product.get('cost_currency') or '',
            'currency': product.get('currency') or '',
            'gtin': '', 'asin': '', 'supplier_sku': source.get('supplier_sku') or '',
            'source_variant_id': source['source_variant_id'], 'source_row': source['source_row'],
            'source_fields': {**source.get('inherited_fields', {}), **source.get('raw_fields', {})},
            'source_inherited_fields': list(source.get('inherited_fields', {})), 'active': True})
        existing.append(sku)
        assigned.add(source['source_variant_id'])
    for sku in existing:
        source = match_variant(rows, sku)
        if not source:
            continue
        changed = False
        for key in ('source_variant_id', 'supplier_sku', 'source_row'):
            value = source['source_variant_id'] if key == 'source_variant_id' else (
                source['supplier_sku'] if key == 'supplier_sku' else source['source_row'])
            if value and not sku.get(key):
                sku[key] = value
                changed = True
        # Never replace a merchant-edited identifier. Invalid source values remain evidence.
        if source['gtin'] and not sku.get('gtin'):
            from app.commerce.identifiers import valid_gtin
            sku['source_gtin'] = source['gtin']
            if valid_gtin(source['gtin']):
                sku['gtin'] = source['gtin']
            else:
                sku['gtin_issue'] = '原表条码不是有效 UPC-A/EAN-13 文本，请核对原码'
            changed = True
        if source.get('asin') and not sku.get('asin'):
            if re.fullmatch(r'[A-Z0-9]{10}',source['asin']):
                sku['asin']=source['asin']
            else:
                sku['asin_issue']='原表 ASIN 格式无效，请核对'
            changed=True
        elif source.get('gtin_issue') and not sku.get('gtin_issue'):
            sku['gtin_issue'] = source['gtin_issue']
            changed = True
        if (source.get('raw_fields') or source.get('inherited_fields')) and not sku.get('source_fields'):
            sku['source_fields'] = {**source.get('inherited_fields', {}), **source['raw_fields']}
            sku['source_inherited_fields'] = list(source.get('inherited_fields', {}))
            changed = True
        if changed:
            s.save(c, 'sku', sku)
    return product
