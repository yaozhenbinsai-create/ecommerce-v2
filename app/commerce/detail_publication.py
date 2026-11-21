"""Platform-scoped detail publication, with persistent progress and recovery."""
import hashlib
import re
import time
from fastapi import HTTPException
from . import store as s,amazon,amazon_aplus,locales

BUSY={'queued','uploading','waiting_listing','waiting_asin','waiting_catalog','submitted','approved','unknown'}
SYNC_WINDOW=48*3600
SYNC_DELAYS=(300,900,1800,3600)
LABELS={'missing':'未包含详情页','waiting_listing':'等待商品提交','queued':'待提交详情','uploading':'正在上传详情',
        'waiting_asin':'等待平台商品编号','waiting_catalog':'等待亚马逊同步商品，随后自动继续详情','submitted':'详情待平台审核','approved':'详情已审核，等待发布','published':'详情已发布',
        'mock_published':'详情模拟已发布','failed':'详情提交失败','rejected':'详情审核驳回','unknown':'详情结果待核实','superseded':'已由新版替换','grouped':'按规格分别提交详情'}


def identity(c,target):
    account=c.execute('SELECT platform,enabled,seller_id FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
    if not account or not account['enabled'] or not target.get('enabled'):raise HTTPException(409,'平台账号或地区已停用')
    return {**{k:target.get(k) for k in ('id','account_id','marketplace_id','environment','region','seller_id')},'platform':account['platform'],'account_seller_id':account['seller_id'],'language':locales.resolve(target)}


def can_supersede(c,pub):
    if pub['status']!='waiting_listing' or pub.get('reference') or pub.get('write_step'):return False
    return not c.execute("SELECT 1 FROM commerce_jobs WHERE kind='submit' AND json_extract(payload,'$.listing_id')=? AND json_extract(payload,'$.version')=? AND status IN ('queued','running','retry_wait','unknown')",(pub['listing_id'],pub['version'])).fetchone()


def validate_detail(c,listing,draft):
    """Check frozen segment evidence before any new platform write.

    Historical receipt reconciliation uses load(write=False), so old content and
    ambiguous platform responses remain inspectable without granting write approval.
    """
    from app import detail_page
    detail=draft.get('detail_page') or {}
    sections=detail.get('sections')
    expected=[entry.get('sku_id') for entry in draft.get('skus',[])]
    # Keep one verified section per actual SKU when a family has more than the
    # usual layout budget. Standard EBC modules are validated by Amazon before
    # creation; never drop a real variant to satisfy a local seven-panel limit.
    section_limit=detail_page.MAX_SECTIONS*max(1,len(expected))
    if not isinstance(sections,list) or not 1<=len(sections)<=section_limit:
        raise HTTPException(422,f'当前详情最多支持 {section_limit} 个完整分段，请明确拆分或选择；不会截断或丢弃分段')
    product=s.get(c,'product',listing['product_id'])
    if detail.get('project_id')!=product['project_id']:raise HTTPException(422,'详情页与当前商品不匹配')
    if any(not isinstance(section,dict) for section in sections):raise HTTPException(422,'详情分段资料不完整，请重新生成')
    if not detail_page.manifest_ready(detail):
        raise HTTPException(422,'详情分段生成记录或输入凭据不完整，请重新生成详情页')
    if (not expected or any(type(sku_id) is not int or sku_id<=0 for sku_id in expected) or len(set(expected))!=len(expected)):
        raise HTTPException(422,'上架 SKU 身份不完整，请先核对商品档案并重新生成资料')
    covered=[section.get('sku_id') for section in sections]
    if any(type(sku_id) is not int for sku_id in covered) or set(covered)!=set(expected):
        raise HTTPException(422,'详情分段未完整覆盖本次上架 SKU 或混入其他款式；请明确拆分或重新选择，不会静默遗漏')
    if any(covered.count(sku_id)>detail_page.MAX_SECTIONS for sku_id in expected):
        raise HTTPException(422,'每个规格最多7段详情；保留原分段，不截断或跨规格混排')
    if detail.get('presentation_protocol')=='complete-per-sku-v2' and any(covered.count(sku_id)<4 for sku_id in expected):
        raise HTTPException(422,'完整展示要求每个规格至少4段连续详情')
    if detail.get('presentation_protocol')=='one-detail-per-sku-v1' and any(covered.count(sku_id)!=1 for sku_id in expected):
        raise HTTPException(422,'每个规格必须包含自己的一张完整详情图')
    keys=[(section.get('sku_id'),section.get('page_key')) for section in sections]
    if len(set(keys))!=len(keys):raise HTTPException(422,'同一规格详情重复引用同一分段，不能重复凑数')
    for section in sections:
        digest=section.get('sha256')
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or not s.main().simple_design_flow.usable_result(section):
            raise HTTPException(422,'详情分段缺少与图片绑定的生成记录，请重新生成详情页')
        path=s.main().simple_design_flow.local_path(section.get('url',''),pid=product['project_id'])
        if not path.is_file():raise HTTPException(422,'详情分段图片不存在，请重新生成详情页')
        if s.main().simple_design_flow.file_hash(path)!=digest:
            raise HTTPException(422,'详情分段图片与已保存版本不一致，请保留历史文件并重新生成详情页')
    return detail


def preflight(c,listing,draft,target):
    from app import detail_page
    detail=draft.get('detail_page')
    if not detail:
        product=s.get(c,'product',listing['product_id'])
        historical=detail_page.current(product['project_id'],c)
        if historical:
            if not detail_page.manifest_ready(historical):
                raise HTTPException(422,'历史或候选详情尚未通过当前审核，请从商品档案重新设计详情，再生成上架资料；历史图片保留')
            raise HTTPException(422,'当前上架资料未包含已完成的详情页，请先重新生成生产资料并确认覆盖')
        if target['environment']=='production':raise HTTPException(422,'正式上架需要完整图文详情，请先完成详情页并生成生产资料')
        return None
    dest=identity(c,target)
    if target['environment']!='mock' and dest['platform']!='亚马逊':raise HTTPException(422,'该平台尚未接入图文详情提交接口')
    if target['environment']=='sandbox':raise HTTPException(422,'静态沙箱不能发布本商品详情页，请使用正式授权店铺或本地模拟')
    lang=locales.assert_current(target,draft)
    image_language=str(detail.get('language','')).replace('-','_')
    if not image_language or (image_language!=lang and image_language!=lang.split('_')[0]):
        raise HTTPException(422,'详情图语言与店铺地区不一致，请先生成对应语言的详情页和资料')
    validate_detail(c,listing,draft)
    from . import gallery_assembly
    reuse=gallery_assembly.reusable_detail(c,listing,draft,target)
    reused_ids={reuse['id'],*reuse.get('children',[])} if reuse else set()
    for other in s.all_rows(c,'detail_publication'):
        stable_older=(detail.get('presentation_protocol')=='complete-per-sku-v2' and other.get('status')=='approved' and other.get('reference') and not other.get('write_step') and other.get('destination')==dest)
        if not stable_older and other['id'] not in reused_ids and other['listing_id']==listing['id'] and other['version']!=listing['draft_version'] and other['status'] in BUSY and not can_supersede(c,other):
            raise HTTPException(409,'上一版详情仍在处理或待核实，请先完成再提交新版')
    return dest


def stage(c,listing,version):
    key=f"{listing['id']}:{version}";old=s.by_key(c,'detail_publication',key)
    if old:return old
    material=s.by_key(c,'material_version',f"{listing['id']}:{version}");draft=material['draft']
    target=s.get(c,'target',listing['target_id']);dest=preflight(c,listing,draft,target)
    if not dest:return None
    from . import gallery_assembly
    reuse=gallery_assembly.reusable_detail(c,listing,draft,target)
    if reuse:
        s.put(c,'detail_publication_reuse',key,{'listing_id':listing['id'],'version':version,
            'publication_id':reuse['id'],'source_version':reuse['version'],
            'reason':'仅补齐当前规格图库；详情原图、SKU与目的地逐项相同，沿用原回执'})
        listing['detail_publication_id']=reuse['id'];s.save(c,'listing',listing)
        return reuse
    for other in s.all_rows(c,'detail_publication'):
        if other['listing_id']==listing['id'] and other['version']!=version and can_supersede(c,other):
            other.update(status='superseded',error='商品资料已修订，尚未提交平台的旧详情已替换');s.save(c,'detail_publication',other)
    digest=hashlib.sha256(s.dump({'destination':dest,'detail':draft['detail_page'],'version':version,'listing_id':listing['id']}).encode()).hexdigest()[:20]
    record=s.put(c,'detail_publication',key,{'listing_id':listing['id'],'version':version,'target_id':target['id'],'destination':dest,
        'name':f'ZT-{digest}-L{listing["id"]}-V{version}','locale':locales.resolve(target).replace('_','-'),
        'status':'waiting_listing','uploads':[],'asins':[],'reference':'','error':'','next_at':0,'checked_at':0,'write_step':''})
    listing['detail_publication_id']=record['id'];s.save(c,'listing',listing)
    return record


def ready(c,listing,version,*,manual_pause_epoch=None):
    pub=stage(c,listing,version)
    if not pub:return
    from . import execution_controls
    if execution_controls.manual_current(c,{'manual_pause_epoch':manual_pause_epoch}):
        pub=s.save(c,'detail_publication',pub|{'manual_pause_epoch':manual_pause_epoch})
    if pub['status']=='waiting_listing':
        pub.update(status='queued',next_at=time.time());s.save(c,'detail_publication',pub)
    schedule_one(c,pub)


def continue_listing(c,listing):
    """Continue the accepted version's remaining steps; never send the product again."""
    version=listing.get('published_version')
    if not version or version!=listing.get('draft_version') or listing.get('paused'):
        raise HTTPException(409,'请先核对已提交商品版本和本地修改，不能重复发布或恢复已停售商品')
    unresolved=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('submit','availability') AND json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(listing['id'],)).fetchone()
    if unresolved:raise HTTPException(409,'原商品请求尚未结束或结果待核实，请先回查原请求')
    target=s.get(c,'target',listing['target_id']);identity(c,target)
    if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):
        raise HTTPException(422,'正式写入未启用，请先在经营设置中配置')
    if not listing.get('detail_publication_id'):
        ready(c,listing,version)
        listing=s.get(c,'listing',listing['id'])
    if not listing.get('detail_publication_id'):
        return {'listing_id':listing['id'],'status':'submitted','already_submitted':True}
    pub=s.get(c,'detail_publication',listing['detail_publication_id'])
    if pub['version']!=version:raise HTTPException(409,'详情与已提交商品版本不一致，请先核对原资料')
    result=action(c,listing,'retry')
    s.audit(c,'listing_continued',listing['id'],s.dump({'version':version,'publication_id':pub['id']}))
    return {'listing_id':listing['id'],'version':version,'already_submitted':True,'continuation':result}


