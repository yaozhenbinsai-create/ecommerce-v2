from __future__ import annotations
import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime,timezone
from fastapi import APIRouter,HTTPException,UploadFile,File,Form,Request
from fastapi.responses import FileResponse,StreamingResponse
from pydantic import BaseModel,Field
from . import store as s, media, catalog, operations, amazon, reports, categories, targets, support

router=APIRouter(prefix='/api/commerce')
router.include_router(support.router)
KINDS={'product','sku','target','listing','media','order','production','shipment','aftersale','expense','transaction','rule','campaign','notice','replenishment','report','category','category_mapping'}


@router.get('/security-readiness')
def security_readiness(request:Request):
    if getattr(request.state, 'commerce_role', None) != 'admin':
        raise HTTPException(403, '安全检查需要管理员账号')
    from app.security_readiness import inspect
    return inspect()


@router.get('/accounts/{ident}/connection')
def account_connection(ident:int):
    from .account_connection import read
    return read(ident)


@router.post('/accounts/{ident}/credentials')
def account_credentials(ident:int,payload:dict):
    from .account_connection import save
    return save(ident,payload)


@router.post('/businesses/map',status_code=202)
def map_businesses(payload:dict|None=None):
    bid=(payload or {}).get('business_id')
    read_official=(payload or {}).get('read_official',False)
    if not isinstance(read_official,bool):raise HTTPException(422,'官方分类同步选项必须为布尔值')
    with s.transaction() as c:
        ids=[r[0] for r in c.execute('SELECT id FROM businesses WHERE enabled=1 ORDER BY id') if bid is None or r[0]==s.quantity(bid,1)]
        if not ids:raise HTTPException(422,'请先保存并启用业务')
        encoded=s.dump(ids)
        running=c.execute("SELECT id FROM commerce_jobs WHERE kind='business_mapping' AND json_extract(payload,'$.business_ids')=? AND status IN ('queued','running','retry_wait')",(encoded,)).fetchone()
        if running:
            active=s.job(c,running[0])
            if read_official and not active['payload'].get('read_official'):raise HTTPException(409,'本地映射正在运行，完成后请重试官方分类同步')
            return active
        return s.enqueue(c,'business_mapping','business-map:'+uuid.uuid4().hex,{'business_ids':ids,'read_official':read_official})


@router.get('/businesses/mapping-results')
def business_mapping_results():
    from . import category_rules
    with s.main().db() as c:return [category_rules.refresh_result(c,x) for x in s.all_rows(c,'business_mapping')]


@router.get('/businesses/mapping-jobs/{ident}')
def business_mapping_job(ident:int):
    with s.main().db() as c:
        job=s.job(c,ident)
        if job['kind']!='business_mapping':raise HTTPException(404)
        return job


@router.post('/categories')
def category_create(payload:dict):
    with s.transaction() as c:return categories.save_category(c,payload)


@router.patch('/categories/{ident}')
def category_update(ident:int,payload:dict):
    with s.transaction() as c:return categories.save_category(c,payload,ident)


@router.post('/category-mappings')
def mapping_create(payload:dict):
    with s.transaction() as c:return categories.save_mapping(c,payload)


@router.patch('/category-mappings/{ident}')
def mapping_update(ident:int,payload:dict):
    with s.transaction() as c:return categories.save_mapping(c,payload,ident)


@router.get('/categories/export')
def categories_export():
    from fastapi.responses import Response
    with s.main().db() as c:data=categories.export_data(c)
    return Response(s.dump(data),media_type='application/json',headers={'Content-Disposition':'attachment; filename="platform-categories.json"'})


@router.get('/categories/details')
def category_details():
    from . import category_rules
    with s.main().db() as c:
        mappings=s.all_rows(c,'category_mapping');scoped=[]
        for mapping in mappings:
            if mapping.get('platform')!='亚马逊':scoped.append(mapping);continue
            matches=[target for target in s.all_rows(c,'target') if target.get('marketplace_id')==mapping.get('marketplace_id') and target.get('enabled')]
            for target in matches:
                scoped.append(category_rules.mapping_for_target(mapping,target)|{'target_id':target['id'],'target_name':target.get('name',''),'target_account_name':target.get('account_name','')})
            if not matches:scoped.append(category_rules.mapping_for_target(mapping,{'account_id':None,'seller_id':'','marketplace_id':mapping.get('marketplace_id')}))
        return {'categories':s.all_rows(c,'category'),'mappings':mappings,'target_mappings':scoped}


@router.post('/category-mappings/{ident}/classification-choice')
def classification_choice(ident:int,payload:dict):
    from . import category_rules,business_mapping
    mapping=category_rules.choose(ident,payload)
    with s.main().db() as c:category=s.get(c,'category',mapping['category_id_local'])
    updated=[]
    for bid in category.get('business_ids',[]):updated.extend(business_mapping.apply_to_products(bid,category))
    return {'mapping':mapping,'drafts_updated':updated}


@router.post('/categories/import')
def categories_import(payload:dict,preview:bool=True):return categories.import_data(payload,preview)


@router.post('/categories/initialize')
def categories_initialize():return categories.seed_existing()


@router.get('/listings/{ident}/classification')
def listing_classification(ident:int):
    with s.main().db() as c:
        listing=s.get(c,'listing',ident)
        defaults={}
        result=categories.apply_defaults(c,s.get(c,'product',listing['product_id']),s.get(c,'target',listing['target_id']),defaults)
        return result|{'defaults':defaults}


@router.post('/category-types/search')
def category_type_search(payload:dict):
    with s.main().db() as c:
        target=s.get(c,'target',s.quantity(payload.get('target_id'),1))
        if categories.target_platform(c,target)!='亚马逊':raise HTTPException(422,'此平台暂未接入分类查询，请手动维护或导入')
    try:return amazon.search_product_types(target,str(payload.get('query',''))[:200],payload.get('mode','keywords'))
    except amazon.PlatformError as exc:raise HTTPException(502,str(exc))


class Target(BaseModel):
    account_id:int
    marketplace_id:str=Field(default=amazon.US,min_length=1,max_length=80)
    name:str=Field(default='美国站',max_length=100)
    environment:str=Field(default='production',pattern='^(mock|sandbox|production)$')
    region:str=Field(default='NA',pattern='^(NA|EU|FE)$')
    currency:str=Field(default='USD',pattern='^[A-Z]{3}$')
    language:str=Field(default='',max_length=20)
    language_mode:str|None=Field(default=None,pattern='^(marketplace|explicit)$')
    enabled:bool=True
    include_recipient:bool=False
    auto_sync_orders:bool=False
    orders_since:str=''


class AccountRegions(BaseModel):
    account_id:int=Field(gt=0,strict=True)
    marketplace_ids:list[str]=Field(max_length=100)


class TargetBindings(BaseModel):
    environment:str=Field(pattern='^(mock|sandbox|production)$')
    account_ids:list[int]=Field(min_length=1,max_length=500)
    selections:list[AccountRegions]=Field(max_length=500)
    revisions:dict[int,int]


@router.get('/target-bindings')
def target_bindings():return targets.configuration()


@router.put('/target-bindings')
def save_target_bindings(payload:TargetBindings):return targets.save_bindings(payload)


@router.post('/target-sync-batch',status_code=202)
def target_sync_batch(payload:dict):
    with s.transaction() as c:
        selected=targets.selected_targets(c,payload.get('target_ids'))
        queued=[]
        for target in selected:
            queued.append(s.enqueue(c,'sync',f"platform-connect:{target['id']}:{int(time.time()//60)}",{'target_id':target['id'],'scope':'connect'}))
            types={v.get('draft',{}).get('product_type') for v in s.all_rows(c,'listing') if v['target_id']==target['id']}-{None,''}
            for product_type in sorted(types):
                queued.append(s.enqueue(c,'sync',f"platform-schema:{target['id']}:{product_type}:{int(time.time()//3600)}",{'target_id':target['id'],'scope':'definition','product_type':product_type}))
        return {'jobs':queued,'target_count':len(selected)}


