"""Durable selected-project workflow through Amazon listing submission."""
from __future__ import annotations

import time
import uuid

from fastapi import HTTPException

from . import store as s
from app.design_input_compatibility import completed_input_matches as _completed_design_matches


ACTIVE = {'running', 'waiting'}
# Bound read-only propagation waits; never repeat listing writes on a timer.
READBACK_WINDOW = 48 * 3600


def detail(c,workflow):
    """Expose the current saved copy in workflow details, without generating it."""
    from . import listing_copy
    result=dict(workflow)
    result.pop('listing_content',None)
    listing=s.get(c,'listing',workflow.get('listing_id'),required=False)
    if not listing or listing['target_id']!=workflow['target_id']:return result
    product=s.get(c,'product',listing['product_id'])
    if product.get('project_id')!=workflow['project_id']:return result
    draft=listing.get('draft') or {}
    content=draft.get('content')
    if not content:return result
    result['listing_content']={'listing_id':listing['id'],'version':listing['draft_version'],
        'language':draft.get('language'),
        'content':{key:content.get(key,[] if key=='bullets' else '') for key in listing_copy.CONTENT_FIELDS},
        'sku_titles':[{'seller_sku':row['seller_sku'],'title':row.get('title',content.get('title',''))}
                      for row in draft.get('skus',[])]}
    return result


def request(project_id: int, target_id: int, request_id: str):
    from .targets import selected_targets
    if not 8 <= len(request_id) <= 100:
        raise HTTPException(422, '缺少本次上架编号')
    with s.transaction() as c:
        if not c.execute('SELECT 1 FROM design_projects WHERE id=?', (project_id,)).fetchone():
            raise HTTPException(404, '设计项目不存在')
        target = selected_targets(c, [target_id])[0]
        return _request(c, project_id, target, request_id)


def request_batch(project_id: int, request_id: str):
    """Use configured destinations automatically, with a durable workflow per site."""
    from hashlib import sha256
    from . import environment
    if not 8 <= len(request_id) <= 100:
        raise HTTPException(422, '缺少本次上架编号')
    with s.transaction() as c:
        if not c.execute('SELECT 1 FROM design_projects WHERE id=?', (project_id,)).fetchone():
            raise HTTPException(404, '设计项目不存在')
        previous = s.by_key(c, 'listing_workflow_batch', request_id)
        if previous:
            if previous['project_id'] != project_id:
                raise HTTPException(409, '本次上架编号已用于其他商品')
            return {**previous, 'workflows': [s.get(c, 'listing_workflow', wid)
                                              for wid in previous['workflow_ids']]}
        accounts = {row[0] for row in c.execute(
            "SELECT id FROM platform_accounts WHERE enabled=1 AND platform='亚马逊'")}
        targets = [t for t in s.all_rows(c, 'target') if t.get('enabled')
                   and t['account_id'] in accounts and environment.allowed(t['environment'])]
        if not targets:
            raise HTTPException(422, '请先在平台同步中配置并启用店铺与站点')
        ids = sorted(t['id'] for t in targets)
        workflows = [_request(c, project_id, target,
                             'batch:' + sha256(f'{request_id}:{target["id"]}'.encode()).hexdigest(),
                             require_live_writes=False)
                     for target in targets]
        batch = s.put(c, 'listing_workflow_batch', request_id,
                      {'project_id': project_id, 'target_ids': ids,
                       'workflow_ids': [row['id'] for row in workflows]})
        return {**batch, 'workflows': workflows}


def _request(c, project_id, target, request_id, *, require_live_writes=True):
    from . import execution_controls
    target_id = target['id']
    if require_live_writes and target.get('environment') == 'production' and not (s.by_key(c, 'settings', 'global') or {}).get('live_writes_enabled'):
        raise HTTPException(422, '正式写入尚未启用，请先完成店铺配置')
    previous = s.by_key(c, 'listing_workflow', request_id)
    if previous:
        if (previous['project_id'], previous['target_id']) != (project_id, target_id):
            raise HTTPException(409, '本次上架编号已用于其他项目或店铺')
        return previous
    product = s.by_key(c, 'product', f'design:{project_id}')
    listing = s.by_key(c, 'listing', f'{product["id"]}:{target_id}') if product else None
    if listing:
        if listing['product_id'] != product['id'] or listing['target_id'] != target_id:
            raise HTTPException(409, '商品记录与当前项目或店铺不一致，请先核对原记录')
        from . import listing_release
        listing_release.begin_first_publication(c, listing)
    for row in s.all_rows(c, 'listing_workflow'):
        if row['project_id'] == project_id and row['target_id'] == target_id and row['status'] in ACTIVE:
            if not execution_controls.manual_current(c,row):
                row=s.save(c,'listing_workflow',row|{'manual_pause_epoch':execution_controls.pause_epoch(c)})
                s.enqueue(c,'listing_workflow',f'listing-workflow:{row["id"]}:continue:{request_id}',
                          {'workflow_id':row['id'],'target_id':target_id})
            return row
    workflow = s.put(c, 'listing_workflow', request_id,
                     {'project_id': project_id, 'target_id': target_id,
                      'environment': target['environment'], 'status': 'running',
                      'manual_pause_epoch':execution_controls.pause_epoch(c),
                      'stage': 'design', 'message': '等待设计与来源核对',
                      'started_at': time.time(), 'jobs': {}})
    s.enqueue(c, 'listing_workflow', f'listing-workflow:{workflow["id"]}:start',
              {'workflow_id': workflow['id'], 'target_id': target_id})
    s.audit(c, 'selected_listing_workflow_requested', workflow['id'],
            s.dump({'project_id': project_id, 'target_id': target_id}))
    return workflow


