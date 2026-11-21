"""Read/export saved detail documents; delegate selected whole-image edits to the current flow."""
import io
import json

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import simple_design_flow as flow, simple_design_prompts as prompts

MODES = ('detail', 'detail_redesign', 'detail_single')
STAGES = {'outline': '整页规划', 'plans': '分段设计', 'images': '生成详情图', 'complete': '已完成'}
MAX_SECTIONS = 7
VERIFICATION_PROTOCOL = 'image-fidelity-v3'
VERIFICATION_KEYS = ('shape', 'color', 'material', 'parts', 'count', 'brand', 'copy',
                     'scene_integration', 'layout', 'readability', 'purpose', 'prop_scope')




def validate_selection_count(products):
    if not products:
        raise ValueError('详情页没有所选款式，请先选择商品档案中的 SKU')
    keys = [product.get('key') for product in products]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError('详情页所选款式缺少唯一身份，请重新核对商品档案')




def migrate(c):
    if 'parent_run_id' not in {r[1] for r in c.execute('PRAGMA table_info(design_simple_runs)')}:
        c.execute('ALTER TABLE design_simple_runs ADD COLUMN parent_run_id INTEGER REFERENCES design_simple_runs(id)')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS design_detail_parent ON design_simple_runs(parent_run_id) WHERE parent_run_id IS NOT NULL')
    if 'detail_current_run' not in {r[1] for r in c.execute('PRAGMA table_info(design_projects)')}:
        c.execute('ALTER TABLE design_projects ADD COLUMN detail_current_run INTEGER')
    c.execute('''CREATE TABLE IF NOT EXISTS design_detail_pages (
        run_id INTEGER PRIMARY KEY REFERENCES design_simple_runs(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES design_projects(id) ON DELETE CASCADE,
        manifest TEXT NOT NULL)''')


class StartRequest(BaseModel):
    action: str = Field(default='detail', pattern='^(detail|detail_redesign|detail_single)$')
    note: str = Field(default='', max_length=1200)
    page_key: str = Field(default='', max_length=100)


def current(pid, c=None):
    if c is None:
        with flow.main().db() as conn:
            return current(pid, conn)
    row = c.execute('''SELECT d.manifest FROM design_projects p JOIN design_detail_pages d
        ON d.run_id=p.detail_current_run AND d.project_id=p.id WHERE p.id=?''', (pid,)).fetchone()
    return json.loads(row[0]) if row else None


def current_matches(saved, p):
    from app.design_input_compatibility import completed_input_matches
    snap = flow.snapshot(p)
    if saved.get('input_hash') == prompts.fingerprint(snap):
        return True
    with flow.main().db() as c:
        row = c.execute('SELECT snapshot,status FROM design_simple_runs WHERE id=? AND project_id=?',
                        (saved.get('run_id'), p['id'])).fetchone()
    return bool(row and row['status'] == 'completed' and
                (flow.compatible_completed_input(row['snapshot'], snap) or
                 completed_input_matches(row['snapshot'], snap)))


def status(pid):
    with flow.main().db() as c:
        p = flow.project(pid, c)
        from app import design_contract
        latest=flow.obj(c.execute("SELECT * FROM design_simple_runs WHERE project_id=? AND status!='archived' AND mode NOT IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1",(pid,)).fetchone())
        if latest and design_contract.active(latest['snapshot']) and latest['recognition'].get('products'):
            return design_contract.detail_status(c,p,latest)
        run = flow.obj(c.execute("SELECT * FROM design_simple_runs WHERE project_id=? AND status!='archived' AND mode IN ('detail','detail_redesign','detail_single') ORDER BY id DESC LIMIT 1", (pid,)).fetchone())
        tasks = [flow.obj(t) for t in c.execute('SELECT * FROM design_simple_tasks WHERE run_id=? ORDER BY id', (run['id'],))] if run else []
        saved = current(pid, c)
        candidate_row = c.execute('SELECT manifest FROM design_detail_pages WHERE run_id=? AND project_id=?',
                                  (run['id'], pid)).fetchone() if run else None
        candidate = json.loads(candidate_row[0]) if candidate_row else None
        if candidate and section_verification(candidate.get('sections') or [])['decision'] in {'eligible','generated'}:
            candidate = None
        quality_blocked = False
    public = None
    if run:
        kind = {'outline': 'outline', 'plans': 'plan', 'images': 'image', 'complete': 'image'}[run['stage']]
        stage = [t for t in tasks if t['kind'] == kind]
        public = {k: run[k] for k in ('id', 'parent_run_id', 'stage', 'status', 'error', 'created_at', 'updated_at')}
        public.update(stage_label=STAGES[run['stage']], completed=sum(t['status'] == 'completed' for t in stage),
                      total=len(stage), quality_blocked=quality_blocked,
                      current_pipeline=run['snapshot'].get('pipeline')==flow.pipeline_manifest())
    try:
        stale = bool(saved and not current_matches(saved, p))
    except HTTPException:
        stale = bool(saved)
    return {'run': public, 'current': saved, 'candidate': candidate, 'stale': stale, 'can_start': bool(p.get('simple_current_run')),
            'preview': [t['result'] | {'page_key': t['task_key']} for t in tasks if t['kind'] == 'image' and t['status'] == 'completed'],
            'tasks': [{k: t[k] for k in ('task_key', 'kind', 'status', 'error', 'attempts', 'model_role')} for t in tasks]}