@router.get('/overview')
def overview(summary:bool=False):
    from . import locales, overview_snapshot, variant_semantics
    with s.main().db() as c:
        c.execute('BEGIN')  # One WAL read snapshot, including its revision token.
        snapshot_revision=overview_snapshot.revision(c)
        data={kind:overview_snapshot.records(c,kind,summary=summary) for kind in KINDS}
        data['media']=[row for row in data['media'] if row.get('status')!='archived']
        families={}
        for sku in sorted(data['sku'],key=lambda row:row['id']):
            families.setdefault(sku.get('product_id'),[]).append(sku)
        image_checks={}
        for product in data['product']:
            product['design_readiness']=overview_snapshot.archive_readiness(product,families.get(product['id'],[]),image_checks)
            product['variant_structure']=variant_semantics.display(product,families.get(product['id'],[]))
        data['languages']=locales.options()
        for target in data['target']:
            try:target['effective_language']=locales.resolve(target);target['language_name']=locales.LANGUAGES[target['effective_language']]
            except HTTPException:target['language_name']='待配置'
        from . import import_classification,category_rules,offer_defaults
        data['listing_defaults']=offer_defaults.policy(c)
        destinations=import_classification.destinations(c)
        for product in data['product']:
            product['amazon_classifications']=[]
            for target in destinations:
                found=categories.resolve(c,product,target,category_rows=data['category'],mapping_rows=data['category_mapping'],platform='亚马逊');mapping=found.get('mapping') or {}
                job=c.execute("""SELECT id,status,error FROM commerce_jobs WHERE kind='import_classification'
                    AND json_extract(payload,'$.product_id')=? AND json_extract(payload,'$.target_id')=? ORDER BY id DESC LIMIT 1""",
                    (product['id'],target['id'])).fetchone()
                status=category_rules.state(mapping,target)
                product['amazon_classifications'].append({'target_id':target['id'],'target_name':target['name'],
                    'product_type':mapping.get('product_type',''),'category_path':mapping.get('category_path',''),
                    'status':status,'job':dict(job) if job else None})
        target_by_id={target['id']:target for target in data['target']}
        data['listing']=[catalog.material_view(v,target=target_by_id.get(v['target_id']),connection=c,copy_record=False) for v in data['listing']]
        from . import detail_publication
        for listing in data['listing']:
            listing['detail_publication']=detail_publication.view(c,listing)
            product=next((p for p in data['product'] if p['id']==listing['product_id']),{})
            selected={row['sku_id'] for row in (listing.get('draft') or {}).get('skus',[])}
            listing['variant_structure']=variant_semantics.display(product,
                [sku for sku in families.get(listing['product_id'],[]) if sku['id'] in selected],listing.get('draft'))
        data['report']=[{k:v for k,v in row.items() if k!='rows'} for row in data['report']]
        from . import overview_jobs
        data['jobs']=overview_jobs.current(c,data['listing'],recent=40 if summary else 200)
        data['jobs_history']=c.execute('SELECT COUNT(*) FROM commerce_jobs').fetchone()[0]
        from . import listing_progress
        target_by_id={target['id']:target for target in data['target']}
        for listing in data['listing']:
            published=s.by_key(c,'material_version',f"{listing['id']}:{listing.get('published_version')}") if listing.get('published_version') else None
            listing['publication_progress']=listing_progress.view(listing,data['jobs'],
                published_draft=published['draft'] if published else None,target=target_by_id.get(listing['target_id']))
            listing['platform_issues']=listing['publication_progress']['local_platform_issues']
        data['listing_batches']=[]
        for batch in s.all_rows(c,'listing_batch')[:10]:
            statuses=[s.job(c,ident)['status'] for ident in batch['job_ids']]
            data['listing_batches'].append(batch|{'job_statuses':statuses})
        environments=sorted({'production'}|{t['environment'] for t in data['target']})
        data['inventory']=[dict(s.stock(c,v['id'],env,create=False),name=v['name'],sku=v['sku'],product_id=v['product_id']) for v in data['sku'] for env in environments]
        data['finance']=operations.finance_summary(c)
        data['settings']=s.by_key(c,'settings','global') or {'live_writes_enabled':False,'media_public_base_url':''}
        from app.security_runtime import public_media_base
        if public_media_base():data['settings']['media_public_base_url']=public_media_base()
        data['pricing_policy']=s.by_key(c,'settings','pricing_policy') or {'missing_cost_mode':'','source_cost_currency':''}
        data['snapshot_revision']=snapshot_revision
    return overview_snapshot.compact(data) if summary else data


@router.get('/overview/revision')
def overview_revision():
    from . import overview_snapshot
    with s.main().db() as c:
        c.execute('BEGIN')
        return {'revision':overview_snapshot.revision(c)}


@router.get('/records/{kind}/{ident}')
def record(kind:str,ident:int,request:Request):
    if kind not in KINDS:raise HTTPException(404)
    with s.main().db() as c:
        row=s.get(c,kind,ident)
        if kind=='order':
            from .customer_pii import view
            row=view(c,row,getattr(request.state,'commerce_role',None),getattr(request.state,'commerce_username',None))
    return catalog.material_view(row) if kind=='listing' else row


@router.post('/targets')
def target_create(payload:Target):
    from . import locales
    from .environment import require
    require(payload.environment)
    if payload.include_recipient:
        from .customer_pii import require as require_pii
        require_pii()
    d=locales.configure(payload.model_dump())
    with s.transaction() as c:
        account=c.execute('SELECT * FROM platform_accounts WHERE id=?',(payload.account_id,)).fetchone()
        if not account:raise HTTPException(422,'平台账号不存在')
        if account['platform']!='亚马逊' and payload.environment!='mock':raise HTTPException(422,'该平台适配器尚未接入')
        d['seller_id']=account['seller_id'];d['account_name']=account['account_name']
        key=f'{payload.account_id}:{payload.marketplace_id}:{payload.environment}'
        old=s.by_key(c,'target',key) or {}
        if any(old.get(k)!=d.get(k) for k in ['seller_id','region']):
            old.update(verified=False,connection={},last_sync_at=None)
        old.update(d)
        return s.put(c,'target',key,old)


@router.get('/targets/{ident}/integration')
def target_integration(ident:int):
    from .integration import inspect
    return inspect(ident)


@router.post('/targets/{ident}/credentials')
def credentials(ident:int,payload:dict):
    with s.main().db() as c:t=s.get(c,'target',ident)
    with s.main().db() as c:account=c.execute('SELECT platform,credential_env FROM platform_accounts WHERE id=?',(t['account_id'],)).fetchone()
    if not account or account['platform']!='亚马逊':raise HTTPException(422,'该平台尚未接入授权配置')
    if account['credential_env']:raise HTTPException(422,'此账号使用指定环境变量，请在服务环境配置凭据，或先清空账号的凭据环境变量设置')
    amazon.save_credentials(t['account_id'],payload)
    with s.transaction() as c:
        for target in s.all_rows(c,'target'):
            if target['account_id']==t['account_id']:
                target.update(verified=False,connection={},last_sync_at=None);s.save(c,'target',target)
    return {'saved':True}


@router.post('/targets/{ident}/sync',status_code=202)
def target_sync(ident:int,payload:dict):
    scope=payload.get('scope','connect')
    if scope not in {'connect','definition','orders','finance'}:raise HTTPException(422,'同步类别无效')
    with s.transaction() as c:
        s.get(c,'target',ident)
        return s.enqueue(c,'sync',f'sync:{ident}:{scope}:{int(time.time()//30)}',{'target_id':ident,'scope':scope,'product_type':str(payload.get('product_type',''))[:100]})


@router.get('/targets/{ident}/definition')
def definition(ident:int,product_type:str):
    with s.main().db() as c:return s.by_key(c,'definition',f'{ident}:{product_type}:NONE') or {}


@router.get('/targets/{ident}/identifier-eligibility')
def identifier_eligibility(ident:int,brand:str,product_type:str):
    """Read merchant-provided Seller Central evidence; no exemption API exists."""
    from .identifiers import check_exemption
    with s.main().db() as c:target=s.get(c,'target',ident)
    return check_exemption(target,brand,product_type)


@router.get('/targets/{ident}/gtin-evidence')
def gtin_evidence(ident:int,brand:str,product_type:str):
    from . import identifiers
    with s.main().db() as c:return identifiers.evidence_view(c,s.get(c,'target',ident),brand,product_type)


@router.post('/targets/{ident}/gtin-evidence')
def gtin_evidence_create(ident:int,payload:dict,request:Request):
    from . import identifiers
    with s.transaction() as c:
        return identifiers.save_evidence(c,s.get(c,'target',ident),payload.get('brand',''),payload.get('product_type',''),payload,actor=getattr(request.state,'commerce_username',''))


@router.patch('/gtin-evidence/{ident}')
def gtin_evidence_update(ident:int,payload:dict,request:Request):
    from . import identifiers
    with s.transaction() as c:
        evidence=s.get(c,'gtin_exemption_evidence',ident)
        target=s.get(c,'target',evidence['target_id'])
        return identifiers.save_evidence(c,target,evidence['scope']['brand'],evidence['scope']['product_type'],payload,ident=ident,actor=getattr(request.state,'commerce_username',''))


@router.post('/products/reprice')
def products_reprice(payload:dict,request:Request):
    from . import costing
    ids=payload.get('product_ids')
    if not isinstance(ids,list) or not ids or len(ids)>1000:
        raise HTTPException(422,'请提供要重算的商品编号')
    ids=list(dict.fromkeys(s.quantity(value,1) for value in ids))
    with s.transaction() as c:
        products=[s.get(c,'product',ident) for ident in ids]
        if any(not row.get('supplier_source') for row in products):
            raise HTTPException(422,'批量重算仅处理有原表来源的商品档案')
        before={sku['id']:(sku.get('price'),sku.get('currency')) for sku in s.all_rows(c,'sku') if sku['product_id'] in ids}
        for product in products:
            costing.apply(c,product)
            catalog.refresh_draft_prices(c,product['id'])
        changed=sum(before[sku['id']]!=(sku.get('price'),sku.get('currency')) for sku in s.all_rows(c,'sku') if sku['id'] in before)
        policies=sorted({(sku.get('price_source') or {}).get('policy','manual') for sku in s.all_rows(c,'sku') if sku['id'] in before})
        result={'products':len(ids),'skus':len(before),'changed':changed,
                'policy':policies[0] if len(policies)==1 else 'mixed_scoped','policies':policies}
        s.audit(c,'import_prices_recomputed',None,s.dump(result|{'actor':str(getattr(request.state,'commerce_actor','admin'))}))
        return result


@router.post('/products/resume-workflows')
def products_resume_workflows(payload:dict):
    from . import environment,listing_workflow
    ids=payload.get('product_ids')
    if not isinstance(ids,list) or not ids or len(ids)>1000:raise HTTPException(422,'请提供要继续的商品编号')
    ids=list(dict.fromkeys(s.quantity(value,1) for value in ids))
    with s.main().db() as c:
        pids={s.get(c,'product',ident).get('project_id') for ident in ids}
        accounts={r[0] for r in c.execute('SELECT id FROM platform_accounts WHERE enabled=1')}
        enabled={row['id'] for row in s.all_rows(c,'target') if row.get('enabled') and
                 row['account_id'] in accounts and environment.allowed(row['environment'])}
        workflows=[row for row in s.all_rows(c,'listing_workflow') if row.get('project_id') in pids and
                   row['target_id'] in enabled and row.get('status')=='needs_action']
    results=[];failures=[]
    for row in workflows:
        try:results.append(listing_workflow.resume(row['id'])['id'])
        except HTTPException as exc:failures.append({'workflow_id':row['id'],'reason':exc.detail})
    return {'resumed':len(results),'workflow_ids':results,'failures':failures}


