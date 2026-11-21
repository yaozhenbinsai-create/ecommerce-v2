"""The only design execution engine: full originals, masters, plans, images."""
from __future__ import annotations
import io
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from urllib.parse import unquote
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel,Field
from PIL import Image
from app import simple_design_prompts as prompts
from app import design_transport as transport
from app import design_quality as quality
from app import design_contract as contract
from app import design_creative as creative
from app.model_retry import ModelFailure,decide

LOG=logging.getLogger(__name__)
STARTED=False
STOP=threading.Event()
WORKFLOW='archive-story-v7'
IDENTITY_EVIDENCE_PROTOCOL=quality.IDENTITY_EVIDENCE_PROTOCOL
IDENTITY_ANCHORS=quality.IDENTITY_ANCHORS


def _pipeline_file_hashes():
    from pathlib import Path
    paths = ('simple_design_flow.py', 'simple_design_prompts.py', 'detail_page.py',
              'design_transport.py', 'design_quality.py', 'model_retry.py', 'codex_bridge.py', 'commerce/product_archive.py',
              'presentation_repair.py', 'presentation_candidate_repair.py', 'master_repair.py',
              'design_contract.py', 'design_inputs.py', 'design_creative.py', 'design_spec.py', 'design_cleanup.py')
    return {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in paths}


# A long-lived Python process must not label its old loaded executor with new
# hashes merely because a deployment has replaced files on disk.
_LOADED_PIPELINE_FILES = _pipeline_file_hashes()


def pipeline_manifest():
    return {'workflow':WORKFLOW, 'prompt_version':contract.PROTOCOL, 'design_contract':contract.PROTOCOL,
            'validation_policy':contract.EXECUTION_POLICY,
            'creative_policy':creative.POLICY,
            'input_policy':contract.inputs.POLICY,
            'reference_policy':contract.REFERENCE_POLICY,
            'photography_policy':contract.PHOTOGRAPHY_POLICY,
            'page_contract':contract.spec.PAGE_VERSION, 'request_contract':contract.spec.REQUEST_VERSION,
            'retry_policy':'model-chain-three-rounds-v1',
            'review_mode':'none', 'generation_protocol':'generated-assets-v1',
            'files':dict(_LOADED_PIPELINE_FILES)}


def usable_result(result):
    return quality.usable(result)


def ensure_loaded_pipeline():
    if _pipeline_file_hashes() != _LOADED_PIPELINE_FILES:
        raise HTTPException(409, '设计源码已更新但当前进程仍加载旧版；历史图片和任务保留，请完成受控服务更新后再设计')
SCHEMA='''
CREATE TABLE IF NOT EXISTS design_simple_runs (
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES design_projects(id) ON DELETE CASCADE,
 mode TEXT NOT NULL, input_hash TEXT NOT NULL, snapshot TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 stage TEXT NOT NULL DEFAULT 'masters', status TEXT NOT NULL DEFAULT 'queued',
 workflow TEXT NOT NULL DEFAULT 'legacy-recognition',
 recognition TEXT NOT NULL DEFAULT '{}', pages TEXT NOT NULL DEFAULT '[]', expected_count INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS design_simple_tasks (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES design_simple_runs(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, task_key TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 prompt TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}', models TEXT NOT NULL DEFAULT '[]',
 model_role INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, total_attempts INTEGER NOT NULL DEFAULT 0,
 retry_round INTEGER NOT NULL DEFAULT 1, next_retry_at REAL NOT NULL DEFAULT 0,
 lease TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0, remote TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(run_id,kind,task_key)
);
CREATE TABLE IF NOT EXISTS design_simple_attempts (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES design_simple_tasks(id) ON DELETE CASCADE,
 model_name TEXT NOT NULL, role INTEGER NOT NULL, round INTEGER NOT NULL, attempt INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'running', error TEXT NOT NULL DEFAULT '', elapsed REAL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS design_simple_due ON design_simple_tasks(status,next_retry_at);
CREATE INDEX IF NOT EXISTS design_simple_run_counts ON design_simple_tasks(run_id,kind,status);
CREATE INDEX IF NOT EXISTS design_simple_project_latest ON design_simple_runs(project_id,id DESC);
CREATE INDEX IF NOT EXISTS design_simple_completed_url ON design_simple_tasks(run_id,json_extract(result,'$.url')) WHERE kind='image' AND status='completed';
'''

def main():
    from app import main as m
    return m

def obj(row):
    if row is None:return None
    d=dict(row)
    for k in ('snapshot','recognition','pages','payload','models','remote'):
        if k in d:d[k]=json.loads(d[k])
    if 'result' in d:d['result']=task_result(d)
    return d


def task_result(row):
    result=json.loads(row['result'])
    if isinstance(result,dict) and result.pop('_prompt_in_task',False) is True:
        result['prompt']=row['prompt']
    return result


def pack_task_result(result,prompt):
    if prompt and result.get('prompt')==prompt:
        result=dict(result)
        del result['prompt']
        result['_prompt_in_task']=True
    return prompts.dump(result)

