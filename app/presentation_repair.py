"""Versioned supplementation: retain completed art and generate only selected repairs/gaps."""
import copy
from fastapi import HTTPException
from app import simple_design_flow as flow, simple_design_prompts as prompts, design_contract


def intact(task, pid):
    result=task.get('result') or {}
    return (task.get('status')=='completed' and flow.usable_result(result)
            and flow.file_hash(flow.local_path(result['url'],pid=pid))==result.get('sha256'))


def prepare_pages(products, pages):
    """Retain composition and SKU identity; supplement the missing narrative slots."""
    output=[]
    for product in products:
        own=[copy.deepcopy(page) for page in pages if page['product_key']==product['key'] and page.get('display_role')!='detail_page']
        if not own:raise HTTPException(422,'当前设计缺少本规格历史页面，不能借用其他规格')
        if own:
            covers=[page for page in own if page.get('display_role')=='sales_cover']
            if not covers:
                covers=[page for page in own if page.get('shot_type') in {'context','use'}]
                if covers:covers[0]['display_role']='sales_cover'
            main=[page for page in own if page.get('type')=='主图']
            cover=[page for page in own if page.get('display_role')=='sales_cover']
            own=main+cover+[page for page in own if page not in main+cover]
        minimum=7
        for index in range(len(own),minimum):
            key=f"repair-gallery-{product['key']}-{index+1}"
            roles=['context','visible_detail','specification','use']
            existing={page.get('shot_type') for page in own}
            role=next((role for role in roles if role not in existing),roles[(index-len(pages))%len(roles)])
            purpose=('销售封面：以真实用途与具体环境建立鲜明视觉焦点，承接现有已确认风格'
                     if not any(page.get('display_role')=='sales_cover' for page in own) else
                     '补足本规格的完整购买故事：根据原图与参数选择尚未表达的使用、可见材质、尺寸或销售组成信息；不要重复已有图，不编造护理、功能或认证')
            own.append({'page_key':key,'product_key':product['key'],
                'type':'效果图','shot_type':role,'purpose':purpose,
                'display_role':'sales_cover' if purpose.startswith('销售封面') else 'story',
                'global_story':own[0].get('global_story',''),
                'creative_direction':copy.deepcopy(own[0].get('creative_direction',{})),
                'style':own[0].get('style',''),'section_index':index+1,
                'section_label':'本规格完整展示','title':'本规格完整展示'})
        if len(own)!=7:raise HTTPException(422,'历史图库超过每规格7张；请先在界面明确选择，不能静默丢弃图片')
        complete=next((copy.deepcopy(page) for page in pages if page['product_key']==product['key'] and page.get('display_role')=='detail_page'),None)
        complete=complete or design_contract.slots(product)[-1]
        output.extend(own+[complete])
    return output


