"""Platform cases and durable actions. Saving an action never completes a case."""
import time
import uuid
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from . import store as s, aftersale_platforms as platforms, amazon, targets

CLOSED={'resolved','cancelled'}
IN_FLIGHT={'queued','sending','unknown','pending_confirmation'}
ACTION_LABELS={'approve_return':'同意退货','reject':'拒绝申请','refund':'退款','replacement':'回填补发物流'}


def error_text(exc):return str(exc.detail) if isinstance(exc,HTTPException) else str(exc)


def ingest(target,incoming):
    external=str(incoming.get('external_id','')).strip()
    if not external or not incoming.get('external_order_id'):raise HTTPException(422,'平台售后缺少售后编号或原订单编号')
    if incoming.get('platform_status') not in {'open','processing','needs_attention',*CLOSED}:raise HTTPException(422,'平台售后状态无效')
    if incoming.get('marketplace_id',target['marketplace_id'])!=target['marketplace_id']:raise HTTPException(409,'售后地区不匹配')
    with s.transaction() as c:
        key=f"platform:{target['account_id']}:{target['marketplace_id']}:{target['environment']}:{external}"
        old=s.by_key(c,'aftersale',key)
        if not old and incoming['platform_status'] in CLOSED:return None
        if old and incoming['external_order_id']!=old['external_order_id']:raise HTTPException(409,'平台售后关联订单发生变化')
        if old and old.get('observed_at',0)>incoming.get('observed_at',time.time()):return old
        account=c.execute('SELECT platform,account_name FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
        order=next((o for o in s.all_rows(c,'order') if o['target_id']==target['id'] and o['external_id']==incoming['external_order_id']),None)
        row={**(old or {}),**incoming,'execution':'platform','target_id':target['id'],'account_id':target['account_id'],
             'marketplace_id':target['marketplace_id'],'environment':target['environment'],'platform':account['platform'],
             'account_name':account['account_name'],'order_id':order['id'] if order else None,
             'last_sync_at':time.time(),'status':incoming['platform_status']}
        row.setdefault('amount',None);row.setdefault('currency',target.get('currency',''));row.setdefault('type','return')
        # The case may close outside the app while our submission still needs its own receipt.
        if old:
            active=[a for a in s.all_rows(c,'aftersale_action') if a['case_id']==old['id'] and a['status'] in IN_FLIGHT]
            if active:row['status']=active[0]['status']
        return s.save(c,'aftersale',row) if old else s.put(c,'aftersale',key,row)


def enqueue(c,kind,payload,delay=0):
    active=c.execute("SELECT id FROM commerce_jobs WHERE kind=? AND payload=? AND status IN ('queued','running','retry_wait')",(kind,s.dump(payload))).fetchone()
    if active:return s.job(c,active[0])
    job=s.enqueue(c,kind,'aftersale:'+uuid.uuid4().hex,payload)
    if delay:c.execute('UPDATE commerce_jobs SET next_at=? WHERE id=?',(time.time()+delay,job['id']))
    return job


def queue_sync(payload):
    from . import reports
    end=reports.date(payload['end']) if payload.get('end') else datetime.now(timezone.utc)
    start=reports.date(payload['start']) if payload.get('start') else end-timedelta(days=60)
    if end<=start or end>datetime.now(timezone.utc)+timedelta(minutes=1) or end-start>timedelta(days=60):raise HTTPException(422,'售后刷新时间范围须为过去60天以内的一段时间')
    with s.transaction() as c:
        chosen=targets.selected_targets(c,payload.get('target_ids'))
        for target in chosen:platforms.resolve(target)
        jobs=[]
        for target in chosen:
            # Repeated clicks share the existing target refresh, even if the default clock changed.
            active=c.execute("SELECT id FROM commerce_jobs WHERE kind='aftersale_sync' AND json_extract(payload,'$.target_id')=? AND status IN ('queued','running','retry_wait')",(target['id'],)).fetchone()
            jobs.append(s.job(c,active[0]) if active else enqueue(c,'aftersale_sync',{'target_id':target['id'],'start':start.isoformat(),'end':end.isoformat()}))
        return {'target_count':len(chosen),'jobs':jobs}


def sync(job):
    with s.main().db() as c:target=s.get(c,'target',job['payload']['target_id'])
    adapter=platforms.resolve(target)
    if isinstance(adapter,platforms.AmazonAftersales):
        from . import aftersales_reports
        return aftersales_reports.start(target,job)
    rows=adapter.search(target);seen=set();count=0
    for row in rows:
        if row['platform_status'] in CLOSED:continue
        seen.add(row['external_id']);ingest(target,row);count+=1
    with s.main().db() as c:
        pending=[r for r in s.all_rows(c,'aftersale') if r.get('execution')=='platform' and r['target_id']==target['id'] and r['status'] not in CLOSED and r['external_id'] not in seen]
    failures=[]
    for old in pending:
        try:ingest(target,adapter.get(target,old['external_id']))
        except (HTTPException,amazon.PlatformError) as exc:failures.append({'id':old['id'],'message':error_text(exc)})
    with s.transaction() as c:
        latest=s.get(c,'target',target['id']);latest.update(last_aftersale_sync=time.time(),aftersale_sync_issues=failures);s.save(c,'target',latest)
    return {'count':count,'recheck_issues':failures}


def details(ident):
    with s.main().db() as c:
        case=s.get(c,'aftersale',ident)
        actions=[a for a in s.all_rows(c,'aftersale_action') if a['case_id']==ident]
        target=s.get(c,'target',case['target_id']) if case.get('target_id') else None
    available=[];message='历史手工登记，仅保留原办理记录。'
    if case.get('execution')=='platform':
        try:
            adapter=platforms.resolve(target,case);available=adapter.actions(case);message=adapter.message
        except HTTPException as exc:message=str(exc.detail)
    if any(a['status'] not in {'confirmed','cancelled'} for a in actions):available=[]
    return {'case':case,'actions':actions,'available_actions':[{'value':a,'label':ACTION_LABELS[a]} for a in available],'message':message}


def create_action(ident,payload):
    request_id=str(payload.get('request_id',''))
    if not 8<=len(request_id)<=100:raise HTTPException(422,'缺少本次处理编号')
    action=payload.get('action');body=payload.get('payload')
    if action not in ACTION_LABELS or not isinstance(body,dict):raise HTTPException(422,'请选择处理动作并填写内容')
    clean={'note':str(body.get('note','')).strip()[:2000]}
    if action=='refund':
        clean.update(amount=s.money(body.get('amount')),currency=str(body.get('currency','')))
        if float(clean['amount'])<=0:raise HTTPException(422,'退款金额必须大于0')
    if action=='replacement':
        clean.update(carrier=str(body.get('carrier','')).strip()[:100],tracking=str(body.get('tracking','')).strip()[:100])
        if not clean['carrier'] or not clean['tracking']:raise HTTPException(422,'请填写实际补发物流公司和单号')
    if action=='reject' and not clean['note']:raise HTTPException(422,'请填写拒绝原因')
    with s.transaction() as c:
        old=s.by_key(c,'aftersale_action',request_id)
        if old:
            if old['case_id']!=ident or old['action']!=action or old['payload']!=clean:raise HTTPException(409,'该处理编号已用于其他内容')
            return old
        case=s.get(c,'aftersale',ident)
        if case.get('execution')!='platform':raise HTTPException(422,'历史手工记录不能直接提交平台')
        if case['revision']!=payload.get('revision'):raise HTTPException(409,'售后状态已更新，请重新打开')
        target=targets.selected_targets(c,[case['target_id']])[0];adapter=platforms.resolve(target,case)
        if case['platform_status'] in CLOSED or action not in adapter.actions(case):raise HTTPException(422,'当前平台或售后状态不支持此操作')
        if any(a['case_id']==ident and a['status'] not in {'confirmed','cancelled'} for a in s.all_rows(c,'aftersale_action')):raise HTTPException(409,'已有处理草稿或待核实操作，请先处理')
        if not case.get('version'):raise HTTPException(422,'平台未提供可用于核对的售后版本')
        if action=='refund':
            from decimal import Decimal
            if clean['currency']!=case['currency'] or case.get('refundable_amount') is None or Decimal(clean['amount'])>Decimal(str(case['refundable_amount'])):raise HTTPException(422,'退款币种不匹配或金额超过平台可退金额')
        result=s.put(c,'aftersale_action',request_id,{'case_id':ident,'target_id':target['id'],'request_id':request_id,'action':action,
            'payload':clean,'platform_version':case['version'],'status':'draft','last_error':''})
        s.audit(c,'aftersale_action_saved',ident,request_id)
        return result


def queue_action(ident,check=False):
    with s.transaction() as c:
        action=s.get(c,'aftersale_action',ident);case=s.get(c,'aftersale',action['case_id'])
        target=targets.selected_targets(c,[case['target_id']])[0];platforms.resolve(target,case)
        if action['status']=='cancelled':raise HTTPException(409,'处理草稿已作废')
        if action['status']=='confirmed':return {'status':'confirmed'}
        if check or action['status'] in {'sending','unknown','pending_confirmation'}:
            if action['status'] not in {'sending','unknown','pending_confirmation'}:raise HTTPException(409,'该操作尚未发送，无需核对')
            return enqueue(c,'aftersale_check',{'action_id':ident})
        if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):raise HTTPException(422,'正式写入未启用')
        if action['status'] not in {'draft','failed','queued'}:raise HTTPException(409,'当前操作不能提交')
        action.update(status='queued',last_error='');s.save(c,'aftersale_action',action)
        case['status']='queued';s.save(c,'aftersale',case)
        return enqueue(c,'aftersale_submit',{'action_id':ident})


