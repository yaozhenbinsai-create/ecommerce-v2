"""Reference currency conversion with auditable price snapshots."""
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import copy
import xml.etree.ElementTree as ET
import httpx
from fastapi import HTTPException
from . import store as s

URL='https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'


def parse(raw):
    try:
        root=ET.fromstring(raw)
        day=next(v for v in root.iter() if 'time' in v.attrib)
        stamp=date.fromisoformat(day.attrib['time'])
        age=(datetime.now(timezone.utc).date()-stamp).days
        if not 0<=age<=7:raise ValueError()
        rates={'EUR':'1'}
        for v in day:
            code=v.attrib['currency'];rate=Decimal(v.attrib['rate'])
            if not rate.is_finite() or rate<=0 or len(code)!=3:raise ValueError()
            rates[code]=str(rate)
        if 'USD' not in rates or 'CNY' not in rates:raise ValueError()
        return {'date':stamp.isoformat(),'base':'EUR','rates':rates,'source':URL,'provider':'ECB参考汇率'}
    except (ET.ParseError,KeyError,StopIteration,ValueError,InvalidOperation):
        raise HTTPException(502,'参考汇率格式无效或超过7天，暂不换算')


def rates(refresh=False,connection=None):
    if connection is not None:cached=s.by_key(connection,'exchange_rates','ECB')
    else:
        with s.main().db() as c:cached=s.by_key(c,'exchange_rates','ECB')
    if cached and not refresh:
        age=(datetime.now(timezone.utc).date()-date.fromisoformat(cached['date'])).days
        if 0<=age<=7:return cached
    if connection is not None:
        raise HTTPException(422,'参考汇率未缓存或已过期，请先读取最新汇率；不在价格写入事务内另开连接或联网')
    try:
        r=httpx.get(URL,timeout=15);r.raise_for_status()
        if len(r.content)>100000:raise HTTPException(502,'汇率响应过大')
        result=parse(r.content)
    except httpx.HTTPError:raise HTTPException(502,'参考汇率读取失败，原售价保留，稍后可重试换算')
    with s.transaction() as c:return s.put(c,'exchange_rates','ECB',result)


def quote(sku,currency,rate_cache=None,connection=None):
    source={'source_amount':sku.get('price'),'source_currency':sku.get('currency',''),'target_currency':currency,'sku_revision':sku.get('revision'),'cost_pricing':copy.deepcopy(sku.get('cost_pricing',{}))}
    if sku.get('price') is None:return {'price':None,'price_source':source|{'status':'missing_price','message':'商品未填写售价'}}
    if not sku.get('currency'):return {'price':None,'price_source':source|{'status':'missing_currency','message':'商品售价币种未填写'}}
    if sku['currency']==currency:return {'price':s.money(sku['price']),'price_source':source|{'status':'same_currency','rate':'1'}}
    try:
        rate_cache=rate_cache if rate_cache is not None else {}
        if 'table' not in rate_cache:
            try:rate_cache['table']=rates(connection=connection) if connection is not None else rates()
            except HTTPException as exc:rate_cache['table']=exc
        table=rate_cache['table']
        if isinstance(table,HTTPException):raise table
        if sku['currency'] not in table['rates'] or currency not in table['rates']:raise HTTPException(422,'参考汇率未提供此币种')
        rate=Decimal(table['rates'][currency])/Decimal(table['rates'][sku['currency']])
        amount=(Decimal(str(sku['price']))*rate).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
        return {'price':s.money(amount),'price_source':source|{'status':'converted','rate':str(rate),'date':table['date'],'source':table['source'],'provider':table['provider']}}
    except HTTPException as exc:return {'price':None,'price_source':source|{'status':'unavailable','message':exc.detail}}

