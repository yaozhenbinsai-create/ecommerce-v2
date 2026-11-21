"""Offline fulfillment: receive open orders, submit tracking, reconcile remote truth."""
import copy
import time
import uuid
from datetime import datetime,timezone,timedelta
from fastapi import HTTPException
from . import store as s, order_platforms, amazon, operations

OPEN={'ready','partially_shipped'}


def platform_time(value):
    if not value:return None
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:raise HTTPException(422,'平台订单更新时间无效')


def ingest(target,incoming,existing_only=False):
    with s.transaction() as c:
        key=f"{target['account_id']}:{target['marketplace_id']}:{target['environment']}:{incoming['external_id']}"
        old=s.by_key(c,'order',key)
        if not old:old=next((o for o in s.all_rows(c,'order') if o['target_id']==target['id'] and o['external_id']==incoming['external_id']),None)
        if not old and (existing_only or incoming['platform_status'] not in OPEN):return None
        # Ignore older platform snapshots instead of reopening a confirmed order.
        prior_time=platform_time((old or {}).get('platform_updated_at'))
        incoming_time=platform_time(incoming.get('platform_updated_at'))
        if prior_time and incoming_time and incoming_time<prior_time:return old
        items=[]
        previous={i['external_item_id']:i for i in (old or {}).get('items',[])}
        for line in incoming['items']:
            quantity=s.quantity(line['quantity'],1);shipped=s.quantity(line.get('shipped',0))
            if shipped>quantity:raise HTTPException(422,'平台发货数量超过订单数量')
            prior=previous.get(line['external_item_id'],{})
            items.append({'sku_id':None,'snapshot':{},'reserved':0,'production_id':None,**prior,**line,
                          'quantity':quantity,'shipped':max(shipped,prior.get('shipped',0)),
                          'price':s.money(line.get('price'),True),'line_total':s.money(line.get('line_total'),True)})
        if not items:return old
        if len({i['external_item_id'] for i in items})!=len(items):raise HTTPException(422,'平台订单商品行重复')
        account=c.execute('SELECT platform,account_name FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
        row={**(old or {}),**incoming,'items':items,'target_id':target['id'],'account_id':target['account_id'],
             'platform':account['platform'],'account_name':account['account_name'],'marketplace_id':target['marketplace_id'],
             'environment':target['environment'],'fulfillment_mode':'offline','last_sync_at':time.time(),'sync_issues':[]}
        state=incoming['platform_status']
        row['status']='cancelled' if state=='cancelled' else 'shipped' if all(i['shipped']>=i['quantity'] for i in items) else 'partially_shipped' if state in OPEN and any(i['shipped'] for i in items) else 'ready' if state in OPEN else 'needs_attention'
        if old:row=s.save(c,'order',row)
        else:row=s.put(c,'order',key,row)
        if not old:operations.notify(c,'order:'+str(row['id']),'新订单 '+row['external_id'],row['id'])
        return row


def sync(target):
    adapter=order_platforms.resolve(target)
    high=(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()
    since=target.get('open_orders_cursor') or target.get('orders_since') or (datetime.now(timezone.utc)-timedelta(days=730)).isoformat()
    incoming=adapter.search(target,since,high);seen=set();count=0
    for row in incoming:
        if row['platform_status'] not in OPEN:continue
        seen.add(row['external_id']);ingest(target,row);count+=1
    # A filtered list cannot report cancellations or shipment outside this app.
    # Recheck known open orders individually; never import historical closed orders.
    with s.main().db() as c:
        open_orders=[o for o in s.all_rows(c,'order') if o['target_id']==target['id'] and o['status'] not in {'shipped','cancelled'} and o['external_id'] not in seen]
    failures=[]
    for old in sorted(open_orders,key=lambda o:o.get('last_sync_at',0))[:50]:
        try:ingest(target,adapter.get(target,old['external_id']),existing_only=True)
        except (HTTPException,amazon.PlatformError) as exc:
            failures.append({'order_id':old['id'],'message':str(exc.detail) if isinstance(exc,HTTPException) else str(exc)})
    with s.transaction() as c:
        latest=s.get(c,'target',target['id']);latest['open_orders_cursor']=(datetime.fromisoformat(high)-timedelta(minutes=5)).isoformat()
        latest['last_order_sync']=time.time();latest['order_sync_issues']=failures;s.save(c,'target',latest)
    return {'count':count,'recheck_issues':failures,'message':f'已刷新 {count} 条待发货订单，另核对已有订单 {min(len(open_orders),50)} 条'}


def remaining(c,order):
    held={}
    for ship in s.all_rows(c,'shipment'):
        if ship['order_id']==order['id'] and ship['status'] not in {'confirmed','cancelled'} and not ship.get('quantities_applied') and not ship.get('inventory_applied'):
            for i in ship['items']:held[i['item_id']]=held.get(i['item_id'],0)+i['quantity']
    return {i['external_item_id']:max(0,i['quantity']-i['shipped']-held.get(i['external_item_id'],0)) for i in order['items']}


def create(ident,payload):
    with s.transaction() as c:
        order=s.get(c,'order',ident);target=s.get(c,'target',order['target_id'])
        request_id=str(payload.get('request_id',''))
        if not 8<=len(request_id)<=100:raise HTTPException(422,'请提供本次回填编号')
        key='offline:'+request_id
        previous=s.by_key(c,'shipment',key)
        if previous:
            if previous['order_id']!=ident:raise HTTPException(409,'该回填编号已用于其他订单')
            if previous.get('request_payload')!=payload:raise HTTPException(409,'本次物流已保存，内容已变化时请在订单中编辑已有草稿')
            return previous
        if order['status'] in {'shipped','cancelled','aftersale_needed'} or order['platform_status'] not in OPEN:raise HTTPException(409,'该订单当前不可回填新包裹')
        if not order.get('manual_shipping_supported',True):raise HTTPException(422,'该平台订单不支持手工确认物流，请在平台处理')
        if not target.get('enabled') or not c.execute('SELECT 1 FROM platform_accounts WHERE id=? AND enabled=1',(target['account_id'],)).fetchone():raise HTTPException(409,'所属账号或地区已停用')
        carrier=str(payload.get('carrier','')).strip();tracking=str(payload.get('tracking','')).strip()
        if not carrier or not tracking or max(len(carrier),len(tracking))>100:raise HTTPException(422,'请填写物流公司和物流单号，最长100字符')
        requested=copy.deepcopy(payload.get('items',[]))
        if not isinstance(requested,list) or not requested or any(not isinstance(i,dict) for i in requested):raise HTTPException(422,'请填写本次包裹商品数量')
        if len({i.get('item_id') for i in requested})!=len(requested):raise HTTPException(422,'包裹商品行不能重复')
        available=remaining(c,order)
        for i in requested:
            i['quantity']=s.quantity(i.get('quantity'),1)
            if i.get('item_id') not in available or i['quantity']>available[i['item_id']]:raise HTTPException(409,'包裹数量超过尚未发货且未分配的数量')
        return s.put(c,'shipment',key,{'order_id':ident,'target_id':target['id'],'external_order_id':order['external_id'],
            'environment':target['environment'],'fulfillment_mode':'offline','carrier':carrier,'tracking':tracking,
            'service':str(payload.get('service',''))[:100],'ship_date':operations.ship_date(payload.get('ship_date')),
            'items':requested,'status':'draft','request_payload':copy.deepcopy(payload),
            'platform_package_id':str(uuid.uuid4().int % 2000000000 + 1),'last_error':''})


def enqueue(c,ship):
    if ship['status']=='cancelled':raise HTTPException(409,'此物流草稿已作废')
    kind='reconcile_shipment' if ship['status'] in {'sending','unknown','pending_confirmation','confirmed'} else 'shipment'
    if kind=='shipment':
        target=s.get(c,'target',ship['target_id'])
        if target['environment']=='production' and not (s.by_key(c,'settings','global') or {}).get('live_writes_enabled'):raise HTTPException(422,'正式平台写入未启用')
    active=c.execute("SELECT id FROM commerce_jobs WHERE kind IN ('shipment','reconcile_shipment') AND json_extract(payload,'$.shipment_id')=? AND status IN ('queued','running','retry_wait')",(ship['id'],)).fetchone()
    if active:return s.job(c,active[0])
    if kind=='shipment':ship['status']='queued';s.save(c,'shipment',ship)
    return s.enqueue(c,kind,f"offline:{kind}:{ship['id']}:{uuid.uuid4().hex}",{'shipment_id':ship['id']})


def submit(ident):
    with s.main().db() as c:
        ship=s.get(c,'shipment',ident);order=s.get(c,'order',ship['order_id']);target=s.get(c,'target',ship['target_id'])
        settings=s.by_key(c,'settings','global') or {}
    if ship['status']=='confirmed':return ship
    if ship['status'] in {'sending','unknown','pending_confirmation'}:return reconcile(ident)
    if ship['status'] not in {'draft','failed','queued'}:raise HTTPException(409,'此包裹不可提交')
    if target['environment']=='production' and not settings.get('live_writes_enabled'):raise HTTPException(422,'正式平台写入未启用')
    adapter=order_platforms.resolve(target)
    # Check remote order immediately before writing: cancellation and other shippers
    # must not be hidden by the pending-order search filter.
    remote=adapter.get(target,order['external_id'])
    ingest(target,remote,existing_only=True)
    if remote['platform_status'] not in OPEN or not remote.get('manual_shipping_supported',True):raise HTTPException(409,'平台订单当前不支持本次发货')
    by_id={i['external_item_id']:i for i in remote['items']}
    if any(i['item_id'] not in by_id or i['quantity']>by_id[i['item_id']]['quantity']-by_id[i['item_id']]['shipped'] for i in ship['items']):raise HTTPException(409,'平台剩余待发数量已变化，请核对包裹')
    with s.transaction() as c:
        current=s.get(c,'shipment',ident)
        if current['revision']!=ship['revision']:raise HTTPException(409,'物流信息已更新，请重新提交')
        current['status']='sending';s.save(c,'shipment',current)
    try:
        receipt=adapter.submit(target,order,ship)
    except Exception as exc:
        with s.transaction() as c:
            current=s.get(c,'shipment',ident)
            current['status']='failed' if isinstance(exc,amazon.PlatformError) and not exc.unknown and 400<=exc.status<500 and exc.status!=408 else 'unknown'
            current['last_error']=str(exc.detail) if isinstance(exc,HTTPException) else str(exc)
            s.save(c,'shipment',current)
            if current['status']=='unknown':s.enqueue(c,'reconcile_shipment',f"offline-unknown:{ident}:{current.get('dispatch_revision',1)}",{'shipment_id':ident})
        raise
    with s.transaction() as c:
        current=s.get(c,'shipment',ident);current.update(status='pending_confirmation',platform_result=receipt,last_error='');s.save(c,'shipment',current)
        s.enqueue(c,'reconcile_shipment',f"offline-check:{ident}:{current.get('dispatch_revision',1)}",{'shipment_id':ident})
    return {'status':'pending_confirmation','message':'平台已接收，正在核对订单与包裹'}


def reconcile(ident):
    with s.main().db() as c:
        ship=s.get(c,'shipment',ident);order=s.get(c,'order',ship['order_id']);target=s.get(c,'target',ship['target_id'])
    if ship['status']=='confirmed':return ship
    if ship['status'] not in {'sending','unknown','pending_confirmation'}:raise HTTPException(409,'请先提交物流')
    remote=order_platforms.resolve(target).get(target,order['external_id'])
    match=next((p for p in remote.get('packages',[]) if str(p.get('packageReferenceId'))==ship['platform_package_id'] and p.get('trackingNumber')==ship['tracking']),None)
    expected={i['item_id']:i['quantity'] for i in ship['items']}
    observed={i.get('orderItemId'):i.get('quantity') for i in (match or {}).get('packageItems',[])}
    totals={i['external_item_id']:i['shipped'] for i in remote['items']}
    with s.main().db() as c:
        confirmed=[p for p in s.all_rows(c,'shipment') if p['order_id']==order['id'] and p['status']=='confirmed']
    minimum=dict(expected)
    for package in confirmed:
        for i in package['items']:minimum[i['item_id']]=minimum.get(i['item_id'],0)+i['quantity']
    consistent=all(totals.get(k,0)>=n for k,n in minimum.items())
    if remote['platform_status']=='shipped':consistent=consistent and all(i['shipped']==i['quantity'] for i in remote['items'])
    if not match or expected!=observed or not consistent or remote['platform_status'] not in {'partially_shipped','shipped'}:
        raise amazon.PlatformError('平台尚未确认对应包裹及发货状态，请稍后核对',503,retry_after=30)
    with s.transaction() as c:
        current=s.get(c,'shipment',ident)
        if current['status']=='confirmed':return current
        current.update(status='confirmed',quantities_applied=True,platform_result={'source':'order_check','package':match},last_error='')
        # Per-item platform totals are authoritative. No stock or production writes.
        local=s.get(c,'order',order['id'])
        totals={i['external_item_id']:i['shipped'] for i in remote['items']}
        for item in local['items']:item['shipped']=max(item['shipped'],totals.get(item['external_item_id'],0))
        local['status']='shipped' if remote['platform_status']=='shipped' else 'partially_shipped';local['platform_status']=remote['platform_status']
        local['platform_updated_at']=remote.get('platform_updated_at','');local['fulfillment_mode']='offline'
        s.save(c,'order',local);current=s.save(c,'shipment',current)
        c.execute("UPDATE commerce_jobs SET status='completed',error='' WHERE kind='shipment' AND status='unknown' AND json_extract(payload,'$.shipment_id')=?",(ident,))
        s.audit(c,'offline_logistics_confirmed',ident)
        return current