def schedule(c):
    from . import workflow_wakeup
    workflow_wakeup.schedule(c)

def _save(workflow, **changes):
    with s.transaction() as c:
        current = s.get(c, 'listing_workflow', workflow['id'])
        if all(current.get(key) == value for key, value in changes.items()):
            return current
        current.update(changes)
        return s.save(c, 'listing_workflow', current)


def _job(ident):
    with s.main().db() as c:
        return s.job(c, ident) if ident else None


def _depend(workflow, name, kind, payload):
    job = _job(workflow.get('jobs', {}).get(name))
    if not job:
        with s.transaction() as c:
            if payload.get('listing_id'):
                from . import listing_release
                listing = s.get(c, 'listing', payload['listing_id'])
                if listing['target_id'] != workflow['target_id']:
                    raise HTTPException(409, '依赖任务的商品与本次店铺不一致')
                payload = listing_release.bind_payload(listing, payload)
            job = s.enqueue(c, kind, f'listing-flow:{workflow["id"]}:{name}:{uuid.uuid4().hex}',
                            payload | {'workflow_id': workflow['id']})
            current = s.get(c, 'listing_workflow', workflow['id'])
            current.setdefault('jobs', {})[name] = job['id']
            s.save(c, 'listing_workflow', current)
        return None
    if job['status'] == 'completed':
        return job
    if job['status'] in {'paused', 'unknown', 'cancelled'}:
        raise HTTPException(409, f'{name}：{job["error"] or job["status"]}')
    return None


def _fields_ready(workflow, listing):
    from . import attribute_completion
    with s.transaction() as c:
        current_listing = s.get(c, 'listing', listing['id'])
        fields_job = attribute_completion.ensure(c, current_listing)
        if fields_job:
            current = s.get(c, 'listing_workflow', workflow['id'])
            from . import execution_controls, jobs
            if fields_job['status'] in {'queued', 'running', 'retry_wait'} and execution_controls.manual_current(c, current):
                fields_job = jobs.authorize_preparation(c, fields_job)
            if current.get('jobs', {}).get('official_fields') != fields_job['id']:
                current.setdefault('jobs', {})['official_fields'] = fields_job['id']
                s.save(c, 'listing_workflow', current)
            workflow.setdefault('jobs', {})['official_fields'] = fields_job['id']
    if not fields_job:
        return True
    if fields_job['status'] in {'paused', 'unknown', 'cancelled'}:
        raise HTTPException(409, fields_job.get('error') or '官方字段任务需要核对原结果，请保留已有资料')
    return fields_job['status'] == 'completed'


def _repair_preflight(workflow, listing, job):
    """Continue sourced field preparation before any goods have been accepted."""
    from . import listing_issues, listing_identity, listing_release
    if (not job or job['status'] != 'paused' or job['kind'] != 'submit'
            or job['payload'].get('preview') or job['payload'].get('version') != listing['draft_version']
            or listing.get('paused') or listing.get('published_version') is not None or listing.get('published_skus')):
        return None
    proof = listing.get('publication_snapshot') or {}
    if proof.get('job_id') != job['id'] or listing.get('preflight_job_id') != job['id']:
        return None
    fields = listing_issues.preflight_missing(listing)
    if not fields:
        return None
    with s.main().db() as c:
        target = s.get(c, 'target', workflow['target_id'])
        if (listing['target_id'] != target['id'] or proof.get('target_scope') != listing_identity.target_scope(target)
                or int(job['payload'].get('publication_cycle_id') or 0) != listing_release.cycle(listing)):
            return None
        unsettled = c.execute("""SELECT 1 FROM commerce_records WHERE kind='submission'
            AND json_extract(data,'$.listing_id')=? AND json_extract(data,'$.version')=?
            AND COALESCE(json_extract(data,'$.preview'),0)=0
            AND json_extract(data,'$.status') IN ('accepted','sending','unknown') LIMIT 1""",
            (listing['id'], listing['draft_version'])).fetchone()
        if unsettled:
            return None
    history = list(workflow.get('preflight_repairs') or [])
    signature = listing_release.fingerprint(fields)
    source = (listing.get('draft') or {}).get('source') or {}
    scope = listing_release.fingerprint({'target': proof['target_scope'], 'cycle': listing_release.cycle(listing),
        'product_type': listing['draft'].get('product_type'),
        'archive_digest': source.get('archive_digest'), 'archive_signature': source.get('archive_signature')})
    current_history = [row for row in history if row.get('scope_sha256') == scope]
    if len(current_history) >= 3 or any(row.get('fields_sha256') == signature for row in current_history):
        return None
    history.append({'job_id': job['id'], 'version': listing['draft_version'], 'fields': fields,
                    'fields_sha256': signature, 'scope_sha256': scope, 'checked_at': time.time()})
    return _save(workflow, stage='official_fields', status='running', preflight_repairs=history,
                 jobs={key: ident for key, ident in workflow.get('jobs', {}).items() if key != 'submit'},
                 message='平台指出的资料缺项正在按本商品来源自动补齐，完成后继续同一上架流程')


