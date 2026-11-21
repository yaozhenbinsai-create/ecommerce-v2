"""Operator-directed repair of selected internal masters before sales art starts."""
import copy
import time

from fastapi import HTTPException

from app import simple_design_flow as flow, simple_design_prompts as prompts, design_contract
from app.commerce import store as s
from app.design_input_compatibility import completed_input_matches
from app.presentation_repair import intact


def request_record(c, pid, task):
    """Follow retained reuse provenance to the request that actually made the art."""
    current=task
    seen=set()
    while not current['prompt']:
        if current['id'] in seen:
            raise HTTPException(409,'母稿复用来源出现循环，请先核对原记录')
        seen.add(current['id'])
        origin=current['result'].get('reused_from') or {}
        source=flow.obj(c.execute("""SELECT t.* FROM design_simple_tasks t
            JOIN design_simple_runs r ON r.id=t.run_id
            WHERE t.id=? AND t.run_id=? AND r.project_id=? AND t.kind='master' AND t.status='completed'""",
            (origin.get('task_id'),origin.get('run_id'),pid)).fetchone())
        if (not source or source['payload'].get('product')!=task['payload']['product']
                or any(source['result'].get(key)!=task['result'].get(key) for key in ('url','sha256','prompt'))):
            raise HTTPException(409,'母稿原生成记录缺失或与当前引用不同，请先核对')
        current=source
    return current


def eligible(c, pid, rid):
    old = flow.obj(c.execute('SELECT * FROM design_simple_runs WHERE id=? AND project_id=?', (rid,pid)).fetchone())
    latest = c.execute("SELECT id FROM design_simple_runs WHERE project_id=? AND mode NOT IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if not old or not latest or latest[0]!=rid or old['status']!='paused' or old['stage'] not in {'masters','plans'}:
        raise HTTPException(409,'请先暂停当前母稿或方案阶段；成品开始后不能单独替换母稿')
    snapshot = flow.snapshot(flow.project(pid,c))
    if (old['workflow']!=flow.WORKFLOW or not design_contract.active(old['snapshot'])
            or not completed_input_matches(old['snapshot'],snapshot)):
        raise HTTPException(409,'商品原图、规格或设计要求已经变化，不能沿用本轮母稿')
    tasks = [flow.obj(t) for t in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=? ORDER BY id',(rid,))]
    if any(t['status']=='running' or t['lease'] or t['lease_until']>time.time() or t['remote'].get('unknown') for t in tasks):
        raise HTTPException(409,'仍有未结束或结果未知的请求，请先保留并恢复原结果')
    flow.assert_request_boundary(c,pid)
    if any(t['kind']=='image' and (t['total_attempts'] or t['result'] or t['remote']) for t in tasks):
        raise HTTPException(409,'已有成品请求，不能只换母稿导致成品引用不一致')
    masters = {t['task_key']:t for t in tasks if t['kind']=='master'}
    products = {p['key']:p for p in prompts.source_catalog(snapshot)['products']}
    if set(masters)!=set(products) or any(not intact(t,pid) for t in masters.values()):
        raise HTTPException(409,'需先完成全部母稿，并保留对应的完整文件及来源记录')
    for key,task in masters.items():
        original=request_record(c,pid,task)
        packet=original['remote'].get('render_request')
        if packet:
            try:design_contract.spec.verify_request(packet,original['prompt'],original['remote'].get('input_references',[]))
            except ValueError as exc:raise HTTPException(409,'原母稿请求或附件记录已变化，不能未经核对复用') from exc
        if (task['payload']['product']!=products[key]
                or task['result'].get('prompt')!=original['prompt']):
            raise HTTPException(409,'原母稿参数或提示词已变化，不能未经核对复用')
    snapshot['_model_candidates']=copy.deepcopy(old['snapshot']['_model_candidates'])
    return old,masters,products,snapshot


def preview(pid,rid):
    flow.ensure_loaded_pipeline()
    with s.main().db() as c:
        try:
            _,masters,products,_=eligible(c,pid,rid)
        except (HTTPException,OSError,KeyError) as exc:
            return {'available':False,'reason':getattr(exc,'detail','母稿文件或来源记录不完整')}
    return {'available':True,'masters':[{'page_key':key,'sha256':task['result']['sha256'],
            'url':task['result']['url'],'name':products[key]['name']} for key,task in masters.items()],
            'reason':'只重新生成勾选的内部母稿，其余母稿原样保留；后续方案重新围绕当前母稿编排，原任务和图片留作历史。'}


def start(pid,rid,candidates,instruction,*,actor=''):
    flow.ensure_loaded_pipeline()
    instruction=instruction.strip()
    selected={x['page_key']:x['sha256'] for x in candidates}
    if not selected or len(selected)!=len(candidates) or not 8<=len(instruction)<=1200:
        raise HTTPException(422,'请选择待修母稿并填写具体事实和修改位置')
    identity=prompts.fingerprint({'pid':pid,'rid':rid,'selected':selected,'instruction':instruction})
    with s.transaction() as c:
        previous=s.by_key(c,'master_repair',identity)
        if previous:return previous
        old,masters,products,snapshot=eligible(c,pid,rid)
        if any(key not in masters or masters[key]['result']['sha256']!=digest for key,digest in selected.items()):
            raise HTTPException(409,'所选母稿与当前版本不同，请刷新后核对')
        evidence={'source_run_id':rid,'selected':selected,'instruction':instruction,'actor':actor,'at':time.time()}
        recognition=prompts.source_catalog(snapshot)
        recognition['master_repairs']=copy.deepcopy(old['recognition'].get('master_repairs',[]))+[evidence]
        pages=design_contract.assignments(recognition['products'],snapshot)
        # Routing is not part of the visual-input fingerprint.
        digest=prompts.fingerprint({k:v for k,v in snapshot.items() if k!='_model_candidates'})
        new_id=c.execute('''INSERT INTO design_simple_runs
            (project_id,mode,input_hash,snapshot,note,stage,recognition,pages,expected_count,workflow)
            VALUES(?,?,?,?,?,'masters',?,?,?,?)''',
            (pid,'master_repair',digest,prompts.dump(snapshot),old['note'],prompts.dump(recognition),
             prompts.dump(pages),len(pages),flow.WORKFLOW)).lastrowid
        for key,task in masters.items():
            payload=copy.deepcopy(task['payload'])
            if key in selected:
                payload['master_repair']={'url':task['result']['url'],'sha256':task['result']['sha256'],
                    'task_id':task['id'],'run_id':rid,'instruction':instruction}
                result=None
            else:
                result=copy.deepcopy(task['result'])
                result['reused_from']={'task_id':task['id'],'run_id':rid,'reason':'仅返修所选母稿，其余引用及字节保留'}
            flow.add_task(c,new_id,'master',key,payload,result)
        outcome=s.put(c,'master_repair',identity,evidence|{'project_id':pid,'run_id':new_id,
            'preserved_count':len(masters)-len(selected),'new_master_count':len(selected),'status':'queued'})
        s.audit(c,'selected_masters_repaired',pid,s.dump(outcome))
    # Existing listing workflows discover the newest compatible run themselves.
    # A design-only repair never creates an Amazon workflow.
    return outcome
