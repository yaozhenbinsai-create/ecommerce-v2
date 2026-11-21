"""Seller-scoped product attributes maintained in the product archive."""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

from . import store as s, locales, categories, source_attributes

# Marketing, offer state, identifiers and images have dedicated sources.
SEPARATE = {'item_name', 'brand', 'bullet_point', 'product_description', 'generic_keyword',
            'purchasable_offer', 'fulfillment_availability', 'merchant_shipping_group',
            'condition_type', 'externally_assigned_product_identifier', 'merchant_suggested_asin',
            'supplier_declared_has_product_identifier_exemption', 'parentage_level',
            'variation_theme', 'child_parent_sku_relationship'}
LABELS = {'manufacturer': '制造商', 'model_name': '型号名称', 'model_number': '制造商型号编号',
          'color': '颜色', 'size': '尺寸', 'cover': '外罩材料', 'material': '材料',
          'fabric_type': '面料成分', 'fill_material': '填充材料', 'item_length_width': '商品长宽',
          'item_dimensions': '商品尺寸', 'item_weight': '商品重量',
          'item_package_dimensions': '包装尺寸', 'item_package_weight': '包装重量',
          'number_of_items': '销售件数', 'unit_count': '单位计数', 'item_shape': '形状',
          'pattern': '图案', 'special_feature': '真实特点', 'country_of_origin': '原产国',
          'pillow_type': '枕头类型', 'item_thickness': '商品厚度', 'style': '风格',
          'recommended_uses_for_product': '建议用途', 'indoor_outdoor_usage': '室内外用途',
          'item_firmness_description': '软硬程度',
          'list_price': '有依据的参考价', 'supplier_declared_dg_hz_regulation': '危险品法规适用性',
          'required_product_compliance_certificate': '合规证书适用性'}
FACT_ATTRIBUTES={'material':{'material','fabric_type','cover'},'fabric':{'material','fabric_type','cover'},'fill_material':{'fill_material','fill_material_type'},
    'size':{'size','item_length_width','item_dimensions'},'package_quantity':{'number_of_items','item_package_quantity','unit_count'},
    'item_weight':{'item_weight'},'package_weight':{'item_package_weight'},'color':{'color'},
    'item_shape':{'item_shape'},'pattern':{'pattern'},'manufacturer':{'manufacturer'},
    'model_name':{'model_name'},'model_number':{'model_number'}}


def canonical_field(field):
    from .product_archive import FACT_ALIASES
    for name,aliases in FACT_ALIASES.items():
        if field in aliases:return 'item_weight' if name=='weight' else name
    return field


def fact_aliases(field):
    from .product_archive import FACT_ALIASES
    canonical=canonical_field(field)
    return FACT_ALIASES.get('weight' if canonical=='item_weight' else canonical,(canonical,))


def put_fact(record,field,evidence):
    """Replace canonical aliases; the caller saves the prior archive revision."""
    field=canonical_field(field)
    for alias in fact_aliases(field):
        record.setdefault('structured_facts',{}).pop(alias,None)
        record.setdefault('normalized_fields',{}).pop(alias,None)
    record['structured_facts'][field]=copy.deepcopy(evidence)
    record['normalized_fields'][field]=evidence.get('value','')
    if field in {'size','color'} and record.get('product_id'):record[field]=str(evidence.get('value') or '')


def invalidate_fact_attributes(record,changed,except_scope=None):
    removed=set().union(*(FACT_ATTRIBUTES.get(canonical_field(field),{field}) for field in changed)) if changed else set()
    for key,value in (record.get('platform_attributes') or {}).items():
        if key==except_scope:continue
        value['attributes']={k:v for k,v in (value.get('attributes') or {}).items() if k not in removed}
        value['cleared_fields']=[k for k in value.get('cleared_fields',[]) if k not in removed]


