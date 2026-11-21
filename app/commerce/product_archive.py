"""Read-only archive facts and lazy, data-preserving design bindings."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from fastapi import HTTPException
from PIL import Image

from . import store as s

FACT_ALIASES = {
    'size': ('size', 'dimensions', 'item_dimensions', '尺寸', '规格'),
    'material': ('material', '材质', '材料', '材料组成'),
    'fabric': ('fabric', '面料', '面料成分'),
    'package_quantity': ('package_quantity', 'number_of_items', 'item_package_quantity',
                         '销售件数', '每包件数', '销售单位件数', '包装数量'),
    'color': ('color', 'color_name', '颜色', '款式颜色'),
    'fill_material': ('fill_material', 'filling', '填充', '填充物', '填充材质'),
    'weight': ('weight', 'item_weight', '重量', '净重'),
    'item_shape': ('item_shape', 'shape', '形状', '外形'),
    'pattern': ('pattern', '花型', '图案', '纹样'),
    'components': ('components', '部件', '部件关系', '配件'),
    'package_dimensions': ('package_dimensions', '包装尺寸'),
    'package_weight': ('package_weight', '包装重量', '毛重'),
    'fill_weight': ('fill_weight', 'filling_weight', '填充重量', '填充净重'),
    'natural_height': ('natural_height', '自然高度', '平放高度'),
    'compressed_height': ('compressed_height', '受压高度', '压缩高度'),
    'item_length': ('item_length', '商品长度'),
    'item_width': ('item_width', '商品宽度'),
    'item_height': ('item_height', '商品高度'),
}
FIELD_ALIASES = FACT_ALIASES
NON_VISUAL = re.compile(r'price|cost|currency|business|stock|inventory|gtin|asin|product_type|category|manufacturer|moq|minimum_order|freight|shipping|delivery|lead_time|payment|tax|compliance|certificate|采购|成本|供货价|售价|价格|单价|销售价|报价|币种|货币|库存|类目|分类|制造商|起订|运费|物流|交期|账期|税费|认证|证书', re.I)
EMPTY = {'', '-', '—', '未知', '待补充', '未填写', '暂无', 'unknown'}
SKU_IDENTITY_LABELS = {'variant_name', 'supplier_sku', 'variant_key', 'sku_id', 'source_variant_id',
                       'sku', 'sku名', 'sku名称', '供应商sku', '供货sku', '供应商货号', '规格名称', '款式名称'}
MEASUREMENT_UNIT = r'kg|g|cm|mm|m|inches|inch|in|lb|oz|mL|L|mAh|Ah|kWh|Wh|MW|mW|kW|W|mA|A|V|MHz|kHz|Hz|TB|GB|MB|千克|克|厘米|毫米|米|英寸'


def _unit_name(value):
    aliases = {'kg':'kg','g':'g','cm':'cm','mm':'mm','m':'m','in':'in','lb':'lb','oz':'oz',
               '千克':'kg','克':'g','厘米':'cm','毫米':'mm','米':'m','英寸':'in','inch':'in','inches':'in'}
    text = value.strip()
    return aliases.get(text.lower(), {'l':'L','w':'W','v':'V','a':'A','hz':'Hz'}.get(text, text))


def fact_aliases(field):
    return next((values for values in FACT_ALIASES.values() if field in values), (field,))


def _explicitly_resolved(record, field):
    structured = record.get('structured_facts') or {}
    explicit = any(isinstance(structured.get(key), dict) and structured[key].get('status') in
                   {'manual', 'cleared', 'not_applicable'} for key in fact_aliases(field))
    return explicit and field_value(record, field)[1] in {'manual', 'cleared', 'not_applicable'}


def physical_issues(record):
    # Products retain an aggregate import audit, but variant names belong to
    # individual SKUs. Their own gates below must be resolved separately.
    sku_only={'variant_name'} if not record.get('product_id') else set()
    fields=set(key for aliases in FACT_ALIASES.values() for key in aliases)|{'product_name','variant_name','fabric','fill_material','filling','weight','shape','image','source_images'}
    issues=[issue for issue in list(record.get('source_conflicts') or [])+list(record.get('source_parse_issues') or [])
            if isinstance(issue,dict) and issue.get('field') in fields and issue.get('field') not in sku_only and
            not _explicitly_resolved(record, issue['field'])]
    for field,evidence in (record.get('structured_facts') or {}).items():
        if (isinstance(evidence,dict) and evidence.get('status')=='conflict' and
                not NON_VISUAL.search(field) and field not in {'brand','origin','origin_country','origin_place'}|sku_only and
                not _explicitly_resolved(record, field)):
            issues.append({'field':field,'message':'实物事实来源冲突，请明确核对后保存'})
    for field, aliases in FACT_ALIASES.items():
        if field_value(record, field)[1] == 'conflict' and not any(issue.get('field') in aliases for issue in issues):
            issues.append({'field':field,'message':'同一实物字段存在矛盾来源，请明确核对后保存'})
    return issues


def for_project(c, pid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone():
        return None
    linked = [p for p in s.all_rows(c, 'product') if p.get('project_id') == pid]
    if len(linked) > 1:
        raise HTTPException(409, '设计项目关联多个商品档案，请核对关联')
    return linked[0] if linked else s.by_key(c, 'product', 'design:' + str(pid))


def family_skus(c, product):
    """Historical membership decides whether parent facts/photos are truly single-SKU."""
    return [s.decode(row) for row in c.execute(
        "SELECT * FROM commerce_records WHERE kind='sku' AND json_extract(data,'$.product_id')=? ORDER BY id",
        (product['id'],))]


def selected_skus(c, product, sku_ids=None):
    available = [v for v in family_skus(c, product) if v.get('active', True)]
    if sku_ids is None:
        return available
    try:
        wanted = [int(v) for v in sku_ids]
    except (ValueError, TypeError):
        raise HTTPException(422, '请选择有效的商品 SKU') from None
    if not wanted or len(set(wanted)) != len(wanted) or any(v not in {x['id'] for x in available} for v in wanted):
        raise HTTPException(422, '选择的 SKU 不属于当前商品或已停用')
    return [v for v in available if v['id'] in wanted]


def field_value(record, field):
    # Every member (including item_weight) shares one alias group. Inspect all
    # structured evidence before legacy scalar/normalised values are considered.
    aliases = fact_aliases(field)
    structured = record.get('structured_facts') or {}
    normalized = record.get('normalized_fields') or {}
    evidence = [structured[key] for key in aliases if isinstance(structured.get(key), dict)]
    explicit = [value for value in evidence if value.get('status') in {'manual', 'cleared', 'not_applicable'}]
    candidates = explicit or evidence
    values = []
    for item in candidates:
        status = item.get('status', 'source')
        if status == 'conflict':
            return None, 'conflict'
        if status == 'not_applicable':
            values.append(('不适用', status))
        elif status in {'cleared', 'missing'} or item.get('value') in (None, ''):
            values.append((None, 'cleared'))
        elif str(item['value']).strip().casefold() not in EMPTY:
            values.append((item['value'], status))
    if values:
        if len({str(value).strip() if value is not None else None for value, _ in values}) > 1:
            return None, 'conflict'
        return values[0]
    if candidates:
        return None, 'cleared'
    legacy = []
    for key in aliases:
        for value in (None if key == 'components' else record.get(key), normalized.get(key)):
            if value is not None and str(value).strip().casefold() not in EMPTY:
                legacy.append(value)
    if legacy:
        if len({str(value).strip() for value in legacy}) > 1:
            return None, 'conflict'
        return legacy[0], 'archive'
    text = str(record.get('facts') or '')
    source_values = []
    for key in aliases:
        source_values.extend(match.strip() for match in re.findall(
            r'(?im)^\s*' + re.escape(key) + r'\s*[:：]\s*([^\n]+)', text)
            if match.strip().casefold() not in EMPTY)
    if source_values:
        if len(set(source_values)) > 1:
            return None, 'conflict'
        return source_values[0], 'fact_text'
    return None, 'missing'


def facts_text(record):
    structured = record.get('structured_facts') or {}
    replaced = set()
    for field in structured:
        aliases = next((aliases for aliases in FACT_ALIASES.values() if field in aliases), (field,))
        replaced.update(alias.casefold() for alias in aliases)
    lines = []
    for line in str(record.get('facts') or '').splitlines():
        label = re.split(r'[:：]', line, maxsplit=1)[0].strip()
        if label.casefold() not in replaced and not NON_VISUAL.search(label):
            lines.append(line.strip())
    for field, evidence in (record.get('structured_facts') or {}).items():
        if any(field in aliases for aliases in FACT_ALIASES.values()):
            continue
        if isinstance(evidence, dict) and evidence.get('status') not in {'conflict', 'missing', 'cleared'}:
            value = evidence.get('value')
            if value not in (None, '') and not NON_VISUAL.search(field):
                lines.append(f'{field}: {value}')
    for field in FACT_ALIASES:
        value, status = field_value(record, field)
        if value not in (None, '') and status != 'conflict':
            qualifiers = {q for key, evidence in structured.items() if key in fact_aliases(field)
                          and isinstance(evidence, dict) and evidence.get('status') not in {'manual', 'cleared', 'not_applicable'}
                          for q in evidence.get('qualifiers', [])}
            suffix = '（原资料为估算/近似值，不作为实测值）' if qualifiers and status not in {'manual', 'cleared', 'not_applicable'} else ''
            lines.append(f'{field}: {value}{suffix}')
    return '\n'.join(dict.fromkeys(line for line in lines if line))


def _fact_label(line):
    return re.split(r'[:：]', line, maxsplit=1)[0].strip().casefold()


def _shared_facts_text(record):
    # A family archive may retain the first imported SKU's identity as evidence.
    # It is never a shared physical fact or an identity source for another SKU.
    return '\n'.join(line for line in facts_text(record).splitlines()
                     if _fact_label(line) not in SKU_IDENTITY_LABELS)


def canonical_fact_field(field):
    lowered = field.casefold()
    return next((key for key, aliases in FACT_ALIASES.items()
                 if lowered in {v.casefold() for v in aliases}), field)


def shared_design_record(product, skus):
    """A supplier family's first row is evidence, never an implicit SKU default."""
    result = copy.deepcopy(product)
    if len(skus) <= 1:
        return result
    structured = product.get('structured_facts') or {}
    imported = bool(product.get('supplier_source')) or any(
        isinstance(v, dict) and (v.get('source') or {}).get('kind') == 'supplier_cell' for v in structured.values())
    if not imported:
        return result  # Preserve explicitly maintained, historical common briefs.
    names = set(structured) | set(product.get('normalized_fields') or {})
    names.update(_fact_label(line) for line in str(product.get('facts') or '').splitlines() if re.search(r'[:：]', line))
    blocked = set()
    for field in names:
        canonical = canonical_fact_field(field)
        if canonical in {'product_name', 'brand'}:
            continue
        entries = [structured[a] for a in fact_aliases(canonical) if isinstance(structured.get(a), dict)]
        explicit = any(v.get('status') in {'manual', 'cleared', 'not_applicable'} or
                       v.get('scope') in {'product', 'shared'} or (v.get('source') or {}).get('scope') in {'product', 'shared'}
                       for v in entries)
        value, status = field_value(product, canonical)
        own = [field_value(sku, canonical) for sku in skus]
        unanimous = value is not None and status != 'conflict' and all(
            state not in {'missing', 'cleared', 'conflict'} and str(v).strip() == str(value).strip() for v, state in own)
        if not explicit and not unanimous:
            blocked.update(a.casefold() for a in fact_aliases(canonical))
            blocked.add(field.casefold())
    result['structured_facts'] = {k:v for k,v in structured.items() if k.casefold() not in blocked}
    result['normalized_fields'] = {k:v for k,v in (product.get('normalized_fields') or {}).items() if k.casefold() not in blocked}
    result['facts'] = '\n'.join(line for line in str(product.get('facts') or '').splitlines() if _fact_label(line) not in blocked)
    for field in list(result):
        if field.casefold() in blocked:
            result.pop(field)
    return result


