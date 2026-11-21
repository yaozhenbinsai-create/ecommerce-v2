from __future__ import annotations
import copy
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import HTTPException
from . import store as s


def notify(c,key,title,object_id=None):
    if not s.by_key(c,'notice',key):s.put(c,'notice',key,{'title':title,'object_id':object_id,'read':False})




def consume_channel_allocation(c,order):
    if order['platform_status'] not in {'ready','shipped'} or order.get('replacement_of'):return
    for item in order['items']:
        if item.get('allocation_consumed') or not item.get('sku_id'):continue
        listing_id=item['snapshot'].get('listing_id')
        allocation=s.by_key(c,'allocation',f"{listing_id}:{item['sku_id']}") if listing_id else None
        if allocation:
            allocation['quantity']=max(0,allocation['quantity']-item['quantity']);s.save(c,'allocation',allocation)
        item['allocation_consumed']=True


def cancel_in_transaction(c,order):
    for item in order['items']:
        if item['reserved']:
            s.move_stock(c,item['sku_id'],f"cancel:{order['id']}:{item['external_item_id']}",reserved=-item['reserved'],reason='订单取消释放',environment=order['environment'])
            item['reserved']=0
        if item.get('production_id'):
            production=s.get(c,'production',item['production_id'])
            if production['status']=='queued':production['status']='cancelled'
            elif production['status']=='started':production['status']='stop_requested'
            s.save(c,'production',production)
    order['status']='aftersale_needed' if any(x['shipped'] for x in order['items']) else 'cancelled'
    notify(c,'cancel:'+str(order['id']),'订单取消，请查看生产/售后状态',order['id'])


def prepare_order(ident):
    with s.transaction() as c:
        order=s.get(c,'order',ident)
        if order['platform_status']!='ready' or order.get('sync_issues'):raise HTTPException(409,'订单当前不可安排生产，请核对平台状态和同步差异')
        if any(not x['sku_id'] for x in order['items']):raise HTTPException(422,'订单存在未关联SKU')
        for item in order['items']:
            if item.get('production_id'):continue
            remaining=item['quantity']-item['shipped']-item['reserved']
            if remaining<=0:continue
            available=s.stock(c,item['sku_id'],order['environment'],create=False)['available'];take=min(available,remaining)
            if take:
                s.move_stock(c,item['sku_id'],f"reserve:{ident}:{item['external_item_id']}",reserved=take,reason='订单现货预留',environment=order['environment'])
                item['reserved']+=take
            missing=remaining-take
            if missing:
                components=item['snapshot'].get('sku',{}).get('components',[])
                prod=s.put(c,'production',f"{ident}:{item['external_item_id']}",{'order_id':ident,'item_id':item['external_item_id'],
                  'environment':order['environment'],
                  'sku_id':item['sku_id'],'name':item['snapshot'].get('sku',{}).get('name',item['seller_sku']),
                  'quantity':missing,'status':'queued','snapshot':item['snapshot'],
                  'components':[dict(x,total_quantity=s.quantity(x.get('quantity',1),1)*missing) for x in components],
                  'deadline':order.get('deadline',''),'notes':''})
                item['production_id']=prod['id']
        order['status']='preparing' if any(x['production_id'] for x in order['items']) else 'ready_to_ship'
        return s.save(c,'order',order)


def production_action(ident,action):
    with s.transaction() as c:
        p=s.get(c,'production',ident)
        if action=='start':
            if p['status']=='started':return p
            if p['status']!='queued':raise HTTPException(409,'该生产单不可开工')
            p['status']='started'
        elif action=='complete':
            if p['status']=='completed':return p
            if p['status'] not in {'queued','started'}:raise HTTPException(409,'请先处理取消或停产要求')
            order=s.get(c,'order',p['order_id']);item=next(x for x in order['items'] if x['external_item_id']==p['item_id'])
            if order['platform_status']!='ready' or order.get('sync_issues'):raise HTTPException(409,'订单有同步差异或已不可履约，请先核对')
            s.move_stock(c,p['sku_id'],'production:'+str(ident),on_hand=p['quantity'],reserved=p['quantity'],reason='生产完成并预留给订单',environment=order['environment'])
            item['reserved']+=p['quantity'];p['status']='completed'
            if all(x['reserved']+x['shipped']==x['quantity'] for x in order['items']):order['status']='ready_to_ship'
            s.save(c,'order',order)
        elif action=='stop':
            if p['status'] not in {'stop_requested','queued','started'}:raise HTTPException(409,'状态不可停产')
            p['status']='cancelled'
        else:raise HTTPException(422,'未知生产操作')
        return s.save(c,'production',p)