def inherit_product_facts(c,previous,updated,changed,actor=''):
    from .product_archive import field_value
    for sku in s.all_rows(c,'sku'):
        if sku['product_id']!=updated['id']:continue
        before=copy.deepcopy(sku);touched=set()
        for field in changed:
            evidence=next((sku.get('structured_facts',{}).get(alias) for alias in fact_aliases(field) if sku.get('structured_facts',{}).get(alias)),{})
            inherited=evidence.get('status')=='inherited' or evidence.get('source',{}).get('kind')=='product_archive_inheritance'
            prior_value,_=field_value(previous,field);sku_value,_=field_value(sku,field)
            if not inherited or str(prior_value or '').strip()!=str(sku_value or '').strip():continue
            value=copy.deepcopy(updated['structured_facts'][field]);value['status']='inherited' if value.get('status')=='manual' else value.get('status')
            value['source']={'kind':'product_archive_inheritance','parent_field':field,'parent_revision':updated.get('fact_revision',0)}
            put_fact(sku,field,value)
            touched.add(field)
        if touched:
            invalidate_fact_attributes(sku,touched);sku['fact_revision']=before.get('fact_revision',0)+1
            s.put(c,'archive_revision',f"sku:{sku['id']}:{before['revision']}",{'kind':'sku','record_id':sku['id'],'snapshot':before,'actor':str(actor)})
            s.save(c,'sku',sku)


def scope(target, product_type):
    return {k: target.get(k, '') for k in ('account_id', 'seller_id', 'marketplace_id')} | {
        'product_type': product_type, 'parentage': 'NONE', 'requirements': 'LISTING'}


def scope_key(target, product_type):
    return hashlib.sha256(s.dump(scope(target, product_type)).encode()).hexdigest()


def is_fact(name):
    return name not in SEPARATE and 'image_locator' not in name


def definition(c, target, product_type):
    saved = s.by_key(c, 'definition', f"{target['id']}:{product_type}:NONE") or {}
    if saved.get('sample_only'):
        return {}
    descriptor = saved.get('descriptor') or {}
    if descriptor.get('productType', product_type) != product_type:
        return {}
    if descriptor.get('marketplaceIds') and target['marketplace_id'] not in descriptor['marketplaceIds']:
        return {}
    expected = scope(target, product_type)
    actual = saved.get('archive_scope') or saved.get('target_scope')
    if actual and actual != expected:
        return {}
    if target.get('environment')=='production':
        if actual != expected:return {}
        try:checked=datetime.fromisoformat(saved.get('checked_at','').replace('Z','+00:00'))
        except (ValueError,TypeError):return {}
        if checked.tzinfo is None or datetime.now(timezone.utc)-checked>timedelta(hours=24):return {}
    return saved


def values(record, target, product_type, defaults=None):
    saved = (record.get('platform_attributes') or {}).get(scope_key(target, product_type)) or {}
    if saved.get('scope') != scope(target, product_type):
        saved = {}
    result = copy.deepcopy(defaults or {})
    result.update(copy.deepcopy(saved.get('attributes') or {}))
    for name in saved.get('cleared_fields') or []:
        result.pop(name, None)
    return result


