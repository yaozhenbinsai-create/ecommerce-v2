"""Merchant defaults for offers; these are not warehouse stock receipts."""
from __future__ import annotations

import copy
import math
from decimal import Decimal

from fastapi import HTTPException
from jsonschema import Draft7Validator
from . import archive_fields, pricing, store as s


POLICY={'quantity_mode':'amazon_max','quantity_fallback':999,'handling_days':7,'handling_mode':'amazon_max'}


def policy(c):
    return POLICY | {k:v for k,v in (s.by_key(c,'settings','listing_defaults') or {}).items() if k in POLICY}


def resolved(node,root,instance=None,seen=()):
    if not isinstance(node,dict):return
    yield node
    ref=node.get('$ref','')
    if ref.startswith('#/') and ref not in seen:
        value=root
        try:
            for part in ref[2:].split('/'):value=value[part.replace('~1','/').replace('~0','~')]
        except (KeyError,TypeError):value={}
        yield from resolved(value,root,instance,seen+(ref,))
    for part in node.get('allOf',[]):yield from resolved(part,root,instance,seen)
    if isinstance(node.get('if'),dict) and instance is not None:
        branch='then' if Draft7Validator(node['if']).is_valid(instance) else 'else'
        yield from resolved(node.get(branch,{}),root,instance,seen)


def availability_rules(schema,field):
    """Follow the availability path only, including DEFAULT-channel conditions."""
    context={'fulfillment_availability':[{'fulfillment_channel_code':'DEFAULT'}]}
    for root in resolved(schema,schema,context):
        for availability in resolved(root.get('properties',{}).get('fulfillment_availability',{}),schema,context['fulfillment_availability']):
            for item in resolved(availability.get('items',{}),schema,context['fulfillment_availability'][0]):
                yield from resolved(item.get('properties',{}).get(field,{}),schema)


def numeric_upper(rule):
    bounds=[]
    if isinstance(rule.get('maximum'),(int,float)) and not isinstance(rule['maximum'],bool):
        bounds.append(math.floor(rule['maximum']))
    if isinstance(rule.get('exclusiveMaximum'),(int,float)) and not isinstance(rule['exclusiveMaximum'],bool):
        bounds.append(math.ceil(rule['exclusiveMaximum'])-1)
    if rule.get('enum') and all(type(v) is int for v in rule['enum']):bounds.append(max(rule['enum']))
    for keyword in ('anyOf','oneOf'):
        branches=[numeric_upper(branch) for branch in rule.get(keyword,[])]
        if branches and all(value is not None for value in branches):bounds.append(max(branches))
    bounds.extend(value for branch in rule.get('allOf',[]) if (value:=numeric_upper(branch)) is not None)
    return min(bounds) if bounds else None


def maximum_quantity(schema):
    bounds=[value for rule in availability_rules(schema,'quantity') if (value:=numeric_upper(rule)) is not None]
    return min(bounds) if bounds else None


def quantity_default(c,target,product_type):
    saved=archive_fields.definition(c,target,product_type)
    if target['environment']=='production' and not saved:
        return None,{'status':'missing','message':'正在等待当前店铺类目的官方库存规则'}
    if target['environment']=='production' and not any(availability_rules(saved.get('schema') or {},'quantity')):
        return None,{'status':'missing','message':'当前官方类目未提供数量字段，请核对履约方式'}
    config=policy(c);maximum=maximum_quantity(saved.get('schema') or {})
    value=maximum if maximum is not None else config['quantity_fallback']
    value=s.listing_quantity(value)
    source={'status':'confirmed_config','policy':'amazon_max','maximum':maximum,'fallback':config['quantity_fallback'],
            'target_id':target['id'],'schema_sha256':saved.get('schema_sha256',''),
            'request_id':saved.get('request_id',''),'message':('按 Amazon 官方数量上限' if maximum is not None else 'Amazon 未提供数量上限，使用已设置的兜底数量')}
    return value,source


def handling_default(c,target,product_type):
    config=policy(c)
    if config['handling_mode']!='amazon_max' or target['environment']!='production':
        return s.quantity(config['handling_days'],1),{'status':'confirmed_config','policy':'listing_defaults','message':'已设置的默认备货天数'}
    saved=archive_fields.definition(c,target,product_type)
    bounds=[value for rule in availability_rules((saved or {}).get('schema') or {},'lead_time_to_ship_max_days')
            if (value:=numeric_upper(rule)) is not None]
    maximum=min(bounds) if bounds else None
    if maximum is None or maximum<1:
        return None,{'status':'missing','policy':'amazon_max','message':'等待当前店铺类目的官方备货上限，未将兜底值当作最大时效'}
    return maximum,{'status':'confirmed_config','policy':'amazon_max','maximum':maximum,'target_id':target['id'],
                   'schema_sha256':saved.get('schema_sha256',''),'request_id':saved.get('request_id',''),
                   'message':'按当前店铺类目的 Amazon 官方备货天数上限'}


def apply(c,product,target,listing,draft,*,refresh_prices=False):
    previous=draft.get('handling_source') or {}
    if draft.get('handling_days') is None or previous.get('policy') in {'listing_defaults','amazon_max'} or previous.get('status')=='archive':
        draft['handling_days'],draft['handling_source']=handling_default(c,target,draft.get('product_type',''))
    value,source=quantity_default(c,target,draft.get('product_type',''))
    rates={}
    for entry in draft.get('skus',[]):
        previous=entry.get('quantity_source') or {}
        if (entry.get('quantity') is None and previous.get('status') in {None,'missing'}) or previous.get('policy')=='amazon_max':
            entry['quantity']=value;entry['quantity_source']=copy.deepcopy(source)
        if refresh_prices and entry.get('price_source',{}).get('status')!='manual':
            entry.update(pricing.quote(s.get(c,'sku',entry['sku_id']),draft['currency'],rates))


def configured_quantity(sku):
    source=sku.get('quantity_source') or {}
    return source.get('status')=='confirmed_config' and source.get('policy')=='amazon_max'


def available(c,listing,sku,target,draft):
    """Use the merchant's offer policy without inventing on-hand inventory."""
    if not configured_quantity(sku):return None
    quantity,_=quantity_default(c,target,draft['product_type'])
    if quantity is None:raise HTTPException(409,'官方库存规则已过期，请同步类目后重试')
    outstanding=sum(max(0,i['quantity']-i.get('shipped',0)) for order in s.all_rows(c,'order')
                    if order.get('target_id')==target['id'] and order['status'] not in {'cancelled','shipped','aftersale_needed'}
                    for i in order['items'] if i['sku_id']==sku['sku_id'])
    return max(0,quantity-outstanding)


def claim(c,listing,sku,target,draft,count):
    configured=available(c,listing,sku,target,draft)
    if configured is None:return s.claim_allocation(c,listing['id'],sku['sku_id'],target['environment'],count)
    count=s.listing_quantity(count)
    if Decimal(count)>configured:raise HTTPException(409,'发布数量超过当前已设置的可售数量')
    old=s.by_key(c,'allocation',f"{listing['id']}:{sku['sku_id']}")
    s.put(c,'allocation',f"{listing['id']}:{sku['sku_id']}",{'listing_id':listing['id'],'sku_id':sku['sku_id'],
          'environment':target['environment'],'quantity':count,'policy':'amazon_max','source':copy.deepcopy(sku['quantity_source'])})
    return old