def create_shipment(payload):
    with s.transaction() as c:
        order=s.get(c,'order',payload['order_id'])
        if order['status'] in {'cancelled','aftersale_needed'} or order['platform_status']!='ready' or order.get('sync_issues'):raise HTTPException(409,'该订单不可发货，请先核对状态或同步差异')
        key=payload.get('request_id') or uuid.uuid4().hex
        old=s.by_key(c,'shipment',key)
        if old:return old
        carrier=str(payload.get('carrier','')).strip();tracking=str(payload.get('tracking','')).strip()
        if not carrier or not tracking:raise HTTPException(422,'请填写承运商和真实物流单号')
        requested=payload.get('items',[])
        if not requested or len({x['item_id'] for x in requested})!=len(requested):raise HTTPException(422,'请选择不重复的订单行和数量')
        allocated={}
        for old_ship in s.all_rows(c,'shipment'):
            if old_ship['order_id']==order['id'] and old_ship['status'] not in {'confirmed','cancelled'} and not old_ship.get('inventory_applied'):
                for i in old_ship['items']:allocated[i['item_id']]=allocated.get(i['item_id'],0)+i['quantity']
        for i in requested:
            item=next((x for x in order['items'] if x['external_item_id']==i['item_id']),None)
            i['quantity']=s.quantity(i['quantity'],1)
            if not item or i['quantity']>item['reserved']-allocated.get(i['item_id'],0):raise HTTPException(409,'包裹数量超过已备妥且未分配包裹的数量')
        ship=s.put(c,'shipment',key,{'order_id':order['id'],'target_id':order['target_id'],'external_order_id':order['external_id'],
          'environment':order['environment'],'carrier':carrier[:100],'tracking':tracking[:100],
          'service':str(payload.get('service',''))[:100],'ship_date':ship_date(payload.get('ship_date')),
          'items':requested,'status':'draft','platform_package_id':uuid.uuid4().hex[:12],'last_error':''})
        return ship


def ship_date(value=None):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00')) if value else datetime.now(timezone.utc)
        if dt.tzinfo is None or dt.timestamp()>time.time()+300:raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError,TypeError,OverflowError):raise HTTPException(422,'发货时间需包含时区，且不能在未来')


def finalize_shipment(ident,result):
    with s.transaction() as c:
        ship=s.get(c,'shipment',ident)
        if ship['status']=='confirmed':return ship
        order=s.get(c,'order',ship['order_id'])
        for row in ([] if ship.get('inventory_applied') else ship['items']):
            item=next(x for x in order['items'] if x['external_item_id']==row['item_id'])
            s.move_stock(c,item['sku_id'],f"ship:{ident}:{row['item_id']}",on_hand=-row['quantity'],reserved=-row['quantity'],reason='包裹发货出库',environment=order['environment'])
            item['reserved']-=row['quantity'];item['shipped']+=row['quantity']
        order['status']='shipped' if all(x['shipped']==x['quantity'] for x in order['items']) else 'partially_shipped'
        ship['status']='confirmed';ship['platform_result']=result;ship['inventory_applied']=True
        s.save(c,'order',order);s.save(c,'shipment',ship);s.audit(c,'shipment_confirmed',ident)
        return ship


def aftersale(payload):
    with s.transaction() as c:
        order=s.get(c,'order',payload['order_id']);kind=payload.get('type','return')
        if kind not in {'return','refund','replacement'}:raise HTTPException(422,'售后类型无效')
        item=next((x for x in order['items'] if x['external_item_id']==payload.get('item_id')),None)
        if not item:raise HTTPException(422,'请选择订单行')
        key=payload.get('request_id') or uuid.uuid4().hex
        previous=s.by_key(c,'aftersale',key)
        if previous:
            if previous['order_id']!=order['id'] or previous['item_id']!=item['external_item_id']:raise HTTPException(409,'该请求编号已用于其他售后')
            return previous
        count=s.quantity(payload.get('quantity',1),1)
        if count>item['quantity']:raise HTTPException(422,'售后数量超过订单数量')
        amount=s.money(payload.get('amount',0))
        cases=[v for v in s.all_rows(c,'aftersale') if v.get('execution')!='platform' and v['order_id']==order['id'] and v['item_id']==item['external_item_id'] and v['status']!='cancelled']
        if kind in {'return','replacement'} and count+sum(v['quantity'] for v in cases if v['type']==kind)>item['shipped']:
            raise HTTPException(422,'累计退货或补发数量不能超过已发货数量')
        total=item.get('line_total')
        if total is None and item.get('price') is not None:total=Decimal(item['price'])*item['quantity']
        if Decimal(amount)>0 and (total is None or Decimal(amount)+sum(Decimal(v['amount']) for v in cases)>Decimal(total)):
            raise HTTPException(422,'累计退款不能超过该订单行收入；收入未知时请先同步金额')
        return s.put(c,'aftersale',key,{'order_id':order['id'],'external_order_id':order['external_id'],'item_id':item['external_item_id'],
          'sku_id':item['sku_id'],'environment':order['environment'],'type':kind,'quantity':count,'amount':amount,
          'currency':order['currency'],'reason':str(payload.get('reason',''))[:2000],'status':'open','platform_reference':'','execution':'manual'})