def design_facts(shared, sku):
    """Compact sourced attributes; identifiers, free prose and trade values stay separate."""
    omitted = SKU_IDENTITY_LABELS | {'name', 'product_name', 'brand', 'description',
                                   'origin', 'origin_country', 'origin_place', 'model_name', 'model_number'}
    fields = set(FACT_ALIASES)
    for record in (shared, sku):
        fields.update(record.get('structured_facts') or {})
        fields.update(record.get('normalized_fields') or {})
        fields.update(_fact_label(line) for line in str(record.get('facts') or '').splitlines() if re.search(r'[:：]', line))
    result = {}
    for field in sorted({canonical_fact_field(f) for f in fields}):
        if field.casefold() in omitted or NON_VISUAL.search(field):
            continue
        value, status = field_value(sku, field)
        record = sku
        if status == 'missing':
            value, status = field_value(shared, field)
            record = shared
        if status == 'missing':
            continue
        entries = [v for key,v in (record.get('structured_facts') or {}).items()
                   if canonical_fact_field(key) == field and isinstance(v, dict)]
        explicit = [v for v in entries if v.get('status') in {'manual', 'cleared', 'not_applicable'}]
        entries = explicit or entries
        if field == 'special_feature' and not explicit:
            continue  # Supplier marketing text is not a confirmed physical capability.
        evidence = []
        for entry in entries:
            source = entry.get('source') or {}
            evidence.append({k:source[k] for k in ('kind', 'import_id', 'sheet', 'row', 'column', 'column_index',
                           'parent_field', 'parent_revision', 'actor') if k in source})
        item = {'value': value, 'status': status, 'scope': 'sku' if record is sku else 'product',
                'source': evidence[0] if evidence else {'kind': status}}
        if record is shared and shared.get('id') is not None:
            item['inherited_from_product_id'] = shared['id']
        if entries:
            item['raw'] = copy.deepcopy(entries[0].get('raw', value))
            qualifiers = sorted({q for entry in entries for q in entry.get('qualifiers', [])})
            if qualifiers:
                item['qualifiers'] = qualifiers
                item['qualification_evidence'] = [v for entry in entries for v in entry.get('qualification_evidence', [])]
        if len(evidence) > 1:
            item['evidence'] = evidence
        units = set()
        for entry in entries:
            if entry.get('unit'):
                units.add(str(entry['unit']).strip())
            column = str((entry.get('source') or {}).get('column') or '')
            match = re.search(r'[（(]\s*(' + MEASUREMENT_UNIT + r')\s*[）)]', column, re.I)
            if match:
                units.add(match[1])
        match = re.search(r'(?:\d|\s)(' + MEASUREMENT_UNIT + r')\s*$', str(value or ''), re.I)
        if match:
            units.add(match[1])
        units = {_unit_name(v) for v in units}
        if len(units) == 1:
            item['unit'] = next(iter(units))
        elif len(units) > 1:
            item['unit_status'] = 'conflict'
            item['unit_evidence'] = sorted(units)
        conditions = [v.get('measurement_condition') or v.get('condition') for v in entries
                      if v.get('measurement_condition') or v.get('condition')]
        if conditions:
            item['measurement_condition'] = conditions[0] if len(set(map(str, conditions))) == 1 else conditions
        elif field in {'natural_height', 'compressed_height'}:
            item['measurement_condition'] = 'natural_uncompressed' if field == 'natural_height' else 'compressed_unspecified_load'
            item['condition_source'] = 'explicit_field_semantics'
        result[field] = item
    return result


