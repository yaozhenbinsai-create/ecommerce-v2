"""Source-backed destination attributes; never rewrite physical archive facts."""
from __future__ import annotations

import copy
import hashlib
import re

PROTOCOL = 'source-destination-attributes-v2'


def identity(language):
    return {'protocol': PROTOCOL, 'language': language, 'enabled': True}


def normalized(value):
    return re.sub(r'\s+', ' ', str(value)).strip()


IDENTITY_FIELDS = {'brand', 'manufacturer', 'model_number', 'part_number',
                   'item_type_keyword', 'external_product_id', 'merchant_suggested_asin'}


def _node(schema, path):
    root = schema or {}
    node = root
    for part in path:
        for _ in range(8):
            reference = node.get('$ref', '') if isinstance(node, dict) else ''
            if not reference.startswith('#/'):
                break
            node = root
            for key in reference[2:].split('/'):
                node = node.get(key.replace('~1', '/').replace('~0', '~'), {})
        node = (node.get('items', {}) if isinstance(part, int) else
                node.get('properties', {}).get(part, {})) if isinstance(node, dict) else {}
    return node


def translation_request(attributes, language, schema=None):
    """Collect source text by field, without guessing chemistry or identifiers."""
    root_language = language.lower().replace('-', '_').split('_')[0]
    output = []

    def walk(value, path):
        if isinstance(value, dict):
            raw = value.get('value')
            if path and path[0] not in IDENTITY_FIELDS and isinstance(raw, str) and raw.strip():
                chinese = bool(re.search(r'[\u3400-\u9fff]', raw))
                needed = chinese if root_language == 'en' else (
                    not chinese and bool(re.search(r'[A-Za-z]{3,}', raw)) if root_language == 'zh'
                    else chinese or bool(re.search(r'[A-Za-z]{3,}', raw)))
                if needed:
                    node = _node(schema, path + ['value'])
                    allowed = node.get('enum') or []
                    allowed = [item for item in allowed if isinstance(item, str)]
                    item = {'field': str(path[0]), 'source_value': raw,
                            'language': language, 'allowed_values': allowed}
                    item['key'] = hashlib.sha256(repr(sorted(item.items())).encode()).hexdigest()[:20]
                    output.append((item, path + ['value']))
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    walk(child, path + [key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [index])

    walk(attributes, [])
    return output


def parse_translations(raw, requested):
    """Read the requested fields from the same generation response; no AI review."""
    from app.model_retry import ModelFailure
    if not requested:
        return []
    if not isinstance(raw, list):
        raise ModelFailure('模型未返回所需的属性翻译字段', 'invalid')
    values = {item.get('key'): item.get('value') for item in raw if isinstance(item, dict)}
    result = []
    for item in requested:
        value = values.get(item['key'])
        if not isinstance(value, str) or not value.strip():
            raise ModelFailure('模型返回的属性翻译字段不完整', 'invalid')
        result.append({**item, 'value': value.strip()})
    return result


def project(attributes, language, components=None, *, translations=(), schema=None, prior_proof=None):
    output = copy.deepcopy(attributes)
    proof = {'identity': identity(language), 'changes': [], 'unmapped': []}
    if not proof['identity']['enabled']:
        return output, proof
    candidates = {item.get('key'): item for item in translations if isinstance(item, dict)}
    pending = []
    for requested, path in translation_request(output, language, schema):
        item = candidates.get(requested['key']) or {}
        if not item and isinstance(prior_proof,dict) and (prior_proof.get('identity') or {}).get('language')==language:
            stored=[change for change in prior_proof.get('changes',[]) if isinstance(change,dict)
                    and change.get('field')=='/'.join(map(str,path))
                    and change.get('source_value')==requested['source_value']
                    and isinstance(change.get('destination_value'),str) and change['destination_value'].strip()]
            if len({change['destination_value'] for change in stored})==1:
                item={**requested,'value':stored[0]['destination_value']}

        if any(item.get(key) != requested[key] for key in ('source_value', 'language', 'field')) or not item.get('value'):
            pending.append({'field': '/'.join(map(str, path)), 'source_value': requested['source_value']})
            continue
        current = output
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = item['value']
        if 'language_tag' in current:
            current['language_tag'] = language
        proof['changes'].append({'field': '/'.join(map(str, path)),
                                 'source_value': requested['source_value'],
                                 'destination_value': item['value'],
                                 'basis': 'same-call source field translation', 'key': requested['key']})
    # Unresolved fields remain explicit. Never label a missing translation as
    # translated, borrow another SKU's material, or modify original archive facts.
    proof['unmapped'] = pending
    return output, proof