@router.get('/products/{ident}/readiness')
def product_readiness(ident:int):
    from . import product_archive
    with s.main().db() as c:return product_archive.readiness(c,s.get(c,'product',ident))


@router.post('/products/{ident}/design')
def product_design(ident:int,payload:dict|None=None):
    from . import product_archive
    return product_archive.ensure_design_project(ident,(payload or {}).get('sku_ids'))


@router.get('/products/{ident}/platform-fields')
def product_platform_fields(ident:int,target_id:int):
    from . import archive_fields
    with s.main().db() as c:return archive_fields.report(c,ident,target_id)


@router.put('/products/{ident}/platform-fields')
def product_platform_fields_save(ident:int,target_id:int,payload:dict,request:Request):
    from . import archive_fields
    with s.transaction() as c:return archive_fields.save(c,ident,target_id,payload,getattr(request.state,'commerce_username',''))


@router.post('/skus/{ident}/source-images')
async def sku_source_image(ident:int,request:Request,file:UploadFile=File(...)):
    asset=media.ingest(await file.read(),file.filename or '',sku_id=ident)
    with s.main().db() as c:sku=s.get(c,'sku',ident)
    return update_catalog('sku',ident,{'revision':sku['revision'],'source_images':list(dict.fromkeys(sku.get('source_images',[])+[asset['url']]))},getattr(request.state,'commerce_username',''))


@router.get('/skus/{ident}/corrected-design-reference')
def corrected_design_reference_choices(ident:int,request:Request):
    if getattr(request.state,'commerce_role',None)!='admin':
        raise HTTPException(403,'修正参考图需要管理员核对实货和来源')
    from . import design_references
    with s.main().db() as c:
        sku=s.get(c,'sku',ident)
        return {'sku_id':ident,'revision':sku['revision'],'name':sku['name'],
                'parents':design_references.parents(c,sku),'current':sku.get('design_reference')}


@router.post('/skus/{ident}/corrected-design-reference')
async def corrected_design_reference_save(ident:int,request:Request,file:UploadFile=File(...),
        revision:int=Form(...),parent_task_id:int=Form(...),basis:str=Form(...),confirmed:bool=Form(False)):
    if getattr(request.state,'commerce_role',None)!='admin':
        raise HTTPException(403,'修正参考图需要管理员核对实货和来源')
    if confirmed is not True:raise HTTPException(422,'请明确核对修正图符合已确认的实货状态')
    from . import design_references
    with s.main().db() as c:
        sku=s.get(c,'sku',ident)
        if sku['revision']!=revision:raise HTTPException(409,'商品档案已更新，请重新打开')
    asset=media.ingest(await file.read(),file.filename or '',sku_id=ident)
    return design_references.bind(ident,revision,asset,parent_task_id,basis,getattr(request.state,'commerce_username',''))


class ShippingRefresh(BaseModel):
    product_type:str=Field(default='',pattern='^$|^[A-Z][A-Z0-9_]{0,99}$')


class ShippingDefault(BaseModel):
    revision:int
    cache_revision:int|None=None
    product_type:str=Field(default='',pattern='^$|^[A-Z][A-Z0-9_]{0,99}$')
    template_id:str=Field(default='',max_length=300)
    fill_missing_drafts:bool=False
    apply_all_product_types:bool=False


@router.get('/targets/{ident}/shipping-templates')
def shipping_templates(ident:int,product_type:str=''):
    from . import shipping
    return shipping.read(ident,product_type)


@router.post('/targets/{ident}/shipping-templates/refresh')
def shipping_refresh(ident:int,payload:ShippingRefresh):
    from . import shipping
    return shipping.refresh(ident,payload.product_type)


@router.patch('/targets/{ident}/shipping-default')
def shipping_default(ident:int,payload:ShippingDefault):
    from . import shipping
    return shipping.choose(ident,payload.model_dump())


@router.post('/products/from-design/{pid}')
def import_product(pid:int):return catalog.import_project(pid)


@router.post('/products/{ident}/sync-classification',status_code=202)
def sync_product_classification(ident:int):
    from . import import_classification
    with s.transaction() as c:
        s.get(c,'product',ident)
        return {'jobs':import_classification.queue(c,[ident])}


class OneClickListing(BaseModel):
    target_id:int=Field(gt=0,strict=True)
    request_id:str=Field(min_length=8,max_length=100)


@router.post('/design-projects/{pid}/one-click-listing',status_code=202)
def one_click_listing(pid:int,payload:OneClickListing):
    from . import listing_workflow
    return listing_workflow.request(pid,payload.target_id,payload.request_id)


class ConfiguredTargetListing(BaseModel):
    model_config={'extra':'forbid'}
    request_id:str=Field(min_length=8,max_length=100)


@router.post('/design-projects/{pid}/listing-workflow-batches',status_code=202)
def configured_target_listing(pid:int,payload:ConfiguredTargetListing):
    from . import listing_workflow
    return listing_workflow.request_batch(pid,payload.request_id)


@router.get('/listing-workflows/{ident}')
def listing_workflow_status(ident:int):
    from . import listing_workflow
    with s.main().db() as c:
        workflow=listing_workflow.detail(c,s.get(c,'listing_workflow',ident))
        workflow['jobs_detail']={name:s.job(c,job_id) for name,job_id in workflow.get('jobs',{}).items()}
        return workflow


@router.get('/design-projects/{pid}/listing-workflows')
def project_listing_workflows(pid:int):
    from . import listing_workflow
    with s.main().db() as c:
        return [listing_workflow.detail(c,row) for row in s.all_rows(c,'listing_workflow') if row['project_id']==pid]


@router.post('/listing-workflows/{ident}/resume',status_code=202)
def listing_workflow_resume(ident:int):
    from . import listing_workflow
    return listing_workflow.resume(ident)


@router.patch('/products/{ident}')
def update_product(ident:int,payload:dict,request:Request):
    return update_catalog('product',ident,payload,getattr(request.state,'commerce_username',''))


@router.patch('/skus/{ident}')
def update_sku(ident:int,payload:dict,request:Request):return update_catalog('sku',ident,payload,getattr(request.state,'commerce_username',''))


