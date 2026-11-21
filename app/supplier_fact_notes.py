"""Read explicit labelled measurements without turning prose into invented facts."""
from __future__ import annotations

import copy
import re
from decimal import Decimal

LABELS = {
    'natural_height': ('自然高度', '平放高度', 'natural height'),
    'compressed_height': ('受压高度', '睡下高度', '压缩高度', 'compressed height'),
    'fill_weight': ('填充重量', '填充净重', 'fill weight'),
    'weight': ('商品总重', '商品重量', '净重', '总重', 'item weight'),
    'package_weight': ('包装重量', '毛重', 'package weight'),
    'item_length': ('商品长度', 'item length'),
    'item_width': ('商品宽度', 'item width'),
    'item_height': ('商品高度', 'item height'),
}
TITLES = {'natural_height': '自然高度', 'compressed_height': '受压高度', 'fill_weight': '填充重量',
          'weight': '商品重量', 'package_weight': '包装重量', 'item_length': '商品长度',
          'item_width': '商品宽度', 'item_height': '商品高度', 'package_dimensions': '包装尺寸',
          'components': '随售组成', 'source_images': '图片与实际规格', 'description': '原表备注'}
UNITS = {'cm': ('length', Decimal('1')), '厘米': ('length', Decimal('1')),
         'mm': ('length', Decimal('.1')), '毫米': ('length', Decimal('.1')),
         'm': ('length', Decimal('100')), '米': ('length', Decimal('100')),
         'in': ('length', Decimal('2.54')), 'inch': ('length', Decimal('2.54')), '英寸': ('length', Decimal('2.54')),
         'g': ('weight', Decimal('1')), '克': ('weight', Decimal('1')),
         'kg': ('weight', Decimal('1000')), '千克': ('weight', Decimal('1000')), '公斤': ('weight', Decimal('1000')),
         'lb': ('weight', Decimal('453.59237')), 'oz': ('weight', Decimal('28.349523125'))}
UNIT = '|'.join(sorted(map(re.escape, UNITS), key=len, reverse=True))
MEASURE = re.compile(r'^(?P<approx>约|大约|约为|approximately\s*|approx\.?\s*|~|≈)?\s*'
                     r'(?P<low>\d+(?:\.\d+)?)\s*(?P<u1>' + UNIT + r')?\s*'
                     r'(?:(?:[-–—~～至]|to)\s*(?P<high>\d+(?:\.\d+)?)\s*)?'
                     r'(?P<u2>' + UNIT + r')?$', re.I)
EXPLICIT = {'manual', 'cleared', 'not_applicable'}


def measurement(value, column=''):
    text = str(value).strip().rstrip('。.;；')
    match = MEASURE.fullmatch(text)
    if not match:
        return None
    unit = (match['u2'] or match['u1'] or '').lower()
    if not unit:
        header = re.search(r'[（(]\s*(' + UNIT + r')\s*[）)]', column, re.I)
        unit = header[1].lower() if header else ''
    if unit not in UNITS or (match['u1'] and match['u2'] and match['u1'].lower() != match['u2'].lower()):
        return None
    kind, factor = UNITS[unit]
    low, high = Decimal(match['low']) * factor, Decimal(match['high'] or match['low']) * factor
    if low <= 0 or high < low:
        return None
    return {'value': text, 'unit': unit, 'approximate': bool(match['approx']), 'signature': (kind, low, high)}


