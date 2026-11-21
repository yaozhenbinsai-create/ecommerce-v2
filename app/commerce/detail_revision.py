"""Explicit detail-only revisions with stable source and previous receipts."""
import hashlib
import time
from fastapi import HTTPException
from . import store as s, amazon_aplus, aplus_layout, locales, listing_identity


def completed_previous(c,pub):
    """A known rejected format may retain an older, valid remote document."""
    seen=set()
    while pub.get('previous_publication_id') and not pub.get('reference') and pub.get('render_protocol'):
        if pub['id'] in seen or pub.get('write_step') or pub['status']!='failed':
            raise HTTPException(409,'此前详情请求结果未明确，请先回查，不能新建重复请求')
        seen.add(pub['id']);pub=s.get(c,'detail_publication',pub['previous_publication_id'])
    return pub


def stage(c,listing):
    from . import detail_publication as detail
    version=listing.get('published_version')
    if not version or listing.get('paused'):raise HTTPException(409,'请先完成商品提交；已暂停商品不能更新详情')
    key=f"{listing['id']}:{version}:render:{aplus_layout.PROTOCOL}"
    old=s.by_key(c,'detail_publication',key)
    if old:return old
    if not listing.get('detail_publication_id'):raise HTTPException(409,'请先核对原详情回执')
    current=s.get(c,'detail_publication',listing['detail_publication_id']);previous=completed_previous(c,current)
    if previous['version']!=version:raise HTTPException(409,'原详情与已提交商品版本不一致')
    children=[s.get(c,'detail_publication',ident) for ident in previous.get('children',[])] if previous['status']=='grouped' else [previous]
    for pub in [current,previous,*children]:
        if pub.get('write_step') or (pub in children and pub['status'] not in {'failed','approved','published','mock_published'}):
            raise HTTPException(409,'原详情正在处理、被驳回或结果待核实，请先回查')
        if c.execute("SELECT 1 FROM commerce_jobs WHERE kind IN ('detail_submit','detail_check') AND json_extract(payload,'$.publication_id')=? AND status IN ('queued','running','retry_wait','unknown')",(pub['id'],)).fetchone():
            raise HTTPException(409,'原详情任务仍在处理，请等待原回执')
    target=s.get(c,'target',listing['target_id']);dest=detail.identity(c,target)
    draft=s.by_key(c,'material_version',f"{listing['id']}:{version}")['draft']
    locales.assert_current(target,draft);detail.validate_detail(c,listing,draft)
    listing_identity.assert_source_current(c,listing,target,draft)
    sku_map=lambda value:sorted((sku['sku_id'],sku['seller_sku']) for sku in value.get('skus',[]))
    if previous['destination']!=dest or listing['draft'].get('detail_page')!=draft.get('detail_page') or sku_map(listing['draft'])!=sku_map(draft):
        raise HTTPException(409,'店铺、详情原图或实际SKU已变化，请先核对；不会提交另一款产品')
    digest=hashlib.sha256(key.encode()).hexdigest()[:12]
    pub=s.put(c,'detail_publication',key,{'listing_id':listing['id'],'version':version,'target_id':target['id'],
        'destination':dest,'name':f'ZT-{digest}-L{listing["id"]}-V{version}-R2','locale':locales.resolve(target).replace('_','-'),
        'render_protocol':aplus_layout.PROTOCOL,'previous_publication_id':current['id'],'template_publication_id':previous['id'],
        'status':'queued','uploads':[],'asins':[],'reference':'','error':'','next_at':time.time(),'checked_at':0,'write_step':''})
    listing['detail_publication_id']=pub['id'];s.save(c,'listing',listing)
    s.audit(c,'detail_render_revision',listing['id'],s.dump({'publication_id':pub['id'],'previous_publication_id':current['id'],'protocol':aplus_layout.PROTOCOL}))
    detail.schedule_one(c,pub)
    return pub


