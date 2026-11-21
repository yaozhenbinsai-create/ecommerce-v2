from __future__ import annotations

import re
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


def _clean(value: Any) -> str:
    return '' if value is None else str(value).strip()


_PARSE_CACHE = ContextVar('supplier_parse_cache', default=None)


@contextmanager
def parsing_session():
    """Reuse immutable source regions only during one import or backfill."""
    token = _PARSE_CACHE.set({})
    try:
        yield
    finally:
        _PARSE_CACHE.reset(token)


def regions(snapshot, mappings, sheet_name):
    cache = _PARSE_CACHE.get()
    if cache is None:
        return _build_regions(snapshot, mappings, sheet_name)
    key = (id(snapshot), id(mappings), sheet_name)
    if key not in cache:
        # Keep the source objects alive so identities cannot be reused mid-import.
        cache[key] = (snapshot, mappings, _build_regions(snapshot, mappings, sheet_name))
    return cache[key][2]


def _build_regions(snapshot, mappings, sheet_name):
    """Apply the saved parser contract, including multi-row headers and regions."""
    sheet = next((s for s in snapshot.get('sheets', []) if s.get('name') == sheet_name), {})
    rows = {int(r.get('row', i + 1)): list(r.get('values', []))
            for i, r in enumerate(sheet.get('rows', []))}
    if not rows:
        return []
    plans = [p for p in (snapshot.get('parse_plan') or {}).get('regions', [])
             if p.get('sheet_name') == sheet_name]
    if snapshot.get('parse_plan') and not plans:
        return []
    if not plans:
        scoped = [m for m in mappings if not m.get('sheet_name') or m['sheet_name'] == sheet_name]
        sources = {m.get('source_column') for m in scoped if m.get('source_column')}
        candidates = [n for n, values in rows.items() if sources & {_clean(v) for v in values}]
        header = min(candidates) if candidates else min(rows)
        plans = [{'header_rows': [header], 'start_row': header + 1, 'end_row': max(rows)}]
    result = []
    for number, plan in enumerate(plans):
        header_rows = [int(n) for n in plan.get('header_rows', [plan.get('header_row', min(rows))])]
        if not header_rows or any(n not in rows for n in header_rows):
            continue
        first = int(plan.get('start_row', max(header_rows) + 1))
        last = int(plan.get('end_row', max(rows)))
        width = max(len(rows[n]) for n in header_rows)
        headers = {}
        for col in range(width):
            labels = list(dict.fromkeys(_clean(rows[n][col]) for n in header_rows
                                       if col < len(rows[n]) and _clean(rows[n][col])))
            if labels:
                headers[' / '.join(labels)] = col
                # Only unambiguous aliases resolve to a column.
                for label in labels:
                    if sum(_clean(rows[n][i]) == label for n in header_rows
                           for i in range(len(rows[n]))) == 1:
                        headers.setdefault(label, col)
        selected = mappings or plan.get('mappings', [])
        selected = [m for m in selected if not m.get('sheet_name') or m['sheet_name'] == sheet_name]
        selected = [m for m in selected if not m.get('region_id') or m['region_id'] == plan.get('id', f'{sheet_name}:{number}')]
        resolved = []
        issues = []
        for mapping in selected:
            m = dict(mapping)
            index = m.get('column_index')
            if index is None and m.get('column_number') is not None:
                index = int(m['column_number']) - 1
            if index is None:
                index = headers.get(_clean(m.get('source_column')))
            if index is None or not isinstance(index, int) or not 0 <= index < width:
                issues.append({'field': m.get('standard_field', ''), 'message': '映射列无法定位', 'sheet': sheet_name})
                continue
            if float(m.get('confidence', 1)) < .65:
                issues.append({'field': m.get('standard_field', ''), 'message': '字段识别置信度不足', 'sheet': sheet_name})
                m['uncertain'] = True
            m['column_index'] = index
            m.setdefault('source_column', next((h for h, i in headers.items() if i == index), str(index + 1)))
            headers[m['source_column']] = index
            resolved.append(m)
        data_rows = {}
        for n in sorted(rows):
            if not first <= n <= last or n in header_rows:
                continue
            values = rows[n]
            # Repeated headers inside a long supplier table are not products.
            named = [(i, next((h for h, j in headers.items() if j == i), '')) for i in set(headers.values())]
            repeated = sum(i < len(values) and _clean(values[i]) in h.split(' / ') for i, h in named)
            if repeated >= max(1, len(named) * .7):
                continue
            data_rows[n] = values
        result.append({'id': plan.get('id', f'{sheet_name}:{number}'), 'headers': headers,
                       'rows': rows, 'data_rows': data_rows, 'header_row': max(header_rows),
                       'mappings': resolved, 'issues': issues, 'merged_cells': sheet.get('merged_cells', [])})
    return result