def enrich(structured):
    """Keep source cell raw values; fill only blank measurements from explicit notes."""
    facts = copy.deepcopy(structured)
    note = facts.get('description') or {}
    text = str(note.get('value') or '')
    for field, labels in LABELS.items():
        existing = facts.get(field) or {}
        if existing.get('status') in EXPLICIT | {'conflict'}:
            continue
        found = []
        pattern = r'^\s*(?:' + '|'.join(map(re.escape, labels)) + r')(?:栏)?(?:原文)?\s*[:：]\s*([^\n]+)'
        for match in re.finditer(pattern, text, re.M | re.I):
            parsed = measurement(match[1])
            if parsed:
                found.append((parsed, match[0].strip()))
        if not found:
            continue
        signatures = {entry[0]['signature'] for entry in found}
        current = measurement(existing.get('value', ''), str((existing.get('source') or {}).get('column') or ''))
        source = dict(note.get('source') or {}, kind='supplier_note', excerpt=found[0][1])
        if len(signatures) != 1 or (existing.get('value') not in (None, '') and
                                   (not current or current['signature'] not in signatures)):
            facts[field] = dict(existing, value='', status='conflict', raw=existing.get('raw', ''),
                                source=existing.get('source') or source,
                                candidates=[copy.deepcopy(existing)] + [dict(value=p['value'], source=dict(source, excerpt=q)) for p,q in found])
            continue
        parsed, quote = found[0]
        if not existing.get('value'):
            facts[field] = existing = {'value': parsed['value'], 'raw': parsed['value'], 'status': 'source',
                                       'source': dict(source, excerpt=quote), 'unit': parsed['unit']}
        qualifiers = list(existing.get('qualifiers') or [])
        if any(p['approximate'] for p, _ in found) and 'approximate' not in qualifiers:
            qualifiers.append('approximate')
        if qualifiers:
            existing['qualifiers'] = qualifiers
            existing['qualification_evidence'] = [{'text': q, 'source': dict(source, excerpt=q)} for _,q in found]
    for field, evidence in facts.items():
        if field == 'description' or not isinstance(evidence, dict) or evidence.get('status') in EXPLICIT:
            continue
        if re.match(r'^\s*(?:约|大约|≈|approximately\b|approx\.)', str(evidence.get('value') or ''), re.I):
            evidence['qualifiers'] = sorted(set(evidence.get('qualifiers', [])) | {'approximate'})
    # The qualifier must name this field in the same sentence. A nearby unrelated
    # "estimate" never turns every measurement into an estimate.
    for field, labels in LABELS.items():
        evidence = facts.get(field)
        if not evidence or evidence.get('status') in EXPLICIT:
            continue
        for sentence in re.split(r'[。；;\n]', text):
            if re.search(r'^\s*(?:' + '|'.join(map(re.escape, labels)) + r')[^。；;\n]{0,35}(?:估算|估计|待实测|未实测)', sentence):
                evidence['qualifiers'] = sorted(set(evidence.get('qualifiers', [])) | {'estimated'})
                evidence.setdefault('qualification_evidence', []).append({'text': sentence.strip(), 'source': note.get('source', {})})
    return facts


def notices(record):
    """Human-readable source limitations, separate from import corruption."""
    facts = record.get('structured_facts') or {}
    result = []
    for field, fact in facts.items():
        if not isinstance(fact, dict):
            continue
        title = TITLES.get(field, (fact.get('source') or {}).get('column') or field)
        if fact.get('status') == 'conflict':
            result.append({'field': field, 'label': title, 'kind': 'conflict', 'message': '字段与来源记载不一致，请核对', 'source': fact.get('source', {})})
        elif fact.get('qualifiers') and fact.get('status') not in EXPLICIT:
            result.append({'field': field, 'label': title, 'kind': 'estimate', 'message': '原资料为估算/近似值，限定说明已保留',
                           'source': fact.get('source', {}), 'evidence': fact.get('qualification_evidence', [])})
    material = '\n'.join(str((facts.get(k) or {}).get('value') or '') for k in ('material', 'fabric'))
    components = facts.get('components') or {}
    if re.search(r'配的\S{1,15}[:：]|配套的\S{1,15}[:：]|附带的\S{1,15}[:：]', material) and components.get('status') not in EXPLICIT and not components.get('value'):
        result.append({'field': 'components', 'label': '随售组成', 'kind': 'needs_confirmation',
                       'message': '材料文字提到配件，但随售组成未明确；不能据此认定本规格包含配件',
                       'source': (facts.get('fabric') or facts.get('material') or {}).get('source', {})})
    description = facts.get('description') or {}
    text = str(description.get('value') or '')
    for field, labels in LABELS.items():
        fact = facts.get(field) or {}
        if fact.get('status') in EXPLICIT:
            continue
        pattern = r'^\s*(?:' + '|'.join(map(re.escape, labels)) + r')(?:栏)?(?:原文)?\s*[:：]\s*([^\n]+)'
        complex_lines = [m[0].strip() for m in re.finditer(pattern, text, re.M | re.I)
                         if not measurement(m[1]) and not fact.get('value')]
        pending = [line.strip() for line in re.split(r'[。\n]', text)
                   if re.search(r'^\s*(?:' + '|'.join(map(re.escape, labels)) + r')', line)
                   and re.search(r'待.{0,10}(?:确认|核实)|矛盾|冲突', line)]
        evidence = list(dict.fromkeys(pending or complex_lines))
        if evidence:
            result.append({'field': field, 'label': TITLES[field], 'kind': 'needs_confirmation',
                           'message': '原备注明确要求核实，不能选一个值代填' if pending else '备注包含部件或包装说明，不能直接当作本字段的完整量值',
                           'source': description.get('source', {}),
                           'evidence': [{'text': line, 'source': description.get('source', {})} for line in evidence]})
    if re.search(r'未经.{0,18}(?:实拍|规格核验)|图片.{0,15}(?:待核实|未核实)|参考图.{0,25}未经', text):
        result.append({'field': 'source_images', 'label': '图片与实际规格', 'kind': 'needs_confirmation',
                       'message': '原备注说明图片尚未核实到具体规格；图片已保留，不能当成实拍核验通过',
                       'source': description.get('source', {})})
    return result