def _sku_facts_text(shared, sku):
    """Merge shared facts with one SKU, including explicit clears and non-alias fields."""
    overrides=set(SKU_IDENTITY_LABELS)
    for field,aliases in FACT_ALIASES.items():
        if field_value(sku,field)[1]!='missing':
            overrides.update(alias.casefold() for alias in aliases)
    overrides.update(field.casefold() for field in (sku.get('structured_facts') or {}))
    overrides.update(_fact_label(line)
                     for line in str(sku.get('facts') or '').splitlines() if re.search(r'[:：]',line))
    lines=[line for line in _shared_facts_text(shared).splitlines() if _fact_label(line) not in overrides]
    return '\n'.join(dict.fromkeys(lines+facts_text(sku).splitlines()))


def snapshot_facts_text(snapshot):
    """Canonical manufacturing facts from the order freeze, without live reads."""
    return _sku_facts_text({'facts':str((snapshot.get('source') or {}).get('facts') or '')},
                           snapshot.get('sku') or {})


def source_images(product, sku, total=1):
    values = sku.get('source_images') or (product.get('source_images') if total == 1 else []) or []
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = []
    return list(dict.fromkeys(v for v in values if isinstance(v, str)))


def readiness(c, product, sku_ids=None):
    """Design execution availability only, without approving product facts."""
    from app import simple_design_flow as flow
    from app.supplier_fact_notes import notices
    skus=selected_skus(c,product,sku_ids)
    family=family_skus(c,product)
    missing=[];reports=[]
    if not skus:
        missing.append({'field':'skus','label':'商品 SKU','reason':'没有可执行设计的 SKU','sku_id':None})
    for sku in skus:
        errors=[]
        images=source_images(product,sku,len(family))
        if not images:
            errors.append({'field':'source_images','label':'本款原图','reason':'没有可发送的本款原图','sku_id':sku['id']})
        for url in images:
            try:
                with Image.open(flow.local_path(url,source=True)) as image:image.verify()
            except (HTTPException,OSError,ValueError):
                errors.append({'field':'source_images','label':'本款原图','reason':'原图文件无法读取','sku_id':sku['id']})
                break
        missing.extend(errors)
        reports.append({'sku_id':sku['id'],'ready':not errors,'missing':errors,'source_notices':notices(sku)})
    return {'design_ready':not missing,'ready':not missing,'status':'ready' if not missing else 'needs_supplement',
            'missing':missing,'skus':reports,'selected_sku_ids':[sku['id'] for sku in skus]}



