from __future__ import annotations
import copy
import json
import re
import uuid
from fastapi import HTTPException
from . import store as s, media, categories


def _supplier_project_contract(c,project):
    """Allow legacy source reads, but never mutate archives by guessing a region."""
    from app import supplier_identity
    source=str(project.get('source_file') or '')
    match=supplier_identity.SOURCE.search(source)
    if not match:
        if '导入#' in source:raise HTTPException(422,'供应商来源位置不完整，请确认原表解析区域后建档；历史资料仍保留')
        return False
    row=c.execute('SELECT workbook_snapshot,field_mapping FROM supplier_imports WHERE id=?',(int(match[1]),)).fetchone()
    if not row:raise HTTPException(422,'供应商原始导入记录缺失，请核对来源后建档；历史资料仍保留')
    try:
        snapshot=json.loads(row['workbook_snapshot'] or '{}');mappings=json.loads(row['field_mapping'] or '[]')
    except (TypeError,ValueError):raise HTTPException(422,'供应商原始解析资料无效，请重新确认映射') from None
    if not isinstance(snapshot,dict) or snapshot.get('complete') is not True:
        raise HTTPException(422,'供应商快照完整性未确认，请核对完整原表后确认映射；历史资料仍保留')
    main=s.main()
    main.validate_supplier_mappings(mappings)
    plan=main.validate_supplier_parse_plan(snapshot,snapshot.get('parse_plan'),mappings)
    from app.supplier_variants import region_for
    if not region_for(dict(snapshot,parse_plan=plan),mappings,match[2].strip(),int(match[3])):
        raise HTTPException(422,'设计来源不在已确认的商品数据区域，请重新确认映射；历史资料仍保留')
    return True


def _source_backfill_allowed(c,project):
    try:_supplier_project_contract(c,project)
    except HTTPException:return False
    return True



def brand_default(c,project,business=None):
    from app import supplier_evidence
    found=supplier_evidence.project_brand(project,c)
    if found.get('kind') in {'supplier_only_default','source_unbranded','source_logo_only'} or found.get('kind','').startswith('owner_product_'):return found
    if found.get('value') or found['kind']=='conflict':return found
    if business is None and project.get('business_id'):
        row=c.execute('SELECT * FROM businesses WHERE id=?',(project['business_id'],)).fetchone()
        business=dict(row) if row else {}
    value=str((business or {}).get('brand') or '').strip()
    return {'value':value,'kind':'business_default','description':'所属业务配置的默认品牌'} if value else found


def fill_product_brand(c,product):
    if (product.get('brand_source') or {}).get('kind')=='manual':return product
    row=c.execute('SELECT * FROM design_projects WHERE id=?',(product.get('project_id'),)).fetchone()
    if not row:return product
    if not _source_backfill_allowed(c,dict(row)):return product
    found=brand_default(c,dict(row))
    owner_status={'owner_product_brand':'branded','owner_product_unbranded':'unbranded',
                  'owner_product_logo_only':'logo_only','owner_product_unknown':'unknown'}
    if found.get('kind') in owner_status:
        desired=owner_status[found['kind']]
        if (product.get('brand')==found.get('value','') and
                product.get('brand_source',{}).get('kind')==found['kind'] and
                product.get('brand_status')==desired):return product
        return s.save(c,'product',product|{'brand':found.get('value',''),
            'brand_status':desired,'brand_source':found,
            'brand_logo_text':found.get('visible_mark','')})
    if found.get('kind') in {'source_unbranded','source_logo_only'}:
        desired='unbranded' if found['kind']=='source_unbranded' else 'logo_only'
        if (product.get('brand')==found.get('value','') and
                product.get('brand_source',{}).get('kind')==found['kind'] and
                product.get('brand_status')==desired):return product
        return s.save(c,'product',product|{'brand':found.get('value',''),
            'brand_status':desired,'brand_source':found})
    if (found.get('kind')=='supplier_only_default' and
            (product.get('brand_source') or {}).get('kind')=='import_default' and
            (product.get('brand_source') or {}).get('import_id')==found.get('import_id')):
        return s.save(c,'product',product|{'brand':'','brand_status':'unknown',
            'brand_source':found|{'previous_value':product.get('brand',''),
                                  'previous_source':product.get('brand_source',{})}})
    if not found.get('value'):return product
    if str(product.get('brand') or '').strip():
        if (found.get('kind') != 'owner_product_brand' or
                (product.get('brand_source') or {}).get('kind') not in
                {'import_default','confirmed_alias','owner_product_brand'}):
            return product
        if (product['brand'] == found['value'] and
                (product.get('brand_source') or {}).get('kind') == 'owner_product_brand'):
            return product
    return s.save(c,'product',product|{'brand':found['value'],'brand_status':'branded','brand_source':found})


def backfill_import_brands(import_id=None):
    """Fill missing brands without invoking AI or rewriting existing content."""
    changed_products=[];changed_listings=[];skipped=[]
    with s.transaction() as c:
        for product in s.all_rows(c,'product'):
            project=c.execute('SELECT source_file FROM design_projects WHERE id=?',(product.get('project_id'),)).fetchone()
            if import_id is not None and (not project or not str(project[0]).startswith(f'导入#{import_id} ·')):continue
            listings=[v for v in s.all_rows(c,'listing') if v['product_id']==product['id']]
            active=any(c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(v['id'],)).fetchone() for v in listings)
            if active:skipped.append(product['id']);continue
            updated=fill_product_brand(c,product)
            if updated!=product:changed_products.append(product['id'])
            if not updated.get('brand'):continue
            for listing in listings:
                draft=listing.get('draft') or {}
                if not draft or draft.get('brand') or listing.get('published_version') is not None or listing['status']!='not_submitted':continue
                if (draft.get('brand_source') or {}).get('kind')=='manual':continue
                draft=copy.deepcopy(draft);draft['brand']=updated['brand'];draft['brand_source']=copy.deepcopy(updated.get('brand_source') or {'kind':'product','description':'商品档案品牌'})
                listing['draft']=draft;listing['draft_version']+=1;listing['issues']=issues(draft)
                listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
                s.put(c,'material_version',f"{listing['id']}:{listing['draft_version']}",{'listing_id':listing['id'],'version':listing['draft_version'],'draft':draft})
                s.save(c,'listing',listing);changed_listings.append(listing['id'])
        s.audit(c,'brand_defaults_backfilled',import_id,s.dump({'products':changed_products,'listings':changed_listings}))
    return {'products_updated':changed_products,'listings_updated':changed_listings,'skipped_active_products':skipped}


def apply_source_defaults(c,product):
    from . import costing,source_sizes,source_origins
    if product.get('supplier_source'):
        # The archive is canonical. Re-reading it must not reconstruct facts from a design view.
        return costing.apply(c,product)
    project=c.execute('SELECT * FROM design_projects WHERE id=?',(product.get('project_id'),)).fetchone()
    if project and not _source_backfill_allowed(c,dict(project)):return product
    from app import supplier_identity
    supplier_identity.apply(c,product)
    return source_origins.apply(c,source_sizes.apply(c,costing.apply(c,product)))


def import_project(pid):
    with s.transaction() as c:
        linked=[v for v in s.all_rows(c,'product') if v.get('project_id')==pid]
        if len(linked)>1:raise HTTPException(409,'设计项目关联多个商品档案，请核对商品身份')
        old=linked[0] if linked else s.by_key(c,'product','design:'+str(pid))
        if old:
            return apply_source_defaults(c,fill_product_brand(c,old))
        row=c.execute('SELECT * FROM design_projects WHERE id=?',(pid,)).fetchone()
        if not row:raise HTTPException(404,'设计项目不存在')
        p=dict(row);supplier_project=_supplier_project_contract(c,p);business={}
        if p.get('business_id'):
            b=c.execute('SELECT * FROM businesses WHERE id=?',(p['business_id'],)).fetchone()
            if b:business=dict(b)
        rec=json.loads(p.get('simple_recognition') or '{}')
        from app import supplier_identity
        source_rows=supplier_identity.rows_for_project(c,p)
        if supplier_project and not source_rows:
            raise HTTPException(422,'已确认区域未对应实际商品规格，请重新确认映射；不能沿用旧识别结果建档')
        declared=json.loads(p.get('product_variants') or '[]')
        brand=brand_default(c,p,business)
        components=[]
        for value in json.loads(business.get('components') or '[]'):
            match=re.fullmatch(r'(.+?)[×x*](\d+)',value.strip())
            components.append({'name':match[1] if match else value,'quantity':int(match[2]) if match else 1})
        product=s.put(c,'product','design:'+str(pid),{'project_id':pid,'business_id':p.get('business_id'),'name':p['product_name'],
          'brand':brand.get('value',''),'brand_source':brand,
          'brand_status':{'owner_product_unbranded':'unbranded','owner_product_logo_only':'logo_only',
                          'owner_product_unknown':'unknown','source_unbranded':'unbranded',
                          'source_logo_only':'logo_only'}.get(brand.get('kind'),'branded' if brand.get('value') else 'unknown'),
          'brand_logo_text':brand.get('visible_mark',''),
          'facts':p.get('design_brief',''),'supplier':p.get('supplier_name',''),
          'cost':s.money(business.get('cost_price'),True),'price':s.money(business.get('sale_price'),True),
          'cost_currency':business.get('cost_currency',''),'currency':business.get('sale_currency',''),'product_type':business.get('amazon_product_type',''),
          'components':components,'production_days':None,'packing_days':None,'status':'active',
          'business_defaults':{'cost':s.money(business.get('cost_price'),True),'price':s.money(business.get('sale_price'),True),'cost_currency':business.get('cost_currency',''),'currency':business.get('sale_currency','')}})
        category=categories.product_category(c,product)
        if category:
            product['category_id']=category['id'];product=s.save(c,'product',product)
        variants=source_rows or declared or rec.get('products',[]) or [{'name':p['product_name'],'color':''}]
        for index,variant in enumerate(variants,1):
            key=f'p{index}'
            s.put(c,'sku',f"{product['id']}:{key}",{'product_id':product['id'],'variant_key':key,
              'sku':f"W{product['id']:05d}-{key.upper()}",'name':variant.get('name',p['product_name']),
              'color':variant.get('color',''),'size':'','facts':variant.get('parameters',''),'components':copy.deepcopy(components),
              'cost':product['cost'],'price':product['price'],'cost_currency':product['cost_currency'],'currency':product['currency'],
              'gtin':'','asin':'','supplier_sku':variant.get('supplier_sku',''),
              'asin_issue':('原表 ASIN 格式无效，请核对' if variant.get('asin') and
                            not re.fullmatch(r'[A-Z0-9]{10}',variant['asin']) else ''),
              'gtin_issue':variant.get('gtin_issue',''),
              'source_variant_id':variant.get('source_variant_id',''),'source_row':variant.get('source_row'),
              'source_fields':{**variant.get('inherited_fields',{}),**variant.get('raw_fields',{})},
              'source_inherited_fields':list(variant.get('inherited_fields',{})),'active':True})
        return apply_source_defaults(c,product)


