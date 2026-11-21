"""Build destination variation relationships from real, uniquely distinguishable SKUs."""
import copy
import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from . import store as s, amazon, locales

PROTOCOL = 'source-backed-variation-family-v3'


def battery_declaration(draft):
    value=(draft.get('source') or {}).get('battery_declaration') or {}
    ids=sorted(row['sku_id'] for row in draft.get('skus',[]))
    if not (value.get('kind')=='merchant_all_skus_no_battery' and
            value.get('sku_ids')==ids and value.get('batteries_required') is False):return None
    for attributes in [draft.get('attributes',{}),draft.get('parent_attributes',{}),
                       *(row.get('attributes',{}) for row in draft.get('skus',[]))]:
        if any(row.get('value') is True for row in attributes.get('batteries_required',[]) if isinstance(row,dict)):
            raise HTTPException(409,'无需电池的商家声明与已有需要电池事实冲突，请核对商品档案')
    return value


def declare_no_battery(ident, revision, sku_ids, actor=''):
    from . import catalog,gallery_assembly
    with s.transaction() as c:
        current=s.get(c,'listing',ident);target=s.get(c,'target',current['target_id'])
        if current['revision']!=revision:raise HTTPException(409,'资料已变化，请刷新后确认电池事实')
        gallery_assembly._available(c,current,target)
        draft=copy.deepcopy(current['draft'])
        actual=sorted(row['sku_id'] for row in draft['skus'])
        if sorted(sku_ids)!=actual:raise HTTPException(409,'确认范围必须对应本商品的全部实际规格')
        if battery_declaration(draft):return current
        for attributes in [draft.get('attributes',{}),draft.get('parent_attributes',{}),
                           *(row.get('attributes',{}) for row in draft['skus'])]:
            if any(row.get('value') is True for row in attributes.get('batteries_required',[]) if isinstance(row,dict)):
                raise HTTPException(409,'已有需要电池的明确事实，请先在商品档案核对冲突')
        declaration={'kind':'merchant_all_skus_no_battery','batteries_required':False,
                     'product_id':current['product_id'],'sku_ids':actual,'actor':actor,
                     'confirmed_at':time.time(),'statement':'商家确认本商品全部实际规格均无需电池'}
        draft.setdefault('source',{})['battery_declaration']=declaration
        current.update(draft=draft,draft_version=current['draft_version']+1)
        current.pop('preview_results',None)
        current['issues']=catalog.issues(draft,target)
        current['draft_status']='needs_fields' if current['issues'] else 'ready'
        s.put(c,'material_version',f"{ident}:{current['draft_version']}",
              {'listing_id':ident,'version':current['draft_version'],'draft':draft})
        s.audit(c,'listing_battery_declaration',ident,s.dump(declaration))
        return s.save(c,'listing',current)


def active_themes(schema):
    name=(schema.get('properties',{}).get('variation_theme',{}).get('items',{})
          .get('properties',{}).get('name',{}))
    return set(name.get('enum',[]))-set(name.get('$lifecycle',{}).get('enumDeprecated',[]))


def _explicit_dimension(sku, field, target, language):
    """Keep the stored typed value/unit; never guess a new category's facts."""
    attributes=dict(sku.get('attributes',{}))
    attributes.update(sku.get('variation_attributes',{}))
    if field not in attributes:return None
    entries=attributes[field]
    if not isinstance(entries,list):return []
    own=[row for row in entries if isinstance(row,dict)
         and row.get('marketplace_id',target['marketplace_id'])==target['marketplace_id']
         and row.get('language_tag',language)==language]
    if len(own)!=1 or own[0].get('value') in (None,''):return []
    return copy.deepcopy(own)


def _components_name(components, language):
    # Vocabulary is only a translation fallback. An explicit destination
    # set_name takes precedence; unknown source wording remains unresolved.
    translations={'枕芯':'Pillow','枕头':'Pillow','枕套':'Pillowcase',
                  '床单':'Sheet','床笠':'Fitted sheet','被套':'Duvet cover',
                  '被芯':'Duvet insert','毛巾':'Towel','浴巾':'Bath towel',
                  '毯子':'Blanket','靠垫':'Cushion','靠垫套':'Cushion cover'}
    parts=[]
    for row in components:
        if not isinstance(row,dict) or type(row.get('quantity')) is not int or row['quantity']<=0:return ''
        name=str(row.get('name') or '').strip()
        if language.startswith('en'):
            name=translations.get(name,name if re.fullmatch(r'[A-Za-z][A-Za-z -]*',name) else '')
        elif not language.startswith('zh'):return ''
        if not name:return ''
        parts.append(f"{row['quantity']} {name}")
    return ' + '.join(parts)