def _ensure_seller_skus(listing, target):
    """Resolve unowned remote collisions before any listing write."""
    from . import amazon, catalog
    for entry in (listing.get('draft') or {}).get('skus', []):
        key=f"{target['account_id']}:{target['marketplace_id']}:{entry['sku_id']}"
        with s.transaction() as c:
            identity=s.by_key(c,'seller_sku_identity',key)
            if not identity:
                identity=s.put(c,'seller_sku_identity',key,
                               {'account_id':target['account_id'],
                                'marketplace_id':target['marketplace_id'],
                                'target_id':target['id'],'sku_id':entry['sku_id'],
                                'seller_sku':entry['seller_sku'],
                                'checked':entry['seller_sku'] in (listing.get('published_skus') or {})})
        if identity.get('checked'):
            continue
        candidate=identity['seller_sku']
        for serial in range(1, 10):
            with s.main().db() as c:
                local_taken=any(row['id']!=identity['id'] and
                                row.get('account_id')==target['account_id'] and
                                row.get('marketplace_id')==target['marketplace_id'] and
                                row.get('seller_sku')==candidate
                                for row in s.all_rows(c,'seller_sku_identity'))
            occupied=local_taken
            if not occupied:
                try:
                    amazon.get_listing(target,candidate)
                    occupied=True
                except amazon.PlatformError as exc:
                    if exc.status != 404:
                        raise
            if not occupied:
                if candidate != entry['seller_sku']:
                    listing=catalog.assign_seller_sku(listing['id'],entry['sku_id'],candidate)
                else:
                    with s.transaction() as c:
                        identity=s.by_key(c,'seller_sku_identity',key)
                        identity['checked']=True
                        s.save(c,'seller_sku_identity',identity)
                break
            if entry['seller_sku'] in (listing.get('published_skus') or {}):
                raise HTTPException(409,'已发布 Seller SKU 出现平台身份冲突，停止自动改号')
            candidate=(entry['seller_sku'][:34]+f'-{serial+1}')
        else:
            raise HTTPException(409,entry['seller_sku']+'：连续 Seller SKU 冲突，请核对店铺已有刊登')
    return listing


def _verified_catalog_item(sku, draft, target, item, *, identifier=''):
    """Accept a catalog identity only with marketplace, brand and variant evidence."""
    label = sku['seller_sku']
    if not isinstance(item, dict):
        raise HTTPException(422, label+'：目录结果格式错误')
    asin = str(item.get('asin') or '')
    if len(asin) != 10 or not asin.isalnum():
        raise HTTPException(422, label+'：目录未返回有效 ASIN')
    identifiers = {str(v.get('identifier') or '') for group in item.get('identifiers', [])
                   if isinstance(group, dict) for v in group.get('identifiers', [])
                   if isinstance(v, dict)}
    if identifier and identifier not in identifiers:
        raise HTTPException(422, label+'：目录结果未证实该条码归属')
    summaries = [x for x in item.get('summaries', [])
                 if isinstance(x, dict) and x.get('marketplaceId') == target['marketplace_id']]
    brands = {str(x.get('brandName') or '').strip().casefold() for x in summaries
              if x.get('brandName')}
    if not brands or brands != {str(draft.get('brand') or '').strip().casefold()}:
        raise HTTPException(422, label+'：目录品牌与原表品牌不一致，请核对商品归属')
    attrs = item.get('attributes') or {}
    for field, local in [('color', sku.get('color')), ('size', sku.get('size'))]:
        observed = {str(v.get('value') or '').strip().casefold()
                    for v in attrs.get(field, []) if isinstance(v, dict) and v.get('value')}
        observed |= {str(x.get(field) or '').strip().casefold() for x in summaries if x.get(field)}
        if observed and local and local.strip().casefold() not in observed:
            raise HTTPException(422, label+'：目录'+field+'与该规格不一致，请核对原表')
        if field == 'color' and sku.get('asin') and not identifier and local and not observed:
            titles = [str(x.get('itemName') or '').casefold() for x in summaries]
            if not any(local.strip().casefold() in title for title in titles):
                raise HTTPException(422, label+'：现有 ASIN 缺少可核实的颜色对应关系')
    return asin