def import_supplier_family(c,imported,sheet_name,anchor_row):
    """Create the durable archive directly; a design project is optional compatibility."""
    from app import supplier_identity, supplier_evidence, supplier_origin
    from app.supplier_variants import derive_family_variants, family_anchors
    snapshot=imported.get('workbook_snapshot') or {};mappings=imported.get('field_mapping') or []
    fallback_name=f'待补商品名（{sheet_name} 第{anchor_row}行）'
    family,_=derive_family_variants(snapshot,mappings,sheet_name,anchor_row,fallback_name)
    source={'import_id':imported['id'],'sheet':sheet_name,'anchor_row':anchor_row,
            'family_id':f"{imported['id']}:{sheet_name}:{anchor_row}"}
    variants=supplier_identity.rows_for_source(c,source,family,imported=imported)
    if not variants:raise HTTPException(422,f'{sheet_name}!R{anchor_row} 无法识别实际商品规格')
    source_key=f"{imported['id']}:{sheet_name}:{anchor_row}"
    link=s.by_key(c,'supplier_product_link',source_key)
    product=s.get(c,'product',link['product_id']) if link else None
    legacy=[]
    for project in c.execute('SELECT * FROM design_projects WHERE source_file LIKE ?', (f"导入#{imported['id']} ·%",)):
        match=re.search(r'·\s*([^·]+)!R(\d+)\s*$',project['source_file'])
        if match and match[1].strip()==sheet_name and int(match[2]) in {v['source_row'] for v in variants}:
            legacy.append(dict(project))
    if not product and len(legacy)==1:
        product=next((p for p in s.all_rows(c,'product') if p.get('project_id')==legacy[0]['id']),None)
    if not product:
        # A relocated family may still have stable supply codes within this batch.
        supply={v['supplier_sku'] for v in variants if v.get('supplier_sku')}
        # Separate live source groups can legitimately share a product name.
        # A name alone cannot merge them; retain name matching for relocated rows.
        live_anchors=set(family_anchors(snapshot,mappings,sheet_name,[]))
        candidates=[p for p in s.all_rows(c,'product') if
                    (p.get('supplier_source') or {}).get('import_id')==imported['id'] and
                    (p.get('supplier_source') or {}).get('sheet')==sheet_name and
                    (p.get('source_family_name')==family and
                     (p.get('supplier_source') or {}).get('anchor_row') not in live_anchors or supply and supply & {
                        x.get('supplier_sku') for x in s.product_skus(c,p['id'])})]
        if len(candidates)==1:product=candidates[0]
    created=product is None
    first=variants[0];normalized=first.get('normalized_fields') or {}
    raw_category=normalized.get('category','')
    business_candidates=[]
    for row in c.execute('SELECT * FROM businesses WHERE enabled=1'):
        business=dict(row)
        tokens={business['name'],business.get('categories','')}
        for key in ['category_paths']:
            try:tokens.update(json.loads(business.get(key) or '[]'))
            except (TypeError,ValueError):pass
        if raw_category and raw_category in tokens:business_candidates.append(business)
    business=business_candidates[0] if len(business_candidates)==1 else {}
    brand=supplier_evidence.brand_from_sheet(snapshot,mappings,sheet_name,anchor_row,
                                            supplier_evidence.effective_batch_brand(c,imported['id'],imported.get('default_brand','')))
    source_images=list(dict.fromkeys(url for v in variants for url in v.get('source_images',[])))
    source_issues=[issue for v in variants for issue in v.get('source_parse_issues',[])]
    if family==fallback_name:source_issues.append({'field':'product_name','message':'原表未提供明确商品名称','sheet':sheet_name,'row':anchor_row})
    if len(business_candidates)!=1:source_issues.append({'field':'business_id','message':'所属业务无法唯一匹配，请在商品档案选择'})
    def origin_for(fields):
        country=fields.get('origin_country','');place=fields.get('origin_place','');combined=fields.get('origin','')
        sourced=bool(country or place or combined)
        if not sourced:
            country=imported.get('default_origin_country','');place=imported.get('default_origin_place','')
        origin=supplier_origin.split_origin(country,place,combined)
        origin['source']={'kind':'supplier_cell' if sourced else 'batch_default','import_id':imported['id'],'sheet':sheet_name}
        return origin

    def merge_evidence(record,variant):
        facts=copy.deepcopy(record.get('structured_facts') or {})
        fields=copy.deepcopy(record.get('normalized_fields') or {})
        conflicts=copy.deepcopy(record.get('source_conflicts') or [])
        for key,incoming in variant.get('structured_facts',{}).items():
            previous=facts.get(key)
            if previous is None or previous.get('status')=='missing':
                facts[key]=copy.deepcopy(incoming)
                if incoming['status']!='conflict':fields[key]=incoming['value']
            elif previous.get('status') not in {'manual','cleared'} and previous.get('value')!=incoming.get('value'):
                conflict={'field':key,'previous':previous,'incoming':incoming}
                if conflict not in conflicts:conflicts.append(conflict)
        record=record|{'structured_facts':facts,'normalized_fields':fields,'source_conflicts':conflicts}
        for key in ['size','color']:
            if not record.get(key) and (record.get(key+'_source') or {}).get('status')!='manual' and key in fields:
                record[key]=fields[key]
        return record
    if created:
        components=[]
        for value in json.loads(business.get('components') or '[]'):
            match=re.fullmatch(r'(.+?)[×x*](\d+)',value.strip())
            components.append({'name':match[1] if match else value,'quantity':int(match[2]) if match else 1})
        product=s.put(c,'product','supplier:'+source_key,{
            'project_id':legacy[0]['id'] if len(legacy)==1 else None,'business_id':business.get('id'),
            'name':family or normalized.get('product_name',''),'source_family_name':family,
            'name_source':{'kind':'supplier','import_id':imported['id']},'supplier':imported['supplier_name'],
            'brand':brand.get('value',''),'brand_source':brand,
            'brand_status':{'source_unbranded':'unbranded','source_logo_only':'logo_only'}.get(brand['kind'],'branded' if brand.get('value') else 'unknown'),
            'brand_logo_text':'','supplier_source':source,'source_images':source_images,'origin':origin_for(normalized),
            'structured_facts':copy.deepcopy(first.get('structured_facts',{})),
            'normalized_fields':copy.deepcopy(normalized),'source_parse_issues':source_issues,
            'facts':'\n'.join(f'{key}: {value}' for key,value in normalized.items()
                              if key not in {'cost_price','sale_price','cost_currency','sale_currency','price_unit'}),
            'cost':None,'price':None,'cost_currency':'','currency':'',
            'product_type':business.get('amazon_product_type',''),'components':components,
            'production_days':None,'packing_days':None,'status':'needs_fields'})
        category=categories.product_category(c,product)
        if category:product=s.save(c,'product',product|{'category_id':category['id']})
    else:
        # Every re-import is evidence. Never silently overwrite an edited archive.
        evidence_key=source_key+':'+s.main().hashlib.sha256(s.dump(variants).encode()).hexdigest()
        s.put(c,'supplier_archive_evidence',evidence_key,{'product_id':product['id'],'source':source,'variants':variants})
        updates={}
        if not product.get('supplier_source'):updates['supplier_source']=source
        if not product.get('business_id') and business:updates['business_id']=business['id']
        updates['source_images']=list(dict.fromkeys(product.get('source_images',[])+source_images))
        merged=merge_evidence(product,first)
        for key in ['structured_facts','normalized_fields','source_conflicts']:
            updates[key]=merged[key]
        updates['source_parse_issues']=source_issues
        product=s.save(c,'product',product|updates) if any(product.get(k)!=v for k,v in updates.items()) else product
    s.put(c,'supplier_product_link',source_key,{'product_id':product['id'],'source':source})
    existing=s.product_skus(c,product['id'])
    added=0
    for variant in variants:
        fields=variant.get('normalized_fields',{})
        signature=(variant.get('supplier_sku',''),variant.get('color',''),fields.get('size') or variant.get('size',''),fields.get('package_quantity') or fields.get('number_of_items',''))
        matches=[v for v in existing if v.get('source_variant_id')==variant['source_variant_id']]
        if matches and matches[0].get('supplier_sku') and variant.get('supplier_sku')!=matches[0]['supplier_sku']:
            matches=[]
        if not matches:
            matches=[v for v in existing if signature==(
                v.get('supplier_sku',''),v.get('color',''),v.get('size',''),
                (v.get('normalized_fields') or {}).get('package_quantity') or (v.get('normalized_fields') or {}).get('number_of_items','')) and
                (variant.get('supplier_sku') or v.get('name')==variant['name'])]
        if len(matches)>1:
            source_issues.append({'field':'sku_identity','message':'导入规格无法唯一对应既有 SKU','row':variant['source_row']})
            continue
        if matches:
            sku=matches[0]
            s.put(c,'supplier_sku_evidence',variant['source_variant_id']+':'+s.main().hashlib.sha256(s.dump(variant).encode()).hexdigest(),
                  {'sku_id':sku['id'],'source':variant})
            merged=list(dict.fromkeys(sku.get('source_images',[])+variant.get('source_images',[])))
            references=copy.deepcopy(sku.get('source_image_references',[]))
            for reference in variant.get('source_image_references',[]):
                if reference not in references:references.append(copy.deepcopy(reference))
            refreshed=merge_evidence(sku,variant)|{'source_images':merged,'source_image_references':references}
            if refreshed!=sku:s.save(c,'sku',refreshed)
            continue
        used={v['variant_key'] for v in existing}
        number=next(n for n in range(1,10001) if f'p{n}' not in used);key=f'p{number}'
        from .identifiers import valid_gtin
        gtin=variant.get('gtin','');asin=variant.get('asin','')
        sku=s.put(c,'sku',f"{product['id']}:{key}",{
            'product_id':product['id'],'variant_key':key,'sku':f"W{product['id']:05d}-{key.upper()}",
            'local_variant_id':uuid.uuid4().hex,'name':variant['name'],'color':variant.get('color',''),
            'size':fields.get('size') or variant.get('size',''),'origin':origin_for(fields),
            'facts':'\n'.join(f'{k}: {v}' for k,v in fields.items() if k not in {'cost_price','sale_price','cost_currency','sale_currency'}),
            'components':copy.deepcopy(product.get('components',[])),
            'cost':None,'price':None,'cost_currency':'','currency':'',
            'cost_source':{'status':'missing'},'price_source':{'status':'missing'},
            'gtin':gtin if valid_gtin(gtin) else '', 'asin':asin if re.fullmatch('[A-Z0-9]{10}',asin) else '',
            'source_gtin':gtin,'gtin_issue':variant.get('gtin_issue','') or ('原表 GTIN 无效' if gtin and not valid_gtin(gtin) else ''),
            'supplier_sku':variant.get('supplier_sku',''),'source_variant_id':variant['source_variant_id'],
            'source_row':variant['source_row'],'source_images':variant.get('source_images',[]),
            'source_image_references':copy.deepcopy(variant.get('source_image_references',[])),
            'source_fields':{**variant.get('inherited_fields',{}),**variant.get('raw_fields',{})},
            'source_inherited_fields':list(variant.get('inherited_fields',{})),
            'structured_facts':copy.deepcopy(variant.get('structured_facts',{})),
            'normalized_fields':copy.deepcopy(fields),'source_parse_issues':variant.get('source_parse_issues',[]),
            'active':True,'sale_mode':'stock','production_capacity':0})
        existing.append(sku);added+=1
    from . import costing
    product=costing.apply(c,product)
    if product.get('source_parse_issues')!=source_issues:product=s.save(c,'product',product|{'source_parse_issues':source_issues})
    return {'product':product,'created':created,'skus_created':added,'sku_count':len(existing)}