def cancel_action(ident,payload):
    with s.transaction() as c:
        action=s.get(c,'aftersale_action',ident)
        if action['revision']!=payload.get('revision') or action['status'] not in {'draft','failed'}:raise HTTPException(409,'仅未提交或明确失败的草稿可以作废，请刷新')
        action['status']='cancelled';s.save(c,'aftersale_action',action)
        case=s.get(c,'aftersale',action['case_id']);case['status']=case['platform_status'];s.save(c,'aftersale',case)
        return action


def submit(job):
    ident=job['payload']['action_id']
    with s.main().db() as c:
        operation=s.get(c,'aftersale_action',ident);case=s.get(c,'aftersale',operation['case_id']);target=s.get(c,'target',case['target_id'])
    if operation['status']=='confirmed':return {'status':'confirmed'}
    if operation['status'] in {'sending','unknown','pending_confirmation'}:return check(job)
    if operation['status']!='queued':raise HTTPException(409,'操作未排队或已作废')
    sent=False
    try:
        adapter=platforms.resolve(target,case)
        with s.main().db() as c:
            if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):raise HTTPException(422,'正式写入未启用')
        if operation['action'] not in adapter.actions(case):raise HTTPException(422,'当前平台不支持此操作')
        current=adapter.get(target,case['external_id'])
        if current.get('external_id')!=case['external_id'] or current.get('external_order_id')!=case['external_order_id']:raise HTTPException(409,'平台返回的售后身份不匹配')
        ingest(target,current)
        if current.get('version')!=operation['platform_version'] or current['platform_status'] in CLOSED or operation['action'] not in adapter.actions(current):raise HTTPException(409,'平台售后已变化，请作废草稿后重新处理')
        with s.transaction() as c:
            latest=s.get(c,'aftersale_action',ident)
            if latest['status']!='queued':raise HTTPException(409,'该操作已被其他任务处理')
            latest['status']='sending';s.save(c,'aftersale_action',latest)
        sent=True;receipt=adapter.submit(target,case,operation)
        with s.transaction() as c:
            latest=s.get(c,'aftersale_action',ident);latest.update(status='pending_confirmation',receipt=receipt,last_error='');s.save(c,'aftersale_action',latest)
            case=s.get(c,'aftersale',case['id']);case['status']='pending_confirmation';s.save(c,'aftersale',case)
            enqueue(c,'aftersale_check',{'action_id':ident})
        return {'status':'pending_confirmation'}
    except Exception as exc:
        definite=isinstance(exc,amazon.PlatformError) and 400<=exc.status<500 and exc.status!=408 and not exc.unknown
        # Once a request may have left this process, ordinary exceptions are ambiguous too.
        status='unknown' if sent and not definite else 'failed'
        with s.transaction() as c:
            latest=s.get(c,'aftersale_action',ident)
            if latest['status'] in {'queued','sending'}:
                latest.update(status=status,last_error=error_text(exc));s.save(c,'aftersale_action',latest)
                case=s.get(c,'aftersale',case['id']);case['status']=status;s.save(c,'aftersale',case)
                if status=='unknown':enqueue(c,'aftersale_check',{'action_id':ident})
        raise


