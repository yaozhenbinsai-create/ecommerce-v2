"""Preserve source column semantics when several columns share one mapped field."""
from __future__ import annotations

import re
import json

from fastapi import HTTPException


def brand_from_sheet(snapshot,mappings,sheet_name,row_number,default_brand=''):
    """Use explicit brand cells or a user-specified batch default, never a supplier name."""
    from app.supplier_variants import region_for
    region=region_for(snapshot,mappings,sheet_name,row_number)
    rows=region['data_rows'] if region else {}
    fields=[]
    if rows and row_number in rows:
        header_row=region['header_row'];headers=region['headers'];mappings=region['mappings']
        aliases={'brand','品牌','产品品牌','商品品牌','品牌名称'}
        columns={m.get('source_column') for m in mappings if m.get('standard_field')=='brand'}
        columns.update(h for h in headers if re.sub(r'\s+','',h).casefold() in aliases)
        family=next((headers[m['source_column']] for m in mappings if m.get('standard_field')=='product_name' and m.get('source_column') in headers),None)
        if family is None:family=next((i for h,i in headers.items() if h in {'品名','商品名称','产品名称'}),None)
        def cell(row,column):return str(rows.get(row,[])[column] or '').strip() if column<len(rows.get(row,[])) else ''
        start=row_number
        if family is not None:
            while start>header_row+1 and not cell(start,family):start-=1
        end=row_number
        if family is not None:
            while end<max(rows) and not cell(end+1,family):end+=1
        for row in range(start,end+1):
            for column in columns:
                if column not in headers:continue
                value=cell(row,headers[column])
                if value and value not in {'-','—','暂无','待补充','未填写'}:
                    fields.append({'value':value,'column':column,'row':row,'sheet':sheet_name})
        unique={x['value'].casefold():x for x in fields}
        if len(unique)>1:return {'value':'','kind':'conflict','description':'同一商品的品牌信息不一致，请核对原始品牌列'}
        if unique:
            result=next(iter(unique.values()))
            normalized=re.sub(r'\s+','',result['value']).casefold()
            if normalized in {'无品牌','无品牌商品','generic'}:
                return result|{'value':'generic','kind':'source_unbranded',
                               'description':f"原始文件 {sheet_name}!R{result['row']} 声明无品牌；仍需以实物和包装确认无品牌LOGO"}
            if normalized in {'仅logo','只有logo','有logo品牌不明','logo品牌不明'}:
                return result|{'value':'','kind':'source_logo_only',
                               'description':f"原始文件 {sheet_name}!R{result['row']} 仅提供LOGO线索，商品品牌待确认"}
            if len(result['value'])<=120:return result|{'kind':'source_column','description':f"原始文件 {sheet_name}!R{result['row']} · {result['column']}"}
            return {'value':'','kind':'conflict','description':'原始品牌字段超过120字，请核对'}
    if default_brand.strip():return {'value':default_brand.strip(),'kind':'import_default','description':'导入批次中明确设置的默认品牌'}
    return {'value':'','kind':'missing','description':'原始资料尚未提供明确品牌'}