def backfill_supplier_archive_defaults(import_id):
    """Supplement empty archive defaults without touching source or manual values."""
    from app import supplier_origin
    changed_products=[];changed_skus=[]
    with s.transaction() as c:
        batch=dict(c.execute('SELECT * FROM supplier_imports WHERE id=?',(import_id,)).fetchone())
        default_origin=supplier_origin.split_origin(batch.get('default_origin_country',''),batch.get('default_origin_place',''))
        for product in s.all_rows(c,'product'):
            if (product.get('supplier_source') or {}).get('import_id')!=import_id:continue
            original=copy.deepcopy(product)
            if (not product.get('brand') and batch.get('default_brand') and
                    (product.get('brand_source') or {}).get('kind') in {'missing','import_default'}):
                product.update(brand=batch['default_brand'],brand_status='branded',
                               brand_source={'kind':'import_default','import_id':import_id,'description':'明确的批次默认品牌'})
            if (not product.get('origin',{}).get('country') and default_origin['status']=='ready' and
                    (product.get('origin') or {}).get('source',{}).get('kind') not in {'manual','supplier_cell'}):
                product['origin']=default_origin|{'source':{'kind':'batch_default','import_id':import_id}}
            if product!=original:
                s.save(c,'product',product);changed_products.append(product['id'])
            for sku in s.all_rows(c,'sku'):
                if sku['product_id']!=product['id']:continue
                origin=sku.get('origin') or {}
                if (not origin.get('country') and default_origin['status']=='ready' and
                        origin.get('source',{}).get('kind') not in {'manual','supplier_cell'}):
                    s.save(c,'sku',sku|{'origin':default_origin|{'source':{'kind':'batch_default','import_id':import_id}}})
                    changed_skus.append(sku['id'])
    return {'products_updated':changed_products,'skus_updated':changed_skus}


def listing_for(product_id,target_id):
    with s.transaction() as c:
        p=s.get(c,'product',product_id);t=s.get(c,'target',target_id)
        if not t.get('enabled'):raise HTTPException(422,'该上架目标未启用')
        key=f'{product_id}:{target_id}'
        old=s.by_key(c,'listing',key)
        if old:return old
        return s.put(c,'listing',key,{'product_id':product_id,'target_id':target_id,'name':p['name'],
           'environment':t['environment'],'status':'not_submitted','draft':{},'draft_version':0,'published_version':None,
           'issues':[],'sku_results':{},'paused':False,'last_error':''})