def check(job):
    ident=job['payload']['action_id']
    with s.main().db() as c:
        operation=s.get(c,'aftersale_action',ident);case=s.get(c,'aftersale',operation['case_id']);target=s.get(c,'target',case['target_id'])
    if operation['status']=='confirmed':return {'status':'confirmed'}
    if operation['status'] not in {'sending','unknown','pending_confirmation'}:raise HTTPException(409,'该操作无需核对')
    try:
        receipt=platforms.resolve(target,case).check(target,case,operation)
        if receipt.get('status')!='confirmed':raise amazon.PlatformError('平台尚未确认本次处理，请稍后核对；不会重复提交',503,retry_after=30)
        if any(receipt.get(k)!=operation[k] for k in ['request_id','action','payload']) or receipt.get('external_id')!=case['external_id']:raise HTTPException(409,'平台回执与本次操作不匹配')
        remote=receipt.get('case',{})
        if remote.get('external_id')!=case['external_id'] or remote.get('external_order_id')!=case['external_order_id']:raise HTTPException(409,'回执中的售后身份不匹配')
        expected={'processing','resolved'} if operation['action']=='approve_return' else {'resolved'}
        if remote.get('platform_status') not in expected:raise amazon.PlatformError('平台回执尚未体现本次处理结果',503,retry_after=30)
        result=ingest(target,remote)
        with s.transaction() as c:
            latest=s.get(c,'aftersale_action',ident);latest.update(status='confirmed',confirmation=receipt,last_error='');s.save(c,'aftersale_action',latest)
            case=s.get(c,'aftersale',case['id']);case['status']=result['platform_status'];s.save(c,'aftersale',case)
            s.audit(c,'aftersale_platform_confirmed',case['id'],operation['request_id'])
        return {'status':'confirmed','case_status':case['status']}
    except Exception as exc:
        with s.transaction() as c:
            latest=s.get(c,'aftersale_action',ident);latest['last_error']=error_text(exc);s.save(c,'aftersale_action',latest)
        raise
