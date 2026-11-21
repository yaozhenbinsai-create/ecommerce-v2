"""Seven-day ordinary successful reads; retain semantic transitions, never writes."""
import hashlib
import json
import zlib
from datetime import datetime, timedelta, timezone

DAYS=7
SCHEMA="""
CREATE TABLE IF NOT EXISTS commerce_retention_cursor (id INTEGER PRIMARY KEY CHECK(id=1),last_job_id INTEGER NOT NULL);
INSERT OR IGNORE INTO commerce_retention_cursor VALUES(1,0);
CREATE TABLE IF NOT EXISTS commerce_poll_summaries (
 id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL, first_job_id INTEGER NOT NULL,
 last_job_id INTEGER NOT NULL, observations INTEGER NOT NULL,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,state_sha256 TEXT NOT NULL,
 state_zlib BLOB NOT NULL,first_requests TEXT NOT NULL,last_requests TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS commerce_poll_summary_listing ON commerce_poll_summaries(listing_id,last_job_id DESC);
CREATE INDEX IF NOT EXISTS commerce_refresh_history_age ON commerce_jobs(created_at,id,updated_at)
 WHERE kind='refresh' AND status='completed';
"""

def _normal(result):
    if not isinstance(result,dict) or not result:return None
    states={};requests={}
    for sku,row in result.items():
        if not isinstance(row,dict) or row.get('status') not in {'buyable','not_buyable'} or row.get('identity_status')!='confirmed':return None
        response=row.get('response')
        if not isinstance(response,dict):return None
        body=response.get('body')
        if not isinstance(body,dict):return None
        if response.get('http_status')!=200 or not response.get('request_id') or body.get('sku')!=sku:return None
        issues=body.get('issues',[])
        if not isinstance(issues,list) or any(not isinstance(i,dict) or i.get('severity')=='ERROR' for i in issues):return None
        states[sku]={'status':row['status'],'identity_status':row['identity_status'],'confirmed_asin':row.get('confirmed_asin'),'body':body}
        requests[sku]=response['request_id']
    return states,requests

def record_summary(c,job,lid,states,requests,*,after=0):
    """Save a completed observation before removing its full polling row."""
    from . import store as s
    if c.execute('SELECT 1 FROM commerce_poll_summaries WHERE listing_id=? AND first_job_id<=? AND last_job_id>=? LIMIT 1',
                 (lid,job['id'],job['id'])).fetchone():
        return False
    text=json.dumps(states,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False)
    digest=hashlib.sha256(text.encode()).hexdigest()
    last=c.execute('SELECT * FROM commerce_poll_summaries WHERE listing_id=? AND last_job_id>? AND last_job_id<? ORDER BY last_job_id DESC LIMIT 1',
                   (lid,after,job['id'])).fetchone()
    seen=job['updated_at'];refs=s.dump(requests)
    if last and last['state_sha256']==digest:
        c.execute('UPDATE commerce_poll_summaries SET last_job_id=?,last_seen=?,observations=observations+1,last_requests=? WHERE id=?',
                  (job['id'],seen,refs,last['id']))
    else:
        c.execute('INSERT INTO commerce_poll_summaries(listing_id,first_job_id,last_job_id,observations,first_seen,last_seen,state_sha256,state_zlib,first_requests,last_requests) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (lid,job['id'],job['id'],1,seen,seen,digest,zlib.compress(text.encode()),refs,refs))
    deleted=c.execute("DELETE FROM commerce_jobs WHERE id=? AND kind='refresh' AND status='completed' AND result=? AND updated_at=?",
                      (job['id'],job['result'],seen)).rowcount
    if deleted!=1:raise RuntimeError('Read history changed during condensation')
    return True


def maintain(c,*,now=None,limit=100):
    from . import store as s
    if not 1<=limit<=200:raise ValueError('Retention batch must be bounded')
    now=now or datetime.now(timezone.utc)
    cutoff=(now-timedelta(days=DAYS)).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    tail=c.execute('SELECT id FROM commerce_jobs ORDER BY id DESC LIMIT 1 OFFSET 199').fetchone()
    if tail is None:return {'condensed':0}
    protected={r[0] for r in c.execute("SELECT j.value FROM commerce_records r,json_each(r.data,'$.jobs') j WHERE r.kind='listing_workflow'")}
    cursor=c.execute('SELECT last_job_id FROM commerce_retention_cursor WHERE id=1').fetchone()[0]
    candidates=[row[0] for row in c.execute("SELECT id FROM commerce_jobs INDEXED BY commerce_refresh_history_age WHERE kind='refresh' AND status='completed' AND created_at<? AND updated_at<? AND id<? AND id>? ORDER BY id LIMIT ?",(cutoff,cutoff,tail[0],cursor,limit))]
    marks=','.join('?' for _ in candidates)
    rows=c.execute(f'SELECT * FROM commerce_jobs WHERE id IN ({marks}) ORDER BY id',candidates).fetchall() if candidates else []
    c.execute('UPDATE commerce_retention_cursor SET last_job_id=? WHERE id=1',(rows[-1]['id'] if rows else 0,))
    changed=0
    for raw in rows:
        job=dict(raw)
        if job['id'] in protected or job['error']:continue
        try:payload=json.loads(job['payload']);normal=_normal(json.loads(job['result']))
        except (ValueError,TypeError):continue
        if normal is None or not isinstance(payload,dict) or payload.get('retirement_id'):continue
        listing=s.get(c,'listing',payload.get('listing_id'),required=False) if payload.get('listing_id') else None
        if not listing or listing.get('retirement_id'):continue
        states,requests=normal
        current={k:(r.get('response') or {}).get('request_id') for k,r in (listing.get('platform_observed') or {}).items()}
        if any(current.get(k)==v for k,v in requests.items()):continue
        changed+=record_summary(c,job,listing['id'],states,requests)
    return {'condensed':changed}

def summary(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_poll_summaries'").fetchone():
        return {'ordinary_poll_days':DAYS,'state_changes':0,'condensed_observations':0}
    row=c.execute('SELECT COUNT(*),COALESCE(SUM(observations),0) FROM commerce_poll_summaries').fetchone()
    return {'ordinary_poll_days':DAYS,'state_changes':row[0],'condensed_observations':row[1]}


def changes(c,limit=30,*,before=None):
    if not 1<=limit<=100:raise ValueError('History limit must be bounded')
    if before is not None and (type(before) is not int or before<1):raise ValueError('Invalid history cursor')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_poll_summaries'").fetchone():return []
    result=[]
    where,params=('WHERE last_job_id<?',(before,)) if before is not None else ('',())
    for raw in c.execute(f'SELECT * FROM commerce_poll_summaries {where} ORDER BY last_job_id DESC LIMIT ?',(*params,limit)):
        row=dict(raw);data=json.loads(zlib.decompress(row.pop('state_zlib')))
        row['states']=[{'sku':sku,'status':value['status'],'asin':value.get('confirmed_asin')} for sku,value in data.items()]
        for key in ('first_requests','last_requests'):row[key]=json.loads(row[key])
        result.append(row)
    return result
