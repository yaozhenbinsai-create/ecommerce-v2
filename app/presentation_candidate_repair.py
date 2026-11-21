"""Explicit operator correction of paused, unpublished presentation candidates."""
import copy
import time
from fastapi import HTTPException
from app import simple_design_flow as flow, simple_design_prompts as prompts, design_contract
from app.commerce import store as s


def start(pid, rid, candidates, instruction, *, actor=''):
    flow.ensure_loaded_pipeline()
    instruction=instruction.strip()
    if not 8<=len(instruction)<=1200 or not candidates:
        raise HTTPException(422,'请选择本轮错误图片并说明已核实的事实及修改位置')
    identity=prompts.fingerprint({'project_id':pid,'source_run_id':rid,
        'candidates':sorted(candidates,key=lambda x:x['page_key']),'instruction':instruction})
    with s.transaction() as c:
        previous=s.by_key(c,'design_candidate_repair',identity)
        if previous:return previous
        old=flow.obj(c.execute('SELECT * FROM design_simple_runs WHERE id=? AND project_id=?',(rid,pid)).fetchone())
        latest=c.execute("SELECT id FROM design_simple_runs WHERE project_id=? AND mode NOT IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1",(pid,)).fetchone()
        if (not old or not latest or latest[0]!=rid or old['status']!='paused'
                or old['stage'] not in {'images','detail'}):
            raise HTTPException(409,'请先暂停当前设计出图任务，再选择本轮候选返修')
        if (old['workflow']!=flow.WORKFLOW or not design_contract.active(old['snapshot'])
                or old['snapshot'].get('pipeline')!=flow.pipeline_manifest()):
            raise HTTPException(409,'本轮设计版本已变化，不能继续原候选任务')
        snap=flow.snapshot(flow.project(pid,c))
        if prompts.fingerprint(snap)!=old['input_hash']:
            raise HTTPException(409,'本规格原图或资料已变化，请先核对')
        tasks=[flow.obj(row) for row in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=? ORDER BY id',(rid,))]
        child=c.execute('SELECT id FROM design_simple_runs WHERE parent_run_id=?',(rid,)).fetchone()
        child_tasks=[flow.obj(row) for row in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=?',(child[0],))] if child else []
        if any(t['status']=='running' or t['lease'] or t['lease_until']>time.time() or t['remote'].get('unknown') for t in tasks+child_tasks):
            raise HTTPException(409,'已发出的生成请求尚未结束，请等结果保存后返修，避免重复付费')
        if any(t['kind']=='image' and (t['total_attempts'] or t['remote'] or t['status']=='completed') for t in child_tasks):
            raise HTTPException(409,'详情图片已有生成请求；请保留当前成品，完成后使用单张重做，不重复整页生成')
        selected={v['page_key']:v['sha256'] for v in candidates}
        if len(selected)!=len(candidates):raise HTTPException(422,'选择的图片重复')
        images={t['task_key']:t for t in tasks if t['kind']=='image' and t['status']=='completed'}
        from app.presentation_repair import intact
        if any(key not in images or images[key]['result'].get('sha256')!=digest or not intact(images[key],pid)
               for key,digest in selected.items()):
            raise HTTPException(409,'所选候选已变化或文件凭据不完整，请刷新后核对')
        if any(t['kind'] in {'master','image'} and t['status']=='completed' and not intact(t,pid) for t in tasks):
            raise HTTPException(409,'已有图片文件或输入记录不完整，保留原任务并核对')
        if any(t['status']!='completed' and (t['total_attempts'] or t['remote']) for t in tasks):
            raise HTTPException(409,'有已尝试但尚未完成的请求，请先核对该请求结果再返修')
        flow.assert_request_boundary(c,pid)
        recognition=copy.deepcopy(old['recognition']);proof=recognition.get('presentation_repair')
        if old['mode']=='presentation_repair' and not proof:raise HTTPException(409,'缺少原资料修复凭据')
        if not proof:
            proof=recognition.setdefault('design_candidate_repair',{'source_run_id':rid,'correction_sku_ids':[]})
        base_note=recognition.setdefault('candidate_repair_base_note',old['note'])
        pages=copy.deepcopy(old['pages'])
        products={p['key']:p for p in recognition['products']}
        for page in pages:
            key=page['page_key']
            if key in selected:
                task=images[key]
                page['repair_reference']={'url':task['result']['url'],'sha256':task['result']['sha256'],
                    'task_id':task['id'],'run_id':rid,'instruction':instruction}
                page['purpose']=page.pop('repair_base_purpose',page['purpose'])
            # Unselected pending pages keep their own correction and recorded cover policy.
        proof['correction']=instruction
        proof.setdefault('candidate_corrections',[]).append({'source_run_id':rid,'candidates':candidates,
            'instruction':instruction,'actor':actor})
        new_id=c.execute("""INSERT INTO design_simple_runs(project_id,mode,input_hash,snapshot,note,stage,recognition,pages,expected_count,workflow)
            VALUES(?,?,?,?,?,'plans',?,?,?,?)""",(pid,'presentation_repair',old['input_hash'],prompts.dump(old['snapshot']),
            base_note,prompts.dump(recognition),prompts.dump(pages),old['expected_count'],flow.WORKFLOW)).lastrowid
        if not recognition.get('presentation_repair'):
            c.execute("UPDATE design_simple_runs SET mode='candidate_repair' WHERE id=?",(new_id,))
        by_page={p['page_key']:p for p in pages}
        for task in tasks:
            if task['kind']=='image' and (task['task_key'] in selected or task['status']!='completed'):continue
            payload=copy.deepcopy(task['payload'])
            if task['kind']=='plan':payload['page']=by_page[task['task_key']]
            result=copy.deepcopy(task['result']) if task['status']=='completed' and not (task['kind']=='plan' and task['task_key'] in selected) else None
            if result and task['kind'] in {'master','image'}:
                result['reused_from']={'task_id':task['id'],'run_id':rid,'reason':'本轮仅返修明确选中的错误候选，其他已完成图片保留'}
            flow.add_task(c,new_id,task['kind'],task['task_key'],payload,result)
        # A single-image run may have only one plan but many copied images.
        # Derive new work from the page set, not the historical plan row count.
        planned={task['task_key'] for task in tasks if task['kind']=='plan'}
        image_tasks={task['task_key']:task for task in tasks if task['kind']=='image'}
        for page in pages:
            key=page['page_key']
            if key in planned or (key in images and key not in selected):continue
            source_plan=(image_tasks.get(key,{}).get('payload') or {}).get('plan')
            result=source_plan if key not in selected and isinstance(source_plan,dict) and source_plan else None
            flow.add_task(c,new_id,'plan',key,{'product':products[page['product_key']],'page':page},result)
        c.execute("UPDATE design_simple_runs SET status='superseded',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            ('所选候选存在已确认事实冲突，保留历史并在新版本返修：'+instruction,rid))
        if child:
            c.execute("UPDATE design_simple_runs SET status='superseded',updated_at=CURRENT_TIMESTAMP WHERE id=?",(child[0],))
        s.audit(c,'presentation_candidates_corrected',proof.get('listing_id',pid),s.dump({'source_run_id':rid,'run_id':new_id,
            'candidates':candidates,'instruction':instruction,'actor':actor}))
        listing_requested=any(row.get('project_id')==pid and row.get('status') in {'running','waiting'}
            for row in s.all_rows(c,'listing_workflow'))
        outcome=s.put(c,'design_candidate_repair',identity,{'source_run_id':rid,'run_id':new_id,
            'project_id':pid,'status':'queued','candidates':candidates,'instruction':instruction,'actor':actor})
    from app.commerce import listing_workflow
    if listing_requested:listing_workflow.request_batch(pid,'presentation-repair:'+str(new_id))
    return outcome
