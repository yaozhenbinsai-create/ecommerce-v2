"""Amazon returns report bridge; report absence is never evidence of closure."""
import hashlib
import time
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from . import store as s, reports, amazon, aftersales, aftersale_platforms


def start(target,job):
    payload=job['payload']
    with s.transaction() as c:
        ranges=[(reports.date(payload['start']),reports.date(payload['end']))];issues=[]
        pending=[r for r in s.all_rows(c,'aftersale') if r.get('execution')=='platform' and r['target_id']==target['id'] and r['status'] not in aftersales.CLOSED]
        # Also recheck known open requests older than the discovery window.
        for case in pending:
            try:day=request_date(case.get('requested_at',''))
            except HTTPException:
                issues.append({'case_id':case['id'],'message':'退货申请日期无法解析，请选择日期范围刷新核对'});continue
            if not any(start<=day<=end for start,end in ranges):
                ranges.append((day-timedelta(days=1),min(day+timedelta(days=58),datetime.now(timezone.utc))))
        ids=[]
        for number,(start,end) in enumerate(ranges):
            # Stable across job retries; each user refresh requests fresh platform data.
            key=f"aftersale-refresh:{job['id']}:{number}"
            report=s.by_key(c,'report',key)
            if not report:
                report=s.put(c,'report',key,{'target_id':target['id'],'environment':target['environment'],'report_type':reports.RETURNS,
                    'start':start.isoformat(),'end':end.isoformat(),'status':'queued','report_id':'','polls':0,
                    'purpose':'aftersales','requested_at':datetime.now(timezone.utc).isoformat()})
            s.enqueue(c,'report','aftersale-report:'+str(report['id']),{'report_id':report['id']});ids.append(report['id'])
        latest=s.get(c,'target',target['id']);latest['aftersale_sync_issues']=issues;s.save(c,'target',latest)
    return {'report_ids':ids,'recheck_issues':issues,'message':'退货报表生成后自动更新待处理售后，并核对已知未完成申请'}


def request_date(value):
    try:
        day=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return day if day.tzinfo else day.replace(tzinfo=timezone.utc)
    except ValueError:raise HTTPException(422,'无法解析退货申请日期，请在刷新售后中选择包含该申请日期的范围')


def parse(rows,observed_at):
    groups={}
    for raw in rows:
        row={str(k).strip().lower().replace(' ','_').replace('-','_'):v for k,v in raw.items()}
        order=str(row.get('order_id','')).strip();rma=str(row.get('amazon_rma_id','')).strip()
        if not order or not rma or not row.get('return_request_status'):raise HTTPException(422,'退货报表缺少订单编号、Amazon RMA ID 或状态，未自动更新')
        key=(order,rma);raw_status=row['return_request_status'].strip();status=raw_status.lower()
        # Amazon's report schema has no published status enum. Unknown values stay visible.
        state={'closed':'resolved','completed':'resolved','cancelled':'cancelled','canceled':'cancelled',
               'pending':'open','pending authorization':'open','authorized':'processing','approved':'processing'}.get(status,'needs_attention')
        if key not in groups:
            groups[key]={'external_id':rma,'external_order_id':order,'platform_status':state,'platform_status_raw':raw_status,
                'type':'return','reason':'','items':[],'requested_at':row.get('return_request_date',''),
                'currency':row.get('currency_code',''),'amount':None,'observed_at':observed_at,'source':'amazon_returns_report'}
        case=groups[key]
        if case['platform_status_raw']!=raw_status:raise HTTPException(422,'同一售后单返回多个状态，请在平台核对')
        case['items'].append({'seller_sku':row.get('merchant_sku',''),'name':row.get('item_name',''),
            'quantity':s.quantity(row.get('return_quantity'),1),'reason':row.get('return_reason',''),
            'resolution':row.get('resolution',''),'refunded_amount':s.money(row.get('refunded_amount'),True)})
        case['reason']='；'.join(dict.fromkeys(i['reason'] for i in case['items'] if i['reason']))
    for case in groups.values():case['version']=hashlib.sha256(s.dump(case).encode()).hexdigest()
    return list(groups.values())


def import_report(job):
    with s.main().db() as c:
        report=s.get(c,'report',job['payload']['report_id']);target=s.get(c,'target',report['target_id'])
    adapter=aftersale_platforms.resolve(target)
    if not isinstance(adapter,aftersale_platforms.AmazonAftersales) or report.get('purpose')!='aftersales' or report['status']!='completed':raise HTTPException(422,'售后报表未完成或来源不匹配')
    if report.get('aftersales_imported'):return report['aftersales_result']
    observed=reports.date(report['requested_at']).timestamp()
    cases=parse(report.get('rows',[]),observed);count=0;skipped=0
    from .order_platforms import AmazonOrders
    for case in cases:
        with s.main().db() as c:
            known=next((r for r in s.all_rows(c,'aftersale') if r.get('execution')=='platform' and r['target_id']==target['id'] and r['external_id']==case['external_id']),None)
            checked=s.by_key(c,'aftersale_report_order',f"{report['id']}:{case['external_order_id']}")
        if not known and case['platform_status'] in aftersales.CLOSED:skipped+=1;continue
        if not checked:
            raw=amazon.get_order(target,case['external_order_id'])
            if raw.get('orderId')!=case['external_order_id']:raise HTTPException(409,'平台返回的原订单编号不匹配')
            market=raw.get('salesChannel',{}).get('marketplaceId')
            if not market:raise HTTPException(422,'无法核实退货所属地区，请核对平台订单权限')
            snapshot=AmazonOrders().normalize(target,raw) if market==target['marketplace_id'] else None
            if snapshot is not None:snapshot['recipient']={}
            with s.transaction() as c:checked=s.put(c,'aftersale_report_order',f"{report['id']}:{case['external_order_id']}",{'marketplace_id':market,'order':snapshot})
        if checked['marketplace_id']!=target['marketplace_id']:skipped+=1;continue
        case['original_order']=checked['order'];case['currency']=checked['order']['currency']
        case['marketplace_id']=target['marketplace_id'];case['report_id']=report['id']
        if aftersales.ingest(target,case):count+=1
    with s.transaction() as c:
        latest=s.get(c,'report',report['id']);result={'count':count,'skipped':skipped,'report_id':report['id']}
        latest.update(aftersales_imported=True,aftersales_result=result);s.save(c,'report',latest)
        current=s.get(c,'target',target['id']);current.update(last_aftersale_sync=time.time());s.save(c,'target',current)
    return result


def refresh_case(ident):
    with s.main().db() as c:
        case=s.get(c,'aftersale',ident);target=s.get(c,'target',case['target_id'])
    adapter=aftersale_platforms.resolve(target,case)
    if isinstance(adapter,aftersale_platforms.AmazonAftersales):
        day=request_date(case.get('requested_at',''))
        return aftersales.queue_sync({'target_ids':[target['id']],'start':(day-timedelta(days=1)).isoformat(),
            'end':min(day+timedelta(days=1),datetime.now(timezone.utc)).isoformat()})
    remote=adapter.get(target,case['external_id'])
    if remote.get('external_id')!=case['external_id']:raise HTTPException(409,'平台售后编号不匹配')
    return aftersales.ingest(target,remote)