class DetailIntegrityError(HTTPException):
    """Retain the prior proof while invalidating changed/unbound task evidence."""
    def __init__(self, task, actual_sha256, reason):
        super().__init__(409, reason + '；分段保留，原有成品不替换，请重新核对或设计')
        self.task_id = task.get('id')
        result = dict(task.get('result') or {})
        previous = (result.get('integrity') or {}).get('previous_verification', result.get('verification') or {})
        result['integrity'] = {'expected_sha256': result.get('sha256'),
                               'actual_sha256': actual_sha256, 'previous_verification': previous,
                               'reason': reason}
        result['verification'] = {'protocol': VERIFICATION_PROTOCOL, 'decision': 'uncertain',
                                  'image_sha256': actual_sha256, 'checks': dict.fromkeys(VERIFICATION_KEYS, None),
                                  'reason': str(self.detail)}
        self.result = result




def intact_file(record, pid):
    try:
        return bool(record.get('sha256') and
                    flow.file_hash(flow.local_path(record['url'], pid=pid)) == record['sha256'])
    except (HTTPException, OSError, KeyError, TypeError):
        return False


def manifest_ready(saved):
    if not isinstance(saved,dict):return False
    stored=saved.get('verification') or {}
    summary=section_verification(saved.get('sections') or [])
    if summary['decision']=='generated':
        return stored.get('protocol')==flow.quality.GENERATED_PROTOCOL and stored.get('decision')=='generated'
    # Read an intact old receipt without restarting or upgrading its retired reviewer.
    historical=bool(saved.get('sections')) and all(isinstance(section,dict) and flow.usable_result(section)
        and section.get('sha256') and (section.get('verification') or {}).get('image_sha256')==section['sha256']
        and (section.get('verification') or {}).get('protocol') in {'image-fidelity-v2',VERIFICATION_PROTOCOL}
        and (section.get('verification') or {}).get('decision')=='eligible'
        and all((section['verification'].get('checks') or {}).get(key) is True for key in VERIFICATION_KEYS)
        for section in saved.get('sections') or [])
    return (historical and stored.get('protocol')==VERIFICATION_PROTOCOL
            and stored.get('decision')=='eligible'
            and all((stored.get('checks') or {}).get(key) is True for key in VERIFICATION_KEYS))


def for_material(pid):
    saved = current(pid)
    if not saved:
        return None
    if not manifest_ready(saved):
        return None
    if not intact_file(saved, pid) or any(not intact_file(section, pid) for section in saved.get('sections') or []):
        return None
    try:
        if not current_matches(saved, flow.project(pid)):
            return None
    except (HTTPException, OSError, ValueError):
        return None
    return saved


def start(pid: int, payload: StartRequest):
    flow.ensure_loaded_pipeline()
    p=flow.project(pid)
    snap=flow.snapshot(p)
    from app import design_contract
    if not design_contract.active(snap) or payload.action!='detail_single':
        raise HTTPException(409,'旧分段详情生成已停用；历史图片保留，请从设计工作台生成每规格一张完整详情图')
    saved=current(pid)
    if not saved or not any(x.get('page_key')==payload.page_key for x in saved.get('sections',[])):
        raise HTTPException(404,'当前规格的完整详情图不存在')
    return flow.start(pid,flow.StartRequest(action='single',page_key=payload.page_key,note=payload.note))