def update_catalog(kind,ident,payload,actor=''):
    from . import archive_fields
    allowed={'name','brand','brand_status','brand_logo_text','facts','cost','price','cost_currency','currency','product_type','category_id','components','production_days','packing_days'} if kind=='product' else {'name','color','size','facts','cost','price','cost_currency','currency','gtin','asin','components','active','sale_mode','production_capacity'}
    allowed.update({'source_images','structured_facts'})
    if kind=='product':allowed.add('business_id')
    with s.transaction() as c:
        row=s.get(c,kind,ident)
        previous=copy_value(row)
        if payload.get('revision')!=row['revision']:raise HTTPException(409,'记录已更新，请刷新')
        for k in allowed & payload.keys():row[k]=payload[k]
        if kind=='product' and 'product_type' in payload:
            if not isinstance(payload['product_type'],str) or (payload['product_type'] and not re.fullmatch('[A-Z][A-Z0-9_]{0,99}',payload['product_type'])):raise HTTPException(422,'Amazon 商品类型应使用官方英文类型标识')
            row['product_type_source']={'kind':'manual','actor':str(actor)}
        for key in ('name','facts','color','size'):
            if key in payload and (not isinstance(payload[key],str) or len(payload[key])>20000):raise HTTPException(422,'商品事实应为文本且不超过20000字')
        if kind=='product' and 'business_id' in payload:
            row['business_id']=s.quantity(payload['business_id'],1) if payload['business_id'] not in (None,'') else None
            if row['business_id'] and not c.execute('SELECT 1 FROM businesses WHERE id=?',(row['business_id'],)).fetchone():raise HTTPException(422,'业务配置不存在')
        if 'structured_facts' in payload:
            values=payload['structured_facts']
            if not isinstance(values,dict) or len(values)>60:raise HTTPException(422,'规范商品事实应为字段对象')
            row['structured_facts']=copy_value(previous.get('structured_facts',{}))
            for field,evidence in values.items():
                if not isinstance(field,str) or not re.fullmatch('[a-z][a-z0-9_]{0,79}',field):raise HTTPException(422,'规范字段名称无效')
                if not isinstance(evidence,dict) or evidence.get('status') not in {'manual','cleared','not_applicable'}:raise HTTPException(422,'编辑字段须明确手工值、清空或不适用')
                value=evidence.get('value','')
                if not isinstance(value,(str,int,float)) or isinstance(value,bool) or len(str(value))>2000:raise HTTPException(422,'商品字段值无效')
                if evidence['status']=='not_applicable':value='不适用'
                if evidence['status']=='cleared':value=''
                archive_fields.put_fact(row,field,{'value':value,'raw':copy_value(previous.get('structured_facts',{}).get(field,{}).get('raw')),'status':evidence['status'],'source':{'kind':'merchant_edit','actor':str(actor)}})
        for field in ('size','color'):
            if kind=='sku' and field in payload:
                archive_fields.put_fact(row,field,{'value':payload[field],'status':'manual' if payload[field] else 'cleared','source':{'kind':'merchant_edit','actor':str(actor)}})
        if 'name' in payload:
            field='product_name' if kind=='product' else 'variant_name'
            prior=previous.get('structured_facts',{}).get(field,{})
            archive_fields.put_fact(row,field,{'value':payload['name'],'raw':copy_value(prior.get('raw',previous.get('name',''))),
                'status':'manual' if payload['name'].strip() else 'cleared','source':{'kind':'merchant_edit','actor':str(actor)}})
            row['name_source']={'kind':'manual','actor':str(actor)}
        if 'source_images' in payload:
            from app.simple_design_flow import local_path
            from PIL import Image
            images=payload['source_images']
            if not isinstance(images,list) or len(images)>40 or any(not isinstance(x,str) for x in images):raise HTTPException(422,'原图应为上传图片列表')
            for url in images:
                if not url.startswith(('/uploads/','/api/commerce/media/file/')):raise HTTPException(422,'原图只能绑定上传的真实图片')
                try:
                    with Image.open(local_path(url,source=True)) as image:image.verify()
                except (OSError,ValueError):raise HTTPException(422,'原图无法读取') from None
            row['source_images']=list(dict.fromkeys(images))
        brand_classification_changed=False
        if kind=='product' and ({'brand','brand_status','brand_logo_text'} & payload.keys()):
            if not isinstance(row.get('brand',''),str) or len(row['brand'])>120:raise HTTPException(422,'品牌应为120字以内文本')
            if not isinstance(row.get('brand_logo_text',''),str) or len(row['brand_logo_text'])>120:
                raise HTTPException(422,'LOGO线索应为120字以内文本')
            if 'brand_status' in payload:
                status=row['brand_status']
                if status not in {'branded','unbranded','logo_only','unknown'}:
                    raise HTTPException(422,'商品品牌状态无效')
                if status=='branded':
                    if not row['brand'].strip() or row['brand'].strip().casefold()=='generic':
                        raise HTTPException(422,'有品牌商品请填写真实商品品牌')
                    row['brand']=row['brand'].strip()
                elif status=='unbranded':
                    if row.get('brand_logo_text','').strip():
                        raise HTTPException(422,'有可识别LOGO时不能按无品牌商品上架')
                    row['brand']='generic'
                else:
                    row['brand']=''
                old_status=previous.get('brand_status') or ('branded' if previous.get('brand') else 'unknown')
                brand_classification_changed=(status!=old_status or
                    row['brand']!=previous.get('brand','') or
                    row.get('brand_logo_text','').strip()!=str(previous.get('brand_logo_text') or '').strip())
                if brand_classification_changed:
                    row['brand_source']={'kind':'owner_product_'+('brand' if status=='branded' else status),
                                         'description':'商品档案中明确的品牌状态',
                                         'visible_mark':row.get('brand_logo_text','').strip()}
            elif row['brand']!=previous.get('brand'):
                row['brand_source']={'kind':'manual','description':'商品档案手工填写'}
                row['brand_status']='branded' if row['brand'].strip() else 'unknown'
        if kind=='product' and 'category_id' in payload:
            row['category_id']=s.quantity(payload['category_id'],1) if payload['category_id'] not in (None,'') else None
            if row['category_id']:s.get(c,'category',row['category_id'])
        for k in ['cost','price']:row[k]=s.money(row.get(k),True)
        if row.get('cost_pricing') and any(row.get(k)!=previous.get(k) for k in ['cost','cost_currency','price','currency']):
            row['cost_pricing']={'status':'manual','message':'商品档案中手工维护的成本或售价'}
        if kind=='sku' and row.get('size')!=previous.get('size'):
            row['size_source']={'status':'manual','message':'商品档案手工填写的尺寸'}
        for k in ['currency','cost_currency']:
            if row.get(k) and not re.fullmatch('[A-Z]{3}',row[k]):raise HTTPException(422,'币种必须为三个大写字母')
        for amount_key,currency_key,source_key in [('cost','cost_currency','cost_source'),('price','currency','price_source')]:
            if {amount_key,currency_key} & payload.keys():
                row[source_key]={'status':'manual','amount':row.get(amount_key),'currency':row.get(currency_key,''),'source':{'kind':'merchant_edit','actor':str(actor)},'message':'商品档案手工值（明确清空也保留）'}
        components=row.get('components',[])
        if not isinstance(components,list):raise HTTPException(422,'组件应为列表')
        for component in components:
            if not isinstance(component,dict) or not component.get('name'):raise HTTPException(422,'组件需要名称')
            component['quantity']=s.quantity(component.get('quantity',1),1)
        for key in ['production_days','packing_days']:
            if row.get(key) is not None:row[key]=s.quantity(row[key])
        if kind=='sku':
            from .identifiers import valid_gtin
            if not isinstance(row.get('gtin',''),str) or (row.get('gtin') and not valid_gtin(row['gtin'])):
                raise HTTPException(422,'GTIN须为校验位正确的12位UPC或13位EAN，且应属于本商品')
            if not isinstance(row.get('asin',''),str) or (row.get('asin') and not re.fullmatch('[A-Z0-9]{10}',row['asin'])):
                raise HTTPException(422,'ASIN应为10位大写字母或数字')
            if row.get('sale_mode','stock') not in {'stock','made_to_order'}:raise HTTPException(422,'销售库存模式无效')
            row['production_capacity']=s.quantity(row.get('production_capacity',0))
        fact_fields={'name','brand','brand_status','brand_logo_text','facts','color','size','gtin','asin','product_type','category_id','source_images','structured_facts','active'}
        changed_fields={field for field,value in row.get('structured_facts',{}).items() if value!=previous.get('structured_facts',{}).get(field)}
        archive_fields.invalidate_fact_attributes(row,changed_fields)
        if any(row.get(k)!=previous.get(k) for k in fact_fields):row['fact_revision']=previous.get('fact_revision',0)+1
        s.put(c,'archive_revision',f"{kind}:{ident}:{previous['revision']}",{'kind':kind,'record_id':ident,'snapshot':previous,'actor':str(actor)})
        result=s.save(c,kind,row,payload['revision'])
        s.audit(c,'archive_updated',ident,s.dump({'kind':kind,'actor':str(actor),'fields':sorted(allowed & payload.keys())}))
        if kind=='product':
            archive_fields.inherit_product_facts(c,previous,result,changed_fields,actor)
            if 'name' in payload:
                # SKU product_name is the parent identity, not its variant_name.
                # Resolve inherited source warnings using the merchant's parent
                # confirmation, retaining the complete previous SKU evidence.
                for sku in s.all_rows(c,'sku'):
                    if sku['product_id']!=ident:continue
                    evidence=sku.get('structured_facts',{}).get('product_name',{})
                    if (evidence.get('status') in {'manual','cleared','not_applicable'} and
                            evidence.get('source',{}).get('kind')!='product_archive_inheritance'):continue
                    before=copy_value(sku)
                    value=copy_value(row['structured_facts']['product_name'])
                    value['source']={'kind':'product_archive_inheritance','parent_field':'product_name',
                        'parent_revision':result.get('fact_revision',0),'actor':str(actor)}
                    archive_fields.put_fact(sku,'product_name',value)
                    sku['fact_revision']=before.get('fact_revision',0)+1
                    s.put(c,'archive_revision',f"sku:{sku['id']}:{before['revision']}",
                        {'kind':'sku','record_id':sku['id'],'snapshot':before,'actor':str(actor)})
                    s.save(c,'sku',sku)
            if brand_classification_changed and row.get('project_id'):
                project=c.execute('SELECT platform FROM design_projects WHERE id=?',(row['project_id'],)).fetchone()
                if project:
                    s.put(c,'product_brand',f"project:{row['project_id']}",
                          {'project_id':row['project_id'],'platform':project['platform'],
                           'status':'confirmed' if row['brand_status']=='branded' else row['brand_status'],
                           'brand':row['brand'] if row['brand_status']=='branded' else '',
                           'visible_mark':row.get('brand_logo_text','').strip(),
                           'confirmed_by':'product_record'})
            for sku in s.all_rows(c,'sku'):
                if sku['product_id']!=ident:continue
                before=copy_value(sku)
                for fields in [('cost','cost_currency'),('price','currency'),('components',)]:
                    source_key='cost_source' if fields[0]=='cost' else 'price_source'
                    if fields[0] in {'cost','price'} and (sku.get(source_key) or {}).get('status')=='manual':continue
                    if all(sku.get(k)==previous.get(k) for k in fields):
                        for k in fields:sku[k]=copy_value(row.get(k))
                        if fields in {('cost','cost_currency'),('price','currency')}:
                            source_key='cost_source' if fields[0]=='cost' else 'price_source'
                            if {fields[0],fields[1]} & payload.keys():sku[source_key]=copy_value(row[source_key])
                if sku!=before:
                    s.put(c,'archive_revision',f"sku:{sku['id']}:{before['revision']}",{'kind':'sku','record_id':sku['id'],'snapshot':before,'actor':str(actor)})
                    s.save(c,'sku',sku)
            if 'business_id' in payload:
                from . import costing
                result=costing.apply(c,result)
        if {'price','currency','business_id'} & payload.keys():
            catalog.refresh_draft_prices(c,ident if kind=='product' else result['product_id'])
        return result


@router.post('/design-projects/{pid}/materials',status_code=202)
def materials(pid:int,payload:dict|None=None):
    from .material_batches import request
    return request(pid,payload or {})


@router.get('/design-projects/{pid}/materials-plan')
def materials_plan(pid:int):
    from .material_batches import preview
    return preview(pid)


