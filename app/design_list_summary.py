"""Bounded, batched card reads; never load prompts or complete task payloads."""
import json
from collections import defaultdict
from fastapi import HTTPException


def read(project_ids):
    from . import simple_design_flow as flow, design_contract as contract, simple_design_prompts as prompts, detail_page
    ids=list(dict.fromkeys(project_ids))
    if not ids:return {}
    marks=','.join('?' for _ in ids)
    with flow.main().db() as c:
        projects={r['id']:dict(r) for r in c.execute(f"SELECT id,simple_current_run,output_images FROM design_projects WHERE id IN ({marks})",ids)}
        if set(projects)!=set(ids):raise HTTPException(404,'商品不存在')
        fields="id,project_id,parent_run_id,mode,stage,status,expected_count,error,created_at,updated_at,workflow,json_extract(snapshot,'$.pipeline') AS saved_pipeline,json_array_length(recognition,'$.products') AS contract_sku_count"
        runs={};details={}
        for r in c.execute(f"SELECT {fields} FROM design_simple_runs WHERE project_id IN ({marks}) AND status!='archived' ORDER BY id DESC",ids):
            row=dict(r);target=details if row['mode'] in {'detail','detail_redesign','detail_single'} else runs
            target.setdefault(row['project_id'],row)
        rids=set(r['id'] for r in runs.values())|set(r['id'] for r in details.values())|{p['simple_current_run'] for p in projects.values() if p['simple_current_run']}
        counts=defaultdict(dict);urls=defaultdict(set);detail_counts=defaultdict(int)
        if rids:
            rm=','.join('?' for _ in rids);args=tuple(rids)
            for r in c.execute(f"SELECT run_id,kind,SUM(status='completed') AS completed,COUNT(*) AS total FROM design_simple_tasks WHERE run_id IN ({rm}) GROUP BY run_id,kind",args):
                counts[r['run_id']][r['kind']]=(r['completed'],r['total'])
            for r in c.execute(f"SELECT run_id,json_extract(result,'$.url') AS url,json_extract(payload,'$.page.display_role') AS role FROM design_simple_tasks WHERE run_id IN ({rm}) AND kind='image' AND status='completed'",args):
                if r['url']:urls[r['run_id']].add(r['url'])
                if r['role']=='detail_page':detail_counts[r['run_id']]+=1
        saved={r['id']:r['section_count'] for r in c.execute(f"SELECT p.id,COALESCE(json_array_length(d.manifest,'$.sections'),0) AS section_count FROM design_projects p JOIN design_detail_pages d ON d.run_id=p.detail_current_run AND d.project_id=p.id WHERE p.id IN ({marks})",ids)}
        commerce_installed=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone()
        pending={r[0] for r in c.execute("SELECT json_extract(data,'$.project_id') FROM commerce_records WHERE kind='design_cleanup' AND json_extract(data,'$.status')!='completed'")} if commerce_installed else set()
    pipeline=flow.pipeline_manifest();result={}
    for pid in ids:
        p=projects[pid];output=json.loads(p['output_images'] or '[]')
        mapped=sum(url in urls[p['simple_current_run']] for url in output)
        run=runs.get(pid);detail=details.get(pid)
        def decorate(row,is_detail=False):
            if not row:return None
            row=dict(row);saved_pipeline=json.loads(row.pop('saved_pipeline') or 'null') or {}
            row['current_pipeline']=saved_pipeline==pipeline;row['quality_blocked']=False
            stage=row['stage'];own=counts[row['id']]
            if is_detail:
                kind={'outline':'outline','plans':'plan','images':'image','complete':'image'}[stage]
                complete,total=own.get(kind,(0,0));row.update(stage_label=detail_page.STAGES[stage],completed=complete,total=total)
            else:
                row.update(can_resume_with_current_policy=row['workflow']==flow.WORKFLOW and contract.active({'pipeline':saved_pipeline}),design_contract=saved_pipeline.get('design_contract'),retry_policy=saved_pipeline.get('retry_policy'))
                kind={'recognize':'recognize','masters':'master','plans':'plan','images':'image','detail':'image','complete':'image'}[stage]
                complete,total=own.get(kind,(0,0))
                if stage=='plans':
                    a,b=own.get('story',(0,0));complete+=a;total+=b
                label=prompts.STAGES[stage]
                if contract.active({'pipeline':{'design_contract':row['design_contract']}}):
                    label={'masters':'原图与资料 → 内部母稿','plans':'每规格7+1独立方案','images':'销售图与完整详情图','complete':'本轮生成完成'}.get(stage,label)
                row.update(stage_label=label,completed_count=own.get('image',(0,0))[0],stage_completed=complete,stage_total=total)
            row.pop('project_id',None)
            if is_detail:
                for key in ('mode','workflow','expected_count'):row.pop(key,None)
            else:row.pop('parent_run_id',None)
            return row
        sku_count=(run or {}).get('contract_sku_count',0)
        run=decorate(run);detail=decorate(detail,True)
        if run:run.pop('contract_sku_count',None)
        if detail:detail.pop('contract_sku_count',None)
        if run and contract.active({'pipeline':{'design_contract':run.get('design_contract')}}) and sku_count:
            detail={'id':run['id'],'parent_run_id':run['id'],'status':run['status'],'stage':run['stage'],'stage_label':'完整详情图','error':run['error'],'completed':detail_counts[run['id']],'total':sku_count,'quality_blocked':run['quality_blocked'],'current_pipeline':run['current_pipeline']}
        if run and run['stage']=='detail' and detail:
            run.update(stage_label='详情页设计 · '+detail['stage_label'],stage_completed=detail['completed'],stage_total=detail['total'])
            if detail['status']=='paused':run.update(status='paused',error=detail['error'],quality_blocked=run['quality_blocked'] or detail['quality_blocked'])
        result[pid]={'version':'simple-v1','current_workflow':flow.WORKFLOW,'validation_policy':contract.EXECUTION_POLICY,'project_id':pid,'run':run,'detail_run':detail,'output_count':mapped or len(output),'detail_count':saved.get(pid,0),'has_detail':pid in saved,'cleanup_pending':pid in pending,'can_export':bool(output),'can_redo_single':bool(p['simple_current_run'])}
    return result