def region_for(snapshot, mappings, sheet_name, row_number):
    return next((r for r in regions(snapshot, mappings, sheet_name)
                 if row_number in r['data_rows']), None)


LITERAL_HEADERS = {
    'product_name': ('品名', '商品名称', '产品名称'), 'variant_name': ('规格名称', '款式名称'),
    'supplier_sku': ('供应商SKU', '供应商货号'), 'brand': ('品牌',), 'category': ('类别', '品类'),
    'color': ('颜色',), 'size': ('尺寸', '尺寸说明', '商品尺寸', '产品尺寸'),
    'material': ('材料组成', '材质', '材料'), 'fabric': ('面料成分', '面料'),
    'fill_material': ('填充物成分', '填充材料', '填充材质'), 'fill_weight': ('填充重量',),
    'weight': ('商品总重', '商品重量', '净重'), 'natural_height': ('自然高度',),
    'compressed_height': ('受压高度', '睡下高度'), 'item_length': ('商品长度',),
    'item_width': ('商品宽度',), 'item_height': ('商品高度',),
    'number_of_items': ('销售件数',), 'components': ('套装组成', '随售组成'),
    'package_dimensions': ('包装尺寸',), 'package_weight': ('包装重量', '毛重'),
    'cost_price': ('供货价', '采购价'), 'cost_currency': ('供货币种', '采购币种'),
    'price_unit': ('计价单位',), 'sale_price': ('建议售价',), 'sale_currency': ('售价币种',),
    'origin_country': ('原产国',), 'origin_place': ('详细产地',), 'manufacturer': ('制造商',),
    'model_number': ('型号', '制造商型号编号'), 'model_name': ('型号名称',),
    'gtin_upc': ('GTIN/UPC/EAN', 'GTIN', 'UPC', 'EAN'), 'asin': ('ASIN',),
    'description': ('备注', '商品备注', '产品备注', '补充说明', '备注补充说明', '备注/补充说明', 'notes', 'remarks'),
    'special_feature': ('产品特点与用途',),
}


def _literal_field(label):
    value = re.sub(r'[（(]\s*(?:cm|mm|m|in|g|kg|lb|oz|厘米|毫米|米|克|千克)\s*[）)]$', '', label.strip(), flags=re.I).strip().casefold()
    return next((field for field, aliases in LITERAL_HEADERS.items() if value in {field, *(a.casefold() for a in aliases)}), None)


def mapping_gaps(snapshot, mappings):
    """Unbound populated source columns must be resolved before archive writes."""
    gaps = []
    for sheet in snapshot.get('sheets', []):
        for region in regions(snapshot, mappings, sheet['name']):
            bound = {m['column_index'] for m in region['mappings']}
            for index, label in enumerate(region['rows'][region['header_row']]):
                if index in bound or _clean(label) in {'记录类型', '行类型', 'record_type'}:
                    continue
                populated = [n for n, row in region['data_rows'].items() if index < len(row) and row[index] not in (None, '')]
                if populated:
                    gaps.append({'sheet': sheet['name'], 'column': _clean(label) or f'第{index+1}列',
                                 'field': _literal_field(_clean(label)), 'rows': populated})
    return gaps