@router.get('/listings/{ident}/parameter-history')
def listing_parameter_history(ident:int):
    from . import listing_parameters
    return listing_parameters.read(ident)


@router.patch('/listings/{ident}/draft')
def draft_save(ident:int,payload:dict):return catalog.update_draft(ident,payload)


class ArchiveFieldsRefresh(BaseModel):
    revision:int=Field(ge=1)




@router.post('/listings/{ident}/archive-fields')
def refresh_listing_archive_fields(ident:int,payload:ArchiveFieldsRefresh):
    return catalog.refresh_archive_fields(ident,allow_published=True,expected_revision=payload.revision)


@router.post('/listings/{ident}/complete-fields',status_code=202)
def complete_listing_fields(ident:int,payload:ArchiveFieldsRefresh):
    from . import attribute_completion,listing_release
    with s.transaction() as c:
        listing=s.get(c,'listing',ident)
        if listing['revision']!=payload.revision:raise HTTPException(409,'资料已变化，请刷新后自动补齐')
        listing_release.assert_preparation_available(c,listing,s.get(c,'target',listing['target_id']))
        job=attribute_completion.continue_job(c,listing)
        return {'listing_id':ident,'job':job,'already_complete':job is None}


@router.post('/listings/{ident}/gallery-assembly')
def assemble_listing_gallery(ident:int,payload:ArchiveFieldsRefresh,request:Request):
    from . import gallery_assembly
    return gallery_assembly.attach(ident,payload.revision,actor=getattr(request.state,'commerce_username',''))


@router.get('/listings/{ident}/chinese-reference')
def cached_chinese_reference(ident:int):
    from . import content_reference
    return content_reference.reference(ident,generate=False)


@router.post('/listings/{ident}/chinese-reference')
def chinese_reference(ident:int,payload:dict|None=None):
    from . import content_reference
    return content_reference.reference(ident,(payload or {}).get('content'))


class IdentityReviewConfirmation(BaseModel):
    review_id:int=Field(gt=0,strict=True)
    confirmed:bool=Field(strict=True)
    reason:str=Field(min_length=8,max_length=1000)


@router.post('/listings/{ident}/identity-review')
def listing_identity_review(ident:int,request:Request):
    from . import listing_identity_review as review
    return review.prepare(ident,actor=getattr(request.state,'commerce_username',''))


@router.post('/listings/{ident}/identity-review/confirm',status_code=202)
def listing_identity_review_confirm(ident:int,payload:IdentityReviewConfirmation,request:Request):
    from . import listing_identity_review as review
    return review.confirm(ident,payload.review_id,payload.confirmed,payload.reason,actor=getattr(request.state,'commerce_username',''))


class PricingPolicy(BaseModel):
    missing_cost_mode:str=Field(default='',pattern='^(sale_price|cost_price)?$')
    source_cost_currency:str=Field(default='',pattern='^([A-Z]{3})?$')
    fallback_sale_currency:str=Field(default='',pattern='^([A-Z]{3})?$')


@router.patch('/pricing-policy')
def pricing_policy(payload:PricingPolicy):
    with s.transaction() as c:
        saved=s.by_key(c,'settings','pricing_policy') or {}
        saved.update(payload.model_dump(exclude_unset=True))
        result=s.put(c,'settings','pricing_policy',saved)
        s.audit(c,'pricing_policy_saved',None,s.dump(payload.model_dump(exclude_unset=True)))
        return result


@router.post('/exchange-rates/refresh')
def exchange_rates():
    from . import pricing
    return pricing.rates(refresh=True)


class PresentationRepairRequest(BaseModel):
    revision:int
    local_only:bool=Field(default=False,strict=True)
    correction:str=Field(default='',max_length=1000)
    correction_sku_ids:list[int]=Field(default_factory=list)
    correction_sha256:list[str]=Field(default_factory=list)


class CandidateCorrection(BaseModel):
    page_key:str=Field(min_length=1,max_length=200)
    sha256:str=Field(pattern='^[a-f0-9]{64}$')


class CandidateRepairRequest(BaseModel):
    candidates:list[CandidateCorrection]=Field(min_length=1,max_length=200)
    instruction:str=Field(min_length=8,max_length=1200)


@router.post('/design-projects/{pid}/presentation-repairs/{rid}/correct-candidates',status_code=202)
@router.post('/design-projects/{pid}/flows/{rid}/image-repairs',status_code=202)
def presentation_candidate_repair(pid:int,rid:int,payload:CandidateRepairRequest,request:Request):
    from app import presentation_candidate_repair as repair
    return repair.start(pid,rid,[v.model_dump() for v in payload.candidates],payload.instruction,
        actor=getattr(request.state,'commerce_username',''))


class MissingImageRecoveryRequest(BaseModel):
    detail_run_id:int|None=None
    task_ids:list[int]=Field(min_length=1,max_length=200)
    confirmed_new_request:bool


@router.get('/design-projects/{pid}/flows/{rid}/master-repairs')
def master_repair_preview(pid:int,rid:int):
    from app import master_repair
    return master_repair.preview(pid,rid)


@router.post('/design-projects/{pid}/flows/{rid}/master-repairs',status_code=202)
def repair_selected_masters(pid:int,rid:int,payload:CandidateRepairRequest,request:Request):
    from app import master_repair
    return master_repair.start(pid,rid,[x.model_dump() for x in payload.candidates],payload.instruction,
        actor=getattr(request.state,'commerce_username',''))


@router.get('/design-projects/{pid}/flows/{rid}/missing-image-recovery')
@router.get('/design-projects/{pid}/flows/{rid}/missing-detail-recovery')
def missing_detail_recovery_preview(pid:int,rid:int):
    from app import presentation_gap_recovery as recovery
    return recovery.preview(pid,rid)


@router.post('/design-projects/{pid}/flows/{rid}/missing-image-recovery',status_code=202)
@router.post('/design-projects/{pid}/flows/{rid}/missing-detail-recovery',status_code=202)
def missing_detail_recovery(pid:int,rid:int,payload:MissingImageRecoveryRequest,request:Request):
    from app import presentation_gap_recovery as recovery
    return recovery.start(pid,rid,payload.detail_run_id,payload.task_ids,payload.confirmed_new_request,
        actor=getattr(request.state,'commerce_username',''))


@router.post('/listings/{ident}/presentation-repair',status_code=202)
def listing_presentation_repair(ident:int,payload:PresentationRepairRequest,request:Request):
    from app import presentation_repair
    return presentation_repair.start(ident,payload.revision,correction=payload.correction,
        correction_sku_ids=payload.correction_sku_ids,correction_sha256=payload.correction_sha256,
        actor=getattr(request.state,'commerce_username',''),local_only=payload.local_only)


@router.post('/listings/{ident}/variation-family')
def listing_variation_family(ident:int,payload:PresentationRepairRequest,request:Request):
    from . import variation_families
    return variation_families.prepare(ident,payload.revision,actor=getattr(request.state,'commerce_username',''))


@router.get('/listings/{ident}/variation-plan')
def listing_variation_plan(ident:int):
    from . import variation_families
    return variation_families.preview(ident)


class NoBatteryDeclarationRequest(BaseModel):
    revision:int
    sku_ids:list[int]
    confirmed:bool


@router.post('/listings/{ident}/no-battery-declaration')
def listing_no_battery_declaration(ident:int,payload:NoBatteryDeclarationRequest,request:Request):
    if payload.confirmed is not True:raise HTTPException(422,'需要商家明确确认本商品全部规格无需电池')
    from . import variation_families
    return variation_families.declare_no_battery(ident,payload.revision,payload.sku_ids,
        actor=getattr(request.state,'commerce_username',''))


@router.get('/listings/{ident}/presentation-plan')
def listing_presentation_plan(ident:int):
    from . import visual_presentation
    with s.main().db() as c:return visual_presentation.view(c,s.get(c,'listing',ident))


@router.get('/listings/{ident}/design-preview')
def listing_design_preview(ident:int):
    from app import detail_page
    with s.main().db() as c:
        listing=s.get(c,'listing',ident);product=s.get(c,'product',listing['product_id'])
    state=detail_page.status(product['project_id'])
    return {'detail':state['current'],'stale':state['stale']}


@router.get('/listings/{ident}/detail-publication')
def detail_publication_status(ident:int):
    from . import detail_publication
    with s.main().db() as c:return detail_publication.view(c,s.get(c,'listing',ident))


@router.post('/listings/{ident}/detail-publication/{action}',status_code=202)
def detail_publication_action(ident:int,action:str):
    from . import detail_publication
    if action not in {'retry','check','render'}:raise HTTPException(404)
    with s.transaction() as c:return detail_publication.action(c,s.get(c,'listing',ident),action)


@router.get('/listings/{ident}/release-readiness')
def release_readiness(ident:int):
    from . import merchant_terms
    return merchant_terms.readiness(ident)


@router.post('/listings/{ident}/prepare-relisting')
def prepare_relisting(ident:int,payload:dict):
    from . import listing_release
    return listing_release.prepare(ident,payload)


@router.post('/targets/{ident}/shipping-terms')
def shipping_terms(ident:int,payload:dict):
    from . import merchant_terms
    return merchant_terms.record_check(ident,payload)


@router.post('/listings/{ident}/{action}',status_code=202)
def listing_action(ident:int,action:str):
    if action not in {'submit','continue','preview','refresh','pause','resume','reconcile'}:raise HTTPException(404)
    with s.transaction() as c:return enqueue_listing_action(c,ident,action)


