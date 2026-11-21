"""Local categories and reusable platform/marketplace mappings.

Mappings are defaults, never a replacement for listing or order snapshots.
"""
from __future__ import annotations
import copy
import json
import re
import uuid
from pathlib import Path
from fastapi import HTTPException
from . import store as s


def platform_name(value):
    value=str(value or '').strip()
    return '亚马逊' if value.lower() in {'amazon','亚马逊'} else value


def target_platform(c, target):
    account=c.execute('SELECT platform FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
    return platform_name(account[0]) if account else ''


def clean_text(data,key,limit=200,required=False):
    value=data.get(key,'')
    if not isinstance(value,str):raise HTTPException(422,key+'必须为文本')
    value=value.strip()
    if len(value)>limit or (required and not value):raise HTTPException(422,key+'为空或过长')
    return value


def save_category(c,payload,ident=None):
    old=s.get(c,'category',ident) if ident else {}
    if old and payload.get('revision')!=old['revision']:raise HTTPException(409,'分类已更新，请刷新')
    d=old|payload
    row={k:clean_text(d,k,n,k in {'code','name','path'}) for k,n in [('code',80),('name',100),('path',500),('notes',2000)]}
    if not re.fullmatch(r'[A-Za-z0-9_-]+',row['code']):raise HTTPException(422,'本地分类编码只能包含字母、数字、下划线和短横线')
    ids=d.get('business_ids',[])
    if not isinstance(ids,list):raise HTTPException(422,'关联业务必须为列表')
    row['business_ids']=sorted(set(s.quantity(v,1) for v in ids))
    row['enabled']=d.get('enabled',True)
    if not isinstance(row['enabled'],bool):raise HTTPException(422,'启用状态必须为布尔值')
    for bid in row['business_ids']:
        if not c.execute('SELECT 1 FROM businesses WHERE id=?',(bid,)).fetchone():raise HTTPException(422,'关联业务不存在')
    for x in s.all_rows(c,'category'):
        if x['id']==ident:continue
        if x['code'].lower()==row['code'].lower():raise HTTPException(409,'本地分类编码已存在')
        if set(x.get('business_ids',[])) & set(row['business_ids']):raise HTTPException(409,'业务已关联其他分类，请先解除原关联')
    result=s.save(c,'category',old|row,payload['revision']) if old else s.put(c,'category',uuid.uuid4().hex,row)
    s.audit(c,'category_saved',result['id'],row['code'])
    return result


def save_mapping(c,payload,ident=None):
    old=s.get(c,'category_mapping',ident) if ident else {}
    if old and payload.get('revision')!=old['revision']:raise HTTPException(409,'平台映射已更新，请刷新')
    d=old|payload
    row={k:clean_text(d,k,n,k in {'platform','marketplace_id','name'}) for k,n in [
        ('platform',80),('marketplace_id',100),('marketplace_name',100),('name',100),('category_path',500),
        ('category_id',120),('item_type_keyword',200),('product_type',100),('source',2000),('source_date',30),('notes',2000)]}
    row['platform']=platform_name(row['platform'])
    row['category_id_local']=s.quantity(d.get('category_id_local'),1)
    s.get(c,'category',row['category_id_local'])
    if row['platform']=='亚马逊' and row['product_type'] and not re.fullmatch('[A-Z0-9_]{1,100}',row['product_type']):
        raise HTTPException(422,'Amazon产品类型应为平台提供的大写代码')
    if row['platform']=='亚马逊' and row['category_id'] and not row['category_id'].isdigit():
        raise HTTPException(422,'Amazon分类节点ID必须是数字，分类关键词请填写在单独字段')
    row['status']=d.get('status','manual')
    if row['status'] not in {'pending','manual','reference','sample'}:raise HTTPException(422,'分类来源状态无效')
    if row['status'] in {'manual','reference'} and not row['source']:raise HTTPException(422,'请填写分类代码来源或维护依据')
    row['enabled']=d.get('enabled',True)
    if not isinstance(row['enabled'],bool):raise HTTPException(422,'启用状态必须为布尔值')
    row['attributes']=copy.deepcopy(d.get('attributes',{}))
    if not isinstance(row['attributes'],dict) or len(s.dump(row['attributes']))>50000:raise HTTPException(422,'默认属性需要JSON对象，且不超过50000字符')
    for x in s.all_rows(c,'category_mapping'):
        if x['id']!=ident and all(x.get(k)==row[k] for k in ['category_id_local','platform','marketplace_id']):
            raise HTTPException(409,'该本地分类在此平台站点已有映射，请编辑原记录')
    # API provenance is server-owned, never accepted from imports or generic edits.
    if old.get('official_classification') and any(old.get(k,'')!=row.get(k,'') for k in
            ['platform','marketplace_id','product_type','category_id','item_type_keyword','category_path']):
        old=old|{'official_classification':{'status':'pending','message':'分类配置已变化，请重新同步官方分类'}}
    result=s.save(c,'category_mapping',old|row,payload['revision']) if old else s.put(c,'category_mapping',uuid.uuid4().hex,row)
    s.audit(c,'category_mapping_saved',result['id'],row['platform']+':'+row['marketplace_id'])
    return result


def product_category(c,product,category_rows=None):
    if product.get('category_id'):
        if category_rows is not None:
            return next((x for x in category_rows if x['id']==product['category_id']),None)
        return s.get(c,'category',product['category_id'],False)
    choices=[x for x in (s.all_rows(c,'category') if category_rows is None else category_rows)
             if product.get('business_id') in x.get('business_ids',[])]
    if len(choices)==1:return choices[0]
    link=s.by_key(c,'product_category_link',str(product.get('id')))
    return s.get(c,'category',link['category_id'],False) if link else None


def resolve(c,product,target,*,category_rows=None,mapping_rows=None,platform=None):
    category=product_category(c,product,category_rows)
    if not category or not category.get('enabled'):return {'category':category,'mapping':None,'message':'请在商品档案或分类维护中设置启用的本地分类'}
    if platform is None:platform=target_platform(c,target)
    rows=[x for x in (s.all_rows(c,'category_mapping') if mapping_rows is None else mapping_rows)
          if x['category_id_local']==category['id'] and
          x['platform']==platform and x['marketplace_id']==target['marketplace_id'] and x.get('enabled')]
    mapping=rows[0] if len(rows)==1 else None
    if not mapping:return {'category':category,'mapping':None,'message':'此分类尚未维护该平台站点的映射'}
    if mapping['status']=='sample':return {'category':category,'mapping':None,'message':'沙箱样例不作为真实商品分类默认值'}
    if product.get('product_type_source',{}).get('kind')=='manual' and product.get('product_type') and product['product_type']!=mapping['product_type']:
        return {'category':category,'mapping':None,'message':'商品独立指定了商品类型，请读取该类型官方字段并维护对应分类'}
    from . import category_rules
    mapping=category_rules.mapping_for_target(mapping,target)
    return {'category':category,'mapping':mapping,'message':'来自本地分类资料；平台最新要求以站点返回为准'}


def apply_defaults(c,product,target,draft):
    found=resolve(c,product,target);mapping=found['mapping']
    if not mapping:return found
    from . import category_rules
    automatic=category_rules.attributes(mapping,target) if mapping['platform']=='亚马逊' else {}
    previous=draft.get('classification') or {}
    report=category_rules.report_for(mapping,target)
    existing=copy.deepcopy(draft.get('attributes',{}))
    for key,value in previous.get('mapped_attributes',{}).items():
        if existing.get(key)==value:existing.pop(key,None)
    draft['classification']={'category_id':found['category']['id'],'category_name':found['category']['path'],
        'mapping_id':mapping['id'],'mapping_revision':report.get('mapping_revision',mapping['revision']),'platform':mapping['platform'],
        'marketplace_id':mapping['marketplace_id'],'category_id_platform':mapping['category_id'],
        'category_path':mapping['category_path'],'product_type':mapping['product_type'],
        'source':mapping['source'],'source_date':mapping['source_date'],'status':mapping['status'],
        'item_type_keyword':mapping.get('item_type_keyword',''),
        'official_status':category_rules.state(mapping,target), 'mapped_attributes':automatic,
        'official_source':category_rules.report_for(mapping,target).get('source','')}
    draft['product_type']=mapping['product_type']
    draft['platform_category_id']=mapping['category_id']
    draft['platform_category_path']=mapping['category_path']
    draft['attributes']={**copy.deepcopy(mapping['attributes']),**automatic,**existing}
    return found


def export_data(c):
    categories=s.all_rows(c,'category');codes={x['id']:x['code'] for x in categories}
    clean=lambda x:{k:v for k,v in x.items() if k not in {'id','revision','created_at','updated_at'}}
    return {'format':'commerce-categories-v1','categories':[clean(x) for x in categories],
        'mappings':[{**{k:v for k,v in clean(x).items() if k!='category_id_local'},'category_code':codes[x['category_id_local']]}
                    for x in s.all_rows(c,'category_mapping')]}


def import_data(payload,preview=False):
    if payload.get('format')!='commerce-categories-v1':raise HTTPException(422,'分类文件格式无效，请使用导出文件或模板')
    cats=payload.get('categories',[]);maps=payload.get('mappings',[])
    if not isinstance(cats,list) or not isinstance(maps,list) or len(cats)+len(maps)>3000:raise HTTPException(422,'一次最多导入3000条分类和映射')
    if not all(isinstance(x,dict) for x in cats+maps):raise HTTPException(422,'每条分类或映射必须为对象')
    # Imports add records; duplicates fail atomically instead of overwriting maintained data.
    with s.transaction() as c:
        for x in cats:save_category(c,x)
        codes={x['code']:x['id'] for x in s.all_rows(c,'category')}
        for x in maps:
            if x.get('category_code') not in codes:raise HTTPException(422,'映射引用的本地分类不存在')
            save_mapping(c,x|{'category_id_local':codes[x['category_code']]})
        if preview:c.rollback()
    return {'categories':len(cats),'mappings':len(maps),'preview':preview}


def seed_existing():
    """Explicit, repeatable migration of the user's existing home-textile businesses."""
    source=json.loads((Path(__file__).with_name('category_seed.json')).read_text(encoding='utf-8'))
    counts={'categories':0,'mappings':0,'products':0}
    with s.transaction() as c:
        businesses=[dict(x) for x in c.execute('SELECT id,name FROM businesses')]
        for entry in source['categories']:
            bids=[x['id'] for x in businesses if x['name']==entry['business_name']]
            if not bids:continue
            existing=s.all_rows(c,'category')
            category=next((x for x in existing if x['code']==entry['code']),None)
            if not category:
                if any(set(x.get('business_ids',[])) & set(bids) for x in existing):continue
                category=save_category(c,{'code':entry['code'],'name':entry['name'],'path':entry['path'],
                    'business_ids':bids,'notes':entry.get('notes',''),'enabled':True});counts['categories']+=1
            old=next((x for x in s.all_rows(c,'category_mapping') if x['category_id_local']==category['id'] and
                      x['platform']=='亚马逊' and x['marketplace_id']=='ATVPDKIKX0DER'),None)
            if not old:
                save_mapping(c,{'category_id_local':category['id'],'platform':'亚马逊','marketplace_id':'ATVPDKIKX0DER',
                    'marketplace_name':'美国站','name':entry['name'],'product_type':entry['product_type'],
                    'category_path':entry['category_path'],'source':source['source'],'source_date':source['source_date'],
                    'status':'reference','notes':entry.get('notes','')+' 类型代码来自官方历史清单，业务映射为本地整理，待目标站点验证。'})
                counts['mappings']+=1
        for product in s.all_rows(c,'product'):
            if not product.get('category_id'):
                category=product_category(c,product)
                if category:product['category_id']=category['id'];s.save(c,'product',product);counts['products']+=1
        s.audit(c,'category_seed',None,s.dump(counts))
    return counts
