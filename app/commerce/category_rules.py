"""Read official Amazon classification choices and bind them to local mappings."""
from __future__ import annotations
import copy
import hashlib
import re
from datetime import datetime, timezone
from fastapi import HTTPException
from . import amazon, store as s

FIELDS={'recommended_browse_nodes':'category_id','item_type_keyword':'item_type_keyword'}


def scope_key(target,product_type):
    return hashlib.sha256(s.dump({k:target.get(k) for k in ('account_id','seller_id','marketplace_id')}|{'product_type':product_type}).encode()).hexdigest()


def report_for(mapping,target=None):
    if target is None:return mapping.get('official_classification') or {}
    key=scope_key(target,mapping.get('product_type'))
    report=(mapping.get('official_classifications') or {}).get(key) or mapping.get('official_classification') or {}
    if any(report.get(k)!=target.get(k) for k in ('account_id','seller_id','marketplace_id')):return {}
    if report.get('product_type')!=mapping.get('product_type'):return {}
    return report


def selected_for(mapping,target):
    """Display the selected classification for this seller, never the latest other seller."""
    report=report_for(mapping,target)
    selected=report.get('selected_values') or {}
    result={key:'' for key in FIELDS.values()};paths=[]
    for field,key in FIELDS.items():
        value=selected.get(field,'')
        choice=next((option for option in (report.get('fields',{}).get(field) or {}).get('options',[])
                     if option.get('value')==value),None)
        if choice:
            result[key]=value
            if choice.get('path'):paths.append(choice['path'])
    result['category_path']=paths[0] if paths else ''
    return result


def mapping_for_target(mapping,target):
    result=copy.deepcopy(mapping)
    if mapping.get('platform')!='亚马逊':return result
    report=copy.deepcopy(report_for(mapping,target))
    result.update(selected_for(mapping,target))
    result['official_classification']=report
    result['official_classifications']={scope_key(target,mapping.get('product_type')):report} if report else {}
    return result


def walk(node,root,seen=()):
    if isinstance(node,dict):
        ref=node.get('$ref','')
        if ref.startswith('#/') and ref not in seen:
            value=root
            try:
                for part in ref[2:].split('/'):value=value[part.replace('~1','/').replace('~0','~')]
            except (KeyError,TypeError):value=None
            if value is not None:yield from walk(value,root,seen+(ref,))
        yield node
        for key,value in node.items():
            if key not in {'$defs','definitions','not','if','examples','default'}:
                yield from walk(value,root,seen)
    elif isinstance(node,list):
        for value in node:yield from walk(value,root,seen)


def extract(schema):
    if not isinstance(schema,dict) or not isinstance(schema.get('properties'),dict) or not schema['properties']:
        raise HTTPException(422,'官方属性表格式不完整，无法确定分类字段')
    fields={}
    for name in FIELDS:
        definitions=[node['properties'][name] for node in walk(schema,schema)
                     if isinstance(node.get('properties'),dict) and name in node['properties']]
        if not definitions:continue
        choices={}
        for definition in definitions:
            # Only enums under the attribute's value, never marketplace/language enums.
            values=[node['properties']['value'] for node in walk(definition,schema)
                    if isinstance(node.get('properties'),dict) and 'value' in node['properties']]
            for value in values:
                for node in walk(value,schema):
                    names=node.get('enumNames',[])
                    for i,item in enumerate(node.get('enum',[])):
                        if not isinstance(item,(str,int)):continue
                        text=str(item)
                        if name=='recommended_browse_nodes' and not text.isdigit():continue
                        choices.setdefault(text,{'value':text,'path':str(names[i]) if i<len(names) else ''})
        fields[name]={'options':list(choices.values())}
    return fields


def normalized(value):return re.sub(r'\s+',' ',str(value)).strip().casefold()


def select_values(mapping,fields):
    changes={};missing=[];paths=[]
    for field,info in fields.items():
        key=FIELDS[field];options=info['options'];current=mapping.get(key,'')
        chosen=next((o for o in options if o['value']==current),None)
        if not chosen and mapping.get('category_path'):
            exact=[o for o in options if o['path'] and normalized(o['path'])==normalized(mapping['category_path'])]
            if len(exact)==1:chosen=exact[0]
        if not chosen and len(options)==1:chosen=options[0]
        if chosen:
            changes[key]=chosen['value']
            if chosen['path']:paths.append(chosen['path'])
        else:
            changes[key]='';missing.append(field)
    if paths:changes['category_path']=paths[0]
    return changes,missing


def state(mapping,target=None):
    report=report_for(mapping,target)
    status=report.get('status','pending')
    return {'status':status,'complete':status in {'complete','not_required'},
            'message':report.get('message') or '已关联产品类型，官方分类待同步'}