def sales_components(source):
    """Read the SKU's template composition, apart from its production BOM."""
    from .product_archive import field_value
    value, status = field_value(source, 'components')
    if status == 'missing':
        return None
    if status == 'not_applicable':
        return []
    if status in {'cleared', 'conflict'} or not isinstance(value, str):
        raise ValueError('随售组成缺少明确来源')
    parts = []
    for text in re.split(r'[；;\n]+', value.strip()):
        match = re.fullmatch(r'(.+?)\s*[×xX*]\s*([1-9][0-9]*)', text.strip())
        if not match:
            raise ValueError('随售组成须按模板填写名称和数量，如枕芯 × 1；枕套 × 1')
        parts.append({'name': match[1], 'quantity': int(match[2])})
    return parts


def _dimension_identity(row, field):
    encoded=row.get('encoded_dimensions',{}).get(field)
    value=({k:v for k,v in encoded[0].items() if k not in ('marketplace_id','language_tag')}
           if encoded else {'value':row.get(field,'')})
    value=copy.deepcopy(value)
    factors={'millimeters':('mm','1'),'mm':('mm','1'),'centimeters':('mm','10'),'cm':('mm','10'),
             'meters':('mm','1000'),'m':('mm','1000'),'inches':('mm','25.4'),'in':('mm','25.4'),
             'grams':('g','1'),'g':('g','1'),'kilograms':('g','1000'),'kg':('g','1000')}
    unit=factors.get(str(value.get('unit','')).casefold())
    number=value.get('value')
    try:
        if unit or (isinstance(number,(int,float)) and not isinstance(number,bool)):
            decimal=Decimal(str(number))*(Decimal(unit[1]) if unit else 1)
            if decimal.is_finite():
                value['value']=format(decimal.normalize(),'f')
                if unit:value['unit']=unit[0]
    except (InvalidOperation,ValueError):pass
    if field=='size' and isinstance(value.get('value'),str):
        from .source_sizes import normalize
        # Normalize only a complete measurement, retaining every source qualifier.
        if re.fullmatch(r'\s*\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?(?:\s*[*×xX]\s*\d+(?:\.\d+)?)?\s*(?:cm|mm|m|inches|inch|in|厘米|毫米|米|英寸)\s*',value['value'],re.I):
            value['value']=normalize(value['value'])
    return json.dumps(value,sort_keys=True,ensure_ascii=False).casefold()