def schedule_one(c,pub,*,manual=False):
    if manual:
        from . import execution_controls
        pub=s.save(c,'detail_publication',pub|{'manual_pause_epoch':execution_controls.pause_epoch(c)})
    if pub['status'] not in BUSY or pub['status']=='waiting_listing' or pub.get('next_at',0)>time.time():return
    if pub['status'] in {'queued','uploading','waiting_asin','waiting_catalog'}:
        listing=s.get(c,'listing',pub['listing_id'])
        if listing.get('paused'):return
        from . import execution_controls
        if not execution_controls.detail_allowed(c,listing,pub):return
        if listing.get('environment')=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):return
    active=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('detail_submit','detail_check') AND json_extract(payload,'$.publication_id')=? AND status IN ('queued','running','retry_wait')",(pub['id'],)).fetchone()
    if active:return s.job(c,active[0])
    kind='detail_check' if pub['status'] in {'submitted','approved','unknown'} else 'detail_submit'
    return s.enqueue(c,kind,f"{kind}:{pub['id']}:{pub.get('next_at',0)}",{'publication_id':pub['id'],'listing_id':pub['listing_id'],'version':pub['version'],'manual_request':manual})


def schedule(c):
    for pub in s.all_rows(c,'detail_publication'):
        if pub['status']=='waiting_listing' and not pub.get('reference') and not pub.get('write_step'):
            listing=s.get(c,'listing',pub['listing_id'])
            unresolved=c.execute("SELECT 1 FROM commerce_jobs WHERE kind IN ('submit','availability') AND json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait','unknown')",(listing['id'],)).fetchone()
            if listing.get('published_version')==pub['version'] and not listing.get('paused') and not unresolved:
                pub.update(status='queued',next_at=time.time(),error='')
                s.save(c,'detail_publication',pub)
        schedule_one(c,pub)


