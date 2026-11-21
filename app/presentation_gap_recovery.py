"""Explicit replacement of an unrecoverable returned image, preserving its unknown receipt."""
import copy
import time

from fastapi import HTTPException

from app import simple_design_flow as flow, simple_design_prompts as prompts, design_contract
from app.commerce import store as s

RETURNED_WITHOUT_ARTIFACT = '图片请求已返回，但回执或图片损坏；结果未知，已暂停以避免重复生成'
REMOTE_EVIDENCE_KEYS = {'unknown', 'input_references', 'source_references',
                        'requested_model', 'actual_image_model', 'image_receipt', 'reference_policy',
                        'render_request', 'outbound_requests'}
PROTOCOL = 'explicit-missing-image-replacement-v4'
RETRYABLE_HTTP_ERRORS = {f'模型服务HTTP {code}' for code in (425, 429, 500, 502, 503)}


def _confirmed_retry(c, task):
    """A sibling may be waiting after a definite HTTP failure when the run pauses."""
    if (task['kind'] not in {'master', 'image'} or task['status'] != 'retry_wait'
            or task['error'] not in RETRYABLE_HTTP_ERRORS or task['remote'].get('unknown')
            or set(task['remote']) - REMOTE_EVIDENCE_KEYS or task['result']):
        return False
    last = c.execute('SELECT status,error FROM design_simple_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1',
                     (task['id'],)).fetchone()
    return bool(last and last['status'] == 'failed' and last['error'] == task['error'])


def _replacement_route(parent, child, missing):
    # A new request is explicitly acknowledged; the old unknown result stays unknown.
    # Carry the failed provider forward instead of resetting every replacement to it.
    failed = set()
    for task in missing:
        models = task['models']
        if not models or not 0 <= task['model_role'] < len(models):
            raise HTTPException(409, '缺少原模型调用身份，不能自动选择备用')
        failed.add(models[task['model_role']]['id'])
    from app.model_retry import ModelFailure
    try:active = {m['id']: m for m in flow.transport.candidates(s.main(), True)}
    except ModelFailure as exc:raise HTTPException(409,str(exc)) from exc
    routes = {}
    for run in [parent, child] if child else [parent]:
        models = run['snapshot'].get('_model_candidates', {}).get('image', [])
        candidates = [m for m in models if (design_contract.active(run['snapshot']) or m['id'] not in failed) and m['id'] in active
                      and all(m.get(k) == active[m['id']].get(k)
                              for k in ('model_identifier', 'protocol', 'base_url', 'image_edit_endpoint'))]
        if not candidates:
            raise HTTPException(409, '本次已配置的主备图片模型没有可继续的备用；原结果和成品保留，请检查模型服务配置')
        routes[run['id']] = candidates
    return routes