def material_input(listing_id):
    from .targets import selected_targets
    from . import product_archive
    with s.main().db() as c:
        listing=s.get(c,'listing',listing_id);p=s.get(c,'product',listing['product_id']);t=s.get(c,'target',listing['target_id'])
        selected_targets(c,[t['id']])
        skus=sorted([v for v in s.all_rows(c,'sku') if v['product_id']==p['id'] and v.get('active',True)],key=lambda v:v['id'])
        row=c.execute('SELECT * FROM design_projects WHERE id=?',(p.get('project_id'),)).fetchone()
        if not row:raise HTTPException(422,'请先在商品档案选择合格 SKU 并完成设计')
        project=dict(row)
        rid=project.get('simple_current_run')
        run=c.execute('SELECT * FROM design_simple_runs WHERE id=?',(rid,)).fetchone()
        if not run or run['status']!='completed':raise HTTPException(422,'请先完成当前设计图组，再组装刊登资料')
        tasks=[dict(x) for x in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND status='completed' AND kind IN ('image','master')",(rid,))] if rid else []
        snapshot=json.loads(run['snapshot']) if run else {}
        if snapshot.get('archive_digest'):
            skus=product_archive.selected_skus(c,p,snapshot.get('selected_sku_ids'))
            if product_archive.fact_digest(c,p,snapshot.get('selected_sku_ids'))!=snapshot['archive_digest']:raise HTTPException(409,'商品事实或原图已更新，请重新设计后生成刊登资料')
        elif t['environment']=='production':raise HTTPException(409,'历史设计缺少商品档案版本，请按当前档案重新设计；历史图片仍可查看下载')
        from . import listing_identity
        start_signature=listing_identity.archive_signature(c,p,t,[v['id'] for v in skus])
    if not tasks:raise HTTPException(422,'请先完成设计，生成成品图')
    order={url:i for i,url in enumerate(json.loads(project.get('output_images') or '[]'))}
    tasks.sort(key=lambda task:order.get(json.loads(task['result']).get('url'),99999))
    return listing,p,t,skus,project,dict(run)|{'_archive_signature':start_signature},tasks


def archive_for_project(pid):
    from . import product_archive
    with s.main().db() as c:product=product_archive.for_project(c,pid)
    # Legacy binding happens once; an existing archive is read without backfill.
    return product if product is not None else import_project(pid)


def sku_for_design_product(skus, design_product):
    """Resolve frozen design tasks by facts, never by a drifting p1/p2 index."""
    if design_product.get('sku_id'):
        return next((v for v in skus if v['id']==design_product['sku_id']),None)
    name=str(design_product.get('name') or '').strip()
    color=str(design_product.get('color') or '').strip()
    matches=[v for v in skus if v.get('name','').strip()==name and
             str(v.get('color') or '').strip()==color]
    if len(matches)==1:return matches[0]
    matches=[v for v in skus if v.get('name','').strip()==name]
    if len(matches)==1:return matches[0]
    return None




def generate_materials(job):
    from app import design_transport, detail_page
    from . import product_archive, listing_copy
    from app.simple_design_prompts import clean_brief
    from app.model_retry import ModelFailure
    listing,p,t,skus,project,run,tasks=material_input(job['payload']['listing_id'])
    requested_run = job['payload'].get('presentation_run_id')
    if requested_run is not None:
        repair = json.loads(run.get('recognition') or '{}').get('presentation_repair') or {}
        if (requested_run != run['id'] or run.get('mode') != 'presentation_repair' or not repair.get('local_request')
                or repair.get('listing_id') != listing['id'] or not listing.get('paused')
                or listing.get('status') != 'not_submitted' or listing.get('published_version') is not None
                or listing.get('retirement_id') or listing.get('published_skus') or job['payload'].get('replace_existing')):
            raise HTTPException(409,'本次图片修正与当前设计或上架资料不一致，请核对当前资料')
        if (listing.get('draft') or {}).get('source',{}).get('design_run_id') == requested_run:
            return {'listing_id': listing['id'], 'version': listing['draft_version'], 'already_completed': True}
        if listing['revision'] != repair.get('listing_revision'):
            raise HTTPException(409,'修图期间上架资料已更新，保留当前版本，请核对后组装')
    if job['payload'].get('replace_existing'):
        from .material_batches import target_signature
        if listing.get('last_material_request')==job['payload'].get('batch_request_id'):
            return {'listing_id':listing['id'],'version':listing['draft_version'],'already_completed':True}
        if listing['revision']!=job['payload'].get('expected_revision') or target_signature(t)!=job['payload'].get('expected_target'):
            raise HTTPException(409,'确认覆盖后原资料或地区配置已改变，请重新点击生成并确认')
    from . import locales
    localization=locales.settings(t);language=localization['language'];translated={};copy_review=None
    presentation_only=run.get('mode')=='presentation_repair' and not job['payload'].get('replace_existing')
    previous_draft=listing.get('draft') or {}
    from . import archive_fields, attribute_localization
    attribute_translations=copy.deepcopy(previous_draft.get('source',{}).get('attribute_translations',[])) if presentation_only else []
    attribute_requests={}
    if not presentation_only and t['environment']!='mock':
        with s.main().db() as c:
            destination={}
            categories.apply_defaults(c,p,t,destination)
            material_type=destination.get('product_type') or p.get('product_type','')
            schema=(archive_fields.definition(c,t,material_type) or {}).get('schema') or {}
            scopes=[(archive_fields.encode(c,p,actual,t,schema,material_type),actual.get('components'))
                    for actual in skus+[{'source_fields':{}}]]
            scopes.append((destination.get('attributes',{}),None))
            for raw,components in scopes:
                projected,_=attribute_localization.project(raw,language,components,schema=schema)
                for item,_ in attribute_localization.translation_request(projected,language,schema):
                    attribute_requests[item['key']]=item
    preserved_skus={v['sku_id']:v for v in previous_draft.get('skus',[])}
    if presentation_only:
        proof=json.loads(run.get('recognition') or '{}').get('presentation_repair') or {}
        if (proof.get('listing_id')!=listing['id'] or
                proof.get('source_run_id')!=previous_draft.get('source',{}).get('design_run_id') or
                set(preserved_skus)!={v['id'] for v in skus} or
                previous_draft.get('currency')!=t.get('currency','USD') or
                previous_draft.get('localization')!=localization):
            raise HTTPException(409,'展示修复的原资料、规格或销售地区已变化，请核对后重新编排')
    from app import design_quality
    photos=[];paths=[]
    for task in tasks:
        result=json.loads(task['result']);payload=json.loads(task['payload'])
        if task['kind']!='image' or design_quality.is_reference_board(result):continue
        if payload.get('page',{}).get('display_role')=='detail_page':continue
        matching=sku_for_design_product(skus,payload.get('product') or {})
        if not matching:continue
        path=s.main().simple_design_flow.local_path(result['url'],pid=p['project_id'])
        asset=media.ingest(path.read_bytes(),path.name,source_path=path)
        photo={'asset_id':asset['id'],'variant_key':matching['variant_key'],
               'role':'detail' if task['task_key'].startswith('detail') else 'effect',
               'design_run_id':task['run_id'],'design_task_id':task['id'],
               'page_key':payload.get('page',{}).get('page_key',task['task_key']),
               'display_role':payload.get('page',{}).get('display_role',''),
               'shot_type':payload.get('page',{}).get('shot_type','')}
        if result.get('asset_role'):
            photo['asset_role']=result['asset_role']
        if result.get('asset_role')==design_quality.LISTING_MAIN_ROLE:
            photo.update(source='design_main_candidate',design_run_id=task['run_id'],design_task_id=task['id'])
        photos.append(photo)
        if len(paths)<3:paths.append(path)
    from . import visual_presentation
    photos=visual_presentation.order_generated(photos,skus,json.loads(run.get('pages') or '[]'))
    if presentation_only:
        content=copy.deepcopy(previous_draft['content'])
        translated={v['variant_key']:copy.deepcopy(v) for v in preserved_skus.values()}
        copy_review=copy.deepcopy(previous_draft.get('source',{}).get('copy_review'))
    elif t['environment']=='mock':
        content={'title':p['name'],'bullets':list(dict.fromkeys(clean_brief(v['facts'])[:150] for v in skus[:5] if v['facts'])), 'description':clean_brief(p['facts'])[:1500],'search_terms':'','source':'mock'}
    else:
        models=design_transport.candidates(s.main())
        model=models[min(job['model_role'],len(models)-1)]
        prompt=listing_copy.prompt(p,skus,language,target=t,feedback=job.get('error',''),
                                   attribute_requests=list(attribute_requests.values()),reference=previous_draft.get('content'))
        # One call produces copy, variant text and destination attribute text.
        # Generated pictures never become new physical product facts.
        raw=design_transport.text(s.main(),model,prompt,[])
        try:
            content,translated,attribute_translations=listing_copy.parse_response(
                raw,skus,language,list(attribute_requests.values()),brand=p.get('brand',''))
        except (ValueError,TypeError,AttributeError):raise ModelFailure('文案语言、格式、SKU映射或长度不符合约定','invalid')
    from . import shipping,pricing
    rate_cache={}
    draft={'content':content,'product_type':p.get('product_type',''),'brand':p.get('brand',''),'brand_source':copy.deepcopy(p.get('brand_source',{})),
      'origin':copy.deepcopy(p.get('origin',{})),
      'language':language,'localization':localization,'currency':t.get('currency','USD'),'images':photos,
      'image_language':json.loads(run.get('snapshot') or '{}').get('language','unknown'),
      'detail_page':detail_page.for_material(p['project_id']),
      'handling_days':None,'shipping_template_required':None,'shipping_group':'','attributes':{},'skus':[{'sku_id':v['id'],'seller_sku':v['sku'],'variant_key':v['variant_key'],
         'name':translated.get(v['variant_key'],v)['name'],'color':translated.get(v['variant_key'],v)['color'],'size':translated.get(v['variant_key'],v)['size'],'size_source':copy.deepcopy(v.get('size_source',{})),
         **({'title':translated[v['variant_key']]['title'],'title_source':{'status':'generated','language':language,'basis':'reviewed_variant_copy'}} if v['variant_key'] in translated and 'title' in translated[v['variant_key']] else {}),
         **({'search_terms':translated[v['variant_key']]['search_terms']} if 'search_terms' in translated.get(v['variant_key'],{}) else {}),
         **({key:copy.deepcopy(preserved_skus[v['id']][key]) for key in ('price','price_source') if key in preserved_skus[v['id']]}
            if presentation_only else pricing.quote(v,t.get('currency','USD'),rate_cache)),
         'quantity':None,'gtin':v.get('gtin',''),'asin':v.get('asin',''),'gtin_exempt':False,'origin':copy.deepcopy(v.get('origin',{})),
         'attributes':{},'images':[]} for v in skus],
      'source':{'product_revision':p['revision'],'product_fact_revision':p.get('fact_revision',0),'design_run_id':run.get('id'),'facts':product_archive.facts_text(p),
                'origin':copy.deepcopy(p.get('origin',{})),
                'skus':skus,'masters':[s.main().simple_design_flow.task_result(x) for x in tasks if x['kind']=='master']}}
    if copy_review:draft['source']['copy_review']=copy_review
    if presentation_only and previous_draft.get('source', {}).get('copy_generation'):
        draft['source']['copy_generation'] = copy.deepcopy(previous_draft['source']['copy_generation'])
    if attribute_translations:draft['source']['attribute_translations']=attribute_translations
    if not presentation_only:
        draft['source']['copy_generation']={'policy':'single-pass-no-review-v1',
            'language':language,'model_id':model.get('id') if t['environment']!='mock' else None,
            'writing_policy':listing_copy.WRITING_POLICY if t['environment']!='mock' else 'mock'}
        if t['environment']!='mock':
            strategy=listing_copy.marketing_strategy(raw)
            if strategy:draft['source']['copy_generation']['marketing_strategy']=strategy
    from app import design_contract
    design_snapshot=json.loads(run.get('snapshot') or '{}')
    if design_contract.active(design_snapshot):
        draft['source']['design_presentation']={'design_contract':design_snapshot['pipeline']['design_contract'],
            'first_image':design_contract.first_image(design_snapshot)}
    with s.transaction() as c:
        live=s.get(c,'listing',listing['id'])
        if live['revision']!=listing['revision']:raise HTTPException(409,'生成期间资料已编辑，保留用户版本，请重新生成')
        current_target=s.get(c,'target',t['id'])
        if locales.settings(current_target)!=localization or not current_target.get('enabled'):raise HTTPException(409,'生成期间店铺地区或语言配置已改变，请重新生成')
        previous_skus={v['sku_id']:v for v in (live.get('draft') or {}).get('skus',[])}
        from . import product_archive, listing_identity, archive_fields
        current_product=s.get(c,'product',p['id'])
        current_design=c.execute('SELECT simple_current_run FROM design_projects WHERE id=?',(p['project_id'],)).fetchone()
        current_run=c.execute('SELECT status FROM design_simple_runs WHERE id=?',(run['id'],)).fetchone()
        if not current_design or current_design['simple_current_run']!=run['id'] or not current_run or current_run['status']!='completed':raise HTTPException(409,'生成期间设计版本已变化，请使用当前已完成设计重新生成')
        selected_ids=[v['id'] for v in skus]
        if listing_identity.archive_signature(c,current_product,current_target,selected_ids)!=run['_archive_signature']:raise HTTPException(409,'生成期间商品档案字段已变化，请重新组装资料')
        for frozen in skus:
            actual=s.get(c,'sku',frozen['id'])
            if actual.get('fact_revision',0)!=frozen.get('fact_revision',0):raise HTTPException(409,'生成期间 SKU 字段已更新，请重新生成')
            frozen.setdefault('fact_revision',0)
        digest=product_archive.fact_digest(c,current_product,selected_ids)
        if (json.loads(run.get('snapshot') or '{}').get('archive_digest') and digest!=json.loads(run['snapshot'])['archive_digest']):raise HTTPException(409,'生成期间商品事实已更新，请重新设计')
        if current_product.get('fact_revision',0)!=p.get('fact_revision',0):raise HTTPException(409,'生成期间商品字段已编辑，请重新生成')
        draft['source'].update(archive_digest=digest,selected_sku_ids=selected_ids,target_scope=listing_identity.target_scope(t))
        draft['source']['archive_signature']=listing_identity.archive_signature(c,current_product,t,selected_ids)
        from . import supplier_declarations
        declared=supplier_declarations.for_product(current_product)
        if declared:draft['source']['supplier_declarations']=declared
        days=[p.get('production_days'),p.get('packing_days')]
        if all(v is not None for v in days) and sum(days)>0:
            draft['handling_days']=sum(days);draft['handling_source']={'status':'archive','production_days':days[0],'packing_days':days[1]}
        old_draft=live.get('draft') or {}
        if old_draft.get('content',{}).get('source')=='manual':
            draft['content']=copy.deepcopy(old_draft['content'])
            draft['source'].pop('copy_review',None)
            draft['content_review_required']=(old_draft.get('source',{}).get('product_fact_revision')!=p.get('fact_revision',0) or old_draft.get('source',{}).get('archive_digest')!=digest)
        if old_draft.get('handling_days') is not None and old_draft.get('handling_source',{}).get('status') not in {'archive','confirmed_config'}:
            draft['handling_days']=old_draft['handling_days'];draft['handling_source']=copy.deepcopy(old_draft.get('handling_source',{'status':'historical','message':'保留已有备货配置，请核实'}))
        for entry in draft['skus']:
            previous=previous_skus.get(entry['sku_id'],{})
            if old_draft.get('content',{}).get('source')=='manual':
                entry.pop('search_terms',None)
                if 'search_terms' in previous:
                    entry['search_terms']=previous['search_terms']
            if previous.get('title_source',{}).get('status')=='manual' and 'title' in previous:
                entry['title']=previous['title'];entry['title_source']=copy.deepcopy(previous['title_source'])
                draft['source'].pop('copy_review',None)
                if old_draft.get('source',{}).get('archive_digest')!=digest:
                    draft['content_review_required']=True
            if previous.get('price_source',{}).get('status')=='manual' and old_draft.get('currency')==draft['currency']:
                entry['price']=previous.get('price');entry['price_source']=copy.deepcopy(previous['price_source'])
            # Initial merchandising quantity is not a warehouse receipt. Keep
            # manual values, including zero, when regenerating the same draft.
            preserve_quantity='quantity' in previous and previous.get('quantity_source',{}).get('status') not in {'stock','archive_capacity','confirmed_config','missing'}
            if preserve_quantity:entry['quantity']=previous['quantity']
            source_sku=next(v for v in skus if v['id']==entry['sku_id'])
            if preserve_quantity:entry['quantity_source']=copy.deepcopy(previous.get('quantity_source',{'status':'historical','message':'保留已有上架数量，请核实'}))
            else:
                balance=c.execute('SELECT * FROM commerce_stock_balances WHERE sku_id=? AND environment=?',(source_sku['id'],t['environment'])).fetchone()
                state=s.stock(c,source_sku['id'],t['environment'],create=False)
                capacity=source_sku.get('sale_mode')=='made_to_order' and source_sku.get('production_capacity',0)>0
                if state.get('legacy_environment_pending'):
                    entry['quantity']=0;entry['quantity_source']={'status':'missing','message':'历史库存环境归属待核实','environment':t['environment']}
                elif balance or capacity:
                    other=sum(int(row.get('quantity',0)) for row in s.all_rows(c,'allocation')
                              if row.get('policy')!='amazon_max' and row['sku_id']==source_sku['id'] and row['listing_id']!=live['id'] and row.get('environment','production')==t['environment'])
                    entry['quantity']=max(0,s.sales_capacity(c,source_sku['id'],t['environment'])-other)
                    entry['quantity_source']={'status':'archive_capacity' if capacity else 'stock','environment':t['environment']}
                else:entry['quantity_source']={'status':'missing','message':'请确认真实库存或生产能力'}
            identity_key=f"{t['account_id']}:{t['marketplace_id']}:{entry['sku_id']}"
            identity=s.by_key(c,'seller_sku_identity',identity_key)
            if not identity:
                candidate=previous_skus.get(entry['sku_id'],{}).get('seller_sku') or entry['seller_sku']
                identity=s.put(c,'seller_sku_identity',identity_key,
                               {'account_id':t['account_id'],'marketplace_id':t['marketplace_id'],
                                'target_id':t['id'],'sku_id':entry['sku_id'],
                                'seller_sku':candidate,
                                'checked':candidate in (live.get('published_skus') or {})})
            entry['seller_sku']=identity['seller_sku']
        if not presentation_only and (old_draft.get('content',{}).get('source')=='manual' or any(
                row.get('title_source',{}).get('status')=='manual' and 'title' in row for row in previous_skus.values())):
            generation=draft['source']['copy_generation']
            generation['manual_copy_preserved']=True
            generation.pop('marketing_strategy',None)
        found=categories.apply_defaults(c,s.get(c,'product',p['id']),t,draft)
        if not found['mapping'] and not categories.product_category(c,p) and categories.target_platform(c,t)=='亚马逊':
            draft['product_type']=p.get('product_type','')
        saved_schema=archive_fields.definition(c,t,draft['product_type'])
        schema=saved_schema.get('schema') or {}
        draft['attributes'],shared_projection=attribute_localization.project(
            draft.get('attributes',{}),language,translations=attribute_translations,schema=schema)
        draft['source'].setdefault('attribute_localization',{})['shared']=shared_projection
        from . import offer_defaults
        offer_defaults.apply(c,current_product,t,live,draft)
        draft['source']['schema_hash']=saved_schema.get('schema_sha256') or ''
        for entry in draft['skus']:
            actual=next(v for v in skus if v['id']==entry['sku_id'])
            from . import attribute_localization
            fallback_sources={}
            entry['attributes'],projection=attribute_localization.project(
                archive_fields.encode(c,current_product,actual,t,schema,draft['product_type'],provenance=fallback_sources),
                locales.resolve(t),actual.get('components'),translations=attribute_translations,schema=schema,
                prior_proof=previous_draft.get('source',{}).get('attribute_localization',{}).get(str(entry['sku_id'])) if presentation_only else None)
            draft['source'].setdefault('attribute_localization',{})[str(entry['sku_id'])]=projection
            draft['source'].setdefault('attribute_fallbacks',{})[str(entry['sku_id'])]=fallback_sources
            if live.get('publication_sku_names'):
                entry['seller_sku']=live['publication_sku_names'].get(str(entry['sku_id']),entry['seller_sku'])
            entry['submission_intent']=listing_identity.submission_intent(c,live,entry)
        shared=archive_fields.encode(c,current_product,{'source_fields':{}},t,schema,draft['product_type'])
        draft['parent_attributes'],parent_projection=attribute_localization.project(
            {k:v for k,v in shared.items() if k not in {'color','size'}},locales.resolve(t),
            translations=attribute_translations,schema=schema)
        draft['source']['attribute_localization']['parent']=parent_projection
        from . import variation_families
        declaration=variation_families.battery_declaration(old_draft)
        if declaration and declaration['sku_ids']==sorted(row['sku_id'] for row in draft['skus']):
            draft['source']['battery_declaration']=copy.deepcopy(declaration)
        if old_draft.get('source',{}).get('variation_preference'):
            draft['source']['variation_preference']=old_draft['source']['variation_preference']
        if old_draft.get('variation_theme') and not old_draft.get('source',{}).get('variation_family'):
            draft['variation_theme']=old_draft['variation_theme']
            draft['parent_sku']=old_draft.get('parent_sku')
        draft['shipping_template_required']=shipping.required(t,draft['product_type'])
        draft['shipping_group']=shipping.default_value(t,draft['product_type'])
        if old_draft.get('shipping_group'):draft['shipping_group']=old_draft['shipping_group']
        if presentation_only:
            for key in ('handling_days','handling_source','shipping_group','shipping_template_required','content_review_required'):
                if key in old_draft:draft[key]=copy.deepcopy(old_draft[key])
            for entry in draft['skus']:
                old=previous_skus[entry['sku_id']]
                for key in ('price','price_source','quantity','quantity_source','title','title_source'):
                    if key in old:entry[key]=copy.deepcopy(old[key])
                    else:entry.pop(key,None)
            draft['source']['presentation_repair']={'source_run_id':proof['source_run_id'],
                'source_material_version':listing['draft_version'],'preserved_copy_and_offers':True}
        from . import attribute_completion
        attribute_completion.restore(draft,old_draft)
        from . import listing_release, main_images
        listing_release.apply_cycle_names(live,draft)
        main_images.bind_material_covers(c, draft, old_draft)
        if old_draft.get('price_shipping_mode'):draft['price_shipping_mode']=old_draft['price_shipping_mode']
        version=live['draft_version']+1;live['draft']=draft;live['draft_version']=version
        if job['payload'].get('replace_existing'):
            live['last_material_request']=job['payload']['batch_request_id'];live.pop('preview_results',None)
        live['issues']=issues(draft,t);live['draft_status']='needs_fields' if live['issues'] else 'ready'
        s.put(c,'material_version',f"{listing['id']}:{version}",{'listing_id':listing['id'],'version':version,'draft':draft})
        s.save(c,'listing',live);s.audit(c,'materials_generated',listing['id'],f'version {version}')
        attribute_completion.ensure(c,live)
        if job['payload'].get('replace_existing'):
            from .material_batches import cleanup
            cleanup(c,live)
    return {'listing_id':listing['id'],'version':version}


def attach_current_detail(ident):
    """Version an intact current detail page without rewriting existing copy."""
    from app import detail_page
    from . import listing_identity
    with s.main().db() as c:
        listing=s.get(c,'listing',ident)
        product=s.get(c,'product',listing['product_id'])
    detail=detail_page.for_material(product.get('project_id')) if product.get('project_id') else None
    if not detail or not listing.get('draft') or listing['draft'].get('detail_page')==detail:
        return listing
    with s.transaction() as c:
        live=s.get(c,'listing',ident)
        if live['revision']!=listing['revision']:
            raise HTTPException(409,'组装详情期间资料已编辑，保留用户版本，请重试')
        current=c.execute('SELECT detail_current_run FROM design_projects WHERE id=?',(product['project_id'],)).fetchone()
        if not current or current['detail_current_run']!=detail['run_id']:
            raise HTTPException(409,'组装期间详情版本已改变，请重试')
        target=s.get(c,'target',live['target_id'])
        listing_identity.assert_source_current(c,live,target,live['draft'])
        live['draft']=copy.deepcopy(live['draft'])
        live['draft']['detail_page']=detail
        live['draft_version']+=1
        live.pop('preview_results',None)
        live['issues']=issues(live['draft'],target)
        live['draft_status']='needs_fields' if live['issues'] else 'ready'
        s.put(c,'material_version',f"{ident}:{live['draft_version']}",
              {'listing_id':ident,'version':live['draft_version'],'draft':live['draft']})
        s.audit(c,'current_detail_attached',ident,str(detail['run_id']))
        return s.save(c,'listing',live)


def material_view(listing,*,target=None,connection=None,copy_record=True):
    result=copy.deepcopy(listing) if copy_record else listing
    draft=result.get('draft') or {};missing=[]
    if not draft:missing.append('尚未生成资料')
    else:
        if not draft.get('brand'):missing.append('品牌')
        if not draft.get('skus'):missing.append('实际SKU')
        for sku in draft.get('skus',[]):
            if not sku.get('size'):missing.append(sku['seller_sku']+'：实际尺寸')
            if not sku.get('images') and not any(a.get('variant_key')==sku.get('variant_key') for a in draft.get('images',[])):
                missing.append(sku['seller_sku']+'：商品图片')
    result['material_issues']=missing
    if draft:
        from . import locales
        if target is None:
            if connection is not None:target=s.get(connection,'target',listing['target_id'])
            else:
                with s.main().db() as c:target=s.get(c,'target',listing['target_id'])
        try:locales.assert_current(target,draft)
        except HTTPException as exc:missing.append(str(exc.detail))
    result['material_status']='needs_fields' if missing else 'ready'
    result['platform_issues']=issues(draft,target,connection=connection) if draft else ['尚未生成资料']
    return result


def refresh_draft_prices(c,product_id):
    """Propagate archive prices to idle, unsubmitted drafts without regenerating content."""
    from . import environment,pricing
    changed=[];rate_cache={}
    for listing in s.all_rows(c,'listing'):
        if (listing['product_id']!=product_id or not listing.get('draft') or
                listing.get('published_version') is not None or listing.get('status')!='not_submitted'):continue
        target=s.get(c,'target',listing['target_id'])
        account=c.execute('SELECT enabled FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
        if not target.get('enabled') or not account or not account['enabled'] or not environment.allowed(target['environment']):continue
        if c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(listing['id'],)).fetchone():continue
        draft=copy.deepcopy(listing['draft'])
        for entry in draft.get('skus',[]):
            status=(entry.get('price_source') or {}).get('status')
            if status=='manual':continue
            # A historical amount without provenance is not permission to replace it.
            if entry.get('price') is not None and status not in {'same_currency','converted'}:continue
            sku=s.get(c,'sku',entry['sku_id'])
            if sku['product_id']!=product_id:continue
            entry.update(pricing.quote(sku,draft['currency'],rate_cache,connection=c))
        if draft==listing['draft']:continue
        listing['draft']=draft;listing['draft_version']+=1;listing['issues']=issues(draft,target,connection=c)
        listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
        listing.pop('preview_results',None)
        s.put(c,'material_version',f"{listing['id']}:{listing['draft_version']}",
              {'listing_id':listing['id'],'version':listing['draft_version'],'draft':draft})
        s.audit(c,'archive_prices_refreshed',listing['id'],'商品档案售价同步；保留手工站点售价、文案及图片')
        s.save(c,'listing',listing);changed.append(listing['id'])
    return changed


def refresh_archive_fields(ident,*,refresh_offers=False,allow_published=False,expected_revision=None):
    """Reassemble classification from archive/configuration while retaining copy/offers."""
    from . import archive_fields,product_archive,listing_identity
    with s.transaction() as c:
        listing=s.get(c,'listing',ident)
        if expected_revision is not None and listing['revision']!=expected_revision:raise HTTPException(409,'资料已变化，请刷新后重新组装')
        draft=copy.deepcopy(listing.get('draft') or {})
        if not draft:raise HTTPException(422,'请先生成资料')
        product=s.get(c,'product',listing['product_id']);target=s.get(c,'target',listing['target_id'])
        if refresh_offers:
            from . import costing
            product=costing.apply(c,product)
        selected=[v['sku_id'] for v in draft['skus']]
        digest=product_archive.fact_digest(c,product,selected)
        old_digest=draft.get('source',{}).get('archive_digest')
        if old_digest and digest!=old_digest:raise HTTPException(409,'实物资料已变化，请重新设计后组装资料')
        if not allow_published and (listing.get('published_version') is not None or listing.get('status')!='not_submitted'):raise HTTPException(409,'已提交资料请通过重新生成流程更新')
        if allow_published:
            if refresh_offers:raise HTTPException(422,'档案字段重组不改动已确认报价与库存')
            listing_identity.assert_target_scope(target,draft)
            if target.get('environment')=='production' and not old_digest:raise HTTPException(409,'旧资料缺少实物输入身份，请先核验设计')
        if c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(ident,)).fetchone():raise HTTPException(409,'资料任务正在处理，请稍后重试')
        categories.apply_defaults(c,product,target,draft)
        definition=archive_fields.definition(c,target,draft.get('product_type','')) or {}
        schema=definition.get('schema',{})
        if allow_published and not schema:raise HTTPException(422,'请先读取当前店铺的完整官方字段规则')
        for entry in draft['skus']:
            sku=s.get(c,'sku',entry['sku_id'])
            from . import attribute_localization,locales
            fallback_sources={}
            entry['attributes'],projection=attribute_localization.project(
                archive_fields.encode(c,product,sku,target,schema,draft.get('product_type',''),provenance=fallback_sources),
                locales.resolve(target),sku.get('components'),
                translations=draft.get('source',{}).get('attribute_translations',()),schema=schema,
                prior_proof=draft.get('source',{}).get('attribute_localization',{}).get(str(entry['sku_id'])))
            draft.setdefault('source',{}).setdefault('attribute_localization',{})[str(entry['sku_id'])]=projection
            draft['source'].setdefault('attribute_fallbacks',{})[str(entry['sku_id'])]=fallback_sources
        if refresh_offers:
            from . import offer_defaults
            offer_defaults.apply(c,product,target,listing,draft,refresh_prices=True)
        draft.setdefault('source',{})['schema_hash']=definition.get('schema_sha256','')
        source=draft.setdefault('source',{})
        source['product_fact_revision']=product.get('fact_revision',0)
        source['skus']=[v|{'fact_revision':v.get('fact_revision',0)} for v in product_archive.selected_skus(c,product,selected)]
        if hasattr(listing_identity,'archive_signature'):source['archive_signature']=listing_identity.archive_signature(c,product,target,selected)
        from . import supplier_declarations
        declared=supplier_declarations.for_product(product)
        if declared:source['supplier_declarations']=declared
        else:source.pop('supplier_declarations',None)
        from . import attribute_completion
        attribute_completion.restore(draft,listing['draft'])
        if allow_published:listing_identity.assert_source_current(c,listing,target,draft)
        if draft==listing['draft']:return listing
        from . import listing_release,merchant_terms
        listing_release.apply_cycle_names(listing,draft)
        merchant_terms.validate_mode(draft)
        listing['draft']=draft;listing['draft_version']+=1;listing['issues']=issues(draft,target)
        listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
        listing.pop('preview_results',None)
        s.put(c,'material_version',f"{ident}:{listing['draft_version']}",{'listing_id':ident,'version':listing['draft_version'],'draft':draft})
        s.audit(c,'archive_classification_reassembled',ident,'保留文案与店铺经营值')
        return s.save(c,'listing',listing)