def preserve_note_mappings(snapshot, mappings):
    """Retain unambiguous literal columns even when the model omits mappings.

    This only fills an unbound, unambiguous literal header in an already located
    region. It never guesses product regions or replaces a merchant's mapping.
    """
    plan = snapshot.get('parse_plan') or {}
    if not plan.get('regions'):
        return snapshot, mappings
    additions = []
    for sheet in snapshot.get('sheets', []):
        for region in regions(snapshot, mappings, sheet['name']):
            bound = {m['column_index'] for m in region['mappings']}
            candidates = {}
            for column, label in enumerate(region['rows'][region['header_row']]):
                field = _literal_field(_clean(label))
                if field and column not in bound:
                    candidates.setdefault(field, []).append((column, _clean(label)))
            for field, values in candidates.items():
                if len(values) != 1:
                    continue
                column, label = values[0]
                if not any(column < len(row) and row[column] not in (None, '') for row in region['data_rows'].values()):
                    continue
                additions.append({'standard_field': field, 'source_column': label, 'column_index': column,
                                  'sheet_name': sheet['name'], 'region_id': region['id'], 'confidence': 1.0,
                                  'reason': '程序按明确表头补全；逐行保留原值、单位和限定说明',
                                  'mapping_source': 'literal_notes_header' if field == 'description' else 'literal_header'})
    if not additions:
        return snapshot, mappings
    plan = copy.deepcopy(plan)
    sheet_indexes = {}
    for region in plan['regions']:
        name = region['sheet_name']
        index = sheet_indexes.get(name, 0)
        sheet_indexes[name] = index + 1
        region_id = region.get('id', f'{name}:{index}')
        extra = [m for m in additions if m['region_id'] == region_id and m['sheet_name'] == name]
        if extra and 'mappings' in region:
            existing = {(m.get('column_index'), m.get('standard_field')) for m in region['mappings']}
            region['mappings'].extend(copy.deepcopy([m for m in extra if (m['column_index'], m['standard_field']) not in existing]))
    return dict(snapshot, parse_plan=plan), [*mappings, *additions]


def field_records(snapshot, mappings, sheet_name, row_number):
    region = region_for(snapshot, mappings, sheet_name, row_number)
    if not region:
        return []
    values = region['rows'][row_number]
    records = []
    for mapping in region['mappings']:
        if mapping.get('uncertain'):
            continue
        index = mapping['column_index']
        value = values[index] if index < len(values) else None
        source_row = row_number
        inherited = False
        if value in (None, ''):
            for merged in region['merged_cells']:
                if (merged['min_row'] <= row_number <= merged['max_row'] and
                        merged['min_col'] - 1 <= index < merged['max_col']):
                    source_row = merged['min_row']
                    source_values = region['rows'].get(source_row, [])
                    value = source_values[merged['min_col'] - 1] if merged['min_col'] <= len(source_values) else None
                    inherited = True
                    break
        if value in (None, ''):
            continue
        records.append({'field': mapping['standard_field'], 'column': mapping['source_column'],
                        'column_index': index, 'value': _clean(value), 'raw': value,
                        'row': source_row, 'sheet': sheet_name, 'inherited': inherited})
    return records


def _variant_color(sku_name: str, family_name: str) -> str:
    remainder = _clean(sku_name)
    family = _clean(family_name)
    if family and remainder.startswith(family):
        remainder = remainder[len(family):]
    remainder = remainder.strip(" -—_/")
    color_match = re.search(r"([^\s\-—_/]{1,12}色)$", remainder)
    return color_match.group(1) if color_match else ""


def normalize_variants(values: Any, product_name: str = "") -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values or [], start=1):
        item = dict(raw) if isinstance(raw, dict) else {"name": _clean(raw)}
        name = _clean(item.get("name") or item.get("sku") or item.get("label"))
        identity = f'row:{item["source_row"]}' if item.get('source_row') else name.casefold()
        if not name or identity in seen:
            continue
        seen.add(identity)
        color = _clean(item.get("color")) or _variant_color(name, product_name)
        variants.append({
            "key": _clean(item.get("key")) or f"variant-{index}",
            "name": name,
            "label": color or _clean(item.get("label")) or name,
            "color": color,
            "primary": bool(item.get("primary")) or not variants,
            **({"source_row": int(item["source_row"])} if item.get("source_row") else {}),
        })
    if not variants and _clean(product_name):
        variants.append({
            "key": "variant-1", "name": _clean(product_name),
            "label": _variant_color(product_name, "") or _clean(product_name),
            "color": _variant_color(product_name, ""), "primary": True,
        })
    if variants and not any(item["primary"] for item in variants):
        variants[0]["primary"] = True
    return variants