def _advance(workflow):
    from app import simple_design_flow, simple_design_prompts
    from . import catalog
    pid, tid = workflow['project_id'], workflow['target_id']
    if workflow['stage'] == 'design':
        with s.main().db() as c:
            project = c.execute('SELECT * FROM design_projects WHERE id=?', (pid,)).fetchone()
            current = (c.execute('SELECT id,status,input_hash,snapshot,workflow FROM design_simple_runs WHERE id=?',
                                  (project['simple_current_run'],)).fetchone()
                       if project and project['simple_current_run'] else None)
        design_snapshot = simple_design_flow.snapshot(dict(project))
        expected = simple_design_prompts.fingerprint(design_snapshot)
        compatible = bool(current and current['status'] == 'completed' and
                          current['workflow'] == simple_design_flow.WORKFLOW and
                          (current['input_hash'] == expected or
                           simple_design_flow.compatible_completed_input(current['snapshot'], design_snapshot) or
                           _completed_design_matches(current['snapshot'], design_snapshot)))
        # Multiple destinations share the product's current design work.
        # Starting another site's workflow must not supersede that same run.
        # Preserved older pauses are history once a newer compatible design completes.
        # A newer active/paused run still takes precedence over an older completion.
        with s.main().db() as c:
            candidates = c.execute("""SELECT id,status,error,input_hash,snapshot FROM design_simple_runs
                WHERE project_id=? AND workflow=?
                AND mode NOT IN ('detail','detail_redesign','detail_single')
                AND status IN ('queued','running','retry_wait','paused') AND id>?
                ORDER BY id DESC""", (pid, simple_design_flow.WORKFLOW,
                    current['id'] if compatible else 0))
            shared = next((row for row in candidates if row['input_hash'] == expected or
                           simple_design_flow.same_design_inputs(row['snapshot'], design_snapshot)), None)
        if shared:
            if shared['status'] == 'paused':
                from app import design_contract
                if design_contract.active(design_snapshot):
                    raise HTTPException(409,'设计任务已暂停，请保留已完成结果并从工作台处理：'+shared['error'])
                from app import presentation_gap_recovery
                recovery = presentation_gap_recovery.preview(pid, shared['id'])
                if recovery.get('available'):
                    replacement = presentation_gap_recovery.start(pid, shared['id'],
                        recovery['detail_run_id'], recovery['task_ids'], True,
                        actor='automatic_listing', authorization='configured_backup_after_unusable_return')
                    return _save(workflow, design_run_id=replacement['run_id'], status='waiting',
                        message='主图片模型未返回可用成品，已保留原请求并切换备用模型继续缺图')
                raise HTTPException(409, '设计任务需要处理：' + shared['error'])
            return _save(workflow, design_run_id=shared['id'], status='waiting', message='正在生成设计图')
        if compatible:
            return _save(workflow, design_run_id=current['id'], stage='materials', status='running', message='设计完成，正在生成商品资料')
        if workflow.get('design_run_id'):
            with s.main().db() as c:
                run = c.execute('SELECT status,error,input_hash,snapshot FROM design_simple_runs WHERE id=?',
                                (workflow['design_run_id'],)).fetchone()
            code_transition=bool(run and run['status']!='archived' and run['input_hash']!=expected and
                                 _completed_design_matches(run['snapshot'],design_snapshot) and
                                 '设计源码' in run['error'])
            if code_transition:
                state=simple_design_flow.start(pid,simple_design_flow.StartRequest(
                    action='redesign',reuse_matching_masters=True))
                return _save(workflow,design_run_id=(state.get('run') or {}).get('id'),
                             status='waiting',message='版本修复后继续设计，保留并复用已生成且输入一致的母稿')
            if run and run['status']=='archived':
                raise HTTPException(409,'旧设计已归档，请先在设计工作台开始新版设计')
            if not run or run['status'] in {'paused', 'superseded'}:
                raise HTTPException(409, '设计任务需要处理：' + (run['error'] if run else '设计任务不存在'))
            if run['input_hash'] != expected:
                state = simple_design_flow.start(pid, simple_design_flow.StartRequest(action='redesign'))
                return _save(workflow, design_run_id=(state.get('run') or {}).get('id'),
                             status='waiting', message='商品来源更新，正在按新事实重新设计')
            return _save(workflow, status='waiting', message='正在生成设计图')
        action='redesign' if current and current['input_hash'] != expected else 'design'
        state = simple_design_flow.start(pid, simple_design_flow.StartRequest(action=action))
        rid = (state.get('run') or {}).get('id')
        return _save(workflow, design_run_id=rid, status='waiting', message='正在生成设计图')

    if workflow['stage'] == 'materials':
        product = catalog.import_project(pid)
        with s.main().db() as c:target=s.get(c,'target',tid)
        if target['environment']=='production':
            from . import import_classification
            import_classification.ensure(product['id'],tid)
        listing = catalog.listing_for(product['id'], tid)
        materials_job = _job(workflow.get('jobs', {}).get('materials'))
        if materials_job and materials_job['status'] in {'paused', 'unknown', 'cancelled'}:
            raise HTTPException(409, materials_job.get('error') or '商品资料任务需要核对原结果')
        if materials_job and materials_job['status'] != 'completed':
            return _save(workflow, listing_id=listing['id'], status='waiting', message='正在生成商品资料')
        if not materials_job and listing.get('draft'):
            from . import product_archive, listing_identity
            source=listing['draft'].get('source') or {}
            selected=source.get('selected_sku_ids') or [v['sku_id'] for v in listing['draft'].get('skus',[])]
            with s.main().db() as c:
                changed_physical=bool(source.get('archive_digest') and
                    source['archive_digest']!=product_archive.fact_digest(c,product,selected))
                try:
                    listing_identity.assert_source_current(c, listing, target, listing['draft'])
                    source_current = True
                except HTTPException:
                    source_current = False
            # A newly completed design needs new materials when physical facts
            # changed. The narrow classification refresh intentionally refuses
            # that case; keep the old draft as history and generate a new one.
            # Published revisions are rebuilt by the versioned materials job.
            # The narrow refresh is intentionally limited to unsubmitted drafts.
            if (not source_current and not changed_physical and listing.get('published_version') is None
                    and listing.get('status') == 'not_submitted'):
                with s.transaction() as c:
                    pending = c.execute("""SELECT id FROM commerce_jobs
                        WHERE kind IN ('materials','listing_fields') AND json_extract(payload,'$.listing_id')=?
                        AND status IN ('queued','running','retry_wait','unknown') ORDER BY id LIMIT 1""", (listing['id'],)).fetchone()
                    dependency = s.job(c, pending['id']) if pending else None
                    if dependency:
                        current = s.get(c, 'listing_workflow', workflow['id'])
                        current.setdefault('jobs', {})['materials_prepare'] = dependency['id']
                        s.save(c, 'listing_workflow', current)
                if dependency:
                    if dependency['status'] == 'unknown':
                        raise HTTPException(409, '原资料任务结果不明，请先核对原结果，不重复生成')
                    return _save(workflow, listing_id=listing['id'], status='waiting', message='正在等待已有资料任务完成后核对最新来源')
                catalog.refresh_archive_fields(listing['id'],refresh_offers=True)
        with s.main().db() as c:
            current = s.get(c, 'listing', listing['id'])
            run_id = c.execute('SELECT simple_current_run FROM design_projects WHERE id=?', (pid,)).fetchone()[0]
            source = (current.get('draft') or {}).get('source') or {}
            actual_skus = {row['id']: row['revision'] for row in s.all_rows(c, 'sku')
                           if row['product_id'] == product['id'] and row.get('active', True)}
            source_skus = {row['id']: row['revision'] for row in source.get('skus', [])}
            existing = (source.get('design_run_id') == run_id and
                        source.get('product_revision') == product['revision'] and
                        source_skus == actual_skus)
            if current.get('draft'):
                from . import listing_identity
                try:
                    listing_identity.assert_source_current(c,current,s.get(c,'target',tid),current['draft'])
                    # Facts are the design dependency; a price-only revision
                    # does not invalidate an otherwise current factual snapshot.
                    if source.get('archive_digest') and source.get('design_run_id')==run_id:existing=True
                except HTTPException:
                    existing=False
        if existing:
            return _save(workflow, listing_id=listing['id'], stage='official_fields', status='running',
                         message='商品文案已保存，正在补齐并核对官方字段',
                         jobs={k: v for k, v in workflow.get('jobs', {}).items()
                               if not k.startswith('main_image:')})
        if materials_job:
            raise HTTPException(409, '已生成的资料与当前来源或设计不一致，请核对变化后继续；原结果已保留')
        _depend(workflow, 'materials', 'materials', {'listing_id': listing['id']})
        # Even a dependency that finished concurrently must pass the current
        # source/design comparison on the next turn, never its status alone.
        return _save(workflow, listing_id=listing['id'], status='waiting', message='正在生成商品资料')

    if workflow['stage'] == 'official_fields':
        with s.main().db() as c:
            listing = s.get(c, 'listing', workflow['listing_id'])
            from . import listing_identity
            listing_identity.assert_source_current(c, listing, s.get(c, 'target', tid), listing['draft'])
        if not _fields_ready(workflow, listing):
            return _save(workflow, status='waiting', message='正在按本商品来源补齐官方字段和规格关系')
        catalog.attach_current_detail(listing['id'])
        return _save(workflow, stage='catalog', status='running', message='商品资料已就绪，正在核对 Amazon 目录')

    if workflow['stage'] == 'catalog':
        from . import amazon
        with s.main().db() as c:
            listing = s.get(c, 'listing', workflow['listing_id'])
            target = s.get(c, 'target', tid)
            from . import listing_identity
            listing_identity.assert_source_current(c,listing,target,listing.get('draft') or {})
        if target['environment'] == 'production':
            listing=_ensure_seller_skus(listing,target)
            draft = listing.get('draft') or {}
            proposed = []
            for sku in draft.get('skus', []):
                if sku.get('asin'):
                    response = amazon.get_catalog_item(target, sku['asin'])
                    item = response.get('body') or {}
                    if _verified_catalog_item(sku, draft, target, item,
                                              identifier=sku.get('gtin') or '') != sku['asin']:
                        raise HTTPException(422, sku['seller_sku']+'：原表 ASIN 与目录返回值不一致')
                    proposed.append((sku['sku_id'], sku['asin'], sku.get('gtin') or '',
                                     response.get('request_id', '')))
                    continue
                if not sku.get('gtin'):
                    continue
                response = amazon.search_catalog_identifier(target, sku['gtin'])
                items = response.get('body', {}).get('items')
                if not isinstance(items, list):
                    raise HTTPException(422, sku['seller_sku']+'：目录查询未返回可核实的商品列表')
                if (response.get('body', {}).get('pagination') or {}).get('nextToken'):
                    raise HTTPException(422, sku['seller_sku']+'：条码目录结果超过一页，无法自动确认唯一商品')
                if not items:
                    continue
                if len(items) != 1:
                    raise HTTPException(422, sku['seller_sku']+'：同一条码匹配多个目录商品，请核对原表条码')
                asin = _verified_catalog_item(sku, draft, target, items[0], identifier=sku['gtin'])
                proposed.append((sku['sku_id'], asin, sku['gtin'], response.get('request_id', '')))
            if proposed:
                with s.transaction() as c:
                    listing_identity.apply_catalog_matches(c, listing, target, proposed)
        return _save(workflow, stage='main_images', status='running',
                     message='目录核对完成，正在准备逐规格主图')

    if workflow['stage'] == 'main_images':
        from . import main_images
        listing_id = workflow['listing_id']
        with s.main().db() as c:
            listing = s.get(c, 'listing', listing_id)
            draft = listing.get('draft') or {}
            from . import listing_identity
            listing_identity.assert_source_current(c,listing,s.get(c,'target',tid),draft)
            missing = [v for v in draft.get('skus', []) if not any(
                x.get('role') == 'main' for x in v.get('images', [])) and not listing_identity.offer_only(v)]
        if not missing:
            return _save(workflow, stage='preflight', status='running', message='主图已就绪，正在核对上架资料')
        outcomes = []
        for sku in missing:
            name = f'main_image:{sku["sku_id"]}'
            job = _job(workflow.get('jobs', {}).get(name))
            if not job:
                queued = main_images.enqueue(
                    listing_id, sku['sku_id'],
                    retry_key=f"workflow:{workflow['id']}:{workflow.get('main_image_retry', 0)}")
                with s.transaction() as c:
                    current = s.get(c, 'listing_workflow', workflow['id'])
                    current.setdefault('jobs', {})[name] = queued['id']
                    s.save(c, 'listing_workflow', current)
                continue
            if job['status'] in {'paused', 'unknown'}:
                raise HTTPException(409, f'{sku["seller_sku"]} 主图：{job["error"] or job["status"]}')
            if job['status'] == 'completed':
                outcomes.extend(job.get('result', {}).get('skus', []))
        failed = [v for v in outcomes if v.get('status') == 'missing']
        if failed:
            raise HTTPException(422, '；'.join(v.get('reason', '缺少对应规格真实主图') for v in failed))
        return _save(workflow, status='waiting', message='正在为每个规格选择并检查主图')

    if workflow['stage'] == 'preflight':
        from . import shipping
        with s.main().db() as c:
            listing = s.get(c, 'listing', workflow['listing_id'])
            target = s.get(c, 'target', tid)
            from . import listing_identity
            listing_identity.assert_source_current(c,listing,target,listing.get('draft') or {})
        draft = listing.get('draft') or {}
        if not _fields_ready(workflow, listing):
            return _save(workflow,status='waiting',message='正在按本商品来源自动补齐官方字段和规格关系')
        with s.main().db() as c:
            listing = s.get(c, 'listing', listing['id'])
            draft = listing['draft']
        family_plan=draft.get('source',{}).get('variation_family') or {}
        from . import variation_families
        if (target['environment']=='production' and len(draft.get('skus',[]))>1
                and (family_plan.get('protocol')!=variation_families.PROTOCOL or family_plan.get('status') not in {'ready','independent'})
                and draft.get('source',{}).get('variation_preference')!='manual_independent'):
            listing=variation_families.prepare(listing['id'],listing['revision'],actor='automatic_listing_workflow')
            draft=listing['draft']
        shipping_type = (draft.get('product_type') if any(not listing_identity.offer_only(sku)
                         for sku in draft.get('skus', [])) else 'PRODUCT')
        if target['environment'] == 'production' and not draft.get('shipping_group'):
            selected = shipping.default_value(target, shipping_type)
            if selected:
                shipping.ensure_selected(target, shipping_type, selected)
                import copy
                revised = copy.deepcopy(draft)
                revised['shipping_group'] = selected
                listing = catalog.update_draft(listing['id'],
                                               {'revision': listing['revision'], 'draft': revised})
        issues, deferred = catalog.submission_issues(listing.get('draft') or {}, target, listing)
        if issues:
            raise HTTPException(422, '上架资料缺项：' + '；'.join(issues))
        return _save(workflow, stage='submit', status='running',
                     deferred_issues=deferred,
                     message='资料核对完成，准备平台预检与提交')

    if workflow['stage'] == 'submit':
        with s.main().db() as c:
            listing = s.get(c, 'listing', workflow['listing_id'])
            if listing['environment'] == 'production' and not (s.by_key(c, 'settings', 'global') or {}).get('live_writes_enabled'):
                raise HTTPException(422, '上架资料已准备完成；正式写入尚未启用，请在经营设置中启用后继续')
        repair = _repair_preflight(workflow, listing, _job(workflow.get('jobs', {}).get('submit')))
        if repair:
            return repair
        done = _depend(workflow, 'submit', 'submit',
                       {'listing_id': listing['id'], 'version': listing['draft_version'], 'preview': False})
        if not done:
            return _save(workflow, status='waiting', message='正在执行平台预检和刊登提交')
        return _save(workflow, stage='readback', status='running', message='平台已接收，正在回读刊登状态')

    if workflow['stage'] == 'readback':
        with s.main().db() as c:
            listing = s.get(c, 'listing', workflow['listing_id'])
        if listing['environment'] == 'mock':
            deferred=workflow.get('deferred_issues') or []
            note='；正式上架仍需：'+'；'.join(deferred) if deferred else ''
            return _save(workflow, stage='done', status='sample_only',
                         message='模拟提交完成，未验证正式可售'+note)
        done = _depend(workflow, 'readback', 'refresh', {'listing_id': listing['id']})
        if not done:
            return _save(workflow, status='waiting', message='正在回读亚马逊处理结果')
        with s.main().db() as c:
            listing = s.get(c, 'listing', listing['id'])
        return _readback_progress(workflow, listing) or _detail_progress(workflow, listing)
    if workflow['stage']=='detail':
        with s.main().db() as c:listing=s.get(c,'listing',workflow['listing_id'])
        return _detail_progress(workflow,listing)
    return workflow