def enqueue_listing_action(c,ident,action):
    listing=s.get(c,'listing',ident)
    from . import listing_release
    listing_release.assert_action(listing,action)
    if action=='continue' or (action=='submit' and listing.get('published_version') and listing.get('published_version')==listing.get('draft_version')):
        from . import detail_publication
        result=detail_publication.continue_listing(c,listing)
        if action=='submit':
            previous=c.execute("SELECT id FROM commerce_jobs WHERE kind='submit' AND json_extract(payload,'$.listing_id')=? AND json_extract(payload,'$.version')=? AND COALESCE(json_extract(payload,'$.preview'),0)=0 AND status='completed' ORDER BY id DESC LIMIT 1",(ident,listing['published_version'])).fetchone()
            if previous:return {**s.job(c,previous['id']),'listing_continuation':result}
        return result
    if action in {'submit','preview'}:
        target=s.get(c,'target',listing['target_id'])
        missing=catalog.issues(listing.get('draft',{}),target)
        if missing:raise HTTPException(422,'请补齐：'+'；'.join(missing))
        if action=='submit':
            from . import detail_publication
            target=s.get(c,'target',listing['target_id'])
            if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):
                raise HTTPException(422,'正式写入未启用，请先在经营设置中配置')
    running=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('submit','availability') AND status IN ('queued','running','retry_wait','unknown') AND json_extract(payload,'$.listing_id')=?",(ident,)).fetchone()
    if running and action not in {'refresh','reconcile'}:
        existing=s.job(c,running[0])
        return manual_submission(c,existing) if action=='submit' else existing
    if action == 'submit':
        listing = listing_release.begin_first_publication(c, listing)
    if action in {'submit','preview'}:
        kind='submit';key=f"submit:{ident}:{listing['draft_version']}:{action}"
        payload={'listing_id':ident,'version':listing['draft_version'],'preview':action=='preview'}
        if action=='submit':
            from . import execution_controls
            payload['manual_pause_epoch']=execution_controls.pause_epoch(c)
    elif action in {'pause','resume'}:
        kind='availability';key=f'{action}:{ident}:{int(time.time()//30)}';payload={'listing_id':ident,'action':action,'published_version':listing.get('published_version')}
    else:kind='reconcile_listing' if action=='reconcile' else 'refresh';key=f'{kind}:{ident}:{int(time.time()//30)}';payload={'listing_id':ident}
    payload=listing_release.bind_payload(listing,payload)
    job=s.enqueue(c,kind,key,payload)
    if action=='submit':job=manual_submission(c,job)
    if action=='submit' and listing['draft'].get('detail_page'):
        # Enhanced detail content is optional for a basic Amazon listing.
        # A failed detail preflight is reported separately from listing work.
        try:
            detail_publication.stage(c,listing,listing['draft_version'])
            current=s.get(c,'listing',ident)
            if current.pop('detail_publication_warning',None):s.save(c,'listing',current)
        except HTTPException as exc:
            current=s.get(c,'listing',ident)
            current['detail_publication_warning']=str(exc.detail)
            s.save(c,'listing',current)
    return job


def manual_submission(c,job):
    """An explicit merchant click renews only this definite, unfinished submission."""
    if job['kind']!='submit' or job['status'] not in {'queued','retry_wait','paused'}:return job
    from . import execution_controls
    payload={**job['payload'],'manual_pause_epoch':execution_controls.pause_epoch(c)}
    if job['status']=='paused':
        s.audit(c,'manual_listing_continued',payload['listing_id'],s.dump({'job_id':job['id'],'prior_error':job['error']}))
        c.execute("UPDATE commerce_jobs SET payload=?,status='queued',error='',next_at=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",(s.dump(payload),job['id']))
    elif payload!=job['payload']:
        c.execute('UPDATE commerce_jobs SET payload=? WHERE id=?',(s.dump(payload),job['id']))
    return s.job(c,job['id'])


class BatchListing(BaseModel):
    listing_id:int=Field(gt=0,strict=True)
    revision:int=Field(gt=0,strict=True)


class ListingBatch(BaseModel):
    request_id:str=Field(min_length=8,max_length=100)
    listings:list[BatchListing]=Field(min_length=1)


class PublishAll(BaseModel):
    model_config={'extra':'forbid'}
    request_id:str=Field(min_length=8,max_length=100)
    fingerprint:str=Field(min_length=64,max_length=64)
    overwrite:bool=Field(strict=True)
    confirmed:bool=Field(strict=True)


@router.get('/listing-publish-plan')
def listing_publish_plan():
    from . import publish_all
    with s.main().db() as c:return publish_all.plan(c)


@router.post('/listing-publish-all',status_code=202)
def listing_publish_all(payload:PublishAll):
    from . import publish_all
    if not payload.confirmed:raise HTTPException(409,'未确认上架，不执行')
    with s.transaction() as c:return publish_all.request(c,payload)


@router.post('/listing-batches',status_code=202)
def listing_batch(payload:ListingBatch):
    with s.transaction() as c:
        return queue_listing_batch(c,payload)


def queue_listing_batch(c,payload):
    request=payload.model_dump()['listings']
    previous=s.by_key(c,'listing_batch',payload.request_id)
    if previous:
        if previous['selection']!=request:raise HTTPException(409,'批次编号已用于另一组商品')
        return previous
    ids=[r.listing_id for r in payload.listings]
    if len(set(ids))!=len(ids):raise HTTPException(422,'同一商品目标不能重复勾选')
    rows=[s.get(c,'listing',ident) for ident in ids]
    destinations={t['id']:t for t in targets.selected_targets(c,[r['target_id'] for r in rows])}
    global_settings=s.by_key(c,'settings','global') or {}
    for row,chosen in zip(rows,payload.listings):
        from . import locales
        locales.assert_current(destinations[row['target_id']],row.get('draft',{}))
        if row['revision']!=chosen.revision:raise HTTPException(409,'商品资料已更新，请重新打开批量上架')
        errors=catalog.issues(row.get('draft',{}),destinations[row['target_id']])
        if errors:raise HTTPException(422,f"{row['name']}："+'；'.join(errors))
        if row.get('paused'):raise HTTPException(409,'所选商品已暂停，请先恢复')
        running=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('submit','availability') AND status IN ('queued','running','retry_wait','unknown') AND json_extract(payload,'$.listing_id')=?",(row['id'],)).fetchone()
        if running:
            job=s.job(c,running[0])
            if job['kind']!='submit' or job['payload'].get('preview') or job['payload'].get('version')!=row['draft_version'] or job['status']=='unknown':
                raise HTTPException(409,'所选商品有其他平台任务或待核实提交，请处理完成后再分批上架')
        if destinations[row['target_id']]['environment']=='production' and not global_settings.get('live_writes_enabled'):
            raise HTTPException(422,'正式写入未启用，请先在经营设置中配置')
    queued=[enqueue_listing_action(c,row['id'],'submit') for row in rows]
    groups=[{'target_id':t['id'],'account_name':t['account_name'],'name':t['name'],'environment':t['environment'],
             'listing_ids':[r['id'] for r in rows if r['target_id']==t['id']]} for t in destinations.values()]
    result=s.put(c,'listing_batch',payload.request_id,{'selection':request,'groups':groups,'job_ids':[j['id'] for j in queued]})
    s.audit(c,'listing_batch_queued',result['id'],s.dump(groups))
    return result


@router.post('/media')
async def upload(file:UploadFile=File(...),sku_id:int|None=Form(None)):
    raw=await file.read(media.MAX_BYTES+1)
    return media.ingest(raw,file.filename or '',sku_id)


@router.get('/media/file/{key}')
def media_file(key:str):return FileResponse(media.path(key),media_type='image/jpeg',headers={'Cache-Control':'private, max-age=3600'})


@router.post('/media/{ident}/verify')
def verify_media(ident:int):
    with s.main().db() as c:asset=s.get(c,'media',ident)
    url=media.verify_public(asset)
    with s.transaction() as c:
        asset=s.get(c,'media',ident);asset.update(status='public_checked',public_url=url,checked_at=time.time());return s.save(c,'media',asset)


@router.get('/listings/{ident}/main-images')
def main_image_candidates(ident:int):
    from . import main_images
    return main_images.candidate_report(ident)


@router.post('/listings/{ident}/main-images/prepare',status_code=202)
def prepare_main_images(ident:int,payload:dict|None=None):
    from . import main_images
    sku_id=(payload or {}).get('sku_id')
    asset_id=(payload or {}).get('candidate_asset_id')
    return main_images.enqueue(ident,s.quantity(sku_id,1) if sku_id is not None else None,
                               s.quantity(asset_id,1) if asset_id is not None else None)


@router.post('/listings/{ident}/main-images/choose')
def choose_main_image(ident:int,payload:dict):
    from . import main_images
    return main_images.choose(ident,payload)


@router.post('/settings')
def settings(payload:dict):
    with s.transaction() as c:
        old=s.by_key(c,'settings','global') or {}
        for k in ['media_public_base_url','live_writes_enabled','listing_automation_paused']:
            if k in payload:old[k]=payload[k]
        if not isinstance(old.get('live_writes_enabled',False),bool):raise HTTPException(422,'正式写入设置无效')
        if not isinstance(old.get('listing_automation_paused',True),bool):raise HTTPException(422,'自动上架暂停设置无效')
        if payload.get('listing_automation_paused') is True:
            old['listing_pause_epoch']=int(old.get('listing_pause_epoch',0))+1
        if old.get('media_public_base_url'):
            from urllib.parse import urlparse
            p=urlparse(old['media_public_base_url'])
            if p.scheme!='https' or not p.hostname or p.username or p.password:raise HTTPException(422,'请输入完整HTTPS公网图片根地址')
        return s.put(c,'settings','global',old)