def control(pid, rid, action):
    with flow.main().db() as c:
        row = c.execute('SELECT mode,parent_run_id FROM design_simple_runs WHERE id=? AND project_id=?', (rid, pid)).fetchone()
        if not row or row[0] not in MODES:
            raise HTTPException(404, '详情页任务不存在')
    getattr(flow, action)(pid, row['parent_run_id'] or rid)
    return status(pid)
















def section_verification(sections):
    """Summarize current factual and quality checks without upgrading candidates."""
    if sections and all(flow.quality.generated(section) for section in sections):
        complete = all(flow.usable_result(section) for section in sections)
        return {'decision':'generated' if complete else 'incomplete', 'protocol':'generated-assets-v1',
                'section_count':len(sections), 'reason':'详情分段已生成，可查看和使用'}
    keys = VERIFICATION_KEYS
    reviews = [section.get('verification') if isinstance(section, dict) and
               isinstance(section.get('verification'), dict) else {} for section in sections]
    evidence = [review.get('checks') if isinstance(review.get('checks'), dict) else {} for review in reviews]
    checks = {key: False if any(review.get(key) is False for review in evidence)
              else True if reviews and all(review.get(key) is True for review in evidence)
              else None for key in keys}
    rejected = any(review.get('decision') == 'reject' for review in reviews) or any(value is False for value in checks.values())
    eligible = bool(reviews) and all(value is True for value in checks.values()) and all(
        isinstance(section, dict) and flow.usable_result(section)
        and review.get('protocol')==VERIFICATION_PROTOCOL and review.get('decision')=='eligible'
        and bool(section.get('sha256')) and review.get('image_sha256')==section['sha256']
        for section,review in zip(sections,reviews))
    decision = 'reject' if rejected else 'eligible' if eligible else 'uncertain'
    reasons = {'eligible': '所有分段已取得当前事实、画面职责、道具归属及场景、布局、文字可读性检查结果',
               'reject': '存在未通过事实或画面质量检查的分段，长图保留为候选',
               'uncertain': '部分分段缺少当前事实或画面质量确认，长图保留为候选'}
    return {'decision': decision, 'protocol': VERIFICATION_PROTOCOL, 'checks': checks,
            'section_count': len(sections), 'reason': reasons[decision],
            'limitation': '汇总分段视觉检查，不声明商品像素不变，也不能保证百分之百保真'}







def write_zip(z, manifest, prefix='详情页'):
    if not manifest:
        return
    pid = manifest['project_id']
    if manifest.get('sku_documents'):
        for document in manifest['sku_documents']:
            path=flow.local_path(document['url'],pid=pid)
            z.write(path,f"{prefix}/SKU{document['sku_id']}/完整详情图{path.suffix}")
        z.writestr(prefix+'/详情页资料.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        return
    z.write(flow.local_path(manifest['url'], pid=pid), prefix + '/完整详情页.jpg')
    for i, section in enumerate(manifest['sections'], 1):
        path = flow.local_path(section['url'], pid=pid)
        z.write(path, f'{prefix}/分段/{i:02d}{path.suffix}')
    z.writestr(prefix + '/详情页资料.json', json.dumps(manifest, ensure_ascii=False, indent=2))


def export(pid: int):
    import zipfile
    from fastapi.responses import StreamingResponse
    flow.project(pid)
    manifest = current(pid)
    if not manifest:
        raise HTTPException(409, '尚未生成详情页')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        write_zip(z, manifest)
    buf.seek(0)
    return StreamingResponse(buf, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="detail-{pid}.zip"'})


def install_routes(app):
    app.get('/api/design-projects/{pid}/detail-page')(status)
    app.post('/api/design-projects/{pid}/detail-page', status_code=202)(start)
    app.get('/api/design-projects/{pid}/detail-page/export')(export)

    @app.post('/api/design-projects/{pid}/detail-page/{rid}/pause')
    def pause(pid: int, rid: int):
        return control(pid, rid, 'pause')

    @app.post('/api/design-projects/{pid}/detail-page/{rid}/resume')
    def resume(pid: int, rid: int):
        return control(pid, rid, 'resume')
