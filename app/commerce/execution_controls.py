"""Merchant-controlled automatic advancement, separate from platform write access."""
from . import store as s
from fastapi import HTTPException


DEPENDENT_KINDS = {'materials', 'listing_fields', 'main_image'}


def paused(c, target):
    settings = s.by_key(c, 'settings', 'global') or {}
    return target.get('environment') == 'production' and settings.get('listing_automation_paused', True)


def workflow_paused(c, workflow):
    target = s.get(c, 'target', workflow.get('target_id'), required=False)
    return target is None or (paused(c,target) and not manual_current(c,workflow))


def pause_epoch(c):
    return int((s.by_key(c,'settings','global') or {}).get('listing_pause_epoch',0))


def manual_current(c, payload):
    epoch=payload.get('manual_pause_epoch')
    return type(epoch) is int and epoch==pause_epoch(c)


def assert_manual_current(c, payload):
    if 'manual_pause_epoch' in payload and not manual_current(c,payload):
        raise HTTPException(409,'本次上架已被后续暂停操作停止，请从商品页面明确继续')


def assert_submission_allowed(c,payload):
    assert_manual_current(c,payload)
    if manual_current(c,payload):return
    if payload.get('workflow_id'):
        workflow=s.get(c,'listing_workflow',payload['workflow_id'],required=False)
        if workflow is None or workflow_paused(c,workflow):
            raise HTTPException(409,'本款上架流程已暂停，原商品和已完成进度保留')


def submission_epoch(c,payload):
    if 'manual_pause_epoch' in payload:return payload['manual_pause_epoch']
    workflow=s.get(c,'listing_workflow',payload.get('workflow_id'),required=False) if payload.get('workflow_id') else None
    return (workflow or {}).get('manual_pause_epoch')


def detail_allowed(c,listing,publication=None):
    target=s.get(c,'target',listing.get('target_id'),required=False) if listing else None
    return bool(target) and (not paused(c,target) or
        (publication or {}).get('manual_pause_epoch')==pause_epoch(c))


def job_allowed(c, job):
    payload = job.get('payload') or {}
    if job['kind'] in DEPENDENT_KINDS and 'manual_preparation_pause_epoch' in payload:
        epoch = payload['manual_preparation_pause_epoch']
        return (type(epoch) is int and epoch == pause_epoch(c) and bool(payload.get('listing_id'))
                and payload.get('manual_preparation_kind') == job['kind'])
    if job['kind']=='submit' and not payload.get('preview') and 'manual_pause_epoch' in payload:
        if not manual_current(c,payload):return False
        return True  # Explicitly selected submission only; its parent workflow stays held.
    if job['kind']=='detail_submit':
        listing=s.get(c,'listing',payload.get('listing_id'),required=False)
        publication=s.get(c,'detail_publication',payload.get('publication_id'),required=False)
        if not detail_allowed(c,listing,publication):return False
    workflow_id = payload.get('workflow_id')
    if workflow_id:
        workflow = s.get(c, 'listing_workflow', workflow_id, required=False)
        return workflow is not None and not workflow_paused(c, workflow)
    if job['kind'] not in DEPENDENT_KINDS or not payload.get('listing_id'):
        return True
    # Earlier queued dependencies have a durable reference instead of origin metadata.
    rows = c.execute("""SELECT r.* FROM commerce_records r
        WHERE r.kind='listing_workflow' AND json_extract(r.data,'$.listing_id')=?
        AND EXISTS (SELECT 1 FROM json_each(r.data,'$.jobs') j WHERE j.value=?)""",
        (payload['listing_id'], job['id'])).fetchall()
    return all(not workflow_paused(c, s.decode(row)) for row in rows)


def design_continuation_scope(c,run_id):
    scope=s.by_key(c,'design_continuation',str(run_id))
    if not scope:
        parent=c.execute('SELECT parent_run_id FROM design_simple_runs WHERE id=?',(run_id,)).fetchone()
        if parent and parent['parent_run_id']:
            scope=s.by_key(c,'design_continuation',str(parent['parent_run_id']))
    return scope


def design_allowed(c, run_id):
    rows=c.execute("""SELECT * FROM commerce_records WHERE kind='listing_workflow'
        AND json_extract(data,'$.design_run_id')=?
        AND json_extract(data,'$.status') IN ('running','waiting')""",(run_id,))
    try:
        owners=[s.decode(row) for row in rows]
    finally:rows.close()
    if any(not workflow_paused(c,owner) for owner in owners):return True
    # A design-only continuation never resumes its listing owner. A newer
    # explicit listing request may still own the shared design independently.
    scope=design_continuation_scope(c,run_id)
    return manual_current(c,scope) if scope else not owners


def authorize_design_continuation(c,run_id):
    scope=s.put(c,'design_continuation',str(run_id),{
        'run_id':run_id,'manual_pause_epoch':pause_epoch(c),'intent':'design_only'})
    s.audit(c,'design_continuation_authorized',run_id,'Design only; listing workflows remain unchanged')
    return scope