def derive_family_variants(
    snapshot: dict[str, Any],
    mappings: list[dict[str, Any]],
    sheet_name: str,
    anchor_row: int,
    fallback_name: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Recover a product family and adjacent SKU rows from an imported workbook snapshot."""
    region = region_for(snapshot, mappings, sheet_name, anchor_row)
    if not region:
        return fallback_name, normalize_variants([], fallback_name)
    rows = region['data_rows']
    if not rows:
        return fallback_name, normalize_variants([], fallback_name)
    headers = region['headers']
    resolved = region['mappings']
    product_sources = [
        _clean(item.get("source_column")) for item in resolved
        if item.get("standard_field") == "product_name" and _clean(item.get("source_column")) in headers
    ]
    if not product_sources:
        return fallback_name, normalize_variants([], fallback_name)
    family_source = product_sources[0]
    sku_source = next((m['source_column'] for m in resolved if m.get('standard_field') == 'variant_name'), product_sources[-1])
    family_column = headers[family_source]
    sku_column = headers[sku_source]

    def cell(row_number: int, column: int) -> str:
        values = rows.get(row_number, [])
        return _clean(values[column] if column < len(values) else "")

    family_name = ""
    family_row = anchor_row
    for row_number in sorted((n for n in rows if n <= anchor_row), reverse=True):
        family_name = cell(row_number, family_column)
        if family_name:
            family_row = row_number
            break
    family_name = family_name or fallback_name
    sku_names: list[dict[str, Any]] = []
    max_row = max(rows)
    # Images may be anchored to a later SKU in a merged product group. Include
    # the earlier variants from the same family instead of silently losing them.
    for row_number in sorted(n for n in rows if family_row <= n <= max_row):
        if row_number > family_row and cell(row_number, family_column) and cell(row_number, family_column) != family_name:
            break
        sku_name = cell(row_number, sku_column)
        facts = field_records(snapshot, mappings, sheet_name, row_number)
        explicit_variant = any(r['field'] in {'supplier_sku','color','size','item_length','item_width','package_quantity','number_of_items','gtin_upc'} for r in facts)
        if sku_name or cell(row_number, family_column) or explicit_variant:
            def one(field):
                found = {r['value'] for r in facts if r['field'] == field}
                return next(iter(found)) if len(found) == 1 else ''
            sku_names.append({"name": sku_name or family_name, "source_row": row_number,
                              "color": one('color'), "size": one('size')})
    normalized = normalize_variants(sku_names, family_name)
    for value, original in zip(normalized, sku_names):
        value['size'] = original['size']
    return family_name, normalized


def family_anchors(snapshot: dict[str, Any], mappings: list[dict[str, Any]],
                    sheet_name: str, image_rows: list[int]) -> list[int]:
    """Return one anchor per product family, including families without images."""
    anchors = []
    for region in regions(snapshot, mappings, sheet_name):
        anchors.extend(_region_anchors(region, image_rows))
    return anchors


def _region_anchors(region, image_rows):
    rows, headers = region['data_rows'], region['headers']
    mapped = [m.get('source_column') for m in region['mappings'] if m.get('standard_field') == 'product_name']
    family_col = next((headers[x] for x in mapped if x in headers), None)
    sku_col = next((m['column_index'] for m in region['mappings'] if m.get('standard_field') == 'variant_name'),
                   next((headers[x] for x in reversed(mapped) if x in headers), family_col))
    if family_col is None:
        return sorted({n for n in image_rows if n in rows})
    groups: list[tuple[int, list[int]]] = []
    current: list[int] = []
    start = 0
    last_family = ""
    for number in sorted(rows):
        values = rows[number]
        family = _clean(values[family_col] if family_col < len(values) else "")
        sku = _clean(values[sku_col] if sku_col is not None and sku_col < len(values) else "")
        explicit_variant=any(m.get('standard_field') in {'supplier_sku','color','size','item_length','item_width','package_quantity','number_of_items','gtin_upc'}
                             and m['column_index']<len(values) and _clean(values[m['column_index']])
                             for m in region['mappings'])
        if family in {'合计','总计','说明','备注'} and not explicit_variant:
            continue
        if family and current and family != last_family:
            groups.append((start, current))
            current = []
        if family:
            last_family = family
        if not current and (family or sku or explicit_variant):
            start = number
        if family or sku or explicit_variant:
            current.append(number)
    if current:
        groups.append((start, current))
    # The family anchor does not move when images are later added or relocated.
    return [start for start, members in groups]


