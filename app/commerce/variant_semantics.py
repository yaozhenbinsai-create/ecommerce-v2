"""Supplier groups are source containers, not proof of marketplace equivalence.

This is a deterministic projection of existing facts. It neither edits archives
nor guesses facts from generated art, and it is independent of product IDs.
"""
from __future__ import annotations

import json
import re
import unicodedata

LABELS = {
    'color': '颜色', 'size': '尺寸', 'natural_height': '自然高度',
    'compressed_height': '受压高度', 'item_height': '商品高度',
    'fill_weight': '填充量', 'weight': '商品重量', 'fabric': '面料',
    'fill_material': '填充材料', 'material': '材质', 'shape': '形状',
    'item_shape': '形状', 'construction_type': '结构', 'style': '款式',
    'style_name': '款式', 'function': '功能', 'purpose': '用途',
    'model_number': '厂家型号', 'model_name': '型号名称', 'brand': '品牌',
    'number_of_items': '销售件数', 'components': '销售组成',
}
# A changing marketing description is not in itself a different function.
# Only explicit identity facts and unexplained source-name differences count.
IDENTITY_FIELDS = ('product_name', 'brand', 'model_number', 'model_name', 'category',
                   'function', 'purpose', 'construction_type', 'shape',
                   'item_shape', 'style', 'style_name', 'fabric', 'fill_material', 'material')
IMMUTABLE = {'product_name', 'brand', 'model_number', 'model_name', 'category', 'function', 'purpose', 'construction_type'}
DISPLAY_FIELDS = ('color', 'size', 'natural_height', 'compressed_height',
                  'item_height', 'fill_weight', 'weight', 'function', 'purpose')

# Name labels can describe the audience or a measured fit. They are not
# manufacturer models. They only qualify as such when the source also has a
# shared product/material identity and its own measurements; no SKU inherits
# another option's missing facts. Unknown words remain unresolved.
FIT_LABEL = re.compile(
    r'^(?:(?:儿童|婴童|婴幼儿|婴儿|幼儿|青少年|学生|成人|老年|男士|女士|通用|标准|常规|'
    r'加大|加小|特大|特小|超大|超小|大小|大|小)(?:款|版|型|号|码)?|'
    r'(?:junior|adult|kids?|children|child|toddler|infant|youth|student|standard|'
    r'regular|universal|small|medium|large|extra[- ]?(?:small|large)|[2-5]?x[sl])'
    r'(?:[- ]?(?:version|size|fit|edition))?)$', re.I)
IDENTITY_NAME = re.compile(
    r'单[边面层侧]|双[边面层侧]|多层|立体|平面|有线|无线|电动|手动|加热|非加热|'
    r'结构|型号|(?:^|\b)(?:[23]d|structure|construction|model|wired|wireless|'
    r'heated|electric|manual|single[- ]sided|double[- ]sided)(?:\b|$)', re.I)


def _fit_evidence(sources):
    shared = ('product_name', 'fabric', 'fill_material')
    if not all(all(fact(source, key) not in (None, '') for source in sources)
               and len({norm(fact(source, key)) for source in sources}) == 1 for key in shared):
        return False
    return all(any(fact(source, key) not in (None, '') for key in
                   ('size', 'natural_height', 'item_height', 'item_length', 'item_width'))
               for source in sources)


def norm(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False).casefold()
    return re.sub(r'[\s，,;；：:()（）\-—_/×*]+', '', str('' if value is None else value)).casefold()


def fact(source, field):
    from .product_archive import field_value
    value, status = field_value(source, field)
    return value if status not in {'missing', 'cleared', 'conflict'} else None


def source_name(source):
    from .product_archive import field_value
    value,status=field_value(source,'variant_name')
    return str(value or '') if status!='missing' else str(source.get('name') or '')


def measurement(source, field, language='zh'):
    value = fact(source, field)
    if value in (None, ''):
        return ''
    evidence = (source.get('structured_facts') or {}).get(field) or {}
    text = str(value).strip()
    # These normalized archive fields are defined in cm/g. Preserve explicit
    # units, ranges and qualifiers rather than parsing them to one number.
    unit = {'natural_height': 'cm', 'compressed_height': 'cm', 'item_height': 'cm',
            'fill_weight': 'g', 'weight': 'g'}.get(field, '')
    if unit and re.fullmatch(r'[\d.\s~～–—−\-至到约≈<>≤≥]+', text):
        text += ' ' + unit
    if 'approximate' in evidence.get('qualifiers', []) and not re.search(r'约|≈|about|approx', text, re.I):
        text = ('约' if language.startswith('zh') else 'approx. ') + text
    return text