def clone_images(c, rid, source, pages, products, *, correction='', correction_sku_ids=(), correction_sha256=()):
    tasks=[flow.obj(row) for row in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND kind='image'",(source['id'],))]
    by_key={task['task_key']:task for task in tasks}
    for page in pages:
        product=next(product for product in products if product['key']==page['product_key'])
        task=by_key.get(page['page_key'])
        if task and not intact(task,source['project_id']):raise HTTPException(422,'历史成品生成记录或文件不完整，请先核对')
        payload={'product':product,'page':page}
        if not task and product.get('sku_id') in correction_sku_ids:
            page['fact_correction']=correction
        if task and task['result']['sha256'] in correction_sha256:
            page['repair_reference']={'url':task['result']['url'],'sha256':task['result']['sha256'],
                'task_id':task['id'],'run_id':source['id'],'instruction':correction}
            plan=copy.deepcopy(task['payload']['plan'])
            flow.add_task(c,rid,'plan',page['page_key'],payload,plan)
        elif task:
            plan=copy.deepcopy(task['payload']['plan'])
            flow.add_task(c,rid,'plan',page['page_key'],payload,plan)
            result=copy.deepcopy(task['result'])
            result['reused_from']={'task_id':task['id'],'run_id':source['id'],'reason':'保留现有成品，仅补齐缺页与明确冲突'}
            flow.add_task(c,rid,'image',page['page_key'],payload|{'plan':plan},result)
        else:
            flow.add_task(c,rid,'plan',page['page_key'],payload)


def start(listing_id, revision, *, correction='', correction_sku_ids=(), correction_sha256=(), actor='', local_only=False):
    from app.commerce import store as s, gallery_assembly, listing_release
    flow.ensure_loaded_pipeline()
    with s.transaction() as c:
        listing=s.get(c,'listing',listing_id);target=s.get(c,'target',listing['target_id'])
        if listing['revision']!=revision:raise HTTPException(409,'资料已变化，请刷新')
        if local_only:
            if (not listing.get('paused') or listing.get('status') != 'not_submitted'
                    or listing.get('published_version') is not None or listing.get('published_skus')
                    or listing.get('retirement_id') or not correction.strip() or not correction_sha256):
                raise HTTPException(409,'只修本地图片需选择已暂停、尚未提交商品的具体图片并填写修改要求')
            listing_release.assert_preparation_available(c,listing,target)
        else:
            gallery_assembly._available(c,listing,target)
        product=s.get(c,'product',listing['product_id']);pid=product['project_id']
        if local_only:
            key=prompts.fingerprint([listing_id, revision, sorted(correction_sha256), sorted(correction_sku_ids), correction])
            previous=c.execute("""SELECT id,status FROM design_simple_runs WHERE project_id=? AND mode='presentation_repair'
                AND json_extract(recognition,'$.presentation_repair.local_request')=? ORDER BY id DESC LIMIT 1""",(pid,key)).fetchone()
            if previous:
                return {'run_id':previous['id'],'project_id':pid,'status':previous['status'],'message':'已读取本次图片修正进度'}
        if c.execute("SELECT 1 FROM design_simple_runs WHERE project_id=? AND status IN ('queued','running','retry_wait')",(pid,)).fetchone():
            raise HTTPException(409,'本商品已有设计正在执行，请等待或先在设计工作台暂停')
        flow.assert_request_boundary(c,pid)
        source=flow.obj(c.execute("SELECT * FROM design_simple_runs WHERE id=? AND project_id=? AND status='completed'",
            (listing['draft']['source']['design_run_id'],pid)).fetchone())
        if not source:raise HTTPException(422,'当前资料没有可复用的完整设计来源')
        snap=flow.snapshot(flow.project(pid,c));recognition=prompts.reuse_catalog(source['recognition'],snap)
        products=recognition['products']
        if set(correction_sku_ids)-{p.get('sku_id') for p in products}:raise HTTPException(422,'修正范围必须是本商品真实SKU')
        if correction_sku_ids and not correction.strip():raise HTTPException(422,'请说明有依据的图内事实冲突')
        detail_id=listing['draft'].get('detail_page',{}).get('run_id')
        eligible={}
        for row in c.execute("SELECT * FROM design_simple_tasks WHERE run_id IN (?,?) AND kind='image' AND status='completed'",(source['id'],detail_id)):
            task=flow.obj(row)
            if local_only and task['result'].get('sha256') in eligible and task['result']['sha256'] in correction_sha256:
                raise HTTPException(409,'所选文件对应多个设计页面，请按页面核对后修正')
            eligible[task['result'].get('sha256')]=task['payload']['product'].get('sku_id')
        if correction.strip() and not correction_sha256:raise HTTPException(422,'请逐张选择确实存在事实冲突的图片')
        if any(eligible.get(digest) not in correction_sku_ids for digest in correction_sha256):
            raise HTTPException(422,'修正图片必须属于本次原成品及所选真实SKU')
        if correction_sku_ids and set(correction_sku_ids)!={eligible.get(digest) for digest in correction_sha256}:
            raise HTTPException(422,'每个修正规格必须选择明确的冲突图片')
        # Source facts must match. A new implementation version alone does not
        # authorize replacing a product or rewriting old generation evidence.
        for p in products:
            old=next((old for old in source['recognition']['products'] if old['key']==p['key']),None)
            if old!=p:raise HTTPException(409,'本规格原图或参数已改变，请先完成档案纠正设计')
        if design_contract.active(source['snapshot']) and flow.same_design_inputs(source['snapshot'],snap):
            source_masters=[flow.obj(row) for row in c.execute(
                "SELECT * FROM design_simple_tasks WHERE run_id=? AND kind='master' AND status='completed'",(source['id'],))]
            masters={p['key']:next((copy.deepcopy(task['result']) for task in source_masters
                if task['task_key']==p['key'] and task['payload'].get('product')==p and intact(task,pid)),None) for p in products}
        else:
            masters={p['key']:flow.matching_master(c,pid,p,snap) for p in products}
        if not all(masters.values()):raise HTTPException(409,'原图、参数或母稿提示词已变化，不能复用旧母稿')
        pages=copy.deepcopy(source['pages']) if design_contract.active(source['snapshot']) else prepare_pages(products,source['pages'])
        digest=prompts.fingerprint(snap)
        snap['_model_candidates']={'image':flow.transport.candidates(flow.main(),True),
                                   'text':flow.transport.candidates(flow.main(),False)}
        # Record the repair contract outside the input snapshot/hash.
        recognition['presentation_repair']={'source_run_id':source['id'],
            'detail_run_id':detail_id,
            'correction':correction,'correction_sku_ids':list(correction_sku_ids),
            'correction_sha256':list(correction_sha256),
            'listing_id':listing_id,'material_version':listing['draft_version'],'actor':actor}
        if local_only:
            recognition['presentation_repair'].update(local_request=key,listing_revision=revision)
        rid=c.execute("""INSERT INTO design_simple_runs(project_id,mode,input_hash,snapshot,note,stage,recognition,pages,expected_count,workflow)
            VALUES(?,?,?,?,?,'plans',?,?,?,?)""",(pid,'presentation_repair',digest,prompts.dump(snap),
            '保留现有好图；补齐每规格7张图库及完整连续详情。'+correction,
            prompts.dump(recognition),prompts.dump(pages),len(pages),flow.WORKFLOW)).lastrowid
        for p in products:flow.add_task(c,rid,'master',p['key'],{'product':p},masters[p['key']])
        clone_images(c,rid,source,pages,products,correction=correction,correction_sku_ids=correction_sku_ids,correction_sha256=correction_sha256)
        # clone_images adds exact reference hashes to only the newly corrected pages.
        c.execute('UPDATE design_simple_runs SET pages=? WHERE id=?',(prompts.dump(pages),rid))
        s.audit(c,'presentation_repair_started',listing_id,s.dump({'run_id':rid,'source_run_id':source['id'],'actor':actor}))
    if local_only:
        return {'run_id':rid,'project_id':pid,'status':'queued','message':'正在修正所选图片，完成后更新本地上架资料'}
    from app.commerce import listing_workflow
    listing_workflow.request_batch(pid,'presentation-repair:'+str(rid))
    return {'run_id':rid,'project_id':pid,'status':'queued','message':'现有成品保留；程序将补齐缺页并修正明确的图内事实冲突'}