def issues(draft,target=None,*,connection=None):
    from . import listing_copy
    errors=[]
    if draft.get('content_review_required'):errors.append('商品事实更新后请核对保留的人工文案并保存')
    from .listing_identity import offer_only
    has_new=any(not offer_only(v) for v in draft.get('skus',[]))
    if has_new and not draft.get('content',{}).get('title'):errors.append('商品标题')
    if has_new and len(draft.get('content',{}).get('title',''))>listing_copy.TITLE_MAX:errors.append('商品标题最多75字符（含空格和标点）')
    if has_new and not listing_copy.search_terms_valid(draft.get('content',{}).get('search_terms','')):errors.append('搜索词过长，请精简相关词')
    if has_new and not draft.get('product_type'):errors.append('Amazon产品类型')
    if has_new and not draft.get('brand'):errors.append('品牌')
    if not draft.get('handling_days'):errors.append('备货天数')
    shipping_required=draft.get('shipping_template_required')
    if target and target.get('environment')=='production':
        from . import shipping
        product_type=draft.get('product_type') if has_new else 'PRODUCT'
        shipping_required=(shipping.required(target,product_type,connection=connection)
                           if connection is not None else shipping.required(target,product_type))
    if shipping_required is True and not draft.get('shipping_group'):errors.append('配送模板')
    if not draft.get('skus'):errors.append('实际SKU')
    if draft.get('variation_theme') and len(draft.get('skus',[]))<2:errors.append('变体家族至少需要两个实际SKU')
    relation_change=(draft.get('source',{}).get('variation_family') or {}).get('existing_relationship_change') or {}
    if relation_change.get('status')=='requires_platform_change':
        errors.append('已发布父子关系需先在平台解除或调整并回读；新分组尚未改变线上关系')
    for sku in draft.get('skus',[]):
        label=sku['seller_sku']
        if not offer_only(sku) and 'title' in sku and not str(sku.get('title') or '').strip():
            errors.append(label+'：SKU上架标题')
        if not offer_only(sku) and len(sku.get('title',''))>listing_copy.TITLE_MAX:errors.append(label+'：SKU上架标题最多75字符（含空格和标点）')
        if not offer_only(sku) and 'search_terms' in sku and not listing_copy.search_terms_valid(sku['search_terms']):
            errors.append(label+'：规格搜索词过长，请重新生成资料或调整共用搜索词')
        if sku.get('price') is None:errors.append(label+'：站点售价')
        if (target and target.get('environment')=='production' and sku.get('price') is not None and
                sku.get('price_source',{}).get('status') not in {'manual','same_currency','converted'}):
            errors.append(label+'：历史售价来源待核对，请按商品档案补报价或明确确认售价')
        if not (sku.get('gtin') or sku.get('asin') or sku.get('gtin_exempt')) and not (target and target.get('environment')=='production'):
            errors.append(label+'：GTIN/ASIN或无GTIN平台预检')
        images=sku.get('images',[])
        if sku.get('quantity') is None:errors.append(label+'：真实库存或生产能力')
        if target and target.get('environment')=='production' and sku.get('quantity_source',{}).get('status') not in {'manual','stock','archive_capacity','confirmed_config'}:errors.append(label+'：可售量来源待核对')
        if not offer_only(sku) and not any(x.get('role')=='main' for x in images):errors.append(label+'：主图')
        if offer_only(sku) and draft.get('variation_theme'):errors.append(label+'：已有ASIN报价模式请清空新建变体主题')
    if target and target.get('environment')=='production' and draft.get('handling_source',{}).get('status') not in {'manual','archive','confirmed_config'}:errors.append('备货期来源待核对')
    return errors


