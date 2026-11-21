"""One-click material replacement for all enabled account/region bindings."""
import hashlib
import uuid
from fastapi import HTTPException
from . import environment, store as s,locales

def target_signature(target):
    return {**locales.settings(target),'currency':target.get('currency'),'region':target.get('region'),'enabled':target.get('enabled')}


def progress(c, pid):
    """Read only: report copy plus its field handoff, never resume a job."""
    from . import attribute_completion, archive_fields
    row = c.execute("SELECT * FROM commerce_records WHERE kind='material_batch' AND COALESCE(json_extract(data,'$.status'),'')!='archived' "
                    "AND json_extract(data,'$.project_id')=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if not row:return None
    batch = s.decode(row)
    rows = []
    active = {'queued', 'running', 'retry_wait'}
    for jid in batch['job_ids']:
        job = s.job(c, jid)
        listing = s.get(c, 'listing', job['payload']['listing_id'])
        target = s.get(c, 'target', listing['target_id'])
        proof = (listing.get('draft') or {}).get('source', {}).get('attribute_completion') or {}
        field_row = c.execute("SELECT id FROM commerce_jobs WHERE kind='listing_fields' "
                             "AND json_extract(payload,'$.listing_id')=? ORDER BY id DESC LIMIT 1",
                             (listing['id'],)).fetchone()
        fields = s.job(c, field_row[0]) if field_row else None
        unknown_row = c.execute("SELECT id FROM commerce_jobs WHERE kind='listing_fields' AND status='unknown' "
                               "AND json_extract(payload,'$.listing_id')=? ORDER BY id DESC LIMIT 1",
                               (listing['id'],)).fetchone()
        if fields and (fields['id'] != proof.get('job_id') and
                       fields['payload'].get('version') != listing.get('draft_version')):
            fields = None
        error = job.get('error', '')
        status = job['status']
        can_continue = False
        if status in active:status = 'generating'
        elif status == 'completed':
            if fields and fields['status'] in active:
                status = 'filling'
            elif fields and fields['status'] == 'unknown':
                status = 'unknown';error = fields.get('error', '')
            elif (target['environment'] != 'production' or
                  (proof.get('status') == 'ready' and
                   proof.get('signature') == attribute_completion.signature(listing.get('draft') or {}))):
                status = 'ready'
            elif unknown_row:
                status = 'unknown';error = s.job(c, unknown_row[0]).get('error') or '原字段请求结果尚未确认'
            else:
                status = 'needs_fields'
                missing = proof.get('remaining') or {}
                error = '；'.join(sku+'：'+'、'.join(archive_fields.LABELS.get(name, name) for name in names)
                                 for sku, names in missing.items())
                error = error or (fields or {}).get('error', '') or '官方字段尚未完成'
                # A changed generic completion policy can be applied once by
                # explicit UI continuation, keeping old copy/art and receipts.
                can_continue = (proof.get('policy') != attribute_completion.POLICY and
                                not (fields and fields['status'] in {'failed', 'unknown'}))
        rows.append({'listing_id': listing['id'], 'revision': listing['revision'],
                     'draft_version': listing.get('draft_version', 0), 'target_id': target['id'],
                     'target_name': target.get('name', ''), 'status': status, 'error': error,
                     'can_continue': can_continue, 'material_job_id': job['id'],
                     'field_job_id': fields['id'] if fields else None})
    return {'batch_id': batch['id'], 'rows': rows,
            'active': any(row['status'] in {'generating', 'filling'} for row in rows),
            'ready': bool(rows) and all(row['status'] == 'ready' for row in rows)}


def plan(c,pid):
    project=c.execute('SELECT id,simple_current_run,updated_at FROM design_projects WHERE id=?',(pid,)).fetchone()
    if not project:raise HTTPException(404,'设计项目不存在')
    accounts={a['id']:dict(a) for a in c.execute('SELECT id,platform,enabled FROM platform_accounts WHERE enabled=1')}
    chosen=sorted([t for t in s.all_rows(c,'target') if t.get('enabled') and environment.allowed(t['environment']) and t['account_id'] in accounts],key=lambda t:t['id'])
    if not chosen:raise HTTPException(422,'请先在平台同步中配置并启用账号与地区')
    from . import product_archive
    product=product_archive.for_project(c,pid)
    listings=[r for r in s.all_rows(c,'listing') if product and r['product_id']==product['id'] and r['target_id'] in {t['id'] for t in chosen}]
    signatures=[{**target_signature(t),'platform':accounts[t['account_id']]['platform']} for t in chosen]
    fingerprint=hashlib.sha256(s.dump({'project':dict(project),'targets':signatures,'listings':sorted((r['id'],r['revision']) for r in listings)}).encode()).hexdigest()
    active=[]
    for listing in listings:
        active.extend(s.job(c,row[0]) for row in c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('materials','listing_fields') AND json_extract(payload,'$.listing_id')=? AND status IN ('queued','running','retry_wait')",(listing['id'],)))
    return {'fingerprint':fingerprint,'has_existing':any(r.get('draft') for r in listings),'existing_count':sum(bool(r.get('draft')) for r in listings),
        'target_count':len(chosen),'targets':chosen,'active_jobs':active,'progress':progress(c,pid)}


def preview(pid):
    with s.main().db() as c:return plan(c,pid)


def validate(plan,payload):
    if plan['active_jobs']:raise HTTPException(409,'该商品已有资料生成任务正在运行，请等待完成')
    if payload.get('fingerprint')!=plan['fingerprint']:raise HTTPException(409,'账号地区、设计或原资料已变化，请重新点击生成并确认')
    if plan['has_existing'] and payload.get('overwrite') is not True:raise HTTPException(409,'已有生产资料，请先确认是否覆盖原来数据')


def request(pid,payload):
    from . import catalog
    if 'target_ids' in payload:raise HTTPException(422,'生产资料统一按全部启用账号地区生成，不再选择地区')
    request_id=str(payload.get('request_id',''))
    if not 8<=len(request_id)<=100:raise HTTPException(422,'缺少本次生成编号')
    if type(payload.get('overwrite',False)) is not bool:raise HTTPException(422,'覆盖选项必须为是或否')
    intent={'project_id':pid,'fingerprint':payload.get('fingerprint'),'overwrite':payload.get('overwrite',False)}
    with s.main().db() as c:
        previous=s.by_key(c,'material_batch',request_id)
        if previous:
            if previous.get('status')=='archived':raise HTTPException(409,'原资料批次已归档，请从原始商品重新开始')
            if previous['intent']!=intent:raise HTTPException(409,'该生成编号已用于另一批资料')
            return {**previous,'jobs':[s.job(c,jid) for jid in previous['job_ids']]}
        validate(plan(c,pid),payload)
    # Import/default refresh does not change drafts. Revalidate the authorized
    # scope before queuing the entire batch in a single transaction.
    product=catalog.archive_for_project(pid)
    with s.transaction() as c:
        previous=s.by_key(c,'material_batch',request_id)
        if previous:
            if previous['intent']!=intent:raise HTTPException(409,'该生成编号已用于另一批资料')
            return {**previous,'jobs':[s.job(c,jid) for jid in previous['job_ids']]}
        current=plan(c,pid);validate(current,payload)
        jobs=[]
        for target in current['targets']:
            key=f"{product['id']}:{target['id']}";listing=s.by_key(c,'listing',key)
            if not listing:
                listing=s.put(c,'listing',key,{'product_id':product['id'],'target_id':target['id'],'name':product['name'],
                    'environment':target['environment'],'status':'not_submitted','draft':{},'draft_version':0,'published_version':None,
                    'issues':[],'sku_results':{},'paused':False,'last_error':''})
            jobs.append(s.enqueue(c,'materials','materials:'+uuid.uuid4().hex,{'listing_id':listing['id'],'replace_existing':True,
                'expected_revision':listing['revision'],'expected_target':target_signature(target),'batch_request_id':request_id}))
        batch=s.put(c,'material_batch',request_id,{'intent':intent,'project_id':pid,'product_id':product['id'],
            'target_count':len(jobs),'target_ids':[t['id'] for t in current['targets']],'job_ids':[j['id'] for j in jobs]})
        s.audit(c,'materials_replace_requested',product['id'],s.dump({'request_id':request_id,'overwrite':intent['overwrite'],'targets':batch['target_ids']}))
        return {**batch,'jobs':jobs}


def cleanup(c,listing):
    """Historical materials and translations are business records, including drafts."""
    s.audit(c,'material_history_retained',listing['id'],str(listing['draft_version']))
    return 0