@router.post('/orders/{ident}/prepare')
def prepare(ident:int):return operations.prepare_order(ident)


@router.post('/orders/sync',status_code=202)
def orders_sync(payload:dict):
    with s.transaction() as c:
        selected=targets.selected_targets(c,payload.get('target_ids'))
        queued=[]
        for target in selected:
            active=c.execute("SELECT id FROM commerce_jobs WHERE kind='sync' AND json_extract(payload,'$.target_id')=? AND json_extract(payload,'$.scope')='orders' AND status IN ('queued','running','retry_wait')",(target['id'],)).fetchone()
            queued.append(s.job(c,active[0]) if active else s.enqueue(c,'sync','orders:'+uuid.uuid4().hex,{'target_id':target['id'],'scope':'orders'}))
        return {'jobs':queued,'target_count':len(selected)}


@router.get('/orders/{ident}/logistics')
def order_logistics_view(ident:int,request:Request):
    from . import order_logistics
    with s.main().db() as c:
        order=s.get(c,'order',ident)
        from .customer_pii import view
        return {'order':view(c,order,getattr(request.state,'commerce_role',None),getattr(request.state,'commerce_username',None)),'remaining':order_logistics.remaining(c,order),
                'shipments':[ship for ship in s.all_rows(c,'shipment') if ship['order_id']==ident]}


@router.post('/orders/{ident}/logistics')
def order_logistics_create(ident:int,payload:dict):
    from . import order_logistics
    return order_logistics.create(ident,payload)


@router.post('/shipments/{ident}/cancel')
def cancel_logistics(ident:int,payload:dict):
    with s.transaction() as c:
        ship=s.get(c,'shipment',ident)
        active=c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.shipment_id')=? AND status IN ('queued','running','retry_wait')",(ident,)).fetchone()
        if ship.get('fulfillment_mode')!='offline' or ship['status'] not in {'draft','failed'} or active:raise HTTPException(409,'只能作废尚未提交或明确失败的物流草稿')
        if ship['revision']!=payload.get('revision'):raise HTTPException(409,'物流已更新，请刷新')
        ship['status']='cancelled';return s.save(c,'shipment',ship)


@router.post('/orders/{ident}/associate')
def associate_order(ident:int,payload:dict):
    with s.transaction() as c:
        order=s.get(c,'order',ident);item=next((x for x in order['items'] if x['external_item_id']==payload.get('item_id')),None)
        if not item or item['reserved'] or item['shipped'] or item.get('production_id'):raise HTTPException(409,'该订单行不可重新关联')
        sku=s.get(c,'sku',payload['sku_id']);product=s.get(c,'product',sku['product_id'])
        item['sku_id']=sku['id'];item['snapshot']={'sku':copy_value(sku),'source':{'facts':product['facts']},'material_version':None,'images':[]}
        order['status']='ready' if order['platform_status']=='ready' and all(x['sku_id'] for x in order['items']) else 'needs_attention'
        return s.save(c,'order',order)


def copy_value(value):return json.loads(s.dump(value))


@router.post('/inventory/adjust')
def inventory_adjust(payload:dict):
    with s.transaction() as c:
        if not payload.get('reason'):raise HTTPException(422,'请填写入库/调整原因')
        try:delta=int(payload.get('delta',0))
        except (ValueError,TypeError):raise HTTPException(422,'调整数量必须为整数')
        if str(delta)!=str(payload.get('delta')):raise HTTPException(422,'调整数量必须为整数')
        return s.move_stock(c,int(payload['sku_id']),'adjust:'+str(payload.get('request_id') or uuid.uuid4().hex),on_hand=delta,reason=str(payload['reason'])[:300],environment=payload.get('environment','production'))


@router.post('/replenishments')
def replenish(payload:dict):
    with s.transaction() as c:
        sku=s.get(c,'sku',payload['sku_id'])
        key=payload.get('request_id') or uuid.uuid4().hex
        old=s.by_key(c,'replenishment',key)
        if old:return old
        env=payload.get('environment','production');s.stock(c,sku['id'],env)
        return s.put(c,'replenishment',key,{'sku_id':sku['id'],'environment':env,'name':sku['name'],'quantity':s.quantity(payload['quantity'],1),'status':'ordered','notes':str(payload.get('notes',''))[:1000]})


@router.post('/replenishments/{ident}/receive')
def receive(ident:int):
    with s.transaction() as c:
        row=s.get(c,'replenishment',ident)
        if row['status']=='received':return row
        s.move_stock(c,row['sku_id'],'replenish:'+str(ident),on_hand=row['quantity'],reason='补货入库',environment=row.get('environment','production'))
        row['status']='received';return s.save(c,'replenishment',row)


@router.post('/production/{ident}/{action}')
def production(ident:int,action:str,request:Request):
    result=operations.production_action(ident,action)
    return production_view(result) if getattr(request.state,'commerce_role','admin')=='production' else result


def production_view(row):
    from app.simple_design_prompts import clean_brief
    from .product_archive import snapshot_facts_text
    snap=row.get('snapshot',{})
    value={k:row.get(k) for k in ['id','name','quantity','status','components','deadline','environment']}|{'facts':clean_brief(snapshot_facts_text(snap))}
    detail=snap.get('detail_page')
    if detail:value['detail_page']={'run_id':detail['run_id'],'sections':len(detail['sections']),'language':detail['language']}
    if row.get('environment') in {'mock','sandbox'}:value['name']='【测试任务】'+value['name']
    return value


@router.get('/production-queue')
def production_queue():
    with s.main().db() as c:return [production_view(r) for r in s.all_rows(c,'production')]


@router.post('/shipments')
def shipment_create(payload:dict):return operations.create_shipment(payload)


@router.post('/shipments/{ident}/submit',status_code=202)
def shipment_submit(ident:int):
    with s.transaction() as c:
        ship=s.get(c,'shipment',ident)
        if ship.get('fulfillment_mode')=='offline':
            from . import order_logistics
            return order_logistics.enqueue(c,ship)
        return s.enqueue(c,'shipment',f'ship:{ident}:{ship.get("dispatch_revision",1)}',{'shipment_id':ident})


@router.patch('/shipments/{ident}')
def shipment_edit(ident:int,payload:dict):
    with s.transaction() as c:
        ship=s.get(c,'shipment',ident)
        if ship.get('fulfillment_mode')=='offline':
            active=c.execute("SELECT 1 FROM commerce_jobs WHERE json_extract(payload,'$.shipment_id')=? AND status IN ('queued','running','retry_wait')",(ident,)).fetchone()
            if active or ship['status'] not in {'draft','failed'}:raise HTTPException(409,'已提交物流请先核对平台；已确认物流请到平台后台更正')
        if ship['status'] not in {'draft','failed','confirmed'}:raise HTTPException(409,'已提交或结果未知的包裹请先核对平台')
        if payload.get('revision')!=ship['revision']:raise HTTPException(409,'包裹已更新')
        for field in ['carrier','tracking','service','ship_date']:
            if field in payload:ship[field]=str(payload[field])[:120]
        if not ship['carrier'] or not ship['tracking']:raise HTTPException(422,'承运商和单号不能为空')
        ship['ship_date']=operations.ship_date(ship['ship_date'])
        ship['dispatch_revision']=ship.get('dispatch_revision',1)+1;ship['status']='draft'
        return s.save(c,'shipment',ship)


@router.post('/shipments/{ident}/reconcile',status_code=202)
def shipment_reconcile(ident:int):
    with s.transaction() as c:
        s.get(c,'shipment',ident)
        return s.enqueue(c,'reconcile_shipment',f'reconcile-ship:{ident}:{int(time.time()//30)}',{'shipment_id':ident})


@router.post('/aftersales')
def aftersale_create(payload:dict):return operations.aftersale(payload)


@router.post('/aftersales/sync',status_code=202)
def aftersales_sync(payload:dict):
    from .aftersales import queue_sync
    return queue_sync(payload)


@router.get('/aftersales/{ident}/details')
def aftersales_details(ident:int):
    from .aftersales import details
    return details(ident)


@router.post('/aftersales/{ident}/refresh')
def aftersales_refresh(ident:int):
    from .aftersales_reports import refresh_case
    return refresh_case(ident)


@router.post('/aftersales/{ident}/actions')
def aftersales_action_create(ident:int,payload:dict):
    from .aftersales import create_action
    return create_action(ident,payload)


@router.post('/aftersale-actions/{ident}/submit',status_code=202)
def aftersales_action_submit(ident:int):
    from .aftersales import queue_action
    return queue_action(ident)


@router.get('/aftersale-actions/{ident}')
def aftersales_action_get(ident:int):
    with s.main().db() as c:return s.get(c,'aftersale_action',ident)


@router.post('/aftersale-actions/{ident}/check',status_code=202)
def aftersales_action_check(ident:int):
    from .aftersales import queue_action
    return queue_action(ident,check=True)


@router.post('/aftersale-actions/{ident}/cancel')
def aftersales_action_cancel(ident:int,payload:dict):
    from .aftersales import cancel_action
    return cancel_action(ident,payload)


@router.post('/aftersales/{ident}/resolve')
def resolve(ident:int,payload:dict):return operations.resolve_aftersale(ident,payload)


@router.post('/expenses')
def expense_create(payload:dict):
    with s.transaction() as c:
        order=s.get(c,'order',payload['order_id'])
        currency=str(payload.get('currency',order['currency']))
        if not re.fullmatch('[A-Z]{3}',currency):raise HTTPException(422,'币种无效')
        return s.put(c,'expense',payload.get('request_id') or uuid.uuid4().hex,{'order_id':order['id'],'environment':order['environment'],
          'type':str(payload.get('type','shipping'))[:80],'amount':s.money(payload['amount']),'currency':currency,
          'reference':str(payload.get('reference',''))[:300],'source':'manual'})