def save(pub,**changes):
    with s.transaction() as c:
        live=s.get(c,'detail_publication',pub['id']);live.update(changes);result=s.save(c,'detail_publication',live)
    pub.update(result);return result


def load(job,write=True):
    with s.main().db() as c:
        pub=s.get(c,'detail_publication',job['payload']['publication_id']);listing=s.get(c,'listing',pub['listing_id'])
        target=s.get(c,'target',pub['target_id']);dest=identity(c,target)
        if {k:v for k,v in dest.items() if write or k!='language'}!={k:v for k,v in pub['destination'].items() if write or k!='language'}:raise HTTPException(409,'详情提交对应的平台账号、地区或语言配置已变化，请恢复配置后核对')
        if write and listing.get('paused'):raise HTTPException(409,'商品已暂停，详情提交暂停')
        if write and target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):raise HTTPException(422,'正式写入未启用')
        if write:
            from . import execution_controls
            if not execution_controls.detail_allowed(c,listing,pub):
                raise HTTPException(409,'本次上架已暂停，已完成的商品和详情回执保留')
        draft=s.by_key(c,'material_version',f"{listing['id']}:{pub['version']}")['draft']
        if write:
            locales.assert_current(target,draft)
            validate_detail(c,listing,draft)
            if pub.get('parent_id') or pub.get('render_protocol'):
                from . import listing_identity
                listing_identity.assert_source_current(c,listing,target,draft)
    return pub,listing,target,draft