def sync(mapping,target,cache=None):
    cache=cache if cache is not None else {}
    checked=datetime.now(timezone.utc).isoformat(timespec='seconds')
    previous=copy.deepcopy(report_for(mapping,target))
    report=previous|{'checked_at':checked,'status':'unavailable'}
    if target:
        report.update({k:target.get(k) for k in ('account_id','seller_id','marketplace_id')})
        report.update(target_id=target.get('id'),product_type=mapping.get('product_type'))
    changes={}
    try:
        if not target or target.get('environment')=='mock':
            raise HTTPException(422,'尚无可查询的Amazon账号站点；模拟目标不查询官方分类')
        if not mapping.get('product_type'):raise HTTPException(422,'请先关联Amazon产品类型')
        # Explicit read-only official lookup; the saved listing environment stays unchanged.
        official=target|{'environment':'production'}
        key=(target['account_id'],target.get('seller_id'),target['marketplace_id'],mapping['product_type'])
        denied=('denied',target['account_id'],target.get('seller_id'))
        if denied in cache:raise cache[denied]
        if key not in cache:
            try:cache[key]=amazon.definition(official,mapping['product_type'])
            except amazon.PlatformError as exc:
                if exc.status in {401,403}:cache[denied]=exc
                cache[key]=exc
        definition=cache[key]
        if isinstance(definition,Exception):raise definition
        if definition.get('sample_only'):raise HTTPException(422,'沙箱样例不作为官方分类')
        descriptor=definition.get('descriptor') or {}
        if descriptor.get('productType',mapping['product_type'])!=mapping['product_type']:
            raise HTTPException(422,'官方返回的产品类型与当前映射不一致')
        if descriptor.get('marketplaceIds') and target['marketplace_id'] not in descriptor['marketplaceIds']:
            raise HTTPException(422,'官方返回的站点与当前映射不一致')
        fields=extract(definition.get('schema'))
        selected=mapping_for_target(mapping,target)
        if not mapping.get('official_classifications') and not mapping.get('official_classification'):
            selected=mapping  # Explicit shared reference values before any seller API observation.
        changes,missing=select_values(selected,fields)
        for field,key in FIELDS.items():
            if field not in fields:changes[key]=''
        status='not_required' if not fields else ('needs_selection' if missing else 'complete')
        message={'not_required':'此产品类型的官方属性表未声明分类节点或关键词字段',
                 'needs_selection':'已读取官方分类，请选择与商品一致的分类',
                 'complete':'官方分类已补齐'}[status]
        if any(not info['options'] for info in fields.values()):
            status='needs_selection';message='官方属性表未列出全部候选值，需要补充官方分类资料'
        report={'status':status,'message':message,'checked_at':checked,'fields':fields,
                'marketplace_id':target['marketplace_id'],'product_type':mapping['product_type'],
                'account_id':target['account_id'],'target_id':target['id'],
                'seller_id':target.get('seller_id',''),'selected_values':{field:changes.get(key,'') for field,key in FIELDS.items()},
                'request_id':definition.get('request_id',''),'source':'Amazon Product Type Definitions API（正式只读查询）',
                'schema_sha256':hashlib.sha256(s.dump(definition['schema']).encode()).hexdigest()}
    except (amazon.PlatformError,HTTPException) as exc:
        status=exc.status if isinstance(exc,amazon.PlatformError) else exc.status_code
        message=str(exc) if isinstance(exc,amazon.PlatformError) else str(exc.detail)
        if status==403:message='Amazon正式分类接口返回403：当前授权无法读取商品类型定义，请核对Product Listing权限及卖家授权后重试'
        report.update(message=message,http_status=status)
    with s.transaction() as c:
        current=s.get(c,'category_mapping',mapping['id'])
        if current['revision']!=mapping['revision']:raise HTTPException(409,'查询期间映射已更新，请重试')
        comparable=lambda value:{k:v for k,v in value.items() if k not in {'checked_at','request_id','mapping_revision'}}
        if comparable(report)==comparable(previous) and previous.get('mapping_revision'):
            report=previous
        else:
            report['mapping_revision']=current['revision']+1
        updated=current|changes|{'official_classification':report}
        if target:
            updated['official_classifications']=copy.deepcopy(current.get('official_classifications') or {})
            updated['official_classifications'][scope_key(target,mapping.get('product_type'))]=copy.deepcopy(report)
        if isinstance(locals().get('definition'),dict) and definition.get('schema') and not definition.get('sample_only'):
            s.put(c,'definition',f"{target['id']}:{mapping['product_type']}:NONE",definition|{'product_type':mapping['product_type'],'parentage':'NONE','schema':definition['schema']})
        if updated!=current:updated=s.save(c,'category_mapping',updated)
        s.audit(c,'official_classification_sync',mapping['id'],report['status'])
    return updated