def proposal(draft, sources, schema, target):
    """No invented style, manufacturer model, firmness or numeric thickness."""
    skus=draft.get('skus',[])
    if len(skus)<2:return {'protocol':PROTOCOL,'status':'independent','reason':'单规格商品无需建立父子关系'}
    from .listing_identity import offer_only
    if any(offer_only(sku) for sku in skus):
        return {'status':'needs_action','reason':'已有ASIN报价模式不自动重组目录家族'}
    language=locales.resolve(target)
    supported=active_themes(schema)
    dimensions={'size','color','set_name'}|{part.lower() for theme in supported for part in theme.split('/')}
    from . import variant_semantics
    if any(not any(source.get('id')==sku['sku_id'] for source in sources) for sku in skus):
        return {'status':'needs_action','reason':'缺少本次真实规格来源'}
    ordered=[next(source for source in sources if source.get('id')==sku['sku_id']) for sku in skus]
    heights=variant_semantics.height_sizes(ordered,skus,language)
    try:
        source_parts = [sales_components(source) for source in ordered]
    except ValueError as exc:
        return {'status': 'needs_action', 'reason': str(exc)}
    has_sale_evidence = any(parts is not None for parts in source_parts)
    rows=[]
    for sku, source, parts in zip(skus, ordered, source_parts):
        size=heights.get(sku['sku_id'],str(sku.get('size') or '').strip())
        components = (parts or []) if has_sale_evidence else (source.get('components') or [])
        row={'sku_id':sku['sku_id'],'size':size,'color':str(sku.get('color') or '').strip(),
             'set_name':_components_name(components,language),'encoded_dimensions':{},
             'basis':{'source_name':source.get('name'),'source_size':source.get('size'),
                      'source_components':copy.deepcopy(components)}}
        for field in dimensions:
            explicit=_explicit_dimension(sku,field,target,language)
            # The ordinary size/color edit controls are canonical for those
            # two dimensions. Category-specific dimensions retain typed data.
            if field in ('size','color') and row[field]:continue
            if explicit is not None:
                row[field]=explicit[0]['value'] if explicit else ''
                if explicit:row['encoded_dimensions'][field]=explicit
            else:row.setdefault(field,'')
            if field in {'number_of_items','item_package_quantity'} and not row[field] and explicit is None:
                count=variant_semantics.fact(source,'number_of_items')
                if re.fullmatch(r'[1-9][0-9]*',str(count or '')):
                    row[field]=int(count)
                    row['encoded_dimensions'][field]=[{'value':int(count),'marketplace_id':target['marketplace_id']}]
        rows.append(row)
    identity=variant_semantics.analysis(ordered,skus,dimensions)
    base={'protocol':PROTOCOL,'rows':rows,'identity':identity,
          'schema_sha256':hashlib.sha256(json.dumps(schema,sort_keys=True).encode()).hexdigest()}
    if has_sale_evidence and any(parts is None for parts in source_parts):
        return base | {'status': 'needs_action', 'reason': '部分规格的随售组成缺少明确来源，请按实际规格核对'}
    if identity.get('unresolved_names') and not identity['distinct_products']:
        return base|{'status':'needs_action','reason':'规格名称差异尚未确定含义，保留实际规格，不自动拆成多个商品'}
    if identity['distinct_products']:
        groups=[]
        for ids in identity['groups']:
            group=copy.deepcopy(draft)
            group['skus']=[sku for sku in skus if sku['sku_id'] in ids]
            group.pop('variation_theme',None);group.pop('parent_sku',None)
            own=proposal(group,[source for source in ordered if source['id'] in ids],schema,target)
            groups.append({'sku_ids':ids,**own})
        if any(len(group['sku_ids'])>1 for group in groups):
            return base|{'status':'needs_action','groups':groups,
                         'reason':'来源中包含不同商品，各组规格方案已分别保留；不能把组内规格全部拆成独立商品'}
        return base|{'status':'independent','reason':'含不同款式、结构或功能的商品，按实际SKU分别上架：'+'；'.join(identity['reasons'])}
    # Every changing supported fact must be represented; do not hide set contents
    # under a color/size label or silently omit otherwise identical children.
    changing=sorted(key for key in dimensions if len({_dimension_identity(row,key) for row in rows})>1)
    component_sets={json.dumps(row['basis']['source_components'],sort_keys=True,ensure_ascii=False) for row in rows}
    # Packs of the same item can use the official numeric count theme; do not
    # require a redundant SET_NAME axis as well. Mixed contents still differ.
    count_covers_components=False
    for field in ('number_of_items','item_package_quantity'):
        components=[row['basis']['source_components'] for row in rows]
        same_item=all(len(parts)==1 for parts in components) and len({parts[0].get('name') for parts in components})==1
        if same_item and field in changing and all(row.get(field)==parts[0].get('quantity') for row,parts in zip(rows,components)):
            count_covers_components=True
            if 'set_name' in changing:changing.remove('set_name')
            break
    if len(component_sets)>1 and ('set_name' not in changing or any(not row['set_name'] for row in rows)):
        if not count_covers_components:
            return base|{'status':'independent','reason':'销售组成不同且没有完整的套装属性表达，分别上架，不能只按尺寸或颜色合并'}
    if not changing or any(row[key] in (None,'') for key in changing for row in rows):
        return {'status':'needs_action','reason':'现有真实属性不足以唯一表达全部规格，需补齐实际差异'}
    choices=[]
    for theme in supported:
        fields=[value.lower() for value in theme.split('/')]
        if set(fields)!=set(changing):continue
        values=[tuple(_dimension_identity(row,field) for field in fields) for row in rows]
        if len(set(values))==len(rows):choices.append(theme)
    if not choices:return base|{'status':'independent','reason':'当前类目没有完整表达这些差异的有效主题，按实际SKU分别上架','dimensions':changing}
    theme=draft.get('variation_theme') if draft.get('variation_theme') in choices else sorted(choices,key=lambda value:(len(value),value))[0]
    return base|{'status':'ready','theme':theme,'dimensions':changing,
                 'reason':'全部真实差异有当前类目主题对应，且每个选项可唯一识别'}