def perform(pub,step,action):
    assert_writable(pub)
    save(pub,write_step=step,write_started_at=time.time())
    try:result=action()
    except amazon.PlatformError as exc:
        if not exc.unknown:save(pub,write_step='')
        raise
    changes={'write_step':'','error':''}
    if step=='create':
        if not result.get('contentReferenceKey'):raise amazon.PlatformError('创建详情文档结果缺少编号，请核对',503,unknown=True)
        changes['reference']=result['contentReferenceKey']
    elif step=='relations':changes['related']=True
    elif step=='approval':changes.update(status='submitted',next_at=time.time()+3600)
    save(pub,**changes)
    return result


def assert_writable(pub):
    """Recheck an owner's pause between network steps without reprocessing the art."""
    from . import execution_controls
    with s.main().db() as c:
        current=s.get(c,'detail_publication',pub['id'])
        listing=s.get(c,'listing',current['listing_id'])
        target=s.get(c,'target',current['target_id'])
        if identity(c,target)!=current['destination']:
            raise HTTPException(409,'详情对应的店铺或地区已变化，请核对原请求')
        if listing.get('paused') or not execution_controls.detail_allowed(c,listing,current):
            raise HTTPException(409,'本次上架已暂停，已完成的商品和详情回执保留')
        if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):
            raise HTTPException(422,'正式写入未启用')


def recover_create(pub,target):
    matches=amazon_aplus.find_document(target,pub['name'])
    if len(matches)!=1:raise amazon.PlatformError('创建详情的结果待核实，暂未找到唯一匹配文档；不会重复创建',409,unknown=True)
    save(pub,reference=matches[0],write_step='',status='queued',next_at=time.time())


def wait_for_sync(pub,status,message,**changes):
    """Bound propagation waits across restarts; keep every completed write receipt."""
    now=time.time();started=pub.get('sync_started_at',now)
    count=pub.get('sync_waits',0)+1
    changes.update(sync_started_at=started,sync_waits=count,write_step='')
    if now-started>=SYNC_WINDOW:
        return save(pub,**changes,status='failed',next_at=0,sync_expired=True,
                    error='已等待亚马逊同步商品48小时，详情尚未提交成功；请核对商品后继续，已上传图片和草稿会保留')
    delay=SYNC_DELAYS[min(count-1,len(SYNC_DELAYS)-1)]
    return save(pub,**changes,status=status,error=message,
                next_at=min(now+delay,started+SYNC_WINDOW))


def catalog_not_ready(exc,asins):
    """Only definite missing-current-ASIN responses are propagation failures."""
    if not isinstance(exc,amazon.PlatformError) or exc.status!=400 or exc.unknown or not exc.errors:return False
    expected=set(asins);seen=set()
    for row in exc.errors:
        if row.get('code')!='InvalidInput':return False
        message=row.get('message','');details=row.get('details','')
        match=re.fullmatch(r'The following ASINs do not exist in the catalog:\s*(.+)',message.strip())
        if match:
            values=re.split(r'\s*,\s*',match[1].strip())
            if not all(re.fullmatch('[A-Z0-9]{10}',value) for value in values):return False
            missing=set(values)
            if not missing or not missing<=expected:return False
            seen.update(missing)
        elif message.strip()=='Invalid Input' and details.strip() in expected:
            continue
        else:return False
    return bool(seen)