def refresh_result(c,result):
    result=copy.deepcopy(result)
    for destination in result.get('destinations',[]):
        mapping=s.get(c,'category_mapping',destination.get('mapping_id'),False) if destination.get('mapping_id') else None
        if mapping:
            target=s.get(c,'target',destination.get('target_id'),False) if destination.get('target_id') else None
            if target and (target.get('account_id')!=destination.get('account_id') or
                           target.get('marketplace_id')!=mapping.get('marketplace_id')):target=None
            if not target:
                account=c.execute('SELECT seller_id FROM platform_accounts WHERE id=?',(destination.get('account_id'),)).fetchone()
                target={'account_id':destination.get('account_id'),'seller_id':account['seller_id'] if account else '',
                        'marketplace_id':mapping.get('marketplace_id')}
            official=state(mapping,target);destination.update(classification_complete=official['complete'] and mapping.get('enabled',False),message=official['message'],
                official_classification=copy.deepcopy(report_for(mapping,target)))
    complete=bool(result.get('destinations')) and all(d.get('classification_complete') for d in result['destinations'])
    result['status']='completed' if complete else 'partial'
    if result.get('destinations'):
        result['message']=f"已关联 {result['matched_platforms']}/{result['platform_count']} 个平台 · "+('官方分类已补齐' if complete else '分类待补充')
    return result


def choose(ident,payload):
    with s.transaction() as c:
        mapping=s.get(c,'category_mapping',ident)
        if payload.get('revision')!=mapping['revision']:raise HTTPException(409,'映射已更新，请重新打开详情')
        requested=payload.get('target_id')
        target=s.get(c,'target',requested) if requested is not None else None
        if target and target.get('marketplace_id')!=mapping.get('marketplace_id'):
            raise HTTPException(409,'所选店铺与分类映射站点不一致')
        if target is None:
            reports=mapping.get('official_classifications') or {}
            scopes={(report.get('account_id'),report.get('seller_id'),report.get('marketplace_id'))
                    for report in reports.values() if report.get('product_type')==mapping.get('product_type')}
            if len(scopes)>1:raise HTTPException(422,'多个店铺已读取分类，请明确选择要维护的店铺')
        report=copy.deepcopy(report_for(mapping,target))
        fields=report.get('fields',{})
        if report.get('status') not in {'complete','needs_selection'} or not fields:
            raise HTTPException(422,'请先同步官方分类候选')
        if report.get('marketplace_id')!=mapping['marketplace_id'] or report.get('product_type')!=mapping['product_type']:
            raise HTTPException(409,'官方候选不属于当前站点或产品类型，请重新同步')
        choices=payload.get('choices',{})
        if not isinstance(choices,dict) or set(choices)!=set(fields):raise HTTPException(422,'请选择全部官方分类字段')
        changes={};paths=[]
        for field,info in fields.items():
            chosen=next((o for o in info['options'] if o['value']==choices[field]),None)
            if not chosen:raise HTTPException(422,'分类值不在当前官方候选中，请重新选择')
            changes[FIELDS[field]]=chosen['value']
            if chosen['path']:paths.append(chosen['path'])
        if paths:changes['category_path']=paths[0]
        report.update(status='complete',message='官方分类已补齐')
        report['selected_values']=copy.deepcopy(choices)
        report['mapping_revision']=mapping['revision']+1
        updated=mapping|changes|{'official_classification':report}
        target=target or s.get(c,'target',report.get('target_id'),False)
        if not target:
            raise HTTPException(409,'原官方分类报告的店铺已不存在，请选择当前店铺重新同步')
        if any(report.get(k)!=target.get(k) for k in ('account_id','seller_id','marketplace_id')):
            raise HTTPException(409,'官方候选的卖家身份已变化，请重新同步')
        if target:
            updated['official_classifications']=copy.deepcopy(mapping.get('official_classifications') or {})
            updated['official_classifications'][scope_key(target,mapping.get('product_type'))]=copy.deepcopy(report)
        if updated!=mapping:updated=s.save(c,'category_mapping',updated)
        s.audit(c,'official_classification_selected',ident,s.dump(choices))
        return updated


def attributes(mapping,target=None):
    """Emit platform field names; only emit fields declared by the retrieved schema."""
    result={};report=report_for(mapping,target)
    if report.get('marketplace_id')!=mapping.get('marketplace_id') or report.get('product_type')!=mapping.get('product_type'):return result
    if report.get('status') not in {'complete','needs_selection'}:return result
    for field,info in report.get('fields',{}).items():
        if field not in FIELDS:continue
        value=(report.get('selected_values') or {}).get(field,'')
        if value and any(o['value']==value for o in info['options']):
            result[field]=[{'value':value,'marketplace_id':mapping['marketplace_id']}]
    return result