def _physical_facts(product, skus, total, all_skus=None):
    from app import simple_design_flow as flow
    products = []
    shared = shared_design_record(product, all_skus if all_skus is not None else skus)
    for sku in skus:
        urls = source_images(product, sku, total)
        color, _ = field_value(sku, 'color')
        products.append({'sku_id': sku['id'], 'variant_key': sku.get('variant_key', ''),
                         'name': sku.get('name') or product.get('name', ''), 'color': color or '',
                         'canonical_product_name': product.get('name', ''), 'variant_name': sku.get('name', ''),
                         'variant_identity': {k:sku[k] for k in ('variant_key','local_variant_id','supplier_sku') if k in sku} | {'sku_id':sku['id']},
                         'design_facts': design_facts(shared, sku),
                         'parameters': _sku_facts_text(shared, sku),
                         'source_images': urls, 'unresolved_source_issues':physical_issues(sku),
                         'source_hashes': [flow.file_hash(flow.local_path(u, source=True)) for u in urls]})
    return {'product_id': product['id'], 'name': product.get('name', ''),
            'brand': product.get('brand', ''), 'brand_status': product.get('brand_status', 'unknown'),
            'shared_parameters': _shared_facts_text(shared),
            'unresolved_source_issues':physical_issues(product),
            'products': products}