def _submit(job):
    pub,listing,target,draft=load(job,write=False)
    if pub.get('render_protocol')=='full-frame-v1':
        raise HTTPException(409,'该竖图排版已被亚马逊模块尺寸限制拒绝，保留原商品和原详情；请先处理排版方案，不能重试此请求')
    if pub['status'] in {'published','mock_published','submitted','approved','rejected','grouped'}:return pub
    if pub.get('write_step')=='approval':return _check(job)
    pub,listing,target,draft=load(job)
    if pub['status'] in {'waiting_asin','waiting_catalog'} and pub.get('next_at',0)>time.time():return pub
    if listing.get('published_version')!=pub['version']:
        if (listing.get('published_version') or 0)>pub['version']:return save(pub,status='superseded',error='已提交更新版本，旧详情不再自动发布',next_at=0)
        return save(pub,status='waiting_listing',error='先完成同版本商品提交',next_at=0)
    if target['environment']=='mock':return save(pub,status='mock_published',reference='MOCK-'+pub['name'],error='',next_at=0)
    if pub.get('write_step')=='create' and not pub['reference']:recover_create(pub,target)
    full_draft=draft
    sections=draft['detail_page']['sections']
    if pub.get('section_sku_ids'):
        selected=pub['section_sku_ids']
        if len(selected)!=1 or selected[0] not in {sku['sku_id'] for sku in draft['skus']}:
            raise HTTPException(422,'详情分组未绑定本次真实规格')
        sections=[section for section in sections if section['sku_id'] in selected]
        if not 1<=len(sections)<=7:raise HTTPException(422,'本规格需要1至7个完整分段，不能丢失、截断或混入其他规格')
        own_detail={**draft['detail_page'],'sections':sections}
        if 'sku_documents' in own_detail:
            own_detail['sku_documents']=[item for item in own_detail['sku_documents'] if item.get('sku_id') in selected]
        draft={**draft,'skus':[sku for sku in draft['skus'] if sku['sku_id'] in selected],
               'detail_page':own_detail}
    elif len(sections)>7 or (len(draft['skus'])>1 and not pub.get('reference') and draft['detail_page'].get('presentation_protocol') in {'complete-per-sku-v2','one-detail-per-sku-v1'}):
        return partition(pub,listing,target,draft)
    if 'sync_started_at' not in pub:save(pub,sync_started_at=time.time(),sync_waits=0)
    asins=pub.get('asins') or amazon_aplus.resolve_asins(target,draft)
    if not asins:return wait_for_sync(pub,'waiting_asin','平台尚未返回全部款式的商品编号，稍后自动继续')
    if any(not re.fullmatch('[A-Z0-9]{10}',a) for a in asins):raise HTTPException(422,'平台商品编号格式无效')
    if target['environment']=='production' and not pub.get('catalog_ready'):
        for asin in asins:
            try:catalog=amazon.get_catalog_item(target,asin)['body']
            except amazon.PlatformError as exc:
                if exc.status!=404 or exc.unknown:raise
                return wait_for_sync(pub,'waiting_catalog','商品编号已分配，等待亚马逊同步后自动继续详情',asins=asins)
            if catalog.get('asin')!=asin or not any(row.get('marketplaceId')==target['marketplace_id'] for row in catalog.get('summaries',[])):
                raise HTTPException(409,'平台目录编号或站点不一致，请核对')
        if pub.get('await_catalog'):
            with s.main().db() as c:
                from . import listing_identity
                listing_identity.assert_source_current(c,listing,target,full_draft)
        save(pub,await_catalog=False,catalog_ready=True)
    save(pub,asins=asins,status='uploading',error='')
    if not pub['reference']:
        from . import aplus_layout,detail_revision
        use_layout=(pub.get('render_protocol')==aplus_layout.PROTOCOL or pub.get('image_render_protocol')==aplus_layout.PROTOCOL or
                    (draft['detail_page'].get('presentation_protocol')=='one-detail-per-sku-v1' and not pub.get('uploads')))
        if use_layout:
            document=detail_revision.prepare_document(pub,target,draft,sections)
        else:
            uploads=list(pub['uploads'])
            for section in sections[len(uploads):]:
                assert_writable(pub)
                path=s.main().simple_design_flow.local_path(section['url'],pid=draft['detail_page']['project_id'])
                upload_id=amazon_aplus.upload(target,amazon_aplus.prepare_image(path));uploads.append(upload_id);save(pub,uploads=uploads)
            document=amazon_aplus.document(pub['name'],pub['locale'],uploads,draft['content']['title'])
        amazon_aplus.call(target,'POST','/contentAsinValidations',{'contentDocument':document},asinSet=','.join(asins))
        perform(pub,'create',lambda:amazon_aplus.call(target,'POST','/contentDocuments',{'contentDocument':document}))
    # Repeating the same relation set on our version-owned document is idempotent.
    if not pub.get('related'):perform(pub,'relations',lambda:amazon_aplus.call(target,'POST',amazon_aplus.resource(pub['reference'],'/asins'),{'asinSet':asins}))
    perform(pub,'approval',lambda:amazon_aplus.call(target,'POST',amazon_aplus.resource(pub['reference'],'/approvalSubmissions')))
    return pub