def encode(c, product, sku, target, schema, product_type, *, provenance=None):
    from . import supplier_declarations
    mid, language = target['marketplace_id'], locales.resolve(target)
    shared_sources, sku_sources = {}, {}
    defaults = source_attributes.from_sku(product | {'source_fields': {}}, schema, mid, language, provenance=shared_sources)
    descriptions = {name for name, source in shared_sources.items()
                    if source.get('rule') == 'source-description-projection-v1'}
    if descriptions:
        from . import product_archive
        common = product_archive.shared_design_record(product, product_archive.selected_skus(c, product))
        common_values = source_attributes.from_sku(common | {'source_fields': {}}, schema, mid, language)
        for name in descriptions:
            if name not in common_values:
                defaults.pop(name, None);shared_sources.pop(name, None)
    for name,value in supplier_declarations.model_defaults(product,schema,mid,language).items():
        defaults.setdefault(name,value)
    shared = values(product, target, product_type, defaults)
    sku_defaults=source_attributes.from_sku(sku,schema,mid,language,provenance=sku_sources)
    inherited={attr for field,evidence in (sku.get('structured_facts') or {}).items() if isinstance(evidence,dict) and evidence.get('status')=='inherited' for attr in FACT_ATTRIBUTES.get(canonical_field(field),{field})}
    product_manual=((product.get('platform_attributes') or {}).get(scope_key(target,product_type)) or {}).get('attributes',{})
    sources={k:v|{'scope':'product'} for k,v in shared_sources.items() if k not in product_manual}
    for key,value in sku_defaults.items():
        if key in inherited and key in product_manual:continue
        if key in sku_sources and key in shared and key not in sources:continue
        shared[key]=value
        sources.pop(key,None)
        if key in sku_sources:sources[key]=sku_sources[key]|{'scope':'sku'}
    for field,evidence in (sku.get('structured_facts') or {}).items():
        if isinstance(evidence,dict) and not source_attributes.missing(sku,field) and (
                evidence.get('status') in {'conflict','cleared','not_applicable'} or evidence.get('value') in ('',None)):
            canonical=canonical_field(field)
            direct={'material':{'material'},'fabric':{'fabric_type','cover'}}.get(
                canonical,FACT_ATTRIBUTES.get(canonical,{field}))
            blocked=direct|{key for key,source in sources.items() if source['source_field']==canonical}
            for attr in blocked:shared.pop(attr,None);sources.pop(attr,None)
    result=values(sku, target, product_type, shared)
    saved=(sku.get('platform_attributes') or {}).get(scope_key(target,product_type)) or {}
    manual=set(saved.get('attributes') or {})|set(saved.get('cleared_fields') or [])
    if provenance is not None:
        provenance.update({key:value for key,value in sources.items() if key in result and key not in manual})
    return result


def resolved(rule, root, seen=()):
    rule = copy.deepcopy(rule)
    ref = rule.get('$ref') if isinstance(rule, dict) else None
    if ref and ref.startswith('#/') and ref not in seen:
        current = root
        try:
            for part in ref[2:].split('/'):
                current = current[part.replace('~1', '/').replace('~0', '~')]
            return resolved(current, root, seen + (ref,)) | {k: v for k, v in rule.items() if k != '$ref'}
        except (KeyError, TypeError):
            return rule
    return rule


def field_groups(schema):
    groups = []
    required = set(schema.get('required') or [])
    for name, rule in (schema.get('properties') or {}).items():
        if not is_fact(name):
            continue
        groups.append({'name': name, 'title': LABELS.get(name, rule.get('title') or name),
                       'description': rule.get('description', ''), 'required': name in required,
                       'rule': rule})
    return sorted(groups, key=lambda x: (not x['required'], x['title']))