def fact_digest(c, product, sku_ids=None):
    all_skus = family_skus(c, product)
    physical = _physical_facts(product, selected_skus(c, product, sku_ids), len(all_skus), all_skus)
    return hashlib.sha256(s.dump(physical).encode('utf-8')).hexdigest()


def snapshot_for_project(c, project):
    product = for_project(c, project['id'])
    if product is None:
        return None
    selection = s.by_key(c, 'design_selection', str(project['id'])) or {}
    ids = selection.get('sku_ids')
    skus = selected_skus(c, product, ids)
    all_skus = family_skus(c, product)
    physical = _physical_facts(product, skus, len(all_skus), all_skus)
    business = {}
    if product.get('business_id'):
        row = c.execute('SELECT content_language,visual_style FROM businesses WHERE id=?', (product['business_id'],)).fetchone()
        business = dict(row) if row else {}
    products = [v | {'key': v['variant_key'] or 'sku-' + str(v['sku_id'])} for v in physical['products']]
    urls = list(dict.fromkeys(u for v in products for u in v['source_images']))
    return {'name': physical['name'], 'canonical_product_name': physical['name'], 'brand': physical['brand'], 'parameters': physical['shared_parameters'],
            'platform': project.get('platform') or '亚马逊', 'language': business.get('content_language') or 'en',
            'style': business.get('visual_style', ''), 'source_images': urls,
            'source_hashes': [flow_hash(u) for u in urls], 'variants': [],
            'archive_products': products, 'archive_product_id': product['id'],
            'product_fact_revision': product.get('fact_revision'),
            'sku_fact_revisions': {str(v['id']): v.get('fact_revision') for v in skus},
            'archive_digest': fact_digest(c, product, ids), 'selected_sku_ids': [v['id'] for v in skus],
            'preservation_mode': 'reference_guided', 'design_readiness': readiness(c, product, ids)}