def partition(pub,listing,target,draft):
    """Keep each real variant's full section in its own version-owned document."""
    if pub.get('write_step'):
        raise amazon.PlatformError('旧详情写入结果待核实，请先回查',409,unknown=True)
    with s.main().db() as c:
        from . import listing_identity
        listing_identity.assert_source_current(c,listing,target,draft)
    remote=None
    if pub.get('reference'):
        remote=amazon_aplus.call(target,'GET',amazon_aplus.resource(pub['reference']),includedDataSet='METADATA')
        record=remote.get('contentRecord') or {};meta=record.get('contentMetadata') or {}
        if record.get('contentReferenceKey')!=pub['reference'] or meta.get('marketplaceId')!=target['marketplace_id'] or meta.get('status')!='DRAFT':
            raise HTTPException(409,'旧共享详情不是当前站点已确认的草稿，请先完成审核状态核对')
    with s.transaction() as c:
        current=s.get(c,'detail_publication',pub['id']);live=s.get(c,'listing',listing['id'])
        if current.get('status')=='grouped':return current
        if current.get('write_step') or live.get('paused') or live.get('published_version')!=pub['version']:
            raise HTTPException(409,'详情或商品提交状态在分组前已变化')
        children=[]
        for sku in draft['skus']:
            from . import detail_revision
            sku_id=sku['sku_id'];base=f"{listing['id']}:{pub['version']}"
            if pub.get('render_protocol'):base+=':render:'+pub['render_protocol']
            key=f"{base}:sku:{sku_id}"
            child=s.by_key(c,'detail_publication',key)
            if not child:
                child=s.put(c,'detail_publication',key,{**{key:pub[key] for key in ('listing_id','version','target_id','destination','locale')},
                    **detail_revision.child_fields(c,pub,sku_id),
                    'name':pub['name']+f'-S{sku_id}','parent_id':pub['id'],'section_sku_ids':[sku_id],
                    'status':'queued','uploads':[],'asins':[],'reference':'','error':'','next_at':time.time(),'checked_at':0,'write_step':''})
            from . import execution_controls
            if execution_controls.manual_current(c,current):
                child=s.save(c,'detail_publication',child|{'manual_pause_epoch':current['manual_pause_epoch']})
            children.append(child['id']);schedule_one(c,child)
        current.update(status='grouped',children=children,partition_evidence=remote,
                       error='',next_at=0)
        result=s.save(c,'detail_publication',current)
        s.audit(c,'detail_partitioned_by_actual_sku',listing['id'],s.dump({'publication_id':pub['id'],'children':children,'preserved_reference':pub.get('reference','')}))
    return result


def _check(job):
    pub,listing,target,draft=load(job,write=False)
    if pub['status']=='grouped':
        with s.main().db() as c:children=[s.get(c,'detail_publication',ident) for ident in pub.get('children',[])]
        for child in children:
            if child['status'] in {'submitted','approved','unknown'}:check({'payload':{'publication_id':child['id'],'manual_check':job['payload'].get('manual_check',False)}})
        return pub
    if pub['status']=='mock_published':return pub
    if target['environment']=='mock':return pub
    if not pub['reference']:
        if pub.get('write_step')=='create':recover_create(pub,target)
        return pub
    if time.time()-pub.get('checked_at',0)<(60 if job['payload'].get('manual_check') else 3600):return pub
    save(pub,checked_at=time.time(),next_at=time.time()+3600)
    body=amazon_aplus.call(target,'GET',amazon_aplus.resource(pub['reference']),includedDataSet='METADATA')
    record=body.get('contentRecord',{});meta=record.get('contentMetadata',{})
    if record.get('contentReferenceKey')!=pub['reference'] or meta.get('marketplaceId')!=target['marketplace_id']:
        raise amazon.PlatformError('详情回查返回的文档或地区不匹配',422)
    status=meta.get('status')
    if status=='APPROVED':
        live=amazon_aplus.published(target,pub['reference'],pub['asins'],pub['locale'])
        return save(pub,status='published' if live else 'approved',write_step='',error='',platform_result=body)
    if status=='SUBMITTED':return save(pub,status='submitted',write_step='',error='',platform_result=body)
    if status=='REJECTED':return save(pub,status='rejected',write_step='',error='平台审核驳回，请在平台查看原因并修改详情后重新提交',platform_result=body)
    if status=='DRAFT':
        if pub.get('write_step')=='approval' and time.time()-pub.get('write_started_at',0)<3600:return save(pub,status='unknown',error='提交结果尚未确认，等待平台回查')
        return save(pub,status='failed',write_step='',error='平台文档仍为草稿，可重试提交审核',platform_result=body)
    raise amazon.PlatformError('平台返回未识别的详情状态',422)


