"""Required-only packaging projection; never rewrites source measurements."""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from jsonschema import Draft201909Validator
from referencing import Registry

from . import source_attributes as source

FIELDS = {'item_package_dimensions', 'item_package_weight'}


def required_fields(schema, attributes):
    if not isinstance(schema, dict):
        return set()
    try:
        errors = Draft201909Validator(schema, registry=Registry()).iter_errors(attributes)
        return {name for error in errors if error.validator == 'required' and not error.absolute_path
                for name in error.validator_value if name in FIELDS and name not in attributes}
    except Exception as exc:
        raise HTTPException(422, '平台包装必填条件无法核对，请重新读取商品规则') from exc


def _cm_attributes(attributes):
    for name in ('item_dimensions', 'item_length_width'):
        values = attributes.get(name) or []
        if len(values) != 1 or not isinstance(values[0], dict):
            continue
        result = {}
        try:
            for axis in ('length', 'width', 'height'):
                value = values[0].get(axis)
                if value is None:
                    continue
                amount = Decimal(str(value['value']))
                if not amount.is_finite() or amount <= 0 or value['unit'] not in source.LENGTH_UNITS:
                    return {}
                result[axis] = amount * source.LENGTH_UNITS[value['unit']]
        except (KeyError, TypeError, InvalidOperation):
            return {}
        if result:
            return result
    return {}


def _product_dimensions(record, attributes):
    result = _cm_attributes(attributes)
    if not {'length', 'width', 'height'} <= set(result):
        raw = source.fact(record, 'size', r'^尺寸$|^商品尺寸$|^size$', r'包装|高度|height')
        for axis, value in (source._dimensions(raw) or {}).items():
            result.setdefault(axis, value)
    if 'height' not in result:
        from ..supplier_fact_notes import measurement
        for field, pattern in [('item_height', r'^商品高度$|^item.height$'),
                               ('natural_height', r'^自然高度|^平放高度|^natural.height')]:
            raw = source.fact(record, field, pattern)
            parsed = measurement(raw)
            if parsed and parsed['signature'][0] == 'length':
                result['height'] = parsed['signature'][2]
                break
    return result if {'length', 'width', 'height'} <= set(result) else {}


def project(attributes, schema, record, marketplace_id, language_tag, *, provenance=None):
    result = copy.deepcopy(attributes)
    if not isinstance(schema, dict):
        return result
    # Empty controls carry no measurement. Keep conflicts/explicit clears in
    # the source record authoritative when deciding whether fallback is allowed.
    for name in FIELDS:
        if name in result and result[name] in (None, [], ''):
            result.pop(name)
    properties = schema.get('properties') or {}
    required = required_fields(schema, result)
    for name in FIELDS & set(properties):
        if name in result:
            continue  # Existing values are validated normally, never replaced.
        field = 'package_dimensions' if name == 'item_package_dimensions' else 'package_weight'
        raw = (source.fact(record, field, r'包装尺寸|包裹尺寸|package.dimensions') if name == 'item_package_dimensions'
               else source.weight_fact(record, field, r'包装重量|包裹重量|package.weight'))
        encoded = None
        basis = 'package_source'
        if raw:
            if name == 'item_package_dimensions':
                dimensions = source._dimensions(raw)
                if dimensions:
                    encoded = source._encode_dimensions(properties[name], dimensions, marketplace_id)
            else:
                encoded = source._encode(properties[name], raw, marketplace_id, language_tag, number=True)
        elif name in required and source.missing(record, field):
            basis = 'product_value_fallback'
            if name == 'item_package_dimensions':
                dimensions = _product_dimensions(record, result)
                if dimensions:
                    encoded = source._encode_dimensions(properties[name], dimensions, marketplace_id)
            else:
                values = result.get('item_weight') or []
                if len(values) == 1 and isinstance(values[0], dict):
                    value = values[0]
                    try:
                        amount = Decimal(str(value.get('value')))
                        valid = amount.is_finite() and amount > 0
                    except InvalidOperation:
                        valid = False
                    if valid and value.get('unit') in source.WEIGHT_TO_GRAMS:
                        encoded = source._encode(properties[name], str(value.get('value')) + ' ' + value['unit'],
                                                 marketplace_id, language_tag, number=True)
        if encoded is not None:
            result[name] = encoded
            if provenance is not None:
                provenance[name] = {'status': basis, 'record_id': record.get('id'),
                                    'required': name in required, 'measured_by_system': False,
                                    'rule': 'required-packaging-product-fallback-v1'}
    if result != attributes and required_fields(schema, result):
        return project(result, schema, record, marketplace_id, language_tag, provenance=provenance)
    return result