@router.post('/transactions/{ident}/reconcile')
def transaction_reconcile(ident:int,payload:dict):return reports.reconcile_transaction(ident,payload)


@router.post('/reports',status_code=202)
def report_create(payload:dict):return reports.create(payload)


@router.post('/reports/discover',status_code=202)
def report_discover(payload:dict):
    with s.transaction() as c:
        target=s.get(c,'target',payload['target_id'])
        return s.enqueue(c,'report_discover',f"reports-discover:{target['id']}:{int(time.time()//60)}",{'target_id':target['id']})


@router.post('/reports/{ident}/{action}',status_code=202)
def report_action(ident:int,action:str):
    if action not in {'fetch','reconcile'}:raise HTTPException(404)
    with s.transaction() as c:
        row=s.get(c,'report',ident)
        if action=='fetch' and row['status'] in {'unknown','sending'}:raise HTTPException(409,'报表申请结果未知，请先核对')
        kind='report' if action=='fetch' else 'reconcile_report'
        if action=='fetch':row['polls']=0;s.save(c,'report',row)
        return s.enqueue(c,kind,f'{kind}:{ident}:{int(time.time()//60)}',{'report_id':ident})


@router.get('/reports/{ident}/download')
def report_download(ident:int):
    import csv
    with s.main().db() as c:row=s.get(c,'report',ident)
    if row['status']!='completed':raise HTTPException(409,'报表尚未下载完成')
    buf=io.StringIO();rows=row.get('rows',[])
    if rows:
        writer=csv.DictWriter(buf,fieldnames=list(rows[0]),delimiter='\t');writer.writeheader()
        # Spreadsheet formula characters in external reports stay inert on download.
        for value in rows:writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in value.items()})
    return StreamingResponse(iter([buf.getvalue().encode('utf-8-sig')]),media_type='text/tab-separated-values',headers={'Content-Disposition':f'attachment; filename="report-{ident}.tsv"'})


@router.post('/campaigns')
def campaign_create(payload:dict):
    with s.transaction() as c:
        listing=s.get(c,'listing',payload['listing_id'])
        return s.put(c,'campaign',payload.get('request_id') or uuid.uuid4().hex,{'listing_id':listing['id'],'name':str(payload.get('name','活动计划'))[:100],
          'type':str(payload.get('type','coupon'))[:80],'starts_at':str(payload.get('starts_at',''))[:80],'ends_at':str(payload.get('ends_at',''))[:80],
          'budget':s.money(payload.get('budget',0)),'currency':listing['draft'].get('currency','USD'),'notes':str(payload.get('notes',''))[:2000],
          'execution':'manual','status':'planned','platform_reference':''})


@router.post('/campaigns/{ident}/record')
def campaign_record(ident:int,payload:dict):
    with s.transaction() as c:
        row=s.get(c,'campaign',ident)
        if payload.get('status') not in {'active','cancelled','completed'}:raise HTTPException(422,'活动状态无效')
        if not payload.get('platform_reference'):raise HTTPException(422,'请记录平台办理依据')
        row.update(status=payload['status'],platform_reference=str(payload['platform_reference'])[:500]);return s.save(c,'campaign',row)


@router.post('/rules')
def rule_create(payload:dict):
    with s.transaction() as c:
        target=s.get(c,'target',payload['target_id'])
        if payload.get('action','suggest') not in {'suggest','pause'}:raise HTTPException(422,'运营规则动作无效')
        return s.put(c,'rule',str(target['id'])+':no_sales',{'target_id':target['id'],'name':'长期无销量观察',
          'days':s.quantity(payload.get('days',90),1),'enabled':bool(payload.get('enabled',False)),'action':payload.get('action','suggest'),'status':'configured'})


@router.post('/rules/evaluate')
def evaluate_rules():
    with s.transaction() as c:
        results=[]
        for rule in s.all_rows(c,'rule'):
            if not rule['enabled']:continue
            target=s.get(c,'target',rule['target_id']);coverage=target.get('orders_coverage_since')
            if not target.get('enabled'):continue
            if not coverage or time.time()-target.get('last_order_sync',0)>1800 or (datetime.now(timezone.utc)-datetime.fromisoformat(coverage.replace('Z','+00:00'))).days<rule['days']:
                results.append({'rule_id':rule['id'],'status':'insufficient_data','message':'订单观察期不完整'});continue
            cutoff=time.time()-rule['days']*86400
            for listing in s.all_rows(c,'listing'):
                if listing['target_id']!=target['id'] or listing.get('paused'):continue
                # No destructive automation: without observed buyable duration, surface investigation only.
                skus={x['seller_sku'] for x in listing['draft'].get('skus',[])}
                recent=[o for o in s.all_rows(c,'order') if o['target_id']==target['id'] and o['status']!='cancelled' and datetime.fromisoformat(o['ordered_at'].replace('Z','+00:00')).timestamp()>=cutoff and any(i['seller_sku'] in skus for i in o['items'])]
                if not recent:
                    operations.notify(c,f"no_sales:{listing['id']}:{datetime.now().date()}",'观察期无销量：'+listing['name']+'，请核对可售天数和库存',listing['id'])
                    results.append({'listing_id':listing['id'],'status':'suggest_review'})
                    if rule.get('action')=='pause' and listing.get('buyable_since') and listing['buyable_since']<=cutoff and time.time()-listing.get('checked_at',0)<1800:
                        settings=s.by_key(c,'settings','global') or {}
                        if target['environment']!='production' or settings.get('live_writes_enabled'):
                            active=c.execute("SELECT 1 FROM commerce_jobs WHERE kind IN ('availability','submit') AND status IN ('queued','running','retry_wait','unknown','paused') AND json_extract(payload,'$.listing_id')=?",(listing['id'],)).fetchone()
                            if not active:
                                s.enqueue(c,'availability',f"rule-pause:{rule['id']}:{listing['id']}:{datetime.now().date()}",{'listing_id':listing['id'],'action':'pause','published_version':listing.get('published_version')})
                                results[-1]['status']='pause_queued'
        return results


@router.post('/notices/{ident}/read')
def notice_read(ident:int):
    with s.transaction() as c:
        row=s.get(c,'notice',ident);row['read']=True;return s.save(c,'notice',row)


@router.get('/job-history')
def job_history(before:int|None=None,limit:int=50):
    from . import overview_jobs
    with s.main().db() as c:return overview_jobs.history(c,before=before,limit=limit)


@router.get('/jobs/{ident}')
def job_get(ident:int):
    with s.main().db() as c:return s.job(c,ident)


@router.post('/jobs/{ident}/retry')
def job_retry(ident:int):
    from . import jobs
    with s.transaction() as c:
        return jobs.retry(c,ident)


@router.get('/export/{kind}/{ident}')
def export(kind:str,ident:int,request:Request):
    if kind not in {'listing','order','production'}:raise HTTPException(404)
    with s.main().db() as c:row=s.get(c,kind,ident)
    if kind=='listing':row=catalog.material_view(row)
    exported=row
    if kind=='production':
        frozen=production_view(row)
        exported=frozen if getattr(request.state,'commerce_role','admin')=='production' else row|{'facts':frozen['facts']}
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('资料.json',json.dumps(exported,ensure_ascii=False,indent=2))
        if kind=='production':z.writestr('生产事实.txt',exported['facts'])
        z.writestr('说明.txt','内部资料包。规格和工艺以实际商品/订单快照为准；未提供的工厂工艺文件不包含在本包中。')
        if kind=='listing':
            from . import locales
            draft=row.get('draft',{})
            if draft:
                code=locales.normalize(draft.get('language','en'))
                z.writestr('listing-'+code+'.json',json.dumps({'language':code,'content':draft.get('content',{}),
                    'variants':[{k:v.get(k,'') for k in ['seller_sku','name','color','size']} for v in draft.get('skus',[])]},ensure_ascii=False,indent=2))
                z.writestr('语言与地区说明.txt',f"本资料版本语言：{locales.LANGUAGES[code]} ({code})\n"+
                    json.dumps(draft.get('localization') or {},ensure_ascii=False,indent=2)+'\n'+
                    '\n'.join(row.get('material_issues',[]))+'\n图片与图文详情保留原设计语言，不会因文案语言变化自动翻译图内文字；用于目标地区前需生成对应语言的设计。')
        images=list(row.get('draft',{}).get('images',[]))+[a for v in row.get('draft',{}).get('skus',[]) for a in v.get('images',[])]
        if kind=='order':images=[x for item in row['items'] for x in item['snapshot'].get('images',[])]
        if kind=='production':images=row.get('snapshot',{}).get('images',[])
        from app import detail_page
        detail_pages=[row.get('draft',{}).get('detail_page')]
        if kind=='order':detail_pages=[item['snapshot'].get('detail_page') for item in row['items']]
        if kind=='production':detail_pages=[row.get('snapshot',{}).get('detail_page')]
        detail_seen=set()
        for manifest in detail_pages:
            if manifest and manifest['run_id'] not in detail_seen:
                detail_seen.add(manifest['run_id'])
                detail_page.write_zip(z,manifest,'详情页/'+str(manifest['project_id']))
        seen=set()
        for img in images:
            if img['asset_id'] in seen:continue
            seen.add(img['asset_id'])
            with s.main().db() as c:asset=s.get(c,'media',img['asset_id'])
            z.write(media.path(asset['key']),'images/'+asset['key'])
    buf.seek(0)
    return StreamingResponse(buf,media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{kind}-{ident}.zip"'})