def _readback_progress(workflow, listing):
    """Follow existing observations, without repeating any platform writes."""
    from . import listing_issues
    if listing.get('draft_version')!=listing.get('published_version'):
        return _save(workflow,stage='readback',status='needs_action',message='上架资料已有新版，请先完成对应版本商品提交')
    if listing.get('paused'):
        return _save(workflow,stage='readback',status='needs_action',message='商品已暂停，保留当前提交与详情记录')
    expected={sku['seller_sku'] for sku in (listing.get('draft') or {}).get('skus',[])}
    observed=listing.get('platform_observed') or {}
    if not expected or set(observed)-expected:
        return _save(workflow,stage='readback',status='needs_action',message='平台回读的销售规格与本次资料不一致，请核对商品身份')
    errors=[row for row in listing_issues.observed(listing) if row['severity']=='ERROR']
    # Amazon documents 100521 as a temporary review, not a request for edits.
    blocking=[row for row in errors if row['code']!='100521']
    failures={sku for sku,row in observed.items()
              if row.get('status') in {'identity_conflict','search_suppressed'} or
              row.get('status')=='platform_error' and not any(
                  error['sku']==sku and error['code']=='100521' for error in errors)}
    failures.update(row['sku'] for row in blocking)
    if failures:
        codes='、'.join(dict.fromkeys(row['code'] for row in blocking if row['code']))
        return _save(workflow,stage='readback',status='needs_action',
                     message='平台结果需要处理：'+'、'.join(sorted(failures))+
                             ('（'+codes+'）' if codes else '')+'；原提交和详情记录保留')
    if set(observed)==expected and not errors and all(row.get('status')=='buyable' for row in observed.values()):
        return None
    version=listing['published_version']
    started=(workflow.get('readback_started_at') or time.time()) if workflow.get('readback_wait_version')==version else time.time()
    deadline=started+READBACK_WINDOW
    changes={'readback_started_at':started,'readback_deadline':deadline,
             'readback_wait_version':version,
             'readback_states':{sku:(observed.get(sku) or {}).get('status','waiting') for sku in sorted(expected)}}
    if time.time()>=deadline:
        return _save(workflow,**changes,stage='readback',status='needs_action',
                     message='已等待亚马逊处理48小时，仍有规格未可售；请核对平台结果，继续时只回查，不重复上架')
    count=sum(row.get('status')=='buyable' for row in observed.values())
    label='亚马逊审核中' if errors else '等待亚马逊处理或报价生效'
    return _save(workflow,**changes,stage='readback',status='waiting',
                 message=f'{label}：{count}/{len(expected)} 个规格接口可售；状态变化后自动继续详情')