def report(c, product_id, target_id):
    from . import product_archive, jobs
    product, target = s.get(c, 'product', product_id), s.get(c, 'target', target_id)
    found = categories.resolve(c, product, target)
    mapping = found.get('mapping') or {}
    product_type = mapping.get('product_type') or product.get('product_type') or ''
    saved = definition(c, target, product_type) if product_type else {}
    schema = saved.get('schema') or {}
    schema_hash = hashlib.sha256(s.dump(schema).encode()).hexdigest() if schema else ''
    reports = []
    for sku in product_archive.selected_skus(c, product):
        attribute_sources={}
        attrs = encode(c, product, sku, target, schema, product_type, provenance=attribute_sources) if schema else {}
        if sku.get('color') and 'color' in schema.get('properties', {}):
            attrs.setdefault('color', [{'value': sku['color'], 'language_tag': locales.resolve(target),
                                         'marketplace_id': target['marketplace_id']}])
        if product.get('brand'):
            attrs['brand'] = [{'value': product['brand'], 'language_tag': locales.resolve(target),
                              'marketplace_id': target['marketplace_id']}]
        mapped = {}; categories.apply_defaults(c, product, target, mapped)
        for name, value in (mapped.get('attributes') or {}).items():
            attrs.setdefault(name, value)
        errors = jobs.validate_attributes(schema, attrs) if schema else []
        # Keep conditional facts, while generated content and offers remain later gates.
        fact_errors = []
        for error in errors:
            required_name = re.search(r"'([^']+)' is a required property", error)
            root_name = error.split(':', 1)[0].split('/', 1)[0]
            name = required_name[1] if required_name else root_name
            if is_fact(name):
                fact_errors.append(error)
        reports.append({'sku_id': sku['id'], 'name': sku['name'], 'revision': sku['revision'],
                        'attributes': {k: v for k, v in attrs.items() if is_fact(k)}, 'issues': fact_errors,
                        'attribute_sources':attribute_sources,
                        'cleared_fields': ((sku.get('platform_attributes') or {}).get(scope_key(target,product_type)) or {}).get('cleared_fields',[])})
    from . import supplier_declarations
    declared=supplier_declarations.for_product(product)
    declaration_hint='；本批用户确认没有厂家型号，按当前规则验证无型号表达，仍须完整平台预检' if declared else ''
    return {'product_id': product_id, 'target_id': target_id, 'product_type': product_type,
            'scope': scope(target, product_type), 'schema_hash': schema_hash, 'schema': schema,
            'fields': field_groups(schema), 'skus': reports, 'product_revision': product['revision'],
            'ready': bool(schema), 'classification': found, 'supplier_declarations': declared,
            'message': '按当前店铺官方字段补充商品事实；文案、图片和报价在刊登阶段检查'+declaration_hint if schema else
                       '请选择商品类型并读取当前店铺官方规则；仍可按实物事实准备设计'}


