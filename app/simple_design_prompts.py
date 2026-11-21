"""Source facts and history readers shared by the single current design contract.

Model prompts and executable slot planning live only in design_contract.py.
"""
import hashlib
import json
import re

# Authoring guidance only; factual values are never truncated to these budgets.
LIMITS = {'master':9000, 'image':16000, 'request':16000}
NO_PEOPLE_RULES = ('全局无人设计：母稿、首图、销售图和详情图的场景中不得新增或带入人物，'
    '包括脸、头发、手、手臂、其他人体局部、剪影、倒影、人物影子、人形模特或新增人像插画。'
    '即使来源图、当前成品或旧方案有人，也只参考商品，移除场景人物并自然补齐背景；此规则优先于旧方案中的人物展示要求。'
    '商品自身真实印花中的既有人像属于实物纹样，按来源保留在商品表面，不改花、不增画、不移到场景中充当人物；不得篡改真实印花来伪造实物。')
SOURCE_LANGUAGE_RULES = ('材料译名按实际成分和部位表达，不按中文词面猜成分：羽丝/羽丝绒用fiber fill，不能写feather、down、silk或自行断言聚合物；柞蚕丝用tussah silk。'
    '100支是纱支，用100s yarn count，不是100 thread count。其他语言表达相同事实。'
    '来源的国内“常规/标准/通用”适配说法不自动成为目的地标准适配承诺；跨市场只写实测尺寸并让顾客比较，不替换为当地标准码。'
    '源资料、档案、保留原样等内部设计说明不印在买家画面。')


STAGES = {'recognize':'旧版识别（已停用）', 'masters':'多视角产品母稿', 'plans':'整组创意设计', 'images':'设计出图', 'detail':'详情页出图', 'complete':'已完成'}


def dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


SOURCE_ACCESS = re.compile(r'供应商图源|图片链接|网盘链接|下载地址|下载链接|提取码|source_url|source_link|source_file|download_url', re.I)
ADMINISTRATIVE_FACT = re.compile(r'^(?:package_dimensions|package_weight|包装尺寸|包装重量|\d{4}-\d{2}-\d{2}确认)$', re.I)
IMPORT_IMAGE_BOILERPLATE = re.compile(
    r'参考图按原表款式对应保留[，,]\s*含宣传[/／]多色合图[，,]\s*未经实拍及具体规格核验[。.]?')


def clean_brief(text):
    return '\n'.join(line.strip() for line in str(text or '').splitlines()
                     if line.strip() and not re.search(r'cost|成本|供货价|门市价|采购价',line,re.I)
                     and not SOURCE_ACCESS.search(line))


def clean_model_brief(text):
    """Project the archive into new prompts without changing frozen fact digests."""
    return clean_brief(IMPORT_IMAGE_BOILERPLATE.sub('', str(text or '')))


def parse_object(text):
    text=str(text).strip()
    # One local recovery only: extract an object from markdown/prose.
    start=text.find('{')
    if start<0: raise ValueError('模型未返回JSON对象')
    value,_=json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value,dict): raise ValueError('模型结果必须是对象')
    return value