def run(job,handler):
    try:return handler(job)
    except Exception as exc:
        with s.main().db() as c:pub=s.get(c,'detail_publication',job['payload']['publication_id'])
        if handler is _submit and catalog_not_ready(exc,pub.get('asins',[])) and not pub.get('write_step'):
            return wait_for_sync(pub,'waiting_catalog','商品编号已分配，但亚马逊详情接口尚未识别；稍后自动继续',
                                 catalog_ready=False,last_sync_error={'message':str(exc)[:1500],
                                 'request_id':exc.request_id,'http_status':exc.status})
        unknown=bool(isinstance(exc,amazon.PlatformError) and exc.unknown) or bool(pub.get('write_step'))
        message=str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,amazon.PlatformError) else '详情处理失败，请检查图片与配置'
        checking_pending=handler is _check and pub['status'] in {'submitted','approved','published','unknown'}
        status=pub['status'] if checking_pending else ('unknown' if unknown else 'failed')
        save(pub,status=status,error=message[:1500],next_at=time.time()+3600 if unknown or checking_pending else 0)
        raise


def submit(job):return run(job,_submit)
def check(job):return run(job,_check)


def view(c,listing):
    pub=s.get(c,'detail_publication',listing['detail_publication_id']) if listing.get('detail_publication_id') else None
    if not pub:return {'status':'ready' if listing.get('draft',{}).get('detail_page') else 'missing','label':'详情待随商品提交' if listing.get('draft',{}).get('detail_page') else LABELS['missing']}
    result={k:pub.get(k) for k in ('id','status','version','reference','error','checked_at','next_at','asins','render_protocol','previous_publication_id')}|{'label':LABELS.get(pub['status'],pub['status'])}
    children=[pub]
    if pub['status']=='grouped':
        children=[s.get(c,'detail_publication',ident) for ident in pub.get('children',[])]
        skus={sku['sku_id']:sku for sku in (listing.get('draft') or {}).get('skus',[])}
        def sku_label(child):
            return '、'.join(str(skus.get(ident,{}).get('color') or skus.get(ident,{}).get('name') or
                                skus.get(ident,{}).get('seller_sku') or f'规格 {ident}') for ident in child.get('section_sku_ids',[]))
        result.update(reference='',historical_reference=pub.get('reference',''),children=[
            {key:child.get(key) for key in ('id','section_sku_ids','status','reference','error','asins','next_at','sync_expired')}|
            {'label':LABELS.get(child['status'],child['status']),'sku_label':sku_label(child)} for child in children])
        statuses={child['status'] for child in children}
        published=sum(child['status']=='published' for child in children)
        covered=[ident for child in children for ident in child.get('section_sku_ids',[])]
        coverage_ok=bool(skus) and set(covered)==set(skus) and len(covered)==len(skus)
        if not coverage_ok:result['label']='图文详情未完整关联本次规格，请核对'
        elif children and published==len(children):result['label']='全部规格详情已发布'
        elif statuses & {'failed','rejected'}:result['label']=f'图文详情未完成：{sum(child["status"] in {"failed","rejected"} for child in children)} 个规格需处理'
        elif 'unknown' in statuses:result['label']='图文详情结果待核实，请核对原请求'
        elif statuses & {'waiting_catalog','waiting_asin'}:result['label']='图文详情等待亚马逊同步商品，随后自动继续'
        elif statuses & {'submitted','approved'}:result['label']='图文详情审核或发布中'
    result.update(total_count=len(children),published_count=sum(child['status']=='published' for child in children),
                  complete=bool(children) and all(child['status']=='published' for child in children) and (pub['status']!='grouped' or coverage_ok),
                  needs_action=any(child['status'] in {'failed','rejected','unknown'} for child in children) or (pub['status']=='grouped' and not coverage_ok))
    from . import execution_controls
    unfinished=[child for child in children if child['status'] in {'queued','uploading','waiting_listing','waiting_asin','waiting_catalog','failed'} and not child.get('write_step')]
    held=any(not execution_controls.detail_allowed(c,listing,child) for child in unfinished)
    result['continuation_held']=held
    result['can_continue_listing']=bool(unfinished and not listing.get('paused') and
        listing.get('published_version')==pub['version']==listing.get('draft_version') and
        (held or any(child['status']=='failed' for child in unfinished)))
    result['own_steps_pending']=bool(unfinished and listing.get('published_version')==pub['version'] and
        not held and not result['can_continue_listing'])
    result['needs_action']=result['needs_action'] or held
    if held:result['label']='本款上架待继续，将沿用已提交商品完成详情'
    from . import aplus_layout
    result['can_update_render']=bool(listing.get('published_version')==pub['version'] and not listing.get('paused')
        and pub.get('render_protocol')!=aplus_layout.PROTOCOL
        and not all(child.get('image_render_protocol')==aplus_layout.PROTOCOL for child in children)
        and all(child['status'] in {'failed','approved','published','mock_published'} and not child.get('write_step') for child in children))
    return result


