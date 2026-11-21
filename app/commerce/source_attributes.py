"""Conservative source-fact mapping against the current Amazon PTD schema."""
from __future__ import annotations

import re
from decimal import Decimal

WEIGHT_TO_GRAMS = {'grams': Decimal(1), 'kilograms': Decimal(1000),
                   'pounds': Decimal('453.59237'), 'ounces': Decimal('28.349523125')}
WEIGHT_ALIASES = {'kg': 'kilograms', '千克': 'kilograms', '公斤': 'kilograms',
                  'g': 'grams', '克': 'grams', 'lb': 'pounds', '磅': 'pounds',
                  'oz': 'ounces', '盎司': 'ounces'} | {name: name for name in WEIGHT_TO_GRAMS}


def _header_weight_unit(column):
    # Only an explicit unit suffix in a supplier column is evidence of units.
    match = re.search(r'[（(]\s*([^()（）]+)\s*[）)]\s*$', str(column))
    return WEIGHT_ALIASES.get(match[1].strip().lower()) if match else None


def weight_fact(record, name, pattern, exclude=r'(?!)'):
    from . import product_archive
    raw = fact(record, name, pattern, exclude)
    if not re.fullmatch(r'\d+(?:\.\d+)?', raw):
        return raw
    _, status = product_archive.field_value(record, name)
    if status == 'source':
        entries = [(record.get('structured_facts') or {}).get(alias)
                   for alias in product_archive.fact_aliases(name)]
        entries = [entry for entry in entries if isinstance(entry, dict)
                   and entry.get('status', 'source') == 'source'
                   and str(entry.get('value', '')).strip() == raw]
        units = [_header_weight_unit((entry.get('source') or {}).get('column', ''))
                 for entry in entries]
    elif status == 'missing':
        # Legacy source-only records can also contain an explicit unit header.
        units = [_header_weight_unit(key) for key, value in (record.get('source_fields') or {}).items()
                 if re.search(pattern, key, re.I) and not re.search(exclude, key, re.I)
                 and str(value).strip()]
    else:
        # A bare manual/legacy/inherited amount must not borrow an old source unit.
        return raw
    if units and None not in units and len(set(units)) == 1:
        return raw + ' ' + units[0]
    return raw


def _find(fields, include, exclude=r'(?!)'):
    matches = [str(value).strip() for key, value in fields.items()
               if re.search(include, key, re.I) and not re.search(exclude, key, re.I)
               and str(value).strip()]
    return matches[0] if len(set(matches)) == 1 else ''


def fact(record, name, pattern, exclude=r'(?!)'):
    """An explicit archive edit (including a clear/conflict) outranks supplier text."""
    from . import product_archive
    value, status = product_archive.field_value(record, name)
    if status in {'conflict', 'cleared', 'not_applicable'}:
        return ''
    if status != 'missing':
        return str(value if value is not None else '').strip()
    aliases = product_archive.fact_aliases(name)
    if any(field in (record.get('normalized_fields') or {}) for field in aliases):
        return ''
    return _find(record.get('source_fields') or {}, pattern, exclude)


def _encode(rule, raw, marketplace_id, language_tag, number=False):
    item = rule.get('items') if isinstance(rule, dict) else None
    properties = item.get('properties') if isinstance(item, dict) else None
    if not isinstance(properties, dict) or 'value' not in properties:
        return None
    value_rule = properties['value']
    value = raw
    unit = None
    if number:
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(kg|kilograms|千克|公斤|g|grams|克|lb|pounds|磅|oz|ounces|盎司)?\s*', raw, re.I)
        if not match:
            return None
        value = Decimal(match[1])
        if not match[2]:
            return None
        unit = WEIGHT_ALIASES[match[2].lower()]
        units = (properties.get('unit') or {}).get('enum') or []
        if units and unit not in units:
            selected = next((name for name in units if name in WEIGHT_TO_GRAMS), None)
            if not selected:
                return None
            value = value * WEIGHT_TO_GRAMS[unit] / WEIGHT_TO_GRAMS[selected]
            unit = selected
        value = float(value)
    choices = value_rule.get('enum') or []
    if choices and value not in choices:
        return None
    result = {'value': value}
    if 'unit' in properties:
        choices = properties['unit'].get('enum') or []
        if unit is None or (choices and unit not in choices):
            return None
        result['unit'] = unit
    if 'marketplace_id' in properties:
        result['marketplace_id'] = marketplace_id
    if 'language_tag' in properties:
        result['language_tag'] = language_tag
    required = set(item.get('required') or [])
    if not required <= set(result):
        return None
    return [result]