def _detail_progress(workflow,listing):
    from . import detail_publication
    pending=_readback_progress(workflow,listing)
    if pending:return pending
    cleared={'readback_started_at':0,'readback_deadline':0,'readback_states':{}}
    if not (listing.get('draft') or {}).get('detail_page'):
        return _save(workflow,**cleared,stage='done',status='buyable',message='全部规格接口可售；买家页面仍需确认')
    with s.main().db() as c:detail=detail_publication.view(c,listing)
    if detail.get('complete'):
        return _save(workflow,**cleared,stage='done',status='buyable',message='全部规格接口可售，图文详情已发布；买家页面仍需确认')
    if detail.get('needs_action') or detail['status'] in {'ready','missing'}:
        return _save(workflow,**cleared,stage='detail',status='needs_action',message='商品接口可售，图文详情尚未完成：'+detail['label'])
    return _save(workflow,**cleared,stage='detail',status='waiting',message='商品接口可售；'+detail['label'])


def run(job):
    from . import amazon
    with s.main().db() as c:
        workflow = s.get(c, 'listing_workflow', job['payload']['workflow_id'])
        from . import execution_controls
        if execution_controls.workflow_paused(c, workflow):
            return workflow
    if workflow['status'] not in ACTIVE:
        return workflow
    try:
        return _advance(workflow)
    except HTTPException as exc:
        return _save(workflow, status='needs_action', message=str(exc.detail))
    except amazon.PlatformError as exc:
        return _save(workflow, status='needs_action',
                     message='Amazon 接口核对未完成：'+str(exc))
    except Exception as exc:
        from app import error_log
        error_id = error_log.record('listing_workflow_failed', exc,
                                    workflow_id=workflow['id'], stage=workflow['stage'])
        return _save(workflow, status='needs_action',
                     message='上架流程发生内部错误，请查看错误日志 '+error_id)