def _attribute_values(entry, field):
    attributes = dict(entry.get('attributes') or {})
    attributes.update(entry.get('variation_attributes') or {})
    return [x.get('value') for x in attributes.get(field, [])
            if isinstance(x, dict) and x.get('value') not in (None, '')]


def _remove_named_values(name, source, entry, fields):
    """Match whole source-backed values once, longest first (1 is not part of 10cm)."""
    values = {measurement(source, key) for key in ('natural_height', 'item_height')}
    if 'size' in fields:
        values.add(measurement(source, 'compressed_height'))
    for key in fields:
        for value in (fact(source, key), source.get(key), *_attribute_values(entry, key)):
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values.add(str(value).strip())
    patterns = []
    values = {unicodedata.normalize('NFKC', value) for value in values if value}
    for value in sorted(values, key=lambda text: (-len(text), text)):
        cm = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(?:cm|厘米)', value, re.I)
        token = re.escape(cm[1]) + r'\s*(?:cm|厘米)' if cm else re.escape(value)
        left = r'(?<![a-z0-9.])' if re.match(r'[a-z0-9]', value, re.I) else ''
        right = r'(?![a-z0-9.])' if cm or re.search(r'[a-z0-9]$', value, re.I) else ''
        if re.fullmatch(r'\d+(?:\.\d+)?', value):
            right = r'(?![.]|\s*[^\W_])'
        patterns.append(left + token + right)
    return re.sub('|'.join(patterns), '', name, flags=re.I) if patterns else name


def _name_remainder(source, entry, allowed):
    name = unicodedata.normalize('NFKC', source_name(source).strip())
    base = unicodedata.normalize('NFKC', str(fact(source, 'product_name') or
                                            source.get('source_family_name') or '').strip())
    # Formatting differences between the sheet's series and SKU names are
    # common. Compare the series prefix without dropping identity words.
    prefix = re.match(r'\s*'.join(map(re.escape, re.sub(r'\s+', '', base))), name, re.I) if base else None
    residual = name[prefix.end():] if prefix else name
    removable = {'color', 'size', 'item_height', 'natural_height', *allowed}
    return _remove_named_values(residual, source, entry, removable).strip(' -—_')


def _fit_suffix(name):
    for start in range(len(name)):
        if (start and name[start].isascii() and name[start-1].isascii() and
                name[start-1].isalnum()):
            continue
        label = name[start:]
        if FIT_LABEL.fullmatch(label):
            return name[:start].rstrip(' -—_'), label
    return name, ''


