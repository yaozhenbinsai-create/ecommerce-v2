"""Platform boundary for normalized orders and shipping confirmations."""
from decimal import Decimal
from fastapi import HTTPException
from . import amazon, store as s


class AmazonOrders:
    shipping_template_required=True

    def shipping_templates(self,target,product_type):
        from .shipping import options,requirement
        # New listings with offer details use LISTING, not LISTING_OFFER (which
        # is not a Product Type Definitions requirements value).
        definition=amazon.definition(target,product_type,requirements='LISTING')
        descriptor=definition.get('descriptor') or {}
        if definition.get('sample_only'):raise HTTPException(422,'不能将沙箱样例作为店铺配送模板')
        if descriptor.get('productType',product_type)!=product_type:raise HTTPException(422,'平台返回的产品类型不匹配')
        if descriptor.get('marketplaceIds') and target['marketplace_id'] not in descriptor['marketplaceIds']:
            raise HTTPException(422,'平台返回的站点不匹配')
        schema=definition.get('schema')
        return {'templates':options(schema),'requirement':requirement(schema),
                'request_id':definition.get('request_id','')}

    def normalize(self,target,raw):
        market=raw.get('salesChannel',{}).get('marketplaceId')
        if market and market!=target['marketplace_id']:raise HTTPException(409,'平台返回的订单地区不匹配')
        f=raw.get('fulfillment',{})
        state={'UNSHIPPED':'ready','PARTIALLY_SHIPPED':'partially_shipped','SHIPPED':'shipped','CANCELLED':'cancelled'}.get(f.get('fulfillmentStatus'),'pending')
        if f.get('fulfilledBy')!='MERCHANT':state='platform_fulfilled'
        lines=[];currency=target.get('currency','USD')
        for item in raw.get('orderItems',[]):
            count=s.quantity(item['quantityOrdered'],1);total=item.get('proceeds',{}).get('proceedsTotal',{})
            amount=total.get('amount');currency=total.get('currencyCode',currency)
            lines.append({'external_item_id':item['orderItemId'],'seller_sku':item.get('product',{}).get('sellerSku',''),
                'name':item.get('product',{}).get('title',''),'quantity':count,
                'shipped':s.quantity(item.get('fulfillment',{}).get('quantityFulfilled',count if state=='shipped' else 0)),
                'price':str(Decimal(str(amount))/count) if amount is not None else None,
                'line_total':str(amount) if amount is not None else None})
        return {'external_id':raw['orderId'],'platform_status':state,'items':lines,'currency':currency,
                'recipient':raw.get('recipient',{}),'ordered_at':raw.get('createdTime',''),
                'platform_updated_at':raw.get('lastUpdatedTime',''),
                'deadline':f.get('shipByWindow',{}).get('latestDateTime',''),'packages':raw.get('packages',[]),
                'manual_shipping_supported':'FBM_SHIP_PLUS' not in raw.get('programs',[])}

    def search(self,target,since,before):
        return [self.normalize(target,raw) for raw in amazon.orders(target,since,before)]

    def get(self,target,external_id):
        order=self.normalize(target,amazon.get_order(target,external_id))
        if order['external_id']!=external_id:raise HTTPException(409,'平台返回的订单编号不匹配')
        return order

    def submit(self,target,order,ship):
        package={'packageReferenceId':ship['platform_package_id'],'carrierName':ship['carrier'],
                 'trackingNumber':ship['tracking'],'shipDate':ship['ship_date'],
                 'orderItems':[{'orderItemId':i['item_id'],'quantity':i['quantity']} for i in ship['items']]}
        if ship.get('service'):package['shippingMethod']=ship['service']
        return amazon.confirm_shipment(target,order['external_id'],package)


ADAPTERS={'亚马逊':AmazonOrders()}


def resolve(target):
    with s.main().db() as c:account=c.execute('SELECT platform,enabled FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
    if not account or not account['enabled'] or not target.get('enabled'):raise HTTPException(409,'平台账号或地区已停用')
    from .environment import require
    require(target['environment'])
    adapter=ADAPTERS.get(account['platform'])
    if not adapter:raise HTTPException(422,f"{account['platform']}尚未接入订单与物流接口")
    if target['environment']=='sandbox':raise HTTPException(422,'沙盒仅支持接口样例测试，请选择正式店铺或本地模拟')
    return adapter