def product_facts(product):
    """Lossless source values, grouped by their visual role; no inferred facts."""
    out = {k: product.get(k, '') for k in ('key', 'name', 'color', 'sku_id')}
    out.update(product_name=product.get('canonical_product_name', ''),
               variant_name=product.get('variant_name') or product.get('name', ''))
    groups = {name: {} for name in ('尺寸与测量条件','材料与对应部位','销售组成','其他产品事实')}
    dimensions = {'size','item_length','item_width','item_height','item_depth','diameter',
                  'item_diameter','natural_height','compressed_height','thickness'}
    materials = {'material','fabric','fill_material','fill_weight','lining','cover_material'}
    composition = {'components','package_quantity','number_of_items','set_contents'}
    for field, record in (product.get('design_facts') or {}).items():
        if not isinstance(record, dict):
            continue
        if (re.search(r'cost|price|成本|价格|供货价', field, re.I) or SOURCE_ACCESS.search(field)
                or ADMINISTRATIVE_FACT.fullmatch(field)):
            continue
        value = {k: record[k] for k in ('value','unit','measurement_condition','scope','status','qualifiers') if k in record}
        evidence = [q['text'] for q in record.get('qualification_evidence', [])
                    if isinstance(q, dict) and isinstance(q.get('text'), str) and not SOURCE_ACCESS.search(q['text'])]
        if evidence:
            value['qualification_evidence'] = list(dict.fromkeys(evidence))
        label = (record.get('source') or {}).get('column')
        if label:
            value['source_field'] = label
        group = ('尺寸与测量条件' if field in dimensions else '材料与对应部位' if field in materials
                 else '销售组成' if field in composition else '其他产品事实')
        groups[group][field] = value
    out['parameters'] = {k:v for k,v in groups.items() if v}
    # Supplier descriptions are real input, unlike historical model recognition.
    notes = []
    for line in clean_brief(product.get('parameters', '')).splitlines():
        match = re.match(r'^(?:description|special_feature|商品描述|产品描述|产品说明)\s*[:：]\s*(.+)', line, re.I)
        if match and match[1] not in notes:
            notes.append(match[1])
    if notes:
        out['source_description'] = notes
    if not product.get('design_facts'):
        out['parameters'] = clean_brief(product.get('parameters', ''))
    return out


def product_parameters(text):
    # SKU identity is supplied separately for each variant; a shared brief may name the primary SKU.
    return '\n'.join(line for line in clean_brief(text).splitlines()
                     if not re.match(r'^(?:SKU名|SKU名称)\s*[:：]',line,re.I))


def source_catalog(snapshot):
    """One master per explicit source variant, with no model call or inferred colors."""
    if snapshot.get('archive_products'):
        return {'products': [dict(p) for p in snapshot['archive_products']],
                'style': snapshot.get('style', ''), 'source': 'product_archive'}
    products=[]
    groups=snapshot.get('known_products') or [{'name':snapshot['name'], 'variants':snapshot.get('variants') or [{}]}]
    for group in groups:
        for variant in group.get('variants') or [{}]:
            products.append({'key':f'p{len(products)+1}',
                'name':str(variant.get('name') or group.get('name') or snapshot['name']),
                'color':str(variant.get('color') or variant.get('label') or ''),
                'parameters':product_parameters(variant.get('parameters') or group.get('parameters') or snapshot['parameters'])})
    origin=snapshot.get('origin') or {}
    if origin.get('status')=='ready':
        for product in products:
            if not re.search(r'(?im)^\s*(?:产地|原产地|原产国|origin(?:_country|_place)?)\s*[:：]',product['parameters']):
                product['parameters']+='\n产地: '+origin['display']
    return {'products':products,'style':snapshot.get('style',''),'source':'product_data'}


def reuse_catalog(catalog, snapshot):
    """Retain master keys while replacing historical AI descriptions with current facts."""
    available=source_catalog(snapshot)['products']
    products=[]
    for old in catalog['products']:
        if old.get('sku_id') is not None:
            matches=[p for p in available if str(p.get('sku_id'))==str(old['sku_id'])]
        elif old.get('variant_key'):
            matches=[p for p in available if p.get('variant_key')==old['variant_key']]
        else:
            matches=[p for p in available if p['name']==old['name'] and p['color']==old['color']]
        if len(matches)!=1:
            raise ValueError('无法唯一匹配当前商品规格，请保留旧图并整组重新设计')
        current=matches[0]
        products.append(dict(current)|{'key':old['key']})
    if len({str(p.get('sku_id', p.get('variant_key', p['key']))) for p in products}) != len(products):
        raise ValueError('多个历史款式映射到同一规格，请整组重新设计')
    return {'products':products,'style':snapshot.get('style',''),'source':'product_data',
            **({'creative_direction':catalog['creative_direction']} if catalog.get('creative_direction') else {})}