def child_fields(c,pub,sku_id):
    if pub.get('render_protocol')!=aplus_layout.PROTOCOL:return {}
    previous=s.get(c,'detail_publication',pub['template_publication_id'])
    candidates=[s.get(c,'detail_publication',ident) for ident in previous.get('children',[])] if previous['status']=='grouped' else [previous]
    matches=[entry for entry in candidates if entry.get('section_sku_ids')==[sku_id]]
    if len(matches)!=1:raise HTTPException(409,'原详情回执未唯一对应本次真实规格')
    return {'render_protocol':aplus_layout.PROTOCOL,'previous_publication_id':matches[0]['id'],'template_publication_id':matches[0]['id']}


def template(pub,target,section_count):
    from . import detail_publication as detail
    if not pub.get('template_publication_id'):return None
    if pub.get('document_template'):return pub['document_template']
    with s.main().db() as c:previous=s.get(c,'detail_publication',pub['template_publication_id'])
    if not previous.get('reference'):return None
    body=amazon_aplus.call(target,'GET',amazon_aplus.resource(previous['reference']),includedDataSet='METADATA,CONTENTS')
    record=body.get('contentRecord') or {};meta=record.get('contentMetadata') or {};doc=record.get('contentDocument') or {}
    modules=doc.get('contentModuleList') or []
    if (record.get('contentReferenceKey')!=previous['reference'] or meta.get('marketplaceId')!=target['marketplace_id']
            or meta.get('status') not in {'DRAFT','APPROVED'} or doc.get('name')!=previous['name']
            or amazon_aplus.normalized_locale(doc.get('locale'))!=amazon_aplus.normalized_locale(pub['locale'])
            or doc.get('contentType')!='EBC' or len(modules)!=section_count
            or any(module.get('contentModuleType')!='STANDARD_HEADER_IMAGE_TEXT' or
                   not (module.get('standardHeaderImageText') or {}).get('block',{}).get('image') for module in modules)):
        raise HTTPException(409,'原平台详情身份或模块已变化，请核对；不会覆盖其他模块或人工内容')
    def compact(value):
        if isinstance(value,dict):return {key:compact(item) for key,item in value.items() if item is not None}
        if isinstance(value,list):return [compact(item) for item in value]
        return value
    result=compact(doc);detail.save(pub,document_template=result)
    return result


def prepare_document(pub,target,draft,sections):
    from . import detail_publication as detail
    prior=template(pub,target,len(sections));assets=[]
    for index,section in enumerate(sections):
        path=s.main().simple_design_flow.local_path(section['url'],pid=draft['detail_page']['project_id'])
        module=(prior or {}).get('contentModuleList',[])
        formatted=aplus_layout.prepare(path,force_header=bool(module and aplus_layout.text_present(module[index])))
        for asset in formatted:asset.update(source_section=index,sku_id=section['sku_id'],source_sha256=section['sha256'])
        assets.extend(formatted)
    if len(assets)>aplus_layout.MAX_MODULES:raise HTTPException(422,'完整详情超过当前基础模块额度，请核对发布格式；不会丢弃原图内容')
    manifest=[{key:value for key,value in asset.items() if key!='raw'} for asset in assets]
    if pub.get('render_assets') and pub['render_assets']!=manifest:raise HTTPException(409,'已上传的详情格式与当前结果不同，保留原回执，请先核对')
    detail.save(pub,render_assets=manifest,image_render_protocol=aplus_layout.PROTOCOL)
    uploads=list(pub.get('uploads') or [])
    if len(uploads)>len(assets):raise HTTPException(409,'详情上传回执多于当前模块，请核对原请求')
    for asset in assets[len(uploads):]:
        detail.assert_writable(pub)
        uploads.append(amazon_aplus.upload(target,asset['raw']));detail.save(pub,uploads=uploads)
    return aplus_layout.document(pub['name'],pub['locale'],uploads,manifest,draft['content']['title'],prior)