def missing(record, name):
    """Absence permits a fallback; a merchant clear/conflict does not."""
    from . import product_archive
    _, state = product_archive.field_value(record, name)
    if state == 'missing':return True
    entries = [(record.get('structured_facts') or {}).get(alias)
               for alias in product_archive.fact_aliases(name)]
    entries = [entry for entry in entries if isinstance(entry, dict)]
    # Imported empty cells can carry a missing/source marker. They are not an
    # operator's explicit instruction to keep the destination field empty.
    return bool(entries) and state == 'cleared' and all(
        entry.get('status', 'source') in {'missing', 'source', 'archive', 'inherited'}
        and str(entry.get('value') or '').strip().casefold() in product_archive.EMPTY
        for entry in entries)


def from_sku(sku, schema, marketplace_id, language_tag, *, provenance=None):
    from . import product_archive
    fields = sku.get('source_fields') or {}
    properties = schema.get('properties') if isinstance(schema, dict) else None
    if not isinstance(fields, dict) or not isinstance(properties, dict):
        return {}
    facts = {
        'fabric': fact(sku, 'fabric', r'面料|fabric|shell.material', r'填充|fill|包装'),
        'material': fact(sku, 'material', r'材料组成|^材料$|^材质$|^material$', r'填充|fill|包装'),
        'fill': fact(sku, 'fill_material', r'填充物|填充材质|fill.material', r'净重|重量|weight'),
        'item_weight': weight_fact(sku, 'item_weight', r'^(?:商品)?总重|商品重量|item.weight', r'包装|净重|填充'),
        'package_weight': weight_fact(sku, 'package_weight', r'包装重量|包裹重量|package.weight'),
    }
    # Prefer the actual field, then a related field in this same record. Keep
    # this projection separate from physical facts so later real values win.
    fallbacks = {}
    original = dict(facts)
    for destination, source in (('material', 'fabric'), ('fabric', 'material')):
        if not facts[destination] and original[source] and missing(sku, destination):
            # An explicitly labelled whole composition is not outer fabric.
            material_entry = (sku.get('structured_facts') or {}).get('material') or {}
            if destination == 'fabric' and (material_entry.get('source') or {}).get('column') == '材料组成':
                continue
            facts[destination] = original[source]
            fallbacks[destination] = source
    candidates = {'fabric_type': 'fabric', 'material': 'material',
                  'fill_material': 'fill', 'fill_material_type': 'fill',
                  'item_weight': 'item_weight', 'item_package_weight': 'package_weight'}
    attributes = {}
    for name, source in candidates.items():
        raw = facts[source]
        if not raw or name not in properties:
            continue
        encoded = _encode(properties[name], raw, marketplace_id, language_tag,
                          number=source in {'item_weight', 'package_weight'})
        if encoded is not None:
            attributes[name] = encoded
            if provenance is not None and source in fallbacks:
                provenance[name] = {'status': 'fallback', 'field': source,
                    'source_field': fallbacks[source], 'source_value': raw,
                    'record_id': sku.get('id'), 'rule': 'related-material-field-v1'}
    simple = {'manufacturer': r'^制造商$|^manufacturer$', 'model_name': r'^型号名称$|^model.name$',
              'model_number': r'^型号编号$|^制造商型号$|^model.number$',
              'item_shape': r'^形状$|^item.shape$', 'pattern': r'^图案$|^pattern$',
              'special_feature': r'^特点$|^功能特点$|^special.feature$'}
    for name, pattern in simple.items():
        raw = fact(sku, name, pattern)
        if raw and name in properties:
            encoded = _encode(properties[name], raw, marketplace_id, language_tag)
            if encoded:
                attributes[name] = encoded
    # A destination display field can use a related, sourced description.
    # This is a projection only: never turn it into a manufacturer identifier
    # or infer a performance claim from a material's name.
    descriptions = {
        'model_name': [('product_name', fact(sku, 'product_name', r'^品名$|^商品名称$|^product.name$'), '')],
        'special_feature': [('fill_material', facts['fill'], '填充材料：'),
                            ('fabric', facts['fabric'], '面料：'),
                            ('material', facts['material'], '材料组成：')],
    }
    for name, candidates in descriptions.items():
        if name in attributes or name not in properties or not missing(sku, name):
            continue
        for source, raw, prefix in candidates:
            if not raw:
                continue
            encoded = _encode(properties[name], prefix + raw, marketplace_id, language_tag)
            if encoded is None:
                continue
            from jsonschema import Draft201909Validator
            partial = {'$defs': schema.get('$defs', {}), 'type': 'object',
                       'properties': {name: properties[name]}}
            if not Draft201909Validator(partial).is_valid({name: encoded}):
                continue
            attributes[name] = encoded
            if provenance is not None:
                provenance[name] = {'status': 'fallback', 'source_field': source,
                    'source_value': raw, 'record_id': sku.get('id'),
                    'rule': 'source-description-projection-v1'}
            break
    # Cover is a nested material collection, not the same shape as fabric_type.
    if facts['fabric'] and 'cover' in properties:
        item = properties['cover'].get('items') or {}
        props = item.get('properties') or {}
        material = _encode(props.get('material', {}), facts['fabric'], marketplace_id, language_tag)
        if material:
            cover = {'material': material}
            if 'marketplace_id' in props:
                cover['marketplace_id'] = marketplace_id
            if set(item.get('required') or []) <= set(cover):
                attributes['cover'] = [cover]
                if provenance is not None and 'fabric' in fallbacks:
                    provenance['cover'] = {'status': 'fallback', 'field': 'fabric',
                        'source_field': fallbacks['fabric'], 'source_value': facts['fabric'],
                        'record_id': sku.get('id'), 'rule': 'related-material-field-v1'}
    raw_size = fact(sku, 'size', r'^尺寸$|^商品尺寸$|^size$', r'包装|高度|height')
    dimensions = _dimensions(raw_size)
    if dimensions:
        for name in ('item_length_width', 'item_dimensions'):
            encoded = _encode_dimensions(properties.get(name, {}), dimensions, marketplace_id)
            if encoded:
                attributes[name] = encoded
    raw_count = fact(sku, 'package_quantity', r'^销售件数$|^每包件数$|^包装数量$|^number.of.items$')
    count = re.fullmatch(r'(\d+)\s*(?:件|只|个|套|pcs?|pieces?)?', raw_count, re.I)
    if count and int(count[1]) > 0:
        for name in ('number_of_items', 'item_package_quantity'):
            rule = properties.get(name, {})
            item = rule.get('items') or {}; props = item.get('properties') or {}
            if props.get('value', {}).get('type') not in {'integer', 'number'}:
                continue
            value = {'value': int(count[1])}
            if 'marketplace_id' in props:
                value['marketplace_id'] = marketplace_id
            if set(item.get('required') or []) <= set(value):
                attributes[name] = [value]
    # The current schema defines pillow unit_count as individually consumed
    # pieces. Derive only from an explicit sale count and an accepted Count unit.
    if count and int(count[1])>0 and 'unit_count' in properties:
        rule=properties['unit_count'];item=rule.get('items') or {};props=item.get('properties') or {}
        unit_rule=props.get('type') or {};unit_props=unit_rule.get('properties') or {}
        if (props.get('value',{}).get('type') in {'integer','number'} and
                'Count' in (unit_props.get('value',{}).get('enum') or [])):
            unit={'value':'Count'}
            if 'language_tag' in unit_props:unit['language_tag']=language_tag
            value={'value':int(count[1]),'type':unit}
            if 'marketplace_id' in props:value['marketplace_id']=marketplace_id
            if (set(unit_rule.get('required') or [])<=set(unit) and
                    set(item.get('required') or [])<=set(value)):
                attributes['unit_count']=[value]
    # Amazon's current List Price field explicitly permits zero when no
    # reference price can be provided. Zero declares absence, never a discount,
    # source workbook selling price, supplier cost or calculated selling price.
    price_rule=properties.get('list_price',{});price_item=price_rule.get('items') or {}
    price_props=price_item.get('properties') or {};price_value=price_props.get('value') or {}
    price_state=product_archive.field_value(sku,'list_price')[1]
    currencies=(price_props.get('currency') or {}).get('enum') or []
    if (price_state=='missing' and len(currencies)==1 and price_value.get('type')=='number'
            and 'If you are unable to provide a value, enter 0.' in price_value.get('description','')
            and price_value.get('minimum',0)<=0):
        value={'value':0,'currency':currencies[0]}
        if 'marketplace_id' in price_props:value['marketplace_id']=marketplace_id
        if set(price_item.get('required') or [])<=set(value):attributes['list_price']=[value]
    return attributes


LENGTH_UNITS = {'centimeters': Decimal(1), 'millimeters': Decimal('.1'), 'meters': Decimal(100),
                'inches': Decimal('2.54')}


def _dimensions(raw):
    from .measurements import dimensions
    return dimensions(raw, annotations=True)


def _encode_dimensions(rule, dimensions, marketplace_id):
    item = rule.get('items') or {}; props = item.get('properties') or {}
    result = {}
    for name, cm in dimensions.items():
        sub = props.get(name, {}).get('properties') or {}
        if 'value' not in sub or 'unit' not in sub:
            return None
        units = sub['unit'].get('enum') or ['centimeters']
        selected = next((v for v in units if v in LENGTH_UNITS), None)
        if not selected:
            return None
        result[name] = {'value': float((cm / LENGTH_UNITS[selected]).quantize(Decimal('.000001'))), 'unit': selected}
    if 'marketplace_id' in props:
        result['marketplace_id'] = marketplace_id
    return [result] if result and set(item.get('required') or []) <= set(result) else None