def resume(ident):
    with s.transaction() as c:
        workflow = s.get(c, 'listing_workflow', ident)
        if workflow['status'] != 'needs_action':
            raise HTTPException(409, '当前流程没有待处理缺项')
        if any((s.job(c,job_id) or {}).get('status')=='unknown'
               for job_id in workflow.get('jobs',{}).values()):
            raise HTTPException(409,'原任务结果尚不明确，请先核对原任务，不重复生成或平台写入')
        from . import execution_controls
        workflow['manual_pause_epoch']=execution_controls.pause_epoch(c)
        listing=s.get(c,'listing',workflow.get('listing_id'),required=False) if workflow.get('listing_id') else None
        if listing:
            product = s.get(c, 'product', listing['product_id'])
            if listing['target_id'] != workflow['target_id'] or product.get('project_id') != workflow['project_id']:
                raise HTTPException(409, '流程与商品或店铺不一致，请核对原流程')
            from . import listing_release
            listing = listing_release.begin_first_publication(c, listing)
        if (listing and not listing.get('draft') and listing.get('status')=='not_submitted'
                and listing.get('published_version') is None):
            # Failed initial materials have no frozen source identity to compare.
            # Recheck design first; compatible completed work is still reused,
            # while corrected references wait for their current design run.
            workflow.setdefault('prior_steps',[]).append({'stage':workflow['stage'],
                'jobs':dict(workflow.get('jobs',{})), 'listing_version':listing['draft_version'],
                'reason':'初次资料未生成，重新核对当前设计输入', 'archived_at':time.time()})
            workflow['jobs']={}
            workflow['stage']='design'
        elif (listing and listing.get('draft') and listing.get('status')=='not_submitted'
                and listing.get('published_version') is None):
            from . import product_archive,listing_identity
            product=s.get(c,'product',listing['product_id']);target=s.get(c,'target',workflow['target_id'])
            source=listing['draft'].get('source') or {}
            selected=source.get('selected_sku_ids') or [v['sku_id'] for v in listing['draft'].get('skus',[])]
            physical_changed=bool(source.get('archive_digest') and
                source['archive_digest']!=product_archive.fact_digest(c,product,selected))
            attributes_changed=bool(source.get('archive_signature') and
                source['archive_signature']!=listing_identity.archive_signature(c,product,target,selected))
            if physical_changed or attributes_changed:
                workflow.setdefault('prior_steps',[]).append({'stage':workflow['stage'],
                    'jobs':dict(workflow.get('jobs',{})),'listing_version':listing['draft_version'],
                    'reason':'商品实物事实已补充' if physical_changed else '当前店铺刊登属性已补充',
                    'archived_at':time.time()})
                workflow['jobs']={}
                workflow['stage']='design' if physical_changed else 'materials'
        workflow['status'] = 'running'
        workflow['message'] = '已重新检查当前步骤'
        # A corrected draft or newly supplied image needs a fresh dependent job.
        if workflow['stage'] == 'main_images':
            for key, job_id in workflow.get('jobs', {}).items():
                if key.startswith('main_image:') and (s.job(c, job_id) or {}).get('status') == 'unknown':
                    raise HTTPException(409, '主图任务结果尚不明确，请先核对原任务')
            workflow['jobs'] = {k: v for k, v in workflow.get('jobs', {}).items()
                                if not k.startswith('main_image:')}
            workflow['main_image_retry'] = workflow.get('main_image_retry', 0) + 1
        elif workflow['stage'] in {'materials', 'submit', 'readback'}:
            workflow.get('jobs', {}).pop(workflow['stage'], None)
            if workflow['stage']=='readback':
                workflow.update(readback_started_at=0,readback_deadline=0)
        elif workflow['stage'] in {'official_fields', 'preflight'} and listing:
            from . import attribute_completion
            fields = attribute_completion.continue_job(c, listing)
            if fields:
                workflow.setdefault('jobs', {})['official_fields'] = fields['id']
        elif workflow['stage']=='detail' and listing:
            from . import detail_publication
            detail_publication.action(c,listing,'retry')
        saved = s.save(c, 'listing_workflow', workflow)
        s.enqueue(c, 'listing_workflow', f'listing-workflow:{ident}:resume:{uuid.uuid4().hex}',
                  {'workflow_id': ident, 'target_id': workflow['target_id']})
        return saved