def submission_issues(draft,target,listing=None):
    """Keep mock-only platform prerequisites visible while exercising local flow."""
    found=issues(draft,target)
    if listing is not None:
        from . import listing_relationships
        found=listing_relationships.filter_issues(found,listing|{'draft':draft},target)
    if target.get('environment')!='mock':
        return found,[]
    deferred=[item for item in found if item=='配送模板' or
              item.endswith('：GTIN/ASIN或无GTIN平台预检')]
    return [item for item in found if item not in deferred],deferred


def update_draft(ident,payload):
    from . import shipping,locales,identifiers,main_images,listing_copy
    with s.transaction() as c:
        listing=s.get(c,'listing',ident)
        if listing['revision']!=payload.get('revision'):raise HTTPException(409,'资料已变化，请刷新')
        draft=copy.deepcopy(payload['draft'])
        target=s.get(c,'target',listing['target_id'])
        draft['shipping_template_required']=shipping.required(target,draft.get('product_type'))
        # Identity and source snapshots cannot be rewritten through the editor.
        old=listing['draft'];draft['source']=copy.deepcopy(old.get('source',{}))
        draft.setdefault('price_shipping_mode',old.get('price_shipping_mode','unspecified'))
        from . import merchant_terms
        merchant_terms.validate_mode(draft)
        if not old:raise HTTPException(422,'请先生成资料')
        confirmed_prices=payload.get('confirmed_price_sku_ids',[])
        if (not isinstance(confirmed_prices,list) or any(type(value) is not int for value in confirmed_prices) or
                len(set(confirmed_prices))!=len(confirmed_prices) or
                not set(confirmed_prices)<={row['sku_id'] for row in old.get('skus',[])}):
            raise HTTPException(422,'售价确认必须属于当前实际 SKU')
        confirmed_quantities=payload.get('confirmed_quantity_sku_ids',[])
        if (not isinstance(confirmed_quantities,list) or any(type(value) is not int for value in confirmed_quantities) or
                len(set(confirmed_quantities))!=len(confirmed_quantities) or
                not set(confirmed_quantities)<={row['sku_id'] for row in old.get('skus',[])}):
            raise HTTPException(422,'可售量确认必须属于当前实际 SKU')
        if 'confirm_handling_days' in payload and type(payload['confirm_handling_days']) is not bool:
            raise HTTPException(422,'备货期确认必须为明确确认')
        for field in ('brand','product_type','attributes','parent_attributes','offer_attributes','origin','platform_category_id','platform_category_path'):
            if draft.get(field)!=old.get(field):raise HTTPException(422,'商品事实只在商品档案维护，请修改档案后重新组装资料：'+field)
        if draft.get('language')!=old.get('language'):raise HTTPException(409,'资料语言由店铺地区配置决定，请修改配置后重新生成')
        draft['localization']=copy.deepcopy(old.get('localization'))
        locales.assert_current(s.get(c,'target',listing['target_id']),draft)
        saved_detail=old.get('detail_page')
        if draft.get('detail_page',saved_detail)!=saved_detail:
            raise HTTPException(409,'详情页随生产资料生成，请重新生成生产资料')
        draft['detail_page']=copy.deepcopy(saved_detail)
        draft['brand_source']=copy.deepcopy(old.get('brand_source',{}))
        content=draft.get('content',{})
        content_changed=any(content.get(field)!=old.get('content',{}).get(field) for field in ('title','bullets','description','search_terms'))
        search_terms_changed=content.get('search_terms','')!=old.get('content',{}).get('search_terms','')
        if content_changed:
            content['source']='manual'
            draft['source'].pop('copy_review',None)
        else:
            draft['content']=copy.deepcopy(old.get('content',{}));content=draft['content']
        draft['content_review_required']=bool(old.get('content_review_required') and not content_changed and payload.get('confirm_content_review') is not True)
        draft['handling_source']=copy.deepcopy(old.get('handling_source',{}))
        # Preserve an unchanged legacy title while unrelated fields are edited.
        title_limit=180 if content.get('title')==old.get('content',{}).get('title') else listing_copy.TITLE_MAX
        for name,limit in [('title',title_limit),('description',1800)]:
            if not isinstance(content.get(name,''),str) or len(content.get(name,''))>limit:raise HTTPException(422,'文案字段格式或长度无效：'+name)
        if not isinstance(content.get('search_terms',''),str):raise HTTPException(422,'文案字段格式或长度无效：search_terms')
        if search_terms_changed and not listing_copy.search_terms_valid(content.get('search_terms','')):raise HTTPException(422,'搜索词过长，请精简相关词')
        if not isinstance(content.get('bullets'),list) or len(content['bullets'])>5 or any(not isinstance(x,str) or len(x)>250 for x in content['bullets']):raise HTTPException(422,'卖点最多5条，每条250字符')
        if not isinstance(draft.get('attributes',{}),dict):raise HTTPException(422,'平台属性必须为JSON对象')
        theme=str(draft.get('variation_theme','')).strip()
        if theme and not re.fullmatch('[A-Z0-9_/]{1,100}',theme):raise HTTPException(422,'变体主题请填写平台规则提供的枚举值')
        draft['variation_theme']=theme;draft['parent_sku']=old.get('parent_sku') or f"W{listing['product_id']:05d}-PARENT"
        if theme!=old.get('variation_theme',''):
            draft['source']['variation_preference']='manual_theme' if theme else 'manual_independent'
        for entry in draft.get('skus',[]):
            previous=next((v for v in old.get('skus',[]) if v.get('sku_id')==entry.get('sku_id')), {})
            entry['variation_attributes']=copy.deepcopy(previous.get('variation_attributes',{}))
        classification=draft.get('classification')
        if classification and classification!=old.get('classification'):
            expected={}
            categories.apply_defaults(c,s.get(c,'product',listing['product_id']),s.get(c,'target',listing['target_id']),expected)
            if classification!=expected.get('classification'):raise HTTPException(409,'分类映射已更新，请重新读取本地分类')
        if classification and classification.get('product_type')!=draft.get('product_type'):
            draft.pop('classification',None)
        for img in draft.get('images',[]):
            s.get(c,'media',img['asset_id'])
            if img.get('role') not in {'effect','detail'}:raise HTTPException(422,'成品图角色无效')
            if img.get('variant_key') not in {v['variant_key'] for v in old['skus']}:raise HTTPException(422,'成品图必须绑定实际SKU')
        previous={v['sku_id']:v for v in old['skus']}
        if len(draft.get('skus',[]))!=len(previous) or {v.get('sku_id') for v in draft['skus']}!=set(previous):raise HTTPException(422,'不能在资料编辑中增删SKU，请修改商品档案后重新生成')
        for v in draft['skus']:
            v['seller_sku']=previous[v['sku_id']]['seller_sku'];v['variant_key']=previous[v['sku_id']]['variant_key']
            v['price']=s.money(v.get('price'),True);v['quantity']=s.listing_quantity(v['quantity']) if v.get('quantity') is not None else None
            original=previous[v['sku_id']]
            v.pop('search_terms',None)
            if not search_terms_changed and 'search_terms' in original:
                v['search_terms']=original['search_terms']
            if 'title' in v:
                limit=180 if v.get('title')==original.get('title') else listing_copy.TITLE_MAX
                if not isinstance(v['title'],str) or len(v['title'])>limit:
                    raise HTTPException(422,f'SKU上架标题必须为最多{limit}字符的文本')
                if v.get('title')!=original.get('title') or 'title' not in original:
                    v['title_source']={'status':'manual','language':locales.resolve(target)}
                    draft['source'].pop('copy_review',None)
                else:v['title_source']=copy.deepcopy(original.get('title_source',{}))
            elif 'title' in original:
                # Omitting a field from an older editor cannot erase a title.
                v['title']=original['title'];v['title_source']=copy.deepcopy(original.get('title_source',{}))
            else:v.pop('title_source',None)
            v['quantity_source']=copy.deepcopy(original.get('quantity_source',{}))
            for field in ('name','color','size','gtin','asin','attributes','origin','submission_intent','gtin_exempt'):
                if v.get(field)!=original.get(field):raise HTTPException(422,'规格事实只在商品档案维护，请修改档案后重新组装资料：'+field)
            v['size_source']=copy.deepcopy(original.get('size_source',{}))
            v['price_source']=copy.deepcopy(original.get('price_source',{}))
            if v['price']!=original.get('price') or draft.get('currency')!=old.get('currency') or v['sku_id'] in confirmed_prices:
                v['price_source']={'status':'manual','target_currency':draft.get('currency'),'message':'生产资料中手工填写的售价'}
            if v['sku_id'] in confirmed_quantities and v['quantity'] is None:raise HTTPException(422,'请填写可售量后再确认')
            if v['quantity']!=original.get('quantity') or v['sku_id'] in confirmed_quantities:v['quantity_source']={'status':'manual','message':'商家明确设置或确认的店铺可售量'}
            if v.get('asin') and not re.fullmatch('[A-Z0-9]{10}',v['asin']):raise HTTPException(422,'ASIN应为10位大写字母或数字')
            if v.get('gtin') and not identifiers.valid_gtin(v['gtin']):raise HTTPException(422,'GTIN须为校验位正确的12位UPC或13位EAN，且应属于本商品')
            if not isinstance(v.get('attributes',{}),dict):raise HTTPException(422,'SKU属性必须为JSON对象')
            if len(v.get('images',[]))>9 or sum(x.get('role')=='main' for x in v.get('images',[]))>1:raise HTTPException(422,'每个SKU最多一张主图、八张附图')
            for img in v.get('images',[]):
                s.get(c,'media',img['asset_id'])
                if img.get('role') not in {'main','effect','detail'}:raise HTTPException(422,'图片角色无效')
                if (img.get('role')=='main' and target['environment']=='production' and
                        img['asset_id'] not in {old_image['asset_id'] for old_image in original.get('images',[]) if old_image.get('role')=='main'}):
                    main_images.validate_selection(c,draft,v,img['asset_id'])
        if content_changed or any(row.get('title')!=previous[row['sku_id']].get('title') for row in draft['skus']):
            generation=draft['source'].get('copy_generation') or {}
            if 'marketing_strategy' in generation:
                generation.pop('marketing_strategy')
                generation['manual_copy_preserved']=True
        if draft.get('handling_days') is not None:draft['handling_days']=s.quantity(draft['handling_days'],1)
        if payload.get('confirm_handling_days') is True and draft.get('handling_days') is None:raise HTTPException(422,'请填写备货期后再确认')
        if draft.get('handling_days')!=old.get('handling_days') or payload.get('confirm_handling_days') is True:draft['handling_source']={'status':'manual','message':'商家明确设置或确认的店铺备货期'}
        if not re.fullmatch('[A-Z]{3}',draft.get('currency','')):raise HTTPException(422,'币种需为三个大写字母')
        listing['draft']=draft;listing['draft_version']+=1;listing['issues']=issues(draft,target)
        listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
        s.put(c,'material_version',f"{ident}:{listing['draft_version']}",{'listing_id':ident,'version':listing['draft_version'],'draft':draft})
        return s.save(c,'listing',listing)