def flow_hash(url):
    from app import simple_design_flow as flow
    return flow.file_hash(flow.local_path(url, source=True))


def project_overlay(c, project, *, linked_products=None):
    if linked_products is None:product=for_project(c,project['id'])
    else:
        matches=linked_products.get(project['id'],[])
        if len(matches)>1:raise HTTPException(409,'设计项目关联多个商品档案，请核对关联')
        product=matches[0] if matches else s.by_key(c,'product','design:'+str(project['id']))
    if product is None:
        return project
    # Viewing history must remain possible even when a current source is missing.
    selection=s.by_key(c,'design_selection',str(project['id'])) or {}
    try:
        skus=selected_skus(c,product,selection.get('sku_ids'))
    except HTTPException:
        skus=selected_skus(c,product)
    total=len(family_skus(c,product))
    urls=list(dict.fromkeys(url for sku in skus for url in source_images(product,sku,total)))
    return project | {'product_name': product.get('name', project['product_name']),
                      'design_brief': facts_text(product), 'source_images':s.dump(urls),
                      '_archive_product_id': product['id']}


def ensure_design_project(product_id, sku_ids=None):
    with s.transaction() as c:
        product = s.get(c, 'product', product_id)
        report = readiness(c, product, sku_ids)
        if not report['design_ready']:
            raise HTTPException(422, {'message': '商品档案待补充，暂不能设计', 'readiness': report})
        pid = product.get('project_id')
        created = False
        if pid is not None and not c.execute('SELECT 1 FROM design_projects WHERE id=?', (pid,)).fetchone():
            raise HTTPException(409, '商品档案关联的设计项目缺失，请先核对数据')
        if pid is None:
            pid = c.execute('''INSERT INTO design_projects(product_name,supplier_name,source_file,platform,
                design_brief,source_images,business_id,product_type) VALUES(?,?,?,?,?,?,?,?)''',
                (product['name'], product.get('supplier', ''), '商品档案#' + str(product_id), '亚马逊',
                 facts_text(product), '[]', product.get('business_id'), product.get('product_type', ''))).lastrowid
            s.save(c, 'product', product | {'project_id': pid}, expected=product['revision'])
            created = True
        active = c.execute("SELECT 1 FROM design_simple_runs WHERE project_id=? AND status IN ('queued','running','retry_wait','paused')", (pid,)).fetchone()
        previous = s.by_key(c, 'design_selection', str(pid))
        if active and previous and previous.get('sku_ids') != report['selected_sku_ids']:
            raise HTTPException(409, '设计任务尚未结束，不能更换选定 SKU')
        selection = {'product_id': product_id, 'sku_ids': report['selected_sku_ids']}
        if not previous or any(previous.get(k) != v for k, v in selection.items()):
            s.put(c, 'design_selection', str(pid), selection)
        s.audit(c, 'archive_design_selected', product_id, s.dump({'project_id': pid, 'sku_ids': report['selected_sku_ids']}))
        return {'project_id': pid, 'product_id': product_id, 'selected_sku_ids': report['selected_sku_ids'],
                'created': created, 'readiness': report}