def apply(draft, plan, target, product_id):
    if plan.get('status')=='independent':
        draft.pop('variation_theme',None);draft.pop('parent_sku',None)
        for sku in draft.get('skus',[]):sku.pop('variation_attributes',None)
        draft.setdefault('source',{})['variation_family']=copy.deepcopy(plan)
        return
    if plan.get('status')!='ready':return
    mid=target['marketplace_id'];language=locales.resolve(target)
    draft['variation_theme']=plan['theme']
    draft['parent_sku']=draft.get('parent_sku') or f'W{product_id:05d}-PARENT'
    for sku in draft['skus']:
        row=next(row for row in plan['rows'] if row['sku_id']==sku['sku_id'])
        sku['variation_attributes']={field.lower():copy.deepcopy(row.get('encoded_dimensions',{}).get(field.lower()))
            or [{'value':row[field.lower()],'language_tag':language,'marketplace_id':mid}]
            for field in plan['theme'].split('/')}
    draft.setdefault('source',{})['variation_family']=copy.deepcopy(plan)


def assert_compatible(draft, sources, schema, target):
    """A hand-entered or legacy theme cannot hide another product's identity."""
    if not draft.get('variation_theme') and not draft.get('source',{}).get('variation_family'):
        return
    plan=proposal(draft,sources,schema,target)
    if not draft.get('variation_theme') and plan.get('status')=='independent':
        return
    if plan.get('status')!='ready' or plan.get('theme')!=draft.get('variation_theme'):
        raise HTTPException(422,'商品分组需要更新：'+plan.get('reason','实际差异与规格主题不一致'))
    fields=[field.lower() for field in plan['theme'].split('/')]
    for sku in draft['skus']:
        expected=next(row for row in plan['rows'] if row['sku_id']==sku['sku_id'])
        encoded={key:value for key,value in (sku.get('attributes') or {}).items() if key not in {'size','color'}}
        encoded.update(sku.get('variation_attributes') or {})
        actual={**sku,'encoded_dimensions':encoded}
        if any(_dimension_identity(actual,field)!=_dimension_identity(expected,field) for field in fields):
            raise HTTPException(422,'规格差异尚未完整写入上架选项，请先按来源重新整理分组；不会修改已完成设计')


def preview(ident):
    """Read-only plan from this seller's scoped definitions; no model jobs."""
    from . import variant_semantics
    with s.main().db() as c:
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        draft=listing.get('draft') or {};product=s.get(c,'product',listing['product_id'])
        sources=[s.get(c,'sku',row['sku_id']) for row in draft.get('skus',[])]
        definitions={label:s.by_key(c,'definition',f"{target['id']}:{draft.get('product_type')}:{label}")
                     for label in ('CHILD','PARENT')}
    for label,definition in list(definitions.items()):
        if (not amazon.definition_matches(definition,target,draft.get('product_type'),label)
                or time.time()-definition.get('fetched_at',0)>86400):
            definition=amazon.definition(target,draft.get('product_type'),label)
        amazon.assert_definition_scope(definition,target,draft.get('product_type'),label)
        definitions[label]=definition
    schema=copy.deepcopy(definitions['CHILD']['schema'])
    common=active_themes(schema)&active_themes(definitions['PARENT']['schema'])
    schema.setdefault('properties',{})['variation_theme']={'items':{'properties':{'name':{'enum':sorted(common)}}}}
    plan=proposal(draft,sources,schema,target)
    change=bool(listing.get('published_version') and (draft.get('variation_theme') or '')!=(plan.get('theme') or ''))
    return {'listing_id':ident,'revision':listing['revision'],'plan':plan,
            'source_variants':variant_semantics.display(product,sources,draft),
            'current_theme':draft.get('variation_theme') or '',
            'published_version':listing.get('published_version'),
            'published_relationship_change':change,'allowed_themes':sorted(common),
            'note':'仅预览，不修改档案、图片或亚马逊；已发布关系需要另外处理并回读。'}


