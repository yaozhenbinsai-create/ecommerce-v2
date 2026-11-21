"""Bounded Amazon report retrieval and explicit accounting reconciliation."""
from __future__ import annotations
import csv
import gzip
import hashlib
import io
import time
from datetime import datetime,timezone
from urllib.parse import quote
import httpx
from fastapi import HTTPException
from . import store as s,amazon

RETURNS='GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE'
SETTLEMENT='GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2'
LIMIT=20*1024*1024
RETURN_FIELDS = {'order_id','amazon_rma_id','merchant_sku','return_quantity','return_request_status',
                 'return_request_date','currency_code','item_name','return_reason','resolution','refunded_amount'}
SETTLEMENT_FIELDS = {'settlement_id','settlement_start_date','settlement_end_date','deposit_date','total_amount',
    'currency','transaction_type','order_id','merchant_order_id','adjustment_id','shipment_id','marketplace_name',
    'amount_type','amount_description','amount','fulfillment_id','posted_date','posted_date_time','order_item_code',
    'merchant_order_item_id','merchant_adjustment_item_id','sku','quantity_purchased','promotion_id'}


def date(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if not d.tzinfo:raise ValueError()
        return d
    except (ValueError,TypeError):raise HTTPException(422,'报表时间必须包含时区')


def create(payload):
    with s.transaction() as c:
        target=s.get(c,'target',payload['target_id'])
        start=date(payload['start']);end=date(payload['end'])
        if end<=start or (end-start).total_seconds()>60*86400:raise HTTPException(422,'退货报表时间范围需在60天内')
        key=f"{target['id']}:{RETURNS}:{start.isoformat()}:{end.isoformat()}"
        report=s.by_key(c,'report',key)
        if not report:report=s.put(c,'report',key,{'target_id':target['id'],'environment':target['environment'],'report_type':RETURNS,
          'start':start.isoformat(),'end':end.isoformat(),'status':'queued','report_id':'','polls':0,'requested_at':datetime.now(timezone.utc).isoformat()})
        return s.enqueue(c,'report',f"report-create:{report['id']}",{'report_id':report['id']})


def enqueue_poll(c,report):
    job=s.enqueue(c,'report',f"report-poll:{report['id']}:{report.get('polls',0)}",{'report_id':report['id']})
    c.execute('UPDATE commerce_jobs SET next_at=? WHERE id=?',(time.time()+120,job['id']))


def download(descriptor):
    from .network import public_stream
    url=descriptor['url']
    try:
        with public_stream('GET',url,timeout=45) as response:
            response.raise_for_status();content=bytearray()
            for part in response.iter_bytes():
                content.extend(part)
                if len(content)>LIMIT:raise HTTPException(422,'报表超过20MiB，请缩小范围')
        raw=bytes(content)
        if descriptor.get('compressionAlgorithm')=='GZIP' and raw.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:raw=f.read(LIMIT+1)
        if len(raw)>LIMIT:raise HTTPException(422,'解压后报表超过20MiB')
        return raw.decode('utf-8-sig')
    except (httpx.HTTPError,OSError,UnicodeError):raise amazon.PlatformError('报表文档下载或解码失败',503)


def import_rows(report,text):
    reader=csv.DictReader(io.StringIO(text),delimiter='\t');rows=[]
    if not reader.fieldnames:raise HTTPException(422,'报表没有字段')
    for row in reader:
        if len(rows)>=10000:raise HTTPException(422,'报表超过10000行，请缩小范围')
        if None in row:raise HTTPException(422,'报表列数不一致')
        if report.get('environment') == 'production':
            # Persist only the fields needed for return reconciliation/accounting.
            # No raw report, buyer name, address, phone, email or unknown columns.
            fields = RETURN_FIELDS if report['report_type'] == RETURNS else SETTLEMENT_FIELDS if report['report_type'] == SETTLEMENT else None
            if fields is None:raise HTTPException(422,'此报表尚未配置数据字段白名单')
            row = {k:v for k,v in row.items() if str(k).strip().lower().replace(' ','_').replace('-','_') in fields}
        rows.append(row)
    with s.transaction() as c:
        latest=s.get(c,'report',report['id'])
        latest.update(status='completed',rows=rows,row_count=len(rows),document_sha256=hashlib.sha256(text.encode()).hexdigest())
        result=s.save(c,'report',latest)
        if latest.get('purpose')=='aftersales':
            from .aftersales import enqueue
            enqueue(c,'aftersale_import',{'report_id':latest['id']})
        return result


def run(job):
    with s.main().db() as c:
        report=s.get(c,'report',job['payload']['report_id']);target=s.get(c,'target',report['target_id'])
    if report.get('purpose')=='aftersales':
        from .aftersale_platforms import resolve,AmazonAftersales
        if not isinstance(resolve(target),AmazonAftersales):raise HTTPException(422,'售后报表来源不匹配')
    if report['status']=='completed':return {'report_id':report['id'],'rows':report['row_count']}
    if target['environment']=='mock':
        import_rows(report,'Order ID\tAmazon RMA ID\tMerchant SKU\tReturn quantity\nMOCK-ORDER\tMOCK-RMA\tMOCK-SKU\t1\n')
        return {'environment':'mock','rows':1}
    if not report.get('report_id'):
        if report['status'] in {'sending','unknown'}:raise amazon.PlatformError('报表申请结果待核实，请先查询已生成报表',0,unknown=True)
        with s.transaction() as c:
            report['status']='sending';s.save(c,'report',report)
        try:
            response=amazon.request(target,'POST','/reports/2021-06-30/reports',body={'reportType':report['report_type'],
              'marketplaceIds':[target['marketplace_id']],'dataStartTime':report['start'],'dataEndTime':report['end']})
        except amazon.PlatformError as exc:
            with s.transaction() as c:
                report['status']='unknown' if exc.unknown else 'failed';s.save(c,'report',report)
            raise
        with s.transaction() as c:
            report.update(report_id=response['body']['reportId'],status='processing');s.save(c,'report',report);enqueue_poll(c,report)
        return {'report_id':report['id'],'status':'processing'}
    response=amazon.request(target,'GET','/reports/2021-06-30/reports/'+quote(report['report_id'],safe=''))['body']
    status=response['processingStatus']
    if status=='DONE':
        descriptor=amazon.request(target,'GET','/reports/2021-06-30/documents/'+quote(response['reportDocumentId'],safe=''))['body']
        result=import_rows(report,download(descriptor))
        return {'report_id':report['id'],'rows':result['row_count']}
    with s.transaction() as c:
        report['polls']=report.get('polls',0)+1;report['platform_status']=status
        report['status']='failed' if status in {'CANCELLED','FATAL'} else 'paused' if report['polls']>=30 else 'processing'
        s.save(c,'report',report)
        if report['status']=='processing':enqueue_poll(c,report)
    return {'report_id':report['id'],'status':report['status']}


def reconcile_report(job):
    with s.main().db() as c:
        report=s.get(c,'report',job['payload']['report_id']);target=s.get(c,'target',report['target_id'])
    response=amazon.request(target,'GET','/reports/2021-06-30/reports',{'reportTypes':report['report_type'],
       'marketplaceIds':target['marketplace_id'],'createdSince':report['requested_at'],'pageSize':100})['body']
    matches=[r for r in response.get('reports',[]) if r.get('dataStartTime') and r.get('dataEndTime') and date(r['dataStartTime'])==date(report['start']) and date(r['dataEndTime'])==date(report['end'])]
    if len(matches)!=1:raise amazon.PlatformError('尚不能唯一确认该报表，请稍后核对或填写平台报表ID',0,unknown=True)
    with s.transaction() as c:
        report.update(report_id=matches[0]['reportId'],status='processing',polls=0);s.save(c,'report',report);enqueue_poll(c,report)
    return {'report_id':report['id'],'linked':True}


def reconcile_transaction(ident,payload):
    with s.transaction() as c:
        row=s.get(c,'transaction',ident)
        if row['revision']!=payload.get('revision'):raise HTTPException(409,'交易已更新，请刷新')
        order=s.get(c,'order',payload['order_id'])
        if order['target_id']!=row['target_id'] or order['environment']!=row['environment']:raise HTTPException(422,'交易与订单必须来自同一目标和环境')
        currency=row['raw'].get('totalAmount',{}).get('currencyCode')
        if currency!=order['currency']:raise HTTPException(422,'交易与订单币种不一致')
        category=payload.get('category')
        if category not in {'settlement','platform_fee','refund','other'}:raise HTTPException(422,'对账类别无效')
        if row.get('expense_id') and (row.get('category')!=category or row.get('order_id')!=order['id']):raise HTTPException(409,'已入账交易不能改为其他类别或订单，请保留原关联核对')
        note=str(payload.get('note','')).strip()
        if not note:raise HTTPException(422,'请记录核对依据')
        linked=payload.get('expense_id')
        expense=s.get(c,'expense',int(linked)) if linked else None
        if expense and (expense['order_id']!=order['id'] or expense['currency']!=currency):raise HTTPException(422,'已录费用不属于该订单或币种')
        if category in {'platform_fee','refund'}:
            amount=s.money(payload.get('amount'))
            if expense:
                if expense['amount']!=amount:raise HTTPException(422,'金额与已录费用不一致')
                if expense.get('transaction_id') not in {None,ident}:raise HTTPException(409,'该费用已关联其他交易')
                expense['transaction_id']=ident;s.save(c,'expense',expense)
            else:
                expense=s.put(c,'expense','transaction:'+str(ident),{'order_id':order['id'],'environment':order['environment'],
                   'currency':currency,'amount':amount,'type':category,'source':'transaction','transaction_id':ident,'reference':row['external_id']})
        row.update(status='reconciled',order_id=order['id'],category=category,note=note[:1000],expense_id=expense['id'] if expense else None)
        return s.save(c,'transaction',row)


def discover(job):
    with s.main().db() as c:target=s.get(c,'target',job['payload']['target_id'])
    if target['environment']=='mock':return {'environment':'mock','reports':[]}
    response=amazon.request(target,'GET','/reports/2021-06-30/reports',{'reportTypes':SETTLEMENT,
      'marketplaceIds':target['marketplace_id'],'processingStatuses':'DONE','pageSize':20})['body']
    with s.transaction() as c:
        for r in response.get('reports',[]):
            key=f"{target['id']}:{r['reportId']}"
            if s.by_key(c,'report',key):continue
            s.put(c,'report',key,{'target_id':target['id'],'environment':target['environment'],'report_type':SETTLEMENT,
               'report_id':r['reportId'],'status':'available','polls':0,'start':r.get('dataStartTime',''),'end':r.get('dataEndTime',''),'requested_at':r.get('createdTime','')})
    return {'reports':len(response.get('reports',[])),'message':'已列出最近20份平台结算报表，可选择下载解析'}