def analysis(sources, entries=(), varying_fields=()):
    """Keep an unexplained different model/structure/name out of a size family.

    A source's own named variation values may explain its name. Missing facts
    never borrow another SKU's facts. Same supplier names alone prove nothing.
    """
    allowed = set(varying_fields) - IMMUTABLE
    by_id = {x.get('sku_id'): x for x in entries}
    heights = [fact(x, 'natural_height') for x in sources]
    ranked_height = bool(heights and all(x not in (None, '') for x in heights)
                         and len({norm(x) for x in heights}) > 1)
    fields = {}
    for key in IDENTITY_FIELDS:
        if key == 'material' and all(fact(x, 'fabric') and fact(x, 'fill_material') for x in sources):
            continue  # the composite material text repeats those two facts
        values = [fact(x, key) for x in sources]
        if any(v not in (None, '') for v in values) and len({norm(v) for v in values}) > 1:
            fields[key] = values
    groups = {}
    names = []
    fit_evidence = _fit_evidence(sources)
    name_evidence = []
    residuals = [_name_remainder(source, by_id.get(source.get('id'), {}), allowed) for source in sources]
    if fit_evidence:
        aliases = [_fit_suffix(name) for name in residuals]
        stems = {norm(stem) for stem, _ in aliases}
        # Accept an abbreviated series name only when every measured option
        # uses the same stem and the remaining differences are fit labels.
        if len(stems) == 1 and '' not in stems and any(label for _, label in aliases):
            residuals = [label for _, label in aliases]
    for source, residual in zip(sources, residuals):
        base = str(fact(source, 'product_name') or source.get('source_family_name') or '').strip()
        # Rank words are explained only when each SKU has its own explicit
        # differing height. Remove the rank, never structural words around it.
        if ranked_height:
            residual = re.sub(r'低|中|高|\b(?:low|medium|mid|high)(?:[- ](?:loft|profile))?\b', '', residual, flags=re.I)
        residual = residual.strip(' -—_')
        structural = bool(IDENTITY_NAME.search(residual))
        # A shared product noun in a rank label carries no new identity. A
        # different audience label may accompany size/height, but may never
        # override a real model/function/material conflict below.
        explained = (not residual or (ranked_height and norm(residual) in norm(base))
                     or (fit_evidence and bool(FIT_LABEL.fullmatch(residual))))
        name_evidence.append({'sku_id': source.get('id'), 'remainder': residual,
                              'kind': 'identity' if structural else 'specification' if explained else 'unresolved'})
        names.append('' if explained and not structural else norm(residual))
        identity = {key: fact(source, key) for key in fields if key not in allowed}
        # A name that we cannot explain is uncertainty, not proof of a new
        # product and not permission to create a separate catalog item.
        identity['source_name_remainder'] = norm(residual) if structural else ''
        signature = json.dumps({k: norm(v) for k, v in identity.items()}, sort_keys=True)
        groups.setdefault(signature, []).append(source.get('id'))
    reasons = [LABELS.get(k, k) + '不同或来源未覆盖全部规格' for k in fields if k not in allowed]
    unresolved = [row for row in name_evidence if row['kind'] == 'unresolved'] if len(set(names)) > 1 else []
    if any(row['kind'] == 'identity' for row in name_evidence) and len(set(names)) > 1:
        reasons.append('原始规格名称中有结构、功能或型号差异')
    return {'product_count': len(groups), 'groups': list(groups.values()),
            'distinct_products': len(groups) > 1, 'reasons': reasons,
            'changed_identity_fields': sorted(fields), 'source_name_remainders': names,
            'name_evidence': name_evidence, 'unresolved_names': unresolved}


def display(product, sources, draft=None):
    sources=[row for row in sources if row.get('active',True)]
    entries = (draft or {}).get('skus', [])
    result = analysis(sources, entries)
    rows = []
    for source in sources:
        details = []
        for field in DISPLAY_FIELDS:
            value = measurement(source, field) if field in {
                'natural_height', 'compressed_height', 'item_height', 'fill_weight', 'weight'} else fact(source, field)
            if value not in (None, ''):
                details.append({'field': field, 'label': LABELS[field], 'value': str(value)})
        rows.append({'sku_id': source['id'], 'seller_sku': source.get('sku', ''),
                     'name': source_name(source) or '规格名称待补充', 'details': details})
    return {**result, 'sku_count': len(sources), 'rows': rows,
            'label': (f'供货分组 · 商品归属待确认 · {len(sources)}个销售选项' if result['unresolved_names'] else
                      f'供货分组 · {result["product_count"]}款不同商品 · {len(sources)}个销售选项'
                      if result['distinct_products'] else f'{len(sources)}个实际销售规格'),
            'note': ('供货表归在一起不代表可以共用亚马逊商品页面；结构、功能等差异需分别处理。'
                     if result['distinct_products'] else '规格数量来自原始供货记录，具体合并方式按店铺类目规则决定。')}


def height_sizes(sources, entries, language):
    """Size may express a real height, never an invented function/style name."""
    values = [measurement(source, 'natural_height', language) for source in sources]
    if not values or not all(values) or len(set(values)) < 2:
        return {}
    result = {}
    for source, value in zip(sources, values):
        entry = next(x for x in entries if x['sku_id'] == source['id'])
        if (entry.get('size_source') or {}).get('status') in {'manual', 'cleared'}:
            continue
        # An explicit typed height/thickness theme can express this dimension
        # without also duplicating it in SIZE.
        if any(_attribute_values(entry, key) for key in ('item_thickness', 'thickness', 'item_height')):
            continue
        label = '自然高度 ' if language.startswith('zh') else 'Unloaded height '
        result[source['id']] = str(entry.get('size') or '').strip() + ' / ' + label + value
    return result
