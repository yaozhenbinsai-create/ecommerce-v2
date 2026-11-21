"""Resolve product origin from supplier rows, then the batch default.

The original workbook remains the evidence. A batch default is a separate
user-supplied fact and must never be presented as a value from an Excel cell.
"""
from __future__ import annotations

import json
import re


SOURCE = re.compile(r'导入#(\d+).*?·\s*([^·]+)!R(\d+)\s*$')
EMPTY = {'', '-', '—', '暂无', '待补充', '未填写'}
COUNTRY_ALIASES = {'中国', '中华人民共和国', 'china', 'prc', 'cn'}


def clean(value):
    return str(value or '').strip()


def split_origin(country='', place='', combined=''):
    """Keep the city separate from the country sent to a marketplace."""
    country, place, combined = clean(country), clean(place), clean(combined)
    if combined:
        normalized = re.sub(r'^[\s·,，/-]+', '', combined)
        if normalized.startswith('中国') or normalized.lower().startswith('china'):
            prefix = '中国' if normalized.startswith('中国') else normalized[:5]
            country = country or '中国'
            place = place or re.sub(r'^[\s·,，/-]+', '', normalized[len(prefix):])
        elif not place:
            place = combined
    if country.casefold() in COUNTRY_ALIASES:
        country = '中国'
    if place.startswith('中国') and country == '中国':
        place = re.sub(r'^[\s·,，/-]+', '', place[2:])
    if len(country) > 120 or len(place) > 160:
        return {'status': 'invalid', 'message': '产地内容过长'}
    return {'status': 'ready' if country else 'missing_country',
            'country': country, 'country_code': 'CN' if country == '中国' else '',
            'place': place, 'display': '·'.join(v for v in (country, place) if v)}


def _cell(rows, number, index):
    values = rows.get(number, [])
    return clean(values[index]) if index is not None and index < len(values) else ''


def _mapped_columns(headers, mappings, field, aliases):
    selected = [headers[m['source_column']] for m in mappings
                if m.get('standard_field') == field and m.get('source_column') in headers]
    if selected:
        return selected
    return [index for name, index in headers.items() if name.casefold() in aliases]


def _source_value(rows, numbers, columns):
    found = [(number, index, _cell(rows, number, index)) for number in numbers for index in columns
             if _cell(rows, number, index) not in EMPTY]
    unique = {value for _, _, value in found}
    if len(unique) > 1:
        return None, {'status': 'conflict', 'message': '同款原表产地有不同值'}
    return (found[0] if found else None), None


def project_origin(project, c, variant_name=''):
    """Return a sourced origin for one design project or one named SKU."""
    match = SOURCE.search(clean(project.get('source_file')))
    if not match:
        return {}
    import_id, sheet_name, anchor = int(match[1]), match[2].strip(), int(match[3])
    row = c.execute('SELECT * FROM supplier_imports WHERE id=?', (import_id,)).fetchone()
    if not row:
        return {}
    batch = dict(row)
    snapshot = json.loads(batch.get('workbook_snapshot') or '{}')
    mappings = json.loads(batch.get('field_mapping') or '[]')
    sheet = next((item for item in snapshot.get('sheets', []) if item.get('name') == sheet_name), {})
    rows = {int(item.get('row', i + 1)): item.get('values', [])
            for i, item in enumerate(sheet.get('rows', []))}
    if not rows:
        return {}
    headers = {clean(value): index for index, value in enumerate(rows[min(rows)])
               if clean(value)}
    family_col = next((headers[name] for name in ('品名', '商品名称', '产品名称') if name in headers), None)
    variant_col = next((headers[name] for name in ('SKU名', 'SKU名称', '规格名称') if name in headers), None)
    start, end = anchor, anchor
    if family_col is not None:
        while start > min(rows) + 1 and not _cell(rows, start, family_col):
            start -= 1
        while end < max(rows) and not _cell(rows, end + 1, family_col):
            end += 1
    family_rows = list(range(start, end + 1))
    matching = [number for number in family_rows
                if variant_name and variant_col is not None and _cell(rows, number, variant_col) == variant_name]
    if variant_name and not matching:
        default = split_origin(batch.get('default_origin_country'),
                               batch.get('default_origin_place'))
        if default['status'] == 'ready':
            return default | {'import_id': import_id, 'sheet': sheet_name, 'row': anchor,
                              'source': {'kind': 'batch_default', 'import_id': import_id,
                                         'variant_name_unmatched': variant_name}}
        return {'status': 'unmatched', 'message': 'SKU名称未能定位到原表产地行',
                'import_id': import_id, 'sheet': sheet_name}
    selected_row = matching[0] if len(matching) == 1 else anchor
    columns = {
        'country': _mapped_columns(headers, mappings, 'origin_country',
                                   {'原产国', '产地国家', '原产地国家', 'country_of_origin', 'origin_country'}),
        'place': _mapped_columns(headers, mappings, 'origin_place',
                                 {'生产地', '生产城市', '产地城市', 'origin_place'}),
        'combined': _mapped_columns(headers, mappings, 'origin',
                                    {'产地', '原产地', '生产产地', 'place_of_origin'}),
    }
    # A variant's own origin fields replace the family's origin as one unit.
    # Otherwise a foreign country on a colour row could inherit the anchor's
    # Nantong place and create a false country/city combination.
    variant_has_origin = selected_row != start and any(
        _cell(rows, selected_row, index) not in EMPTY
        for indexes in columns.values() for index in indexes
    )
    evidence = {}
    for key, indexes in columns.items():
        direct, issue = _source_value(rows, [selected_row], indexes)
        if issue:
            return issue | {'import_id': import_id, 'sheet': sheet_name, 'row': selected_row}
        if direct is None and not variant_has_origin:
            # A value on another colour's row is not a family default.
            direct, issue = _source_value(rows, [start], indexes) if selected_row != start else (None, None)
            if issue:
                return issue | {'import_id': import_id, 'sheet': sheet_name, 'row': selected_row}
        if direct is not None:
            number, index, value = direct
            evidence[key] = {'value': value, 'row': number, 'column': rows[min(rows)][index]}
    combined = evidence.get('combined', {}).get('value', '')
    country = evidence.get('country', {}).get('value', '')
    place = evidence.get('place', {}).get('value', '')
    if combined:
        if not country and not (combined.startswith('中国') or combined.lower().startswith('china')):
            return {'status': 'missing_country', 'message': '原表产地没有明确国家，请分列填写原产国',
                    'raw': combined, 'import_id': import_id, 'sheet': sheet_name, 'row': selected_row}
        parsed = split_origin(country, place, combined)
        country, place = parsed.get('country', ''), parsed.get('place', '')
    batch_country = batch.get('default_origin_country', '')
    batch_place = batch.get('default_origin_place', '')
    if (country and split_origin(country)['country'] != split_origin(batch_country)['country']
            and not evidence.get('place') and not combined):
        batch_place = ''
    country = country or batch_country
    place = place or batch_place
    result = split_origin(country, place)
    result.update(import_id=import_id, sheet=sheet_name, row=selected_row,
                  source={'kind': 'excel' if evidence else 'batch_default',
                          'fields': evidence, 'import_id': import_id,
                          'batch_country': clean(batch.get('default_origin_country')),
                          'batch_place': clean(batch.get('default_origin_place'))})
    return result
