from __future__ import annotations
import json
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException


def main():
    from app import main as m
    return m


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def migrate():
    with main().db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS commerce_records (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, natural_key TEXT NOT NULL,
          data TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(kind,natural_key));
        CREATE INDEX IF NOT EXISTS commerce_kind ON commerce_records(kind,id);
        CREATE TABLE IF NOT EXISTS commerce_record_ids (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), high_water INTEGER NOT NULL);
        INSERT OR IGNORE INTO commerce_record_ids(singleton,high_water)
          SELECT 1,COALESCE(MAX(id),0) FROM commerce_records;
        CREATE TABLE IF NOT EXISTS commerce_jobs (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, dedupe TEXT NOT NULL UNIQUE,
          payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, model_role INTEGER NOT NULL DEFAULT 0,
          role_attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
          lease_until REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS commerce_jobs_due ON commerce_jobs(status,next_at,id)
          WHERE status IN ('queued','retry_wait');
        CREATE INDEX IF NOT EXISTS commerce_jobs_lease ON commerce_jobs(lease_until)
          WHERE status='running';
        CREATE INDEX IF NOT EXISTS commerce_jobs_open ON commerce_jobs(id DESC)
          WHERE status!='completed';
        CREATE INDEX IF NOT EXISTS commerce_workflow_pending
          ON commerce_jobs(json_extract(payload,'$.workflow_id'))
          WHERE kind='listing_workflow' AND status IN ('queued','running','retry_wait');
        CREATE INDEX IF NOT EXISTS commerce_jobs_listing_version
          ON commerce_jobs(kind,json_extract(payload,'$.listing_id'),json_extract(payload,'$.version'),id DESC);
        CREATE INDEX IF NOT EXISTS commerce_jobs_classification
          ON commerce_jobs(json_extract(payload,'$.product_id'),json_extract(payload,'$.target_id'),id DESC)
          WHERE kind='import_classification';
        CREATE INDEX IF NOT EXISTS commerce_active_workflows
          ON commerce_records(json_extract(data,'$.status'),id) WHERE kind='listing_workflow';
        CREATE INDEX IF NOT EXISTS commerce_workflow_design_run
          ON commerce_records(json_extract(data,'$.design_run_id')) WHERE kind='listing_workflow';
        CREATE INDEX IF NOT EXISTS commerce_sku_product
          ON commerce_records(json_extract(data,'$.product_id'),id) WHERE kind='sku';
        CREATE TABLE IF NOT EXISTS commerce_stock (
          sku_id INTEGER PRIMARY KEY REFERENCES commerce_records(id),
          on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand>=0),
          reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
          safety INTEGER NOT NULL DEFAULT 0 CHECK(safety>=0));
        CREATE TABLE IF NOT EXISTS commerce_stock_events (
          id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
          sku_id INTEGER NOT NULL REFERENCES commerce_records(id),
          on_hand_delta INTEGER NOT NULL, reserved_delta INTEGER NOT NULL,
          reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS commerce_audit (
          id INTEGER PRIMARY KEY, action TEXT NOT NULL, object_id INTEGER, detail TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS commerce_migrations (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
        INSERT OR IGNORE INTO commerce_migrations(version) VALUES(1);
        CREATE TABLE IF NOT EXISTS commerce_stock_balances (
          sku_id INTEGER NOT NULL REFERENCES commerce_records(id), environment TEXT NOT NULL,
          on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand>=0),
          reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
          safety INTEGER NOT NULL DEFAULT 0 CHECK(safety>=0), PRIMARY KEY(sku_id,environment));
        ''')
        if not c.execute('SELECT 1 FROM commerce_migrations WHERE version=2').fetchone():
            # The old ledger contains no platform environment. Preserve it until
            # its owner reconciles a verified backup; never infer production.
            pending=c.execute('SELECT COUNT(*) FROM commerce_stock WHERE on_hand!=0 OR reserved!=0 OR safety!=0').fetchone()[0]
            if pending:
                if not by_key(c,'notice','legacy-stock-environment'):
                    put(c,'notice','legacy-stock-environment',{
                        'title':f'{pending} 个 SKU 的历史库存归属待核对，原库存已保留；请备份并确认所属环境后再办理库存业务',
                        'read':False,'kind':'legacy_stock_environment'})
            else:
                c.execute('INSERT INTO commerce_migrations(version) VALUES(2)')
        columns={r[1] for r in c.execute('PRAGMA table_info(businesses)')}
        for column in ['cost_currency','sale_currency']:
            if column not in columns:c.execute(f"ALTER TABLE businesses ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        c.execute('INSERT OR IGNORE INTO commerce_migrations(version) VALUES(3)')
        from . import snapshot_clock, poll_retention
        c.executescript(poll_retention.SCHEMA)
        snapshot_clock.install(c)


@contextmanager
def transaction():
    with main().db() as c:
        c.execute('BEGIN IMMEDIATE')
        yield c


def decode(row):
    if not row:
        return None
    d = json.loads(row['data'])
    d.update(id=row['id'], revision=row['revision'], created_at=row['created_at'], updated_at=row['updated_at'])
    return d


def get(c, kind, ident, required=True):
    value = decode(c.execute('SELECT * FROM commerce_records WHERE kind=? AND id=?', (kind, ident)).fetchone())
    if value is None and kind in {'material_version', 'listing_checks'}:
        from .material_history import lookup
        value = lookup(c, kind, ident=ident)
    if value is None and required:
        raise HTTPException(404, '记录不存在')
    return value


def by_key(c, kind, key):
    value = decode(c.execute('SELECT * FROM commerce_records WHERE kind=? AND natural_key=?', (kind, str(key))).fetchone())
    if value is None and kind in {'material_version', 'listing_checks'}:
        from .material_history import lookup
        value = lookup(c, kind, key=key)
    return value


def all_rows(c, kind):
    return [decode(r) for r in c.execute('SELECT * FROM commerce_records WHERE kind=? ORDER BY id DESC', (kind,))]


def product_skus(c, product_id):
    return [decode(r) for r in c.execute("SELECT * FROM commerce_records WHERE kind='sku' "
                                       "AND json_extract(data,'$.product_id')=? ORDER BY id DESC", (product_id,))]


def put(c, kind, key, data, expected=None):
    old = by_key(c, kind, key)
    if data.get('_history_archived') or (old or {}).get('_history_archived'):
        raise HTTPException(409, '已归档的旧草稿只供历史回查，不能覆盖或重新作为工作版本')
    clean = {k: v for k, v in data.items() if k not in {'id','revision','created_at','updated_at'}}
    if old and expected is not None and old['revision'] != expected:
        raise HTTPException(409, '资料已被更新，请刷新后再保存')
    if kind == 'order':
        from .customer_pii import protect
        clean = protect(c, kind, clean)
    if old:
        if expected is not None and old['revision'] != expected:
            raise HTTPException(409, '资料已被更新，请刷新后再保存')
        c.execute('UPDATE commerce_records SET data=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?', (dump(clean),old['id']))
        return get(c,kind,old['id'])
    # Persist the high-water mark independently of removable business records.
    # Reimports must never recycle a product ID embedded in an old seller SKU.
    ident=c.execute('UPDATE commerce_record_ids SET high_water=MAX(high_water, '
                    '(SELECT COALESCE(MAX(id),0) FROM commerce_records))+1 WHERE singleton=1 '
                    'RETURNING high_water').fetchone()[0]
    c.execute('INSERT INTO commerce_records(id,kind,natural_key,data) VALUES(?,?,?,?)',(ident,kind,str(key),dump(clean)))
    return get(c,kind,ident)


def save(c,kind,data,expected=None):
    row=c.execute('SELECT natural_key FROM commerce_records WHERE kind=? AND id=?',(kind,data['id'])).fetchone()
    if not row: raise HTTPException(404,'记录不存在')
    return put(c,kind,row[0],data,expected)


def audit(c,action,ident,detail=''):
    c.execute('INSERT INTO commerce_audit(action,object_id,detail) VALUES(?,?,?)',(action,ident,str(detail)[:1000]))


def money(value, nullable=False):
    if nullable and value in (None,''): return None
    try:
        v=Decimal(str(value))
        if not v.is_finite() or v<0 or v>Decimal('999999999'): raise ValueError()
        return str(v.quantize(Decimal('0.01')))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422,'金额必须为有效非负数')


def quantity(value, minimum=0, maximum=1000000):
    if isinstance(value,bool): raise HTTPException(422,'数量必须为整数')
    try:
        n=int(value)
        if Decimal(str(value))!=n or n<minimum or n>maximum: raise ValueError()
        return n
    except (ValueError,TypeError,InvalidOperation): raise HTTPException(422,'数量必须为有效整数')


def listing_quantity(value):
    # Offer limits can exceed warehouse transaction sizes; remain JSON-safe.
    return quantity(value,maximum=9007199254740991)


def stock(c,sku_id,environment='production',*,create=True):
    if environment not in {'production','sandbox','mock'}:raise HTTPException(422,'库存环境无效')
    get(c,'sku',sku_id)
    legacy=None
    if not c.execute('SELECT 1 FROM commerce_migrations WHERE version=2').fetchone():
        legacy=c.execute('SELECT on_hand,reserved,safety FROM commerce_stock WHERE sku_id=? AND (on_hand!=0 OR reserved!=0 OR safety!=0)',(sku_id,)).fetchone()
    if create and not legacy:
        c.execute('INSERT OR IGNORE INTO commerce_stock_balances(sku_id,environment) VALUES(?,?)',(sku_id,environment))
    current=c.execute('SELECT * FROM commerce_stock_balances WHERE sku_id=? AND environment=?',(sku_id,environment)).fetchone()
    row=dict(current) if current else {'sku_id':sku_id,'environment':environment,'on_hand':0,'reserved':0,'safety':0}
    row['available']=max(0,row['on_hand']-row['reserved']-row['safety'])
    row['legacy_environment_pending']=bool(legacy)
    if legacy:row['legacy_balance']=dict(legacy)
    return row


def move_stock(c,sku_id,event,on_hand=0,reserved=0,reason='',environment='production'):
    event=environment+':'+event
    if c.execute('SELECT 1 FROM commerce_stock_events WHERE event_key=?',(event,)).fetchone(): return stock(c,sku_id,environment)
    s=stock(c,sku_id,environment)
    if s['legacy_environment_pending']:
        raise HTTPException(409,'历史库存尚未确认所属环境，原记录已保留；请先备份并核对库存归属')
    if s['on_hand']+on_hand<0 or s['reserved']+reserved<0 or s['reserved']+reserved>s['on_hand']+on_hand:
        raise HTTPException(409,'库存或预留不足，请核对数量')
    c.execute('UPDATE commerce_stock_balances SET on_hand=on_hand+?,reserved=reserved+? WHERE sku_id=? AND environment=?',(on_hand,reserved,sku_id,environment))
    c.execute('INSERT INTO commerce_stock_events(event_key,sku_id,on_hand_delta,reserved_delta,reason) VALUES(?,?,?,?,?)',(event,sku_id,on_hand,reserved,reason))
    return stock(c,sku_id,environment)


def enqueue(c,kind,key,payload):
    from .environment import check_job
    check_job(c,payload)
    if kind in {'submit','availability','refresh','reconcile_listing','detail_submit','detail_check'} and payload.get('listing_id') and 'publication_cycle_id' not in payload:
        listing=get(c,'listing',payload['listing_id'],required=False)
        if listing and listing.get('publication_cycle_id'):payload={**payload,'publication_cycle_id':listing['publication_cycle_id']}
    c.execute('INSERT OR IGNORE INTO commerce_jobs(kind,dedupe,payload) VALUES(?,?,?)',(kind,key,dump(payload)))
    return job(c,c.execute('SELECT id FROM commerce_jobs WHERE dedupe=?',(key,)).fetchone()[0])


def sales_capacity(c,sku_id,environment):
    sku=get(c,'sku',sku_id)
    balance=stock(c,sku_id,environment,create=False)
    if balance['legacy_environment_pending']:return 0
    pending=[i for o in all_rows(c,'order')
             if o['environment']==environment and o['status'] not in {'cancelled','shipped','aftersale_needed'}
             for i in o['items'] if i['sku_id']==sku_id]
    if sku.get('sale_mode')=='made_to_order':
        return max(0,quantity(sku.get('production_capacity',0))-
                   sum(max(0,i['quantity']-i['shipped']) for i in pending))
    # Ingesting an order consumes its channel allocation before preparation.
    # These outstanding units already belong to customers. Prepared units are
    # in balance.reserved, so subtract only the remainder to avoid double holds.
    unprepared=sum(max(0,i['quantity']-i['shipped']-i['reserved']) for i in pending)
    return max(0,balance['available']-unprepared)


def claim_allocation(c,listing_id,sku_id,environment,count):
    count=quantity(count)
    other=sum(x['quantity'] for x in all_rows(c,'allocation') if x.get('policy')!='amazon_max' and x['sku_id']==sku_id and x['listing_id']!=listing_id and x.get('environment','production')==environment)
    if count>max(0,sales_capacity(c,sku_id,environment)-other):
        raise HTTPException(409,'发布数量超过可分配现货或已配置按单生产容量，请调整数量或补货')
    old=by_key(c,'allocation',f'{listing_id}:{sku_id}')
    put(c,'allocation',f'{listing_id}:{sku_id}',{'listing_id':listing_id,'sku_id':sku_id,'environment':environment,'quantity':count})
    return old


def job(c,ident):
    row=c.execute('SELECT * FROM commerce_jobs WHERE id=?',(ident,)).fetchone()
    if not row: raise HTTPException(404,'任务不存在')
    d=dict(row)
    for field in ['payload','result']: d[field]=json.loads(d[field])
    return d