def migrate(c):
    from app import detail_page
    detail_page.migrate(c)
    if 'workflow' not in {r[1] for r in c.execute('PRAGMA table_info(design_simple_runs)')}:
        c.execute("ALTER TABLE design_simple_runs ADD COLUMN workflow TEXT NOT NULL DEFAULT 'legacy-recognition'")
    # Preserve old assets/requests, but never continue their recognition/crop prompts.
    c.execute("UPDATE design_simple_runs SET status='paused',error='设计版本已更新，历史图片和任务保留；请新建当前流程，不会静默升级旧提示词' WHERE workflow!=? AND status IN ('queued','running','retry_wait')",(WORKFLOW,))
    fields={'simple_recognition':"TEXT NOT NULL DEFAULT '{}'",'simple_input_hash':"TEXT NOT NULL DEFAULT ''",
            'simple_current_run':"INTEGER",'simple_generation':"INTEGER NOT NULL DEFAULT 0"}
    columns={r[1] for r in c.execute('PRAGMA table_info(design_projects)')}
    for k,v in fields.items():
        if k not in columns:c.execute(f'ALTER TABLE design_projects ADD COLUMN {k} {v}')
    # One-time history inventory: retain original URLs and files, no legacy executor.
    c.execute('CREATE TABLE IF NOT EXISTS design_simple_history (project_id INTEGER PRIMARY KEY, images TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    c.execute("INSERT OR IGNORE INTO design_simple_history(project_id,images) SELECT id,output_images FROM design_projects")

def project(pid,c=None):
    if c is None:
        with main().db() as conn:return project(pid,conn)
    row=c.execute('SELECT * FROM design_projects WHERE id=?',(pid,)).fetchone()
    if not row:raise HTTPException(404,'商品不存在')
    from app.commerce import product_archive
    return product_archive.project_overlay(c, dict(row))

def snapshot(p):
    m=main()
    from app.commerce import product_archive
    with m.db() as c:
        archived = product_archive.snapshot_for_project(c, p)
    if archived is not None:
        archived.pop('design_readiness')
        return archived | {'pipeline': pipeline_manifest(), 'first_image':dict(contract.FIRST_IMAGE)}
    sources=json.loads(p.get('source_images') or '[]')
    if not sources:raise HTTPException(422,'请先导入商品原始图')
    for url in sources:local_path(url,source=True)
    business={}
    if p.get('business_id'):
        with m.db() as c:
            row=c.execute('SELECT content_language,visual_style FROM businesses WHERE id=?',(p['business_id'],)).fetchone()
            if row:business=dict(row)
    # Copy only product input data from historical catalogs, never legacy task/approval state.
    catalog=json.loads(p.get('product_catalog') or '{}')
    known=[{'name':x.get('name',''),**({'parameters':x['parameters']} if x.get('parameters') else {}),
            'variants':[{'name':v.get('name',''),'color':v.get('color') or v.get('label',''),
                         **({'parameters':v['parameters']} if v.get('parameters') else {})} for v in x.get('variants',[])]}
           for x in catalog.get('products',[]) if isinstance(x,dict)] if isinstance(catalog,dict) else []
    if len(known)<=1 and json.loads(p.get('product_variants') or '[]'):
        known=[]
    from app import supplier_evidence, supplier_origin
    brand=supplier_evidence.project_brand(p)
    with m.db() as c:
        origin=supplier_origin.project_origin(p,c)
    parameters=prompts.clean_brief(p['design_brief'])
    if origin.get('status')=='ready' and not re.search(r'(?im)^\s*(?:产地|原产地|原产国|origin(?:_country|_place)?)\s*[:：]',parameters):
        parameters += '\n产地: '+origin['display']
    return {'name':p['product_name'],**({'brand':brand['value']} if brand.get('value') else {}),'parameters':parameters,
            'origin':origin,
            'source_images':sources,'variants':json.loads(p.get('product_variants') or '[]'), 'pipeline': pipeline_manifest(),
            **({'known_products':known} if known else {}),
            'platform':p['platform'],'language':business.get('content_language') or ('en' if p['platform'] in {'亚马逊','Shopee','Lazada','Temu'} else 'zh'),
            'style':business.get('visual_style',''),
             'source_hashes':[file_hash(local_path(u,source=True)) for u in sources]}

def compatible_completed_input(saved, current):
    """A batch origin default updates listing facts without invalidating old art."""
    if isinstance(saved, str):
        saved = json.loads(saved)
    if same_design_inputs(saved,current):
        return True
    if saved == current:
        return True

    def visual_input(value):
        value = dict(value)
        origin = value.get('origin') or {}
        if (origin.get('source') or {}).get('kind') == 'batch_default':
            suffix = '\n产地: ' + str(origin.get('display') or '')
            parameters = str(value.get('parameters') or '')
            if not suffix.strip() or not parameters.endswith(suffix):
                return value
            value['parameters'] = parameters[:-len(suffix)]
            value.pop('origin', None)
        return value

    return visual_input(saved) == visual_input(current)


def same_design_inputs(saved,current):
    """A software policy change does not change frozen product/source inputs."""
    if isinstance(saved,str):saved=json.loads(saved)
    if not (contract.active(saved) and contract.active(current)):
        return saved==current
    ignored={'pipeline','_model_candidates','_creative_context'}
    return ({k:v for k,v in saved.items() if k not in ignored} ==
            {k:v for k,v in current.items() if k not in ignored})

def file_hash(path):
    import hashlib
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def local_path(url,source=False,pid=None):
    m=main();url=unquote(str(url)).split('?',1)[0]
    if url.startswith('/api/commerce/media/file/'):
        from app.commerce import media, store as commerce_store
        key=url.rsplit('/',1)[-1]
        prepared=media.path(key)
        if source:
            with m.db() as c:
                asset=commerce_store.by_key(c,'media',key.removesuffix('.jpg'))
            digest=(asset or {}).get('original_sha256','')
            if re.fullmatch(r'[a-f0-9]{64}',digest):
                root=(m.DATA_ROOT/'commerce_originals').resolve()
                original=(root/digest).resolve()
                if original.parent==root and original.is_file() and not original.is_symlink():
                    return original
        return prepared
    prefixes={'/uploads/':m.UPLOAD_ROOT,'/design-outputs/':m.DESIGN_OUTPUT_ROOT}
    for prefix,root in prefixes.items():
        if url.startswith(prefix):
            if source and prefix!='/uploads/':break
            path=(root/url[len(prefix):]).resolve();root=root.resolve()
            if root not in path.parents or not path.is_file():break
            if pid is not None and prefix=='/design-outputs/' and (root/str(pid)) not in path.parents:break
            return path
    raise HTTPException(422,'图片文件不存在或引用不属于本项目')

def add_task(c,rid,kind,key,payload,result=None):
    c.execute('INSERT OR IGNORE INTO design_simple_tasks(run_id,kind,task_key,payload,status,result) VALUES(?,?,?,?,?,?)',
              (rid,kind,key,prompts.dump(payload),'completed' if result else 'queued',prompts.dump(result or {})))

class StartRequest(BaseModel):
    model_config={'extra':'forbid'}
    action:str=Field(default='design',pattern='^(design|redesign|single)$')
    note:str=''
    page_key:str=Field(default='',max_length=100)
    reuse_matching_masters:bool=True


def assert_request_boundary(c, pid):
    """Creating replacement work cannot orphan any request for this project.

    Archived returned-failure receipts remain historical; their unknown marker
    does not resurrect old work. Actual running/leased tasks always count.
    """
    pending=c.execute("""SELECT 1 FROM design_simple_tasks t JOIN design_simple_runs r ON r.id=t.run_id
        WHERE r.project_id=? AND (t.status='running' OR t.lease!='' OR t.lease_until>?
          OR (r.status!='archived' AND json_extract(t.remote,'$.unknown')=1)) LIMIT 1""",(pid,time.time())).fetchone()
    if pending:
        raise HTTPException(409,'仍有未结束或结果未知的生成请求，请先保留并核对原请求，不能重复启动设计')


def same_start(c, active, payload, snap):
    if active['mode']!=payload.action or active['note']!=payload.note or not same_design_inputs(active['snapshot'],snap):
        return False
    if payload.action=='single':
        plans=[row[0] for row in c.execute("SELECT task_key FROM design_simple_tasks WHERE run_id=? AND kind='plan'",(active['id'],))]
        return plans==[payload.page_key]
    return payload.action=='redesign'


def matching_master(c,pid,product,snap):
    """Keep a verified reference board only for the exact same product inputs."""
    hashes=[file_hash(local_path(url,source=True)) for url in product.get('source_images') or snap.get('source_images') or []]
    for row in c.execute("""SELECT t.*,r.snapshot AS source_snapshot FROM design_simple_tasks t JOIN design_simple_runs r ON r.id=t.run_id
            WHERE r.project_id=? AND r.workflow=? AND r.status!='archived' AND t.kind='master' AND t.status='completed' ORDER BY t.id DESC""",(pid,WORKFLOW)):
        task=obj(row);result=task.get('result') or {}
        correction=task['payload'].get('master_repair')
        if (not contract.active(json.loads(task['source_snapshot'])) or
                task['payload'].get('product')!=product or
                result.get('generation',{}).get('source_sha256')!=hashes or not usable_result(result)):
            continue
        try:
            if file_hash(local_path(result['url'],pid=pid))!=result['sha256']:continue
        except (HTTPException,OSError,KeyError):continue
        return result|({'master_correction':correction} if correction else {})|{'reused_from':{'task_id':task['id'],'run_id':task['run_id'],
                                    'reason':'复用相同有效原图、规格事实和已保存明确修正的母稿；规则发布不重画已有母稿，保留原生成版本'}}
    return None

def start(pid:int,payload:StartRequest):
    ensure_loaded_pipeline()
    from app import detail_page
    from app.commerce import product_archive, catalog
    with main().db() as c:
        archived=product_archive.for_project(c,pid)
        from app import design_cleanup
        if design_cleanup.pending(c,pid):raise HTTPException(409,'旧设计文件尚未清理完成，请先重试清理；不会重复生成图片')
    if archived is None:
        archived=catalog.import_project(pid)
    with main().db() as c:
        archived=product_archive.for_project(c,pid)
        if archived is None:raise HTTPException(422,'请先建立商品档案再开始设计')
        selection=c.execute("SELECT data FROM commerce_records WHERE kind='design_selection' AND natural_key=?",(str(pid),)).fetchone()
        sku_ids=json.loads(selection['data']).get('sku_ids') if selection else None
        product_archive.selected_skus(c,archived,sku_ids)
    m=main();p=project(pid);snap=snapshot(p);digest=prompts.fingerprint(snap)
    if snap.get('archive_product_id'):
        snap['_model_candidates']={'image':transport.candidates(m,True),'text':transport.candidates(m,False)}
    with m.db() as c:
        c.execute('BEGIN IMMEDIATE')
        active=c.execute("SELECT * FROM design_simple_runs WHERE project_id=? AND mode NOT IN ('detail','detail_redesign','detail_single') AND status IN ('queued','running','retry_wait','paused') ORDER BY id DESC LIMIT 1",(pid,)).fetchone()
        if active and payload.action=='design':
            if active['workflow']!=WORKFLOW:raise HTTPException(409,'设计流程已升级，请整组重新设计')
            if not same_design_inputs(active['snapshot'],snap):raise HTTPException(409,'商品资料已更新，请重新设计')
            return status(pid)
        if active and active['status'] in {'queued','running','retry_wait'}:
            if same_start(c,active,payload,snap):return status(pid)
        assert_request_boundary(c,pid)
        if active:
            c.execute("UPDATE design_simple_runs SET status='superseded',updated_at=CURRENT_TIMESTAMP WHERE id=?",(active['id'],))
        snap['_creative_context'] = creative.freeze(c,pid)
        c.execute("UPDATE design_simple_runs SET status='superseded',updated_at=CURRENT_TIMESTAMP WHERE project_id=? AND mode IN ('detail','detail_redesign','detail_single') AND status IN ('queued','running','retry_wait','paused')", (pid,))
        cur=c.execute("INSERT INTO design_simple_runs(project_id,mode,input_hash,snapshot,note,stage,workflow) VALUES(?,?,?,?,?,'masters',?)",
                      (pid,payload.action,digest,prompts.dump(snap),payload.note,WORKFLOW));rid=cur.lastrowid
        if payload.action=='single':
            old=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=? AND project_id=? AND status=?',(p.get('simple_current_run'),pid,'completed')).fetchone())
            if not old or not same_design_inputs(old['snapshot'],snap):raise HTTPException(409,'当前图片没有可重用母稿，请整组重新设计')
            try:old['recognition']=prompts.reuse_catalog(old['recognition'],snap)
            except ValueError as exc:raise HTTPException(409,str(exc)) from exc
            target=next((v for v in old['pages'] if v['page_key']==payload.page_key),None)
            if not target:raise HTTPException(404,'图片编号不存在')
            c.execute("UPDATE design_simple_runs SET stage='plans',recognition=?,pages=?,expected_count=? WHERE id=?",(prompts.dump(old['recognition']),prompts.dump(old['pages']),old['expected_count'],rid))
            for row in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND kind IN ('master','story','image') AND status='completed'",(old['id'],)).fetchall():
                t=obj(row)
                if t['kind']=='image' and t['task_key']==payload.page_key:continue
                add_task(c,rid,t['kind'],t['task_key'],t['payload'],t['result'])
            prod=next(v for v in old['recognition']['products'] if v['key']==target['product_key'])
            add_task(c,rid,'plan',target['page_key'],{'product':prod,'page':target})
        else:
            catalog=prompts.source_catalog(snap)
            try:detail_page.validate_selection_count(catalog['products'])
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            pages=contract.assignments(catalog['products'],snap)
            c.execute('UPDATE design_simple_runs SET recognition=?,pages=?,expected_count=? WHERE id=?',
                      (prompts.dump(catalog),prompts.dump(pages),len(pages),rid))
            # Historical column name retained for commerce SKU mapping; values are source facts.
            c.execute('UPDATE design_projects SET simple_recognition=?,simple_input_hash=? WHERE id=?',
                      (prompts.dump(catalog),digest,pid))
            for prod in catalog['products']:
                result=matching_master(c,pid,prod,snap) if payload.reuse_matching_masters else None
                master_payload={'product':prod}
                if result and result.get('master_correction'):
                    master_payload['master_repair']=result['master_correction']
                add_task(c,rid,'master',prod['key'],master_payload,result)
        c.execute('UPDATE design_projects SET simple_generation=simple_generation+1 WHERE id=?',(pid,))
    return status(pid)

def pause(pid:int,rid:int):
    with main().db() as c:
        c.execute("UPDATE design_simple_runs SET status='paused',error='已暂停',updated_at=CURRENT_TIMESTAMP WHERE (id=? OR parent_run_id=?) AND project_id=? AND status IN ('queued','running','retry_wait')",(rid,rid,pid))
    return status(pid)


def untouched_replacement(c, original):
    """Recognize an unused replacement bound to this exact original run."""
    newer = c.execute("""SELECT * FROM design_simple_runs WHERE project_id=? AND id>?
        AND mode NOT IN ('detail','detail_redesign','detail_single') AND status!='archived'
        ORDER BY id LIMIT 2""", (original['project_id'], original['id'])).fetchall()
    if len(newer) != 1:
        return None
    run = obj(newer[0])
    if (run['status'] != 'paused' or run['mode'] != 'redesign' or run['note'] or
            run['workflow'] != WORKFLOW or not same_design_inputs(original['snapshot'], run['snapshot'])):
        return None
    tasks = [obj(row) for row in c.execute(
        'SELECT * FROM design_simple_tasks WHERE run_id=?', (run['id'],))]
    if c.execute('SELECT 1 FROM design_simple_runs WHERE parent_run_id=?', (run['id'],)).fetchone():
        return None
    reused = False
    for task in tasks:
        if (task['status'] not in {'queued', 'completed'} or task['attempts'] or task['total_attempts'] or task['remote'] or
                task['lease'] or task['lease_until'] or c.execute(
                    'SELECT 1 FROM design_simple_attempts WHERE task_id=?', (task['id'],)).fetchone()):
            return None
        if task['status'] == 'completed':
            provenance = task['result'].get('reused_from') or {}
            if task['kind'] != 'master' or provenance.get('run_id') != original['id']:
                return None
            prior = c.execute("""SELECT result FROM design_simple_tasks
                WHERE id=? AND run_id=? AND kind='master' AND status='completed'""",
                (provenance.get('task_id'), original['id'])).fetchone()
            saved = json.loads(prior['result']) if prior else {}
            if not saved.get('sha256') or any(saved.get(key) != task['result'].get(key)
                                              for key in ('url', 'sha256')):
                return None
            reused = True
    return run if reused else None


def resume(pid:int,rid:int):
    ensure_loaded_pipeline()
    p=project(pid);current=snapshot(p)
    with main().db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=? AND project_id=?',(rid,pid)).fetchone())
        replacement = untouched_replacement(c, r) if r and r['status'] == 'superseded' else None
        if not r or (r['status'] != 'paused' and not replacement):
            raise HTTPException(409,'该任务当前不可继续')
        if r['workflow']!=WORKFLOW:raise HTTPException(409,'设计流程已升级，请整组重新设计')
        if not contract.active(r['snapshot']):raise HTTPException(409,'旧设计执行器已停用；历史成品和记录保留')
        if not same_design_inputs(r['snapshot'],current):raise HTTPException(409,'商品资料已变化，请重新设计')
        completed=[obj(row) for row in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND kind IN ('master','image') AND status='completed'",(rid,))]
        if not files_available(c,r,completed):raise HTTPException(409,'已生成图片文件缺失或被替换；请先核对文件，不能重复生成掩盖原记录')
        run_ids=[rid]+[row[0] for row in c.execute('SELECT id FROM design_simple_runs WHERE parent_run_id=?',(rid,))]
        marks=','.join('?' for _ in run_ids)
        unknown=[obj(row) for row in c.execute(f"SELECT * FROM design_simple_tasks WHERE run_id IN ({marks}) AND status='paused' AND json_extract(remote,'$.unknown')=1",run_ids)]
        from app import image_receipts
        resolutions=[]
        for task in unknown:
            resolved=image_receipts.saved_rejection(main(),task)
            if not resolved or not task['models'] or not 0<=task['model_role']<len(task['models']):
                raise HTTPException(409,'上游任务结果尚不明确，不能重复提交；请先核对原请求，已有图片保持不变')
            resolutions.append((task,resolved))
        # Resolve every unknown before mutating any of them. Original receipt
        # files and attempt rows remain intact; a definite rejection continues
        # the ordinary finite model chain instead of submitting it blindly.
        for task,resolved in resolutions:
            remote=task['remote']|{'unknown':False,'rejection_resolution':resolved|{
                'previous_classification':'unknown','previous_error':task['error']}}
            c.execute('UPDATE design_simple_tasks SET remote=? WHERE id=?',(prompts.dump(remote),task['id']))
        if replacement:
            assert_request_boundary(c, pid)
            c.execute("UPDATE design_simple_runs SET status='archived',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      ('替代任务未执行，已继续同输入的原设计', replacement['id']))
            c.execute("UPDATE design_simple_runs SET status='paused' WHERE id=?", (rid,))
            from app.commerce import store
            store.audit(c, 'design_original_continued', rid, prompts.dump({'unused_replacement': replacement['id']}))
        pending_remote=[obj(row) for row in c.execute(f"SELECT * FROM design_simple_tasks WHERE run_id IN ({marks}) AND status='paused'",run_ids)]
        c.execute(f"UPDATE design_simple_tasks SET status='queued',attempts=0,model_role=0,retry_round=1,next_retry_at=0,error='' WHERE run_id IN ({marks}) AND status='paused'",run_ids)
        for task in pending_remote:
            if task['remote'].get('codex') or task['remote'].get('download_url') or task['remote'].get('saved_result'):
                c.execute('UPDATE design_simple_tasks SET model_role=?,attempts=?,retry_round=? WHERE id=?',(task['model_role'],task['attempts'],task['retry_round'],task['id']))
        for task,resolved in resolutions:
            decision=decide(task['model_role'],task['attempts'],len(task['models'])>1,
                            ModelFailure('原回执已确认请求被拒绝',resolved['kind']),
                            model_count=len(task['models']),retry_round=task['retry_round'])
            # An explicit continuation renews an exhausted three-round budget.
            if decision.status!='paused':
                c.execute('UPDATE design_simple_tasks SET model_role=?,retry_round=? WHERE id=?',
                          (decision.role,decision.retry_round,task['id']))
        c.execute(f"UPDATE design_simple_runs SET status='queued',error='',updated_at=CURRENT_TIMESTAMP WHERE id IN ({marks}) AND status='paused'",run_ids)
        from app.commerce import execution_controls
        execution_controls.authorize_design_continuation(c,rid)
    return status(pid)

def task_presentation(task):
    """Read task identity from its frozen SKU/page, without changing execution."""
    product=task['payload'].get('product',{})
    page=task['payload'].get('page',{})
    return {'product_key':product.get('key',page.get('product_key','')),
            'product_name':product.get('name',''),
            'page_label':'销售封面' if page.get('display_role')=='sales_cover' else page.get('type',''),
            'display_role':page.get('display_role','')}


def status(pid:int):
    from app import detail_page
    with main().db() as c:
        p=project(pid,c)
        r=obj(c.execute("SELECT * FROM design_simple_runs WHERE project_id=? AND status!='archived' AND mode NOT IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1",(pid,)).fetchone())
        tasks=[obj(x) for x in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=? ORDER BY id',(r['id'],))] if r else []
        current=p.get('simple_current_run')
        current_tasks=[obj(x) for x in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND kind='image' AND status='completed' ORDER BY id",(current,))] if current else []
    outputs=json.loads(p['output_images'] or '[]')
    current_by_url={t['result'].get('url'):t for t in current_tasks}
    ordered=[current_by_url[u] for u in outputs if u in current_by_url]
    cards=[{'page_key':t['task_key'],'url':t['result'].get('url'),'type':t['payload'].get('page',{}).get('type','成品图'),'prompt':t['result'].get('prompt',''),
            'verification':t['result'].get('verification'),'preservation':t['result'].get('preservation'),
            'sku_id':t['result'].get('sku_id'),'generation_ready':usable_result(t['result']),
            'display_role':t['payload'].get('page',{}).get('display_role',
                'platform_main' if t['payload'].get('page',{}).get('type')=='主图' else 'story'),
            'pipeline':t['result'].get('pipeline')} for t in ordered]
    if not cards:cards=[{'page_key':f'history-{i}','url':u,'type':'历史成品','prompt':''} for i,u in enumerate(outputs)]
    done=[t for t in tasks if t['kind']=='image' and t['status']=='completed']
    public_tasks=[{k:t[k] for k in ('id','kind','task_key','status','model_role','attempts','total_attempts','retry_round','error','prompt','next_retry_at')} | task_presentation(t) | {'result':t['result'],'quality_candidates':[{k:v for k,v in item.items() if k!='receipt'} for item in t['remote'].get('quality_candidates',[])],'model_name':(t['models'][t['model_role']]['name'] if len(t['models'])>t['model_role'] else '')} for t in tasks]
    run_public={k:r[k] for k in ('id','mode','stage','status','expected_count','error','created_at','updated_at','workflow')} if r else None
    if run_public:
        run_public['design_contract']=(r['snapshot'].get('pipeline') or {}).get('design_contract')
        run_public['retry_policy']=(r['snapshot'].get('pipeline') or {}).get('retry_policy')
        run_public['current_pipeline']=r['snapshot'].get('pipeline')==pipeline_manifest()
        run_public['can_resume_with_current_policy']=r['workflow']==WORKFLOW and contract.active(r['snapshot'])
        run_public['quality_blocked']=False
        kind={'recognize':'recognize','masters':'master','plans':'plan','images':'image','detail':'image','complete':'image'}[r['stage']]
        stage_tasks=[t for t in tasks if t['kind']==kind or (r['stage']=='plans' and t['kind']=='story')]
        run_public.update(stage_label=prompts.STAGES[r['stage']],completed_count=len(done),
                          stage_completed=sum(t['status']=='completed' for t in stage_tasks),stage_total=len(stage_tasks))
        if contract.active(r['snapshot']):
            run_public['stage_label']={'masters':'原图与资料 → 内部母稿','plans':'每规格7+1独立方案',
                'images':'销售图与完整详情图','complete':'本轮生成完成'}.get(r['stage'],run_public['stage_label'])
    detail_status=detail_page.status(pid)
    if run_public and r['stage']=='detail' and detail_status['run']:
        child=detail_status['run']
        run_public.update(stage_label='详情页设计 · '+child['stage_label'], stage_completed=child['completed'],stage_total=child['total'])
        if child['status']=='paused':run_public.update(status='paused',error=child['error'],quality_blocked=run_public['quality_blocked'] or child.get('quality_blocked',False))
    from app.commerce import store as commerce_store, product_archive
    main_images=[]
    with main().db() as c:
        has_commerce=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone()
        product=product_archive.for_project(c,pid) if has_commerce else None
        if product:
            for listing in commerce_store.all_rows(c,'listing'):
                if listing.get('product_id')!=product['id']:
                    continue
                target=commerce_store.get(c,'target',listing['target_id'],required=False) or {}
                current_main_run=(listing.get('draft') or {}).get('source',{}).get('design_run_id')==current
                for sku in (listing.get('draft') or {}).get('skus',[]):
                    selected=next((v for v in sku.get('images',[]) if v.get('role')=='main'),None)
                    asset=commerce_store.get(c,'media',selected['asset_id'],required=False) if selected and current_main_run else None
                    main_images.append({'target_id':listing['target_id'],'target_name':target.get('name',''),
                                        'sku_id':sku['sku_id'],'seller_sku':sku['seller_sku'],
                                        'color':sku.get('color',''),'url':asset.get('url','') if asset else '',
                                        'status':'ready' if asset else 'pending'})
    from app import design_cleanup
    with main().db() as c:cleanup_pending=design_cleanup.pending(c,pid)
    return {'version':'simple-v1','current_workflow':WORKFLOW,'validation_policy':contract.EXECUTION_POLICY,'project_id':pid,'run':run_public,'tasks':public_tasks,'outputs':cards,'cleanup_pending':cleanup_pending,
            'preview':[t['result']|{'page_key':t['task_key']} for t in done
                       if t['payload'].get('page',{}).get('display_role')!='detail_page'],
            'masters':[t['result'] for t in tasks if t['kind']=='master' and t['status']=='completed'],
            'recognition':r['recognition'] if r else json.loads(p.get('simple_recognition') or '{}'),
            'creative_direction':r['recognition'].get('creative_direction',{}) if r else {},
            'storyboard':r['pages'] if r else [],
            'detail_page':detail_status,'main_images':main_images,
            'can_export':bool(outputs),'can_redo_single':bool(current),'limits':prompts.LIMITS}


def listing_status(pid:int):
    return listing_status_many([pid])[pid]


def listing_status_many(project_ids):
    from .design_list_summary import read
    return read(project_ids)


def schedule(rid):
    m=main()
    with m.db() as c:
        c.execute('BEGIN IMMEDIATE');r=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=?',(rid,)).fetchone())
        if not r or r['status'] not in {'queued','running','retry_wait'}:return
        if r['workflow']!=WORKFLOW:return
        from app.commerce import execution_controls
        if not execution_controls.design_allowed(c,rid):
            scope=execution_controls.design_continuation_scope(c,rid)
            if scope and not execution_controls.manual_current(c,scope):
                c.execute("UPDATE design_simple_runs SET status='paused',error='本次设计已按后续暂停指令停止；已有图片保留，可明确继续未完成部分',updated_at=CURRENT_TIMESTAMP WHERE id=?",(rid,))
            return
        tasks=[obj(x) for x in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=?',(rid,))]
        from app import detail_page
        if r['mode'] in detail_page.MODES or not contract.active(r['snapshot']):
            c.execute("UPDATE design_simple_runs SET status='paused',error='旧分段设计执行器已停用；历史成品和记录保留' WHERE id=?",(rid,))
            return
        by={k:[t for t in tasks if t['kind']==k] for k in ('master','story','plan','image')}
        def complete(kind):return bool(by[kind]) and all(t['status']=='completed' for t in by[kind])
        if r['stage']=='masters' and complete('master'):
            if not files_available(c,r,by['master']):return
            for product in r['recognition']['products']:
                add_task(c,rid,'story',product['key'],{'product':product})
            c.execute("UPDATE design_simple_runs SET stage='plans',status='running' WHERE id=?",(rid,))
        elif r['stage']=='plans' and complete('story') and not by['plan']:
            pages=[];directions={}
            for story in by['story']:
                product=story['payload']['product'];bundle=story['result']
                directions[product['key']]=bundle['creative_direction']
                for page in bundle['pages']:
                    page=page|{'creative_direction':bundle['creative_direction']}
                    pages.append(page)
                    add_task(c,rid,'plan',page['page_key'],{'product':product,'page':page},bundle['plans'][page['page_key']])
            catalog=r['recognition']|{'creative_directions':directions,'creative_direction':next(iter(directions.values()))}
            c.execute('UPDATE design_simple_runs SET recognition=?,pages=? WHERE id=?',
                (prompts.dump(catalog),prompts.dump(pages),rid))
        elif r['stage']=='plans' and complete('plan'):
            plans={t['task_key']:t for t in by['plan']}
            completed={t['task_key'] for t in by['image'] if t['status']=='completed'}
            missing=[page['page_key'] for page in r['pages'] if page['page_key'] not in plans and page['page_key'] not in completed]
            if missing:
                c.execute("UPDATE design_simple_runs SET status='paused',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    ('页面缺少可执行方案，未发出新的图片请求：'+', '.join(missing),rid))
                return
            for page in r['pages']:
                plan=plans.get(page['page_key'])
                if plan:add_task(c,rid,'image',plan['task_key'],plan['payload']|{'plan':plan['result']})
            c.execute("UPDATE design_simple_runs SET stage='images',status='running' WHERE id=?",(rid,))
        elif r['stage']=='images' and complete('image') and len(by['image'])==r['expected_count']:
            if not files_available(c,r,by['image']):return
            # Re-check original inputs immediately before publishing the new current set.
            if not ready_to_publish(c,r):return
            contract.publish(c,r,tasks)

def ready_to_publish(c,r):
    try:
        if same_design_inputs(r['snapshot'],snapshot(project(r['project_id'],c))):
            return True
        error='商品资料已变化，请重新设计'
    except (HTTPException,OSError,ValueError) as exc:
        # Missing or unreadable originals must pause this run, not abort tick()
        # before the worker can service other products.
        error=str(getattr(exc,'detail',exc))
    c.execute("UPDATE design_simple_runs SET status='paused',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(error,r['id']))
    return False


def files_available(c,r,tasks):
    def accepted(task):
        result=task.get('result') or {}
        try:return bool(result.get('sha256') and file_hash(local_path(result['url'],pid=r['project_id']))==result['sha256'])
        except (HTTPException,OSError,KeyError):return False
    failures=[t for t in tasks if not accepted(t)]
    if not failures:return True
    message='已生成图片文件缺失或与保存记录不一致：'+','.join(t['task_key'] for t in failures)
    c.execute("UPDATE design_simple_runs SET status='paused',error=? WHERE id=?",(message,r['id']))
    return False

def reference_file(result,pid):
    """Bind a reused file to its saved bytes; this makes no visual quality claim."""
    try:
        path=local_path(result['url'],pid=pid)
        if not result.get('sha256') or file_hash(path)!=result['sha256']:
            raise ValueError('changed bytes')
        return path
    except (HTTPException,OSError,KeyError,ValueError) as exc:
        raise ModelFailure('参考图文件缺失或与保存记录不一致；原记录保留，请核对文件','local') from exc

def reference(r,t):
    m=main();payload=t['payload']
    if not contract.active(r['snapshot']):
        raise ModelFailure('旧设计执行器已停用；历史成品和请求保留','local')
    sources = (payload.get('product') or {}).get('source_images')
    if not sources:raise ModelFailure('本款原图缺失，不能借用其他规格图片','local')
    originals = list(dict.fromkeys(local_path(url, source=True) for url in sources))
    if t['kind'] in {'plan','image','story'}:
        with m.db() as c:
            master=obj(c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND kind='master' AND task_key=? AND status='completed'",(r['id'],payload['product']['key'])).fetchone())
        if not master:raise ModelFailure('缺少对应母稿','local')
        master_path=reference_file(master['result'],r['project_id'])
        paths=[master_path]+(originals if contract.source_guided(r['snapshot']) else [])
        roles=['master']+['source']*(len(paths)-1)
        t['_master_reference']={'task_id':master['id'],'run_id':master['run_id'],'result':master['result']}
        t['_master_correction']=master['payload'].get('master_repair') or master['result'].get('master_correction')
        repair=payload.get('page',{}).get('repair_reference')
        if repair and t['kind']=='image':
            path=reference_file(repair,r['project_id'])
            paths.append(path);roles.append('edit_target')
        t['_reference_roles']=roles
        return paths
    if t['kind']=='master':
        repair=payload.get('master_repair')
        if repair:
            path=reference_file(repair,r['project_id'])
            t['_reference_roles']=['source']*len(originals)+['edit_target']
            return originals+[path]
        t['_reference_roles']=['source']*len(originals)
        return originals
    raise ModelFailure('该设计任务类型已停用，请整组重新设计','local')


def save_image(r,t,content):
    with Image.open(io.BytesIO(content)) as im:
        im.load()
        if im.width<1 or im.height<1:raise ValueError('图片尺寸无效')
        fmt=im.format;size=list(im.size)
    extension={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}.get(fmt)
    if not extension:raise ValueError('模型返回图片格式不支持')
    folder=main().DESIGN_OUTPUT_ROOT/str(r['project_id'])/'simple'/str(r['id']);folder.mkdir(parents=True,exist_ok=True)
    path=folder/(str(t['id'])+'-'+uuid.uuid4().hex+extension);temp=folder/(uuid.uuid4().hex+'.tmp')
    temp.write_bytes(content);os.replace(temp,path)
    return {'url':f'/design-outputs/{r["project_id"]}/simple/{r["id"]}/{path.name}',
            'asset_role':('product_reference_board' if t['kind']=='master' else
                          'listing_main_candidate' if t['payload'].get('page',{}).get('type')=='主图' else 'design_image'),
            'workflow':r['workflow'],
            'size':size,'sha256':file_hash(path),'prompt':t['prompt'],'product':t['payload']['product']['name'],
            'color':t['payload']['product']['color'], 'sku_id':t['payload']['product'].get('sku_id'),
             'archive_digest':r['snapshot'].get('archive_digest'), 'pipeline':r['snapshot'].get('pipeline'),
            'execution_pipeline':pipeline_manifest(),
            'verification': {'decision':'not_performed','reason':'图片已生成'}}




def image_input_contract(remote, *, codex):
    """Keep failed tool-input receipts distinct from repairable image content."""
    candidates=[]
    if 'image_input_contract' in remote:candidates.append(remote['image_input_contract'])
    codex_remote=remote.get('codex')
    if isinstance(codex_remote,dict) and 'input_contract' in codex_remote:
        candidates.append(codex_remote['input_contract'])
    if not candidates:
        return ({'protocol':'codex-image-input-v1','decision':'uncertain',
                 'reason':'缺少实际出图工具的完整提示词与参考图输入核对证据'} if codex else None)
    for evidence in candidates:
        if not isinstance(evidence,dict):
            return {'protocol':'codex-image-input-v1','decision':'uncertain','reason':'实际出图工具输入核对证据格式不完整'}
        if evidence.get('decision')!='verified':return evidence
    return candidates[0]










def finish_candidate(r,t,result,lease,attempt_id,started):
    """Persist returned images without any quality/input-evidence approval gate."""
    error = ''
    remote = dict(t['remote']) | {'saved_result':result}
    with main().db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE design_simple_tasks SET status='completed',result=?,remote=?,error=?,lease='',lease_until=0,next_retry_at=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lease=?",
                  (pack_task_result(result,t['prompt']),prompts.dump(remote),error,t['id'],lease))
        if attempt_id:
            c.execute('UPDATE design_simple_attempts SET status=?,error=?,elapsed=? WHERE id=?',
                      ('completed',error,time.monotonic()-started,attempt_id))
        c.execute("UPDATE design_simple_runs SET error='' WHERE id=? AND status IN ('queued','running','retry_wait')",(r['id'],))

    from . import design_checkpoints
    try:design_checkpoints.discard(main(),r,t)
    except (OSError,ValueError):LOG.warning('Completed image checkpoint cleanup deferred')


def execute_one(task_id):
    m=main();lease=uuid.uuid4().hex;start_time=time.monotonic();attempt_id=None
    with m.db() as c:
        c.execute('BEGIN IMMEDIATE')
        t=obj(c.execute('SELECT * FROM design_simple_tasks WHERE id=?',(task_id,)).fetchone())
        if not t or t['status'] not in {'queued','retry_wait'} or t['next_retry_at']>time.time():return
        r=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=?',(t['run_id'],)).fetchone())
        if not r or r['status'] not in {'queued','running','retry_wait'}:return
        if r['workflow']!=WORKFLOW:return
        from app.commerce.execution_controls import design_allowed
        if not design_allowed(c,r['id']):return
        if not creative.task_ready(c,r,t):return
        c.execute("UPDATE design_simple_tasks SET status='running',lease=?,lease_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(lease,time.time()+900,task_id))
        c.execute("UPDATE design_simple_runs SET status='running',error='',updated_at=CURRENT_TIMESTAMP WHERE id=?",(r['id'],))
    def checkpoint(remote):
        t['remote'].update(remote)
        with m.db() as c:c.execute('UPDATE design_simple_tasks SET remote=? WHERE id=? AND lease=?',(prompts.dump(t['remote']),task_id,lease))
    try:
        ensure_loaded_pipeline()
        if r['snapshot'].get('pipeline') != pipeline_manifest() and not contract.active(r['snapshot']):
            raise ModelFailure('设计源码/提示词版本已变化；历史任务保留，请整组重新设计，不会静默替换旧提示词','local')
        if r['snapshot'].get('preservation_mode')!='reference_guided' or not r['snapshot'].get('archive_product_id'):
            raise ModelFailure('设计输入缺少当前商品档案和参考图设计版本；历史图片保留，请从商品档案整组重新设计','local')
        if not same_design_inputs(r['snapshot'],snapshot(project(r['project_id']))):raise ModelFailure('商品资料已变化，请重新设计','local')
        from . import design_checkpoints
        saved=t['remote'].get('saved_result') or design_checkpoints.load(m,r,t)
        if saved:
            local_path(saved['url'],pid=r['project_id'])
            finish_candidate(r,t,saved,lease,attempt_id,start_time)
            return
        if not contract.active(r['snapshot']):
            raise ModelFailure('旧设计执行器已停用；历史成品和记录保留','local')
        # Read the exact master/source inputs before constructing this request,
        # so an explicit current master correction reaches downstream prompts.
        paths=reference(r,t)
        inputs=[{'path':str(path),'sha256':file_hash(path),'role':role}
                for path,role in zip(paths,t['_reference_roles'],strict=True)]
        creative_context=None
        if not t['prompt'] and t['kind'] in {'story','plan'}:
            with m.db() as c:creative_context=creative.planning_context(c,r,t)
        t['prompt'],request=contract.compile_request(r,t,inputs,creative_context,t.get('_master_correction'))
        image=t['kind'] in {'master','image'}
        if not t['models']:
            t['models']=r['snapshot'].get('_model_candidates',{}).get('image' if image else 'text') or transport.candidates(m,image)
        model=t['models'][t['model_role']]
        checkpoint({'input_references':inputs,
                    'render_request':request,
                    'source_references':[item for item in inputs if item['role']=='source'],
                    'reference_policy':r['snapshot'].get('pipeline',{}).get('reference_policy','master-only-frozen'),
                    'requested_model':model.get('model_identifier',model['name']),
                    'actual_image_model':'unconfirmed' if image else None})
        t['attempts']+=1;t['total_attempts']+=1
        with m.db() as c:
            c.execute('UPDATE design_simple_tasks SET prompt=?,payload=?,models=?,attempts=?,total_attempts=? WHERE id=? AND lease=?',
                      (t['prompt'],prompts.dump(t['payload']),prompts.dump(t['models']),t['attempts'],t['total_attempts'],task_id,lease))
            attempt_id=c.execute('INSERT INTO design_simple_attempts(task_id,model_name,role,round,attempt) VALUES(?,?,?,?,?)',
                                 (task_id,model['name'],t['model_role'],t['retry_round'],t['attempts'])).lastrowid
        if image:
            originals=[local_path(u,source=True) for u in t['payload']['product'].get('source_images',r['snapshot']['source_images'])]
            content=transport.download(t['remote']['download_url']) if t['remote'].get('download_url') else transport.generate(m,model,t['prompt'],paths,t,checkpoint)
            result=save_image(r,t,content)
            result['generation']={'mode':'reference_guided','source_sha256':[file_hash(p) for p in originals],
                                  'reference_sha256':[file_hash(p) for p in paths],
                                  'image_transport':'codex' if m.is_codex_model(model) else 'api',
                                  'request_sha256':request['sha256'],
                                  'boundary':'按本规格有效参考与参数生成'}
            if contract.active(r['snapshot']) and t['kind']=='image':
                result['generation']['master_reference']=t['_master_reference']
            image_contract=image_input_contract(t['remote'],codex=m.is_codex_model(model))
            if image_contract is not None:result['generation']['image_input_contract']=image_contract
            result['verification']={'decision':'not_performed','reason':'图片已生成，按商家要求不做自动校验'}
            design_checkpoints.save(m,r,t,result)
            checkpoint({'saved_result':result})
            finish_candidate(r,t,result,lease,attempt_id,start_time)
            return
        else:
            raw=prompts.parse_object(transport.text(m,model,t['prompt'],paths))
            bundle=contract.normalize_bundle(raw,t['payload']['product'],r['snapshot'],
                t['payload'].get('page') if t['kind']=='plan' else None)
            result=bundle if t['kind']=='story' else bundle['plans'][t['task_key']]

        with m.db() as c:
            if t['kind']=='plan':
                c.execute('BEGIN IMMEDIATE')
                if not c.execute('SELECT 1 FROM design_simple_tasks WHERE id=? AND lease=?',(task_id,lease)).fetchone():return
                direction=bundle['creative_direction'] if creative.visual_summary(bundle['creative_direction']) else t['payload']['page'].get('creative_direction',{})
                page=bundle['pages'][0]|{'creative_direction':direction}
                payload=t['payload']|{'page':page}
                # Merge only this page into the new run. Subsequent single-page
                # work must read its current selection, not a copied old page.
                current_pages=json.loads(c.execute('SELECT pages FROM design_simple_runs WHERE id=?',(r['id'],)).fetchone()[0])
                current_pages=[page if value['page_key']==t['task_key'] else value for value in current_pages]
                c.execute('UPDATE design_simple_tasks SET payload=? WHERE id=? AND lease=?',(prompts.dump(payload),task_id,lease))
                c.execute('UPDATE design_simple_runs SET pages=? WHERE id=?',(prompts.dump(current_pages),r['id']))
            c.execute("UPDATE design_simple_tasks SET status='completed',result=?,error='',lease='',lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lease=?",(prompts.dump(result),task_id,lease))
            c.execute("UPDATE design_simple_attempts SET status='completed',elapsed=? WHERE id=?",(time.monotonic()-start_time,attempt_id))
            c.execute("UPDATE design_simple_runs SET error='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running','retry_wait')",(r['id'],))
    except Exception as exc:
        saved_after_lock=False
        if isinstance(exc,sqlite3.OperationalError) and 'locked' in str(exc).lower():
            from . import design_checkpoints
            try:saved_after_lock=bool(t['remote'].get('saved_result') or t['remote'].get('codex') or design_checkpoints.load(m,r,t))
            except (OSError,ValueError):saved_after_lock=False
        if saved_after_lock:exc=ModelFailure('数据库暂忙，继续保存或读取原图片结果，不重新出图','poll',60)
        failure=exc if isinstance(exc,ModelFailure) else ModelFailure(str(getattr(exc,'detail',exc)),
            'invalid' if attempt_id is not None and isinstance(exc,(ValueError,json.JSONDecodeError)) else 'local')
        if t['remote'].get('download_url') and failure.kind=='transient':
            failure=ModelFailure(str(failure),'poll',60)
        decision=decide(t['model_role'],t['attempts'],len(t['models'])>1,failure,
                        model_count=len(t['models']),retry_round=t['retry_round'])
        if failure.kind=='unknown':checkpoint({'unknown':True})
        with m.db() as c:
            c.execute('UPDATE design_simple_tasks SET status=?,model_role=?,attempts=?,retry_round=?,next_retry_at=?,error=?,lease=?,lease_until=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lease=?',
                      (decision.status,decision.role,decision.attempts,decision.retry_round,time.time()+decision.delay,str(failure),'',task_id,lease))
            # Never reactivate a run that the user paused/replaced while a request was in flight.
            c.execute("UPDATE design_simple_runs SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running','retry_wait')",(decision.status,str(failure),r['id']))
            if r.get('parent_run_id') and decision.status=='paused':
                c.execute("UPDATE design_simple_runs SET status='paused',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running','retry_wait')",(str(failure),r['parent_run_id']))
            if attempt_id:c.execute("UPDATE design_simple_attempts SET status='failed',error=?,elapsed=? WHERE id=?",(str(failure),time.monotonic()-start_time,attempt_id))

def recover():
    # A restart cannot tell whether a synchronous image request was accepted.
    with main().db() as c:
        for row in c.execute("SELECT * FROM design_simple_tasks WHERE status='running' AND lease_until<?",(time.time(),)).fetchall():
            t=obj(row)
            if t['kind'] in {'master','image'} and not t['remote'].get('saved_result'):
                from . import design_checkpoints
                run=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=?',(t['run_id'],)).fetchone())
                try:saved=design_checkpoints.load(main(),run,t) if run else None
                except (OSError,ValueError,KeyError):saved=None
                if saved:t['remote'].update(saved_result=saved,unknown=False)
            unknown=t['kind'] in {'master','image'} and not (t['remote'].get('download_url') or t['remote'].get('codex') or t['remote'].get('saved_result'))
            status_='paused' if unknown else 'queued'
            if unknown:t['remote']['unknown']=True
            c.execute('UPDATE design_simple_tasks SET status=?,remote=?,lease=?,lease_until=0,error=? WHERE id=?',(status_,prompts.dump(t['remote']),'','服务重启，图片提交结果未知' if unknown else '',t['id']))
            if unknown:c.execute("UPDATE design_simple_runs SET status='paused',error='服务重启，图片提交结果未知' WHERE id=? AND status IN ('queued','running','retry_wait')",(t['run_id'],))
            if unknown:c.execute("UPDATE design_simple_runs SET status='paused',error='服务重启，图片提交结果未知' WHERE id=(SELECT parent_run_id FROM design_simple_runs WHERE id=?) AND status IN ('queued','running','retry_wait')",(t['run_id'],))

def tick():
    recover()
    with main().db() as c:rids=[x[0] for x in c.execute("SELECT id FROM design_simple_runs WHERE status IN ('queued','running','retry_wait') ORDER BY id")]
    for rid in rids:schedule(rid)
    with main().db() as c:
        from app.commerce.execution_controls import design_allowed
        rows=c.execute("SELECT t.id,t.run_id FROM design_simple_tasks t JOIN design_simple_runs r ON r.id=t.run_id WHERE t.status IN ('queued','retry_wait') AND r.status IN ('queued','running','retry_wait') AND r.workflow=? AND t.next_retry_at<=? ORDER BY COALESCE(r.parent_run_id,r.id),r.id,t.id",(WORKFLOW,time.time()))
        try:row=next((row for row in rows if design_allowed(c,row['run_id'])),None)
        finally:rows.close()
    if row:execute_one(row[0]);return True
    return False

def worker():
    while not STOP.is_set():
        try:
            busy=tick()
        except Exception:
            LOG.exception('Design worker failed');busy=False
        STOP.wait(.1 if busy else 1)

def start_worker():
    global STARTED
    if STARTED:return
    STARTED=True;STOP.clear();threading.Thread(target=worker,name='simple-design-worker',daemon=True).start()

def export(pid:int):
    m=main()
    with m.db() as c:
        p=project(pid,c);urls=json.loads(p['output_images'] or '[]')
        if not urls:raise HTTPException(409,'尚无成品图片')
        rid=p.get('simple_current_run');r=obj(c.execute('SELECT * FROM design_simple_runs WHERE id=?',(rid,)).fetchone()) if rid else None
        tasks=[obj(x) for x in c.execute("SELECT * FROM design_simple_tasks WHERE run_id=? AND status='completed' AND kind IN ('master','image')",(rid,))] if rid else []
        from app import supplier_origin
        origin=supplier_origin.project_origin(p,c)
        from app.commerce import store as commerce_store, product_archive
        main_assets=[]
        has_commerce=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone()
        product=product_archive.for_project(c,pid) if has_commerce else None
        if product:
            for listing in commerce_store.all_rows(c,'listing'):
                if listing.get('product_id')!=product['id']:
                    continue
                if (listing.get('draft') or {}).get('source',{}).get('design_run_id')!=rid:
                    continue
                for sku in (listing.get('draft') or {}).get('skus',[]):
                    main_ref=next((image for image in sku.get('images',[])
                                   if image.get('role')=='main'),None)
                    asset=commerce_store.get(c,'media',main_ref['asset_id'],required=False) if main_ref else None
                    if asset:
                        main_assets.append({'target_id':listing['target_id'],
                                            'sku_id':sku['sku_id'],
                                            'seller_sku':sku['seller_sku'],
                                            'color':sku.get('color',''),
                                            'asset_id':asset['id'],'key':asset['key']})
    paths=[local_path(u,pid=pid) for u in urls]
    folder=m.PRODUCTION_MATERIAL_ROOT/str(pid);folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'design-{uuid.uuid4().hex}.zip'
    manifest={'product_name':p['product_name'],'parameters':prompts.clean_brief(p['design_brief']),
              'origin':origin,
              'format':'simple-v1','images':urls,'run_id':rid,'created_at':time.time(),
              'recognition':r['recognition'] if r else {},'pages':r['pages'] if r else [],
              'assets':[t['result']|{'key':t['task_key'],'kind':t['kind']} for t in tasks],
              'platform_main_images':[{k:v for k,v in entry.items() if k!='key'} for entry in main_assets]}
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for i,f in enumerate(paths):z.write(f,f'成品/{i+1:02d}{f.suffix}')
        for t in tasks:
            if t['kind']=='master':
                f=local_path(t['result']['url'],pid=pid);z.write(f,'母稿/'+t['task_key']+f.suffix)
        from app.commerce import media as commerce_media
        for entry in main_assets:
            image_path=commerce_media.path(entry['key'])
            z.write(image_path,f"主图/店铺{entry['target_id']}/SKU{entry['sku_id']}{image_path.suffix}")
        from app import detail_page
        detail_page.write_zip(z,detail_page.current(pid))
        z.writestr('商品图片资料.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return FileResponse(path,media_type='application/zip',filename=f'design-{pid}.zip')

def install_routes(app):
    from app import detail_page, design_cleanup
    detail_page.install_routes(app)
    design_cleanup.install_routes(app)
    app.get('/api/design-projects/{pid}/flow')(status)
    app.post('/api/design-projects/{pid}/flow',status_code=202)(start)
    app.post('/api/design-projects/{pid}/flow/{rid}/pause')(pause)
    app.post('/api/design-projects/{pid}/flow/{rid}/resume')(resume)
    app.get('/api/design-projects/{pid}/export')(export)