def save(c, product_id, target_id, payload, actor=''):
    from . import jobs
    view = report(c, product_id, target_id)
    if not view['ready']:
        raise HTTPException(422, view['message'])
    if payload.get('schema_hash') != view['schema_hash']:
        raise HTTPException(409, '官方字段规则已更新，请重新打开')
    kind = 'sku' if payload.get('sku_id') else 'product'
    ident = s.quantity(payload.get('sku_id') or product_id, 1)
    record = s.get(c, kind, ident)
    previous_record=copy.deepcopy(record)
    if kind == 'sku' and record['product_id'] != product_id:
        raise HTTPException(422, 'SKU 不属于该商品')
    if payload.get('revision') != record['revision']:
        raise HTTPException(409, '商品档案已更新，请刷新')
    attrs = payload.get('attributes')
    if not isinstance(attrs, dict) or len(s.dump(attrs)) > 250000:
        raise HTTPException(422, '商品字段格式无效或过大')
    properties = view['schema'].get('properties') or {}
    if any(not is_fact(k) or k not in properties for k in attrs):
        raise HTTPException(422, '请仅在档案维护当前规则中的商品属性；文案、标识、报价及图片由各自来源管理')
    partial = {'$defs': view['schema'].get('$defs', {}), 'type': 'object',
               'properties': {k: properties[k] for k in attrs}}
    invalid = [x for x in jobs.validate_attributes(partial, attrs) if 'is a required property' not in x]
    if invalid:
        raise HTTPException(422, '字段取值不符合官方规则：' + '；'.join(invalid[:5]))
    target = s.get(c, 'target', target_id)
    key = scope_key(target, view['product_type'])
    prior = (record.get('platform_attributes') or {}).get(key) or {}
    cleared = payload.get('cleared_fields', prior.get('cleared_fields', []))
    if not isinstance(cleared, list) or any(not isinstance(x, str) or not is_fact(x) or x not in properties for x in cleared):
        raise HTTPException(422, '清空字段列表无效')
    if kind=='sku':
        effective=next(row['attributes'] for row in view['skus'] if row['sku_id']==ident)
    else:
        effective=values(record,target,view['product_type'],source_attributes.from_sku(record,view['schema'],target['marketplace_id'],locales.resolve(target)))
    edited={name for name,value in attrs.items() if value!=effective.get(name) or name in prior.get('cleared_fields',[])}
    edited.update(name for name in cleared if name not in prior.get('cleared_fields',[]) or name in effective)
    if not edited:
        return record
    s.put(c, 'archive_revision', f"{kind}:{ident}:{record['revision']}",
          {'kind': kind, 'record_id': ident, 'snapshot': record, 'actor': str(actor)})
    merged=copy.deepcopy(prior.get('attributes') or {});merged.update(copy.deepcopy(attrs))
    for field in cleared:merged.pop(field,None)
    record.setdefault('platform_attributes', {})[key] = {'scope': view['scope'], 'schema_hash': view['schema_hash'],
        'attributes': merged, 'cleared_fields': list(dict.fromkeys(cleared)), 'source': 'merchant_archive',
        'registered_by': str(actor)}
    # Physical facts have one canonical value regardless of the editor used.
    canonical={'color':'color','size':'size','material':'material','fabric_type':'fabric',
               'fill_material':'fill_material','number_of_items':'package_quantity',
               'item_package_quantity':'package_quantity','item_shape':'item_shape','pattern':'pattern',
               'item_weight':'item_weight','item_package_weight':'package_weight'}
    changed=set()
    for name,field in canonical.items():
        if name not in edited:continue
        entry=(attrs.get(name) or [{}])[0]
        value=entry.get('value','') if name not in cleared else ''
        if value!='' and entry.get('unit'):value=f"{value} {entry['unit']}"
        put_fact(record,field,{'value':value,'status':'manual' if value!='' else 'cleared','source':{'kind':'merchant_archive','actor':str(actor)}})
        changed.add(field)
    if 'cover' in edited and 'cover' in attrs and attrs['cover']:
        materials=attrs['cover'][0].get('material') or []
        value='; '.join(str(x['value']) for x in materials if x.get('value'))
        put_fact(record,'fabric',{'value':value,'status':'manual','source':{'kind':'merchant_archive','actor':str(actor)}})
        changed.add('fabric')
    if 'cover' in edited and 'cover' in cleared:
        put_fact(record,'fabric',{'value':'','status':'cleared','source':{'kind':'merchant_archive','actor':str(actor)}});changed.add('fabric')
    for name in ('item_length_width','item_dimensions'):
        if name not in edited:continue
        if name in cleared:
            put_fact(record,'size',{'value':'','status':'cleared','source':{'kind':'merchant_archive','actor':str(actor)}})
            changed.add('size');continue
        if name not in attrs or not attrs[name]:continue
        entry=attrs[name][0];parts=[]
        for axis in ('length','width','height'):
            v=entry.get(axis) or {};unit={'inches':'in','centimeters':'cm','millimeters':'mm','meters':'m'}.get(v.get('unit'),v.get('unit',''))
            if 'value' in v:parts.append(f"{v['value']}{unit}")
        if len(parts)>=2:
            value=' × '.join(parts)
            put_fact(record,'size',{'value':value,'status':'manual','source':{'kind':'merchant_archive','actor':str(actor)}})
            changed.add('size')
    invalidate_fact_attributes(record,changed,except_scope=key)
    # The form can send unchanged defaults alongside an edit. Drop old aliases
    # from this scope too, so material/cover or size/dimensions cannot disagree.
    aliases=set().union(*(FACT_ATTRIBUTES.get(field,{field}) for field in changed)) if changed else set()
    for name in aliases-edited:
        record['platform_attributes'][key]['attributes'].pop(name,None)
        record['platform_attributes'][key]['cleared_fields']=[field for field in record['platform_attributes'][key]['cleared_fields'] if field!=name]
    record['fact_revision']=record.get('fact_revision',0)+1
    saved = s.save(c, kind, record, expected=payload['revision'])
    if kind=='product':inherit_product_facts(c,previous_record,saved,changed,actor)
    s.audit(c, 'archive_platform_fields_saved', ident, s.dump({'kind': kind, 'target_id': target_id, 'actor': str(actor)}))
    return saved