def _eligible(c, pid, rid):
    parent = flow.obj(c.execute('SELECT * FROM design_simple_runs WHERE id=? AND project_id=?', (rid, pid)).fetchone())
    latest = c.execute("SELECT id FROM design_simple_runs WHERE project_id=? AND mode NOT IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if not parent or not latest or latest[0] != rid or parent['status'] != 'paused' or parent['stage'] not in {'masters', 'images', 'detail'}:
        raise HTTPException(409, '仅可为当前已暂停的母稿、图库或详情图片任务补建缺图请求')
    child = flow.obj(c.execute('SELECT * FROM design_simple_runs WHERE parent_run_id=?', (rid,)).fetchone())
    latest_child = c.execute("SELECT id FROM design_simple_runs WHERE project_id=? AND mode IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if parent['stage'] == 'detail':
        if not child or not latest_child or latest_child[0] != child['id'] or child['status'] != 'paused' or child['stage'] != 'images':
            raise HTTPException(409, '详情尚未到可保留成品并补缺图的状态')
    elif child:
        raise HTTPException(409, '图库与详情阶段不一致，请先核对原任务')
    runs = [parent, child] if child else [parent]
    current = flow.snapshot(flow.project(pid, c))
    for run in runs:
        if (run['workflow'] != flow.WORKFLOW or not design_contract.active(run['snapshot'])
                or not flow.same_design_inputs(run['snapshot'], current)):
            raise HTTPException(409, '商品输入或设计版本已变化，请先核对；已有图片和原请求保留')
    groups = [[flow.obj(t) for t in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=? ORDER BY id', (run['id'],))]
              for run in runs]
    from app.presentation_repair import intact
    missing = []
    unstarted = []
    for run, tasks in zip(runs, groups):
        image_kind = 'master' if run['stage'] == 'masters' else 'image'
        images = [t for t in tasks if t['kind'] == image_kind]
        expected = ({p['key'] for p in run['recognition'].get('products', [])} if image_kind == 'master'
                    else {p['page_key'] for p in run['pages']})
        if (not images or len(images) != len(expected) or
                {t['task_key'] for t in images} != expected or
                (image_kind == 'image' and len(images) != run['expected_count'])):
            raise HTTPException(409, '图片计划与记录不完整，不能只补缺图')
        for task in tasks:
            if task['status'] == 'running' or task['lease'] or task['lease_until'] > time.time():
                raise HTTPException(409, '仍有请求在执行，请等待原请求结束')
            if task['status'] == 'completed':
                if task['kind'] in {'master', 'image'} and not intact(task, pid):
                    raise HTTPException(409, '已有成品文件或来源凭据不完整，请先核对')
                continue
            remote = task['remote']
            if remote.get('render_request'):
                try:design_contract.spec.verify_request(remote['render_request'],task['prompt'],remote.get('input_references',[]))
                except ValueError as exc:raise HTTPException(409,'原请求内容或附件记录已变化，请先核对') from exc
            if remote.get('image_receipt', {}).get('recoverable_handle_present'):
                raise HTTPException(409, '原返回含任务编号或图片线索，需先恢复原结果，不能直接补建请求')
            if run['stage'] in {'masters', 'images'} and _confirmed_retry(c, task):
                missing.append(task)
                continue
            if (run['stage'] in {'masters', 'images'} and task['kind'] == image_kind and task['status'] == 'queued'
                    and not task['attempts'] and not task['total_attempts'] and not remote
                    and not task['result'] and not task['error']
                    and not c.execute('SELECT 1 FROM design_simple_attempts WHERE task_id=?', (task['id'],)).fetchone()):
                unstarted.append(task)
                continue
            if (run['stage'] not in {'masters', 'images'} or task['kind'] != image_kind or task['status'] != 'paused'
                    or task['error'] != RETURNED_WITHOUT_ARTIFACT or remote.get('unknown') is not True
                    or set(remote) - REMOTE_EVIDENCE_KEYS or task['result']):
                raise HTTPException(409, '原请求可能仍可查询或恢复，不能直接补建请求；请先核对原任务')
            missing.append(task)
    if not missing:
        raise HTTPException(409, '没有符合条件的缺失图片')
    routes = _replacement_route(parent, child, missing)
    return parent, child, groups, missing, unstarted, routes


def preview(pid, rid):
    flow.ensure_loaded_pipeline()
    with s.main().db() as c:
        try:
            parent, child, groups, missing, unstarted, routes = _eligible(c, pid, rid)
        except HTTPException as exc:
            return {'available': False, 'reason': exc.detail}
    names = list(dict.fromkeys(m['name'] for candidates in routes.values() for m in candidates))
    modern=design_contract.active(parent['snapshot'])
    return {'available': True, 'source_run_id': parent['id'], 'detail_run_id': child['id'] if child else None,
            'task_ids': [t['id'] for t in missing], 'missing_count': len(missing),
            'unstarted_count': len(unstarted), 'unstarted_task_ids': [t['id'] for t in unstarted],
            'preserved_master_count': sum(t['kind'] == 'master' and t['status'] == 'completed' for t in groups[0]),
            'stage': parent['stage'],
            'preserved_gallery_count': sum(t['kind'] == 'image' and t['status'] == 'completed' and
                t['payload'].get('page',{}).get('display_role')!='detail_page' for t in groups[0]),
            'preserved_detail_count': sum(t['kind'] == 'image' and t['status'] == 'completed' for t in groups[1]) if child else
                sum(t['kind'] == 'image' and t['status'] == 'completed' and
                    t['payload'].get('page',{}).get('display_role')=='detail_page' for t in groups[0]),
            'backup_models': names,
            'reason': ('原请求仍保留未知状态。确认将为缺图建立独立新请求，按配置顺序“'+'、'.join(names)+
                       '”轮换，最多三轮后暂停；可能产生新费用，已有成品和原请求记录保留。') if modern else (
                      '已返回但不可恢复的原请求仍保留未知状态。确认后由备用模型“' + '、'.join(names) +
                      '”继续缺图及尚未开始的图片，不再回到本轮出错的主模型。新请求可能产生费用；已有成品和原请求记录保留。')}


def start(pid, rid, detail_run_id, task_ids, confirmed_new_request, *, actor='', authorization='merchant_confirmation'):
    flow.ensure_loaded_pipeline()
    if confirmed_new_request is not True:
        raise HTTPException(422, '需明确确认新建缺图请求，可能产生新的生成费用')
    selected = sorted(task_ids)
    if not selected or len(set(selected)) != len(selected):
        raise HTTPException(422, '缺图任务范围无效')
    key = f'{pid}:{rid}:{detail_run_id}'
    with s.transaction() as c:
        previous = s.by_key(c, 'design_gap_recovery', key)
        if previous:
            if previous['task_ids'] != selected:
                raise HTTPException(409, '本次补图范围与已创建任务不一致')
            return previous
        parent, child, groups, missing, unstarted, routes = _eligible(c, pid, rid)
        if (child['id'] if child else None) != detail_run_id or sorted(t['id'] for t in missing) != selected:
            raise HTTPException(409, '缺失图片范围已变化，请刷新后核对')
        evidence = {'protocol': PROTOCOL, 'source_run_id': rid, 'source_detail_run_id': detail_run_id,
                    'task_ids': selected, 'unstarted_task_ids': [t['id'] for t in unstarted], 'actor': actor, 'confirmed_at': time.time(),
                    'authorization': authorization,
                    'original_result': 'preserved', 'new_request_may_be_billed': True,
                    'model_handoff': {str(run_id): [{'id': m['id'], 'name': m['name']} for m in candidates]
                                      for run_id, candidates in routes.items()}}
        new_runs = []
        for original, tasks in zip([parent, child] if child else [parent], groups):
            recognition = copy.deepcopy(original['recognition'])
            recognition.setdefault('gap_recoveries', []).append(evidence)
            snapshot = copy.deepcopy(original['snapshot'])
            # Routing metadata is added after input_hash is calculated by flow.start.
            # The source facts, prompts, pipeline identity and original snapshots stay intact.
            snapshot['_model_candidates']['image'] = copy.deepcopy(routes[original['id']])
            new_id = c.execute('''INSERT INTO design_simple_runs
                (project_id,mode,input_hash,snapshot,note,stage,recognition,pages,expected_count,workflow,parent_run_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (pid, original['mode'], original['input_hash'], prompts.dump(snapshot),
                 original['note'], original['stage'], prompts.dump(recognition), prompts.dump(original['pages']),
                 original['expected_count'], original['workflow'], new_runs[0] if new_runs else None)).lastrowid
            new_runs.append(new_id)
            for task in tasks:
                result = copy.deepcopy(task['result']) if task['status'] == 'completed' else None
                if result is not None:
                    result['reused_from'] = {'task_id': task['id'], 'run_id': original['id'],
                                           'reason': '保留完整成品及方案，仅新建缺图请求'}
                flow.add_task(c, new_id, task['kind'], task['task_key'], copy.deepcopy(task['payload']), result)
                c.execute('UPDATE design_simple_tasks SET prompt=? WHERE run_id=? AND kind=? AND task_key=?',
                          (task['prompt'], new_id, task['kind'], task['task_key']))
        listing_requested = any(row.get('project_id') == pid and row.get('status') in {'running', 'waiting'}
                                for row in s.all_rows(c, 'listing_workflow'))
        outcome = s.put(c, 'design_gap_recovery', key, evidence | {'project_id': pid,
                        'run_id': new_runs[0], 'detail_run_id': new_runs[1] if child else None, 'status': 'queued'})
        s.audit(c, 'design_missing_images_replaced', pid, s.dump(outcome))
    from app.commerce import listing_workflow
    if listing_requested:
        listing_workflow.request_batch(pid, 'image-gap-recovery:' + str(new_runs[0]))
    return outcome