def resolve_aftersale(ident,payload):
    with s.transaction() as c:
        case=s.get(c,'aftersale',ident)
        if case.get('execution')=='platform':raise HTTPException(422,'平台售后必须提交并核对平台结果，不能用手工登记关闭')
        if case['status']=='resolved':return case
        ref=str(payload.get('platform_reference','')).strip()
        if not ref and case['environment']!='mock':raise HTTPException(422,'请填写平台处理记录或实际办理依据')
        if payload.get('restock'):
            if case['type']!='return':raise HTTPException(422,'仅已验收退货可以入库')
            if not case['sku_id']:raise HTTPException(422,'请先关联实际SKU后入库')
            s.move_stock(c,case['sku_id'],'return:'+str(ident),on_hand=case['quantity'],reason='退货验收可再售入库',environment=case['environment'])
        if case['type']=='replacement':
            order=s.get(c,'order',case['order_id'])
            original=next(x for x in order['items'] if x['external_item_id']==case['item_id'])
            if not original['sku_id']:raise HTTPException(422,'请先关联补发SKU')
            item=copy.deepcopy(original);item.update(quantity=case['quantity'],reserved=0,shipped=0,production_id=None,price='0.00',line_total='0.00')
            replacement=s.put(c,'order','replacement:'+str(ident),{'external_id':order['external_id']+'-R'+str(ident),
              'target_id':order['target_id'],'environment':order['environment'],'currency':order['currency'],
              'platform_status':'ready','status':'ready','items':[item],'recipient':copy.deepcopy(order.get('recipient',{})),
              'deadline':'','ordered_at':datetime.now(timezone.utc).isoformat(),'sync_issues':[],
              'replacement_of':order['id'],'aftersale_id':ident,'fulfillment_mode':'manual_replacement'})
            case['replacement_order_id']=replacement['id']
            notify(c,'replacement:'+str(ident),'补发单已创建，请安排备货和实际物流',replacement['id'])
        if Decimal(case['amount'])>0:
            s.put(c,'expense','aftersale:'+str(ident),{'order_id':case['order_id'],'environment':case['environment'],
              'type':'refund','amount':case['amount'],'currency':case['currency'],'reference':ref,'source':'manual_aftersale'})
        case.update(status='resolved',platform_reference=ref,restocked=bool(payload.get('restock')))
        return s.save(c,'aftersale',case)


def finance_summary(c):
    groups={}
    def group(env,currency):
        return groups.setdefault((env,currency),{'environment':env,'currency':currency,'revenue':Decimal(0),'cost':Decimal(0),'expenses':Decimal(0),'unknown_cost_orders':set(),'unknown_revenue_orders':set(),'orders':0})
    orders=s.all_rows(c,'order')
    for order in orders:
        if order['status']=='cancelled':continue
        g=group(order['environment'],order['currency']);g['orders']+=1
        for item in order['items']:
            if item.get('line_total') is not None:g['revenue']+=Decimal(item['line_total'])
            elif item.get('price') is not None:g['revenue']+=Decimal(item['price'])*item['quantity']
            else:g['unknown_revenue_orders'].add(order['id'])
            sku=item['snapshot'].get('sku',{})
            if sku.get('cost') is None or sku.get('cost_currency')!=order['currency']:g['unknown_cost_orders'].add(order['id'])
            else:g['cost']+=Decimal(sku['cost'])*item['quantity']
    for expense in s.all_rows(c,'expense'):
        g=group(expense['environment'],expense['currency']);g['expenses']+=Decimal(expense['amount'])
    results=[]
    for g in groups.values():
        g['unknown_cost_orders']=len(g['unknown_cost_orders']);g['unknown_revenue_orders']=len(g['unknown_revenue_orders']);g['estimated_contribution']=None if g['unknown_cost_orders'] or g['unknown_revenue_orders'] else str(g['revenue']-g['cost']-g['expenses'])
        for f in ['revenue','cost','expenses']:g[f]=str(g[f])
        g['status']='预估，平台费用与实际运费需对账';results.append(g)
    return results