def retry_group(c,listing,children):
    return {'children_jobs':[action(c,{**listing,'detail_publication_id':child['id']},'retry')
                             for child in children if child['status'] in {'queued','uploading','waiting_listing','waiting_asin','waiting_catalog','failed','unknown'}]}


def action(c,listing,action):
    if action=='render':
        from . import detail_revision
        pub=detail_revision.stage(c,listing)
        schedule_one(c,pub,manual=True)
        return s.get(c,'detail_publication',pub['id'])
    if not listing.get('detail_publication_id'):raise HTTPException(422,'请先上架包含详情页的商品资料')
    pub=s.get(c,'detail_publication',listing['detail_publication_id'])
    if action=='retry' and pub['status'] in {'published','mock_published','submitted','approved','superseded'}:
        return {'publication_id':pub['id'],'status':pub['status'],'already_submitted':True}
    if action=='retry' and pub.get('render_protocol')=='full-frame-v1':
        raise HTTPException(409,'该竖图排版已被亚马逊模块尺寸限制拒绝，不能重复提交；原商品和原详情保留')
    if action=='retry' and pub['version']!=listing.get('published_version'):raise HTTPException(409,'请先完成对应版本的商品提交')
    if action=='retry' and pub.get('status') not in {'unknown','rejected'}:
        from . import execution_controls
        pub=s.save(c,'detail_publication',pub|{'manual_pause_epoch':execution_controls.pause_epoch(c)})
    if pub['status']=='grouped' and action=='retry':
        children=[s.get(c,'detail_publication',ident) for ident in pub.get('children',[])]
        return retry_group(c,listing,children)
    active=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('detail_submit','detail_check') AND json_extract(payload,'$.publication_id')=? AND status IN ('queued','running','retry_wait')",(pub['id'],)).fetchone()
    if active:return s.job(c,active[0])
    if action=='retry' and pub['status'] in {'waiting_asin','waiting_catalog'} and pub.get('next_at',0)>time.time():
        return {'status':pub['status'],'next_at':pub['next_at'],'waiting':True}
    if action=='retry' and pub['status'] in {'failed','rejected','waiting_asin','waiting_listing'} and not pub.get('write_step'):
        if pub['status']=='rejected':raise HTTPException(422,'详情被驳回，请修改详情并重新生成资料后上架新版')
        if 'do not exist in the catalog' in pub.get('error',''):
            pub['await_catalog']=True
        pub.update(status='queued',error='',next_at=time.time(),sync_started_at=time.time(),sync_waits=0,sync_expired=False,catalog_ready=False);s.save(c,'detail_publication',pub)
    kind='detail_check' if action=='check' or pub['status']=='unknown' else 'detail_submit'
    return s.enqueue(c,kind,f"{kind}:manual:{pub['id']}:{int(time.time()//60)}",{'publication_id':pub['id'],'listing_id':listing['id'],'version':pub['version'],'manual_check':action=='check','manual_request':True})