def effective_batch_brand(c,import_id,default_brand):
    """A supplier name retained in an old batch must not become every item's brand."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone():
        return default_brand
    row=c.execute("SELECT data FROM commerce_records WHERE kind='import_brand_policy' AND natural_key=?",
                  ('import:'+str(import_id),)).fetchone()
    policy=json.loads(row[0]) if row else {}
    if (policy.get('status')=='supplier_only' and
            str(policy.get('source_name') or '').casefold()==str(default_brand or '').casefold()):
        return ''
    return default_brand


def confirmed_product_brand(c,project,source):
    if (not project.get('id') or not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone()):
        return source
    row=c.execute("SELECT data FROM commerce_records WHERE kind='product_brand' AND natural_key=?",
                  ('project:'+str(project['id']),)).fetchone()
    declared=json.loads(row[0]) if row else {}
    if (declared.get('status')=='confirmed' and declared.get('brand') and
            declared.get('platform')==project.get('platform')):
        return {'value':declared['brand'],'kind':'owner_product_brand',
                'description':'商家确认的此商品品牌','project_id':project['id'],
                'source_evidence':source}
    if declared.get('platform')==project.get('platform'):
        if declared.get('status')=='unbranded':
            return {'value':'generic','kind':'owner_product_unbranded',
                    'description':'商家确认商品及包装均无可识别品牌或LOGO',
                    'project_id':project['id'],'source_evidence':source}
        if declared.get('status') in {'logo_only','unknown'}:
            return {'value':'','kind':'owner_product_'+declared['status'],
                    'description':'仅有LOGO，品牌待确认' if declared['status']=='logo_only' else '商品品牌待确认',
                    'visible_mark':declared.get('visible_mark',''),
                    'project_id':project['id'],'source_evidence':source}
    return source


def project_brand(project,c=None):
    from app import main
    if c is None:
        with main.db() as conn:return project_brand(project,conn)
    match=re.search(r'导入#(\d+).*?·\s*([^·]+)!R(\d+)\s*$',str(project.get('source_file') or ''))
    if match:
        row=c.execute('SELECT * FROM supplier_imports WHERE id=?',(int(match.group(1)),)).fetchone()
        if row:
            imported=dict(row)
            default_brand=effective_batch_brand(c,int(match.group(1)),imported.get('default_brand',''))
            found=brand_from_sheet(json.loads(imported.get('workbook_snapshot') or '{}'),json.loads(imported.get('field_mapping') or '[]'),
                                   match.group(2).strip(),int(match.group(3)),default_brand)
            found['import_id']=int(match.group(1))
            if default_brand != imported.get('default_brand','') and found['kind']=='missing':
                found['kind']='supplier_only_default'
                found['description']='旧批次默认值是供应商名称，商品品牌尚未确认'
                found['excluded_default_brand']=imported.get('default_brand','')
            return confirmed_product_brand(c,project,found)
    values={m.group(1).strip() for m in re.finditer(r'(?im)^\s*(?:brand|品牌|商品品牌|产品品牌)\s*[:：]\s*([^\r\n]+)',project.get('design_brief',''))}
    if len(values)==1:
        value=next(iter(values))
        if len(value)<=120:return confirmed_product_brand(c,project,{'value':value,'kind':'source_brief','description':'导入商品资料中的明确品牌字段'})
    if len(values)>1:return confirmed_product_brand(c,project,{'value':'','kind':'conflict','description':'商品资料存在多个不同品牌，请核对'})
    return confirmed_product_brand(c,project,{'value':'','kind':'missing','description':'原始资料尚未提供明确品牌'})


def snapshot_fields(snapshot, mappings, sheet_name, row_number):
    from app.supplier_variants import field_records
    return [r for r in field_records(snapshot,mappings,sheet_name,row_number)
            if r['field'] not in {'image','image_url'}]


def brief_lines(records):
    lines = []
    for record in records:
        if record['field'] == 'product_name':
            continue
        # Existing downstream logic excludes cost_price from consumer-facing facts.
        label = record['field'] if record['field'] in {'cost_price','sale_price'} else record['column']
        lines.append(f"{label}: {record['value']}")
    return lines


def recover(project):
    from app import main
    source = str(project.get('source_file') or '')
    match = re.search(r'导入#(\d+).*?·\s*([^·]+)!R(\d+)\s*$', source)
    if not match:
        return {}
    try:
        imported = main.get_one('supplier_imports', int(match.group(1)))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return {}
    sheet, row = match.group(2).strip(), int(match.group(3))
    snapshot, mappings = imported.get('workbook_snapshot') or {}, imported.get('field_mapping') or []
    fields = snapshot_fields(snapshot, mappings, sheet, row)
    if not fields:
        return {}
    from app.supplier_variants import derive_family_variants
    family, variants = derive_family_variants(snapshot, mappings, sheet, row, project.get('product_name') or '')
    return {'source_file':source, 'sheet':sheet, 'row':row, 'fields':fields,
            'brief':'\n'.join(brief_lines(fields)), 'family':family, 'variants':variants}