def standalone_receipt(listing, target, now=None):
    """Require fresh, explicit relationship readback for every unchanged identity."""
    now=time.time() if now is None else now
    checked=listing.get('checked_at')
    if not isinstance(checked,(int,float)) or not now-900<=checked<=now+5:return None
    draft=listing.get('draft') or {};scope=(draft.get('source') or {}).get('target_scope') or {}
    for field,target_field in (('target_id','id'),('account_id','account_id'),('seller_id','seller_id'),
                               ('marketplace_id','marketplace_id'),('environment','environment')):
        if not scope.get(field) or scope[field]!=target.get(target_field):return None
    if listing.get('target_id')!=target['id'] or not draft.get('skus'):return None
    receipts=[]
    for sku in draft['skus']:
        name=sku['seller_sku'];observed=(listing.get('platform_observed') or {}).get(name) or {}
        response=observed.get('response') or {};body=response.get('body') or {}
        if (observed.get('identity_status')!='confirmed' or response.get('http_status')!=200
                or not response.get('request_id') or body.get('sku')!=name):return None
        summaries=[row for row in body.get('summaries',[]) if row.get('marketplaceId')==target['marketplace_id']]
        asin=observed.get('confirmed_asin')
        if not asin or {row.get('asin') for row in summaries}!={asin}:return None
        relations=body.get('relationships')
        if not isinstance(relations,list):return None
        sites=[row for row in relations if isinstance(row,dict) and row.get('marketplaceId')==target['marketplace_id']]
        if relations and not sites:return None
        for site in sites:
            values=site.get('relationships')
            if not isinstance(values,list) or any(not isinstance(row,dict) or row.get('type')=='VARIATION' for row in values):return None
        receipts.append({'seller_sku':name,'asin':asin,'request_id':response['request_id']})
    return {'status':'confirmed_removed','checked_at':checked,'marketplace_id':target['marketplace_id'],
            'receipts':receipts}


def prepare(ident, revision, actor=''):
    from . import catalog, gallery_assembly
    with s.main().db() as c:
        listing=s.get(c,'listing',ident);target=s.get(c,'target',listing['target_id'])
        if listing['revision']!=revision:raise HTTPException(409,'资料已变化，请刷新后建立规格关系')
        gallery_assembly._available(c,listing,target)
        sources=[s.get(c,'sku',row['sku_id']) for row in listing['draft']['skus']]
    definition=amazon.definition(target,listing['draft']['product_type'],'CHILD')
    amazon.assert_definition_scope(definition,target,listing['draft']['product_type'],'CHILD')
    parent=amazon.definition(target,listing['draft']['product_type'],'PARENT')
    amazon.assert_definition_scope(parent,target,listing['draft']['product_type'],'PARENT')
    schema=copy.deepcopy(definition['schema'])
    allowed=active_themes(schema)&active_themes(parent['schema'])
    schema.setdefault('properties',{})['variation_theme']={'items':{'properties':{'name':{'enum':sorted(allowed)}}}}
    plan=proposal(listing['draft'],sources,schema,target)
    if plan.get('status') not in {'ready','independent'}:raise HTTPException(422,plan.get('reason','无法建立规格关系'))
    previous_change=((listing['draft'].get('source') or {}).get('variation_family') or {}).get('existing_relationship_change')
    if previous_change:
        plan['existing_relationship_change']=copy.deepcopy(previous_change)
    elif listing.get('published_version') and (listing['draft'].get('variation_theme') or '')!=(plan.get('theme') or ''):
        plan['existing_relationship_change']={'parent_sku':listing['draft'].get('parent_sku'),
            'previous_theme':listing['draft'].get('variation_theme'),'published_version':listing['published_version'],
            'status':'requires_platform_change','reason':'已发布父子关系不能靠省略字段解除，须单独处理并回读'}
    if plan.get('status')=='independent' and plan.get('existing_relationship_change'):
        receipt=standalone_receipt(listing,target)
        if receipt:
            plan['existing_relationship_change'].update(receipt,reason='全部实际SKU已按相同商品身份回读，无变体合并关系')
    draft=copy.deepcopy(listing['draft']);apply(draft,plan,target,listing['product_id'])
    with s.transaction() as c:
        current=s.get(c,'listing',ident)
        if current['revision']!=revision:raise HTTPException(409,'查询平台规则期间资料已变化，请刷新')
        gallery_assembly._available(c,current,target)
        for label,value in (('CHILD',definition),('PARENT',parent)):
            s.put(c,'definition',f"{target['id']}:{draft['product_type']}:{label}",value|{'target_id':target['id']})
        if current['draft']==draft:return current
        current.update(draft=draft,draft_version=current['draft_version']+1)
        current.pop('preview_results',None)
        current['issues']=catalog.issues(draft,target)
        current['draft_status']='needs_fields' if current['issues'] else 'ready'
        s.put(c,'material_version',f"{ident}:{current['draft_version']}",{'listing_id':ident,'version':current['draft_version'],'draft':draft})
        result=s.save(c,'listing',current)
        s.audit(c,'variation_family_prepared',ident,s.dump({'actor':actor,'plan':plan,'version':current['draft_version']}))
        return result