def assign_seller_sku(listing_id,sku_id,new_value):
    """Version an unpublished draft after a verified remote Seller SKU collision."""
    with s.transaction() as c:
        listing=s.get(c,'listing',listing_id)
        from . import listing_release
        listing_release.assert_action(listing,'assign_seller_sku')
        if listing.get('published_version'):
            raise HTTPException(409,'已发布刊登不能自动更换 Seller SKU')
        target=s.get(c,'target',listing['target_id'])
        draft=copy.deepcopy(listing.get('draft') or {})
        entry=next((v for v in draft.get('skus',[]) if v['sku_id']==sku_id),None)
        if not entry:
            raise HTTPException(404,'商品规格不存在')
        if any(any(item.get('sku_id')==sku_id for item in order.get('items',[]))
               for order in s.all_rows(c,'order')):
            raise HTTPException(409,'该 SKU 已关联历史订单，不能自动更换 Seller SKU')
        if any(v['seller_sku']==new_value for v in draft['skus'] if v['sku_id']!=sku_id):
            raise HTTPException(409,'Seller SKU 与同商品其他规格冲突')
        identity_key=f"{target['account_id']}:{target['marketplace_id']}:{sku_id}"
        identity=s.by_key(c,'seller_sku_identity',identity_key)
        if not identity:
            raise HTTPException(409,'Seller SKU 映射不存在')
        entry['seller_sku']=new_value
        if listing_release.cycle(listing):
            listing.setdefault('publication_sku_names',{})[str(sku_id)]=new_value
        else:
            identity['seller_sku']=new_value
            identity['checked']=True
            s.save(c,'seller_sku_identity',identity)
        listing['draft']=draft
        listing['draft_version']+=1
        listing['issues']=issues(draft,target)
        listing['draft_status']='needs_fields' if listing['issues'] else 'ready'
        s.put(c,'material_version',f"{listing_id}:{listing['draft_version']}",
              {'listing_id':listing_id,'version':listing['draft_version'],'draft':draft})
        s.audit(c,'seller_sku_collision_reassigned',listing_id,
                s.dump({'sku_id':sku_id,'seller_sku':new_value}))
        return s.save(c,'listing',listing)


def _origin_country_value(origin, schema):
    if not isinstance(origin,dict) or origin.get('status')!='ready' or not isinstance(schema,dict):
        return None
    rule=(schema.get('properties') or {}).get('country_of_origin')
    if not isinstance(rule,dict):
        return None
    value_rule=(((rule.get('items') or {}).get('properties') or {}).get('value') or {})
    allowed=value_rule.get('enum') or []
    candidates={'CN':('China','CHINA','CN','中国')}.get(origin.get('country_code'),(origin.get('country'),))
    if allowed:
        match=next((item for item in allowed if isinstance(item,str) and
                    any(item.casefold()==candidate.casefold() for candidate in candidates if candidate)),None)
        if match is None:
            raise HTTPException(422,'当前 Amazon 商品类型的原产国选项未能匹配已保存的产地')
        return match
    return candidates[0] if candidates and candidates[0] else None


def attributes(target,draft,sku,definition_schema=None,*,packaging_provenance=None):
    from . import locales
    mid=target['marketplace_id'];lang=locales.assert_current(target,draft)
    def text(v):return [{'value':v,'language_tag':lang,'marketplace_id':mid}]
    a=copy.deepcopy(draft.get('attributes',{}));a.update(copy.deepcopy(sku.get('attributes',{})))
    if definition_schema and not draft.get('source',{}).get('archive_signature'):
        from . import source_attributes
        source_sku=next((v for v in (draft.get('source') or {}).get('skus',[])
                         if v.get('id')==sku.get('sku_id')), {})
        for key,value in source_attributes.from_sku(source_sku,definition_schema,mid,lang).items():
            a.setdefault(key,value)
    content=draft['content']
    a.update(item_name=text(sku['title'] if 'title' in sku else content['title']),brand=text(draft['brand']),product_description=text(content.get('description','')),
      bullet_point=[{'value':x,'language_tag':lang,'marketplace_id':mid} for x in content.get('bullets',[])],
      condition_type=[{'value':'new_new','marketplace_id':mid}],
      fulfillment_availability=[{'fulfillment_channel_code':'DEFAULT','quantity':sku['quantity'],'lead_time_to_ship_max_days':draft['handling_days']}],
      purchasable_offer=[{'currency':draft['currency'],'marketplace_id':mid,'audience':'ALL','our_price':[{'schedule':[{'value_with_tax':float(sku['price'])}]}]}],
      merchant_shipping_group=[{'value':draft['shipping_group'],'marketplace_id':mid}])
    if not draft.get('shipping_group'):a.pop('merchant_shipping_group',None)
    from .listing_identity import offer_only
    if offer_only(sku):
        offer={k:a[k] for k in ['condition_type','fulfillment_availability','purchasable_offer','merchant_shipping_group'] if k in a}
        offer.update({k:copy.deepcopy(v) for k,v in draft.get('offer_attributes',{}).items() if k in {'condition_type','fulfillment_availability','purchasable_offer','merchant_shipping_group'}})
        offer['merchant_suggested_asin']=[{'value':sku['asin'],'marketplace_id':mid}]
        return offer
    country=_origin_country_value(sku.get('origin') if 'origin' in sku else draft.get('origin'),definition_schema)
    if country:
        a['country_of_origin']=[{'value':country,'marketplace_id':mid}]
    if sku.get('color'):a['color']=text(sku['color'])
    if sku.get('size'):a['size']=text(sku['size'])
    if draft.get('variation_theme'):
        a.update(copy.deepcopy(sku.get('variation_attributes',{})))
    search_terms=sku.get('search_terms',content.get('search_terms',''))
    if search_terms:a['generic_keyword']=text(search_terms)
    else:a.pop('generic_keyword',None)
    if draft.get('variation_theme'):
        a['parentage_level']=[{'value':'child','marketplace_id':mid}]
        # The theme object is unscoped; current PTDs reject marketplace_id here.
        a['variation_theme']=[{'name':draft['variation_theme']}]
        a['child_parent_sku_relationship']=[{'child_relationship_type':'variation','parent_sku':draft['parent_sku'],'marketplace_id':mid}]
    else:
        # Stale parent/child metadata must not leak through generic attributes
        # when a supported independent-product plan has been selected.
        for name in ('parentage_level','variation_theme','child_parent_sku_relationship'):
            a.pop(name,None)
    if sku.get('asin'):a['merchant_suggested_asin']=[{'value':sku['asin'],'marketplace_id':mid}]
    elif sku.get('gtin'):a['externally_assigned_product_identifier']=[{'type':'ean' if len(sku['gtin'])==13 else 'upc','value':sku['gtin'],'marketplace_id':mid}]
    elif sku.get('gtin_exempt'):a['supplier_declared_has_product_identifier_exemption']=[{'value':True,'marketplace_id':mid}]
    images=list(sku.get('images',[]))+[x for x in draft.get('images',[]) if x.get('variant_key')==sku['variant_key']]
    from . import main_images
    with s.main().db() as c:
        reference_boards=main_images.reference_board_ids(c,images)
    images=[img for img in images if img['asset_id'] not in reference_boards]
    if target['environment']=='production':
        with s.main().db() as c:
            excluded=set(main_images.validate_auxiliary(c,draft,sku)['excluded_asset_ids'])
        # A verified SKU main image may also appear in an old effect slot.
        # Excluding that auxiliary reference must not remove the main image.
        images=[img for img in images if img.get('role')=='main' or img['asset_id'] not in excluded]
    # Prefer the SKU's explicit main image; remove duplicates before limiting auxiliaries.
    main_image = next((img for img in images if img.get('role') == 'main'), None)
    selected = [main_image] if main_image else []
    seen = {main_image['asset_id']} if main_image else set()
    for img in images:
        if img['asset_id'] in seen or img.get('role') == 'main':continue
        seen.add(img['asset_id']);selected.append(img)
        if len(selected) >= (9 if main_image else 8):break
    for name in list(a):
        if name == 'main_product_image_locator' or name.startswith('other_product_image_locator_'):a.pop(name)
    index=0
    with s.main().db() as c:
        for img in selected:
            asset=s.get(c,'media',img['asset_id']);url=media.locator(asset,required=target['environment']!='mock')
            name='main_product_image_locator' if img.get('role')=='main' else f'other_product_image_locator_{index+1}'
            if img.get('role')!='main':index+=1
            if index>8:continue
            a[name]=[{'media_location':url or asset['url'],'marketplace_id':mid}]
    from . import packaging
    archived=next((row for row in draft.get('source',{}).get('skus',[]) if row.get('id')==sku.get('sku_id')), {})
    return packaging.project(a,definition_schema,archived,mid,lang,provenance=packaging_provenance)


def parent_attributes(target,draft,definition_schema=None):
    # A parent represents the family; never inherit one child's material/count/weight.
    shared_sku=copy.deepcopy(draft['skus'][0]);shared_sku['attributes']={}
    shared_sku.pop('title',None)
    shared_sku.pop('search_terms',None)
    a=attributes(target,draft,shared_sku)
    for k in ['fulfillment_availability','purchasable_offer','merchant_shipping_group','merchant_suggested_asin',
              'externally_assigned_product_identifier','supplier_declared_has_product_identifier_exemption',
              'child_parent_sku_relationship','color','size']:
        a.pop(k,None)
    for field in shared_sku.get('variation_attributes',{}):a.pop(field,None)
    a.update(copy.deepcopy(draft.get('parent_attributes',{})))
    # A stored parent value must not override known per-SKU differences (for
    # example, the different weights of low/medium/high versions).
    child_facts=[dict(draft.get('attributes',{}),**sku.get('attributes',{})) for sku in draft['skus']]
    for key in set().union(*(child.keys() for child in child_facts)):
        if any(key not in child or child[key]!=child_facts[0].get(key) for child in child_facts):
            a.pop(key,None)
    a['parentage_level']=[{'value':'parent','marketplace_id':target['marketplace_id']}]
    a['child_parent_sku_relationship']=[{'child_relationship_type':'variation','marketplace_id':target['marketplace_id']}]
    if definition_schema is not None:
        allowed=definition_schema.get('properties',{})
        # Child PTDs accept physical/offer fields that a nonbuyable parent does
        # not. The fresh parent PTD is the authority for the parent projection.
        a={key:value for key,value in a.items() if key in allowed}
        excluded={'parentage_level','child_parent_sku_relationship','variation_theme',
                  'color','size','condition_type','fulfillment_availability',
                  'purchasable_offer','merchant_shipping_group','merchant_suggested_asin',
                  'externally_assigned_product_identifier','supplier_declared_has_product_identifier_exemption'}
        excluded.update(key for sku in draft['skus'] for key in sku.get('variation_attributes',{}))
        children=[sku.get('attributes',{}) for sku in draft['skus']]
        # Copy a sourced declaration only when every real child has exactly the
        # same value. Missing battery/compliance facts remain missing, not false.
        for key,value in children[0].items():
            if (key in allowed and key not in excluded and not key.endswith('_image_locator')
                    and '_image_locator_' not in key and
                    all(key in child and child[key]==value for child in children)):
                a[key]=copy.deepcopy(value)
        from .variation_families import battery_declaration
        if 'batteries_required' in allowed and battery_declaration(draft):
            a['batteries_required']=[{'value':False,'marketplace_id':target['marketplace_id']}]
    return a
