"""Scoped physical design cleanup, retaining source facts and request receipts."""
import hashlib
import json
import stat
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from app import image_receipts, media_preview, simple_design_flow as flow
from app.commerce import product_archive, store as s

OPEN = {'queued', 'running', 'retry_wait', 'unknown'}
PRIVATE = {'file_manifest', 'run_ids', 'state_sha256'}


class CleanupRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    expected_run_id: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100, pattern='^[A-Za-z0-9_-]+$')
    scope_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    confirmed_delete: Literal[True]


def _hash(value):
    return hashlib.sha256(s.dump(value).encode()).hexdigest()


def _public(record):
    return {k: v for k, v in record.items() if k not in PRIVATE}


def _checked_path(raw):
    """Check lexical ancestors before resolving; Windows junctions are links too."""
    data = s.main().DATA_ROOT.resolve()
    path = Path(raw).absolute()
    if not path.is_relative_to(data) or path == data:
        raise HTTPException(409, '清理路径不在当前业务数据范围内')
    for part in (path, *path.parents):
        if part == data:
            break
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise HTTPException(409, '清理目标包含链接或目录联接，已停止')
    if path.resolve() != path:
        raise HTTPException(409, '清理路径发生变化，已停止')
    return path


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _references(c, pid, files):
    selected = {str(_checked_path(s.main().DATA_ROOT / item['path'])) for item in files}
    for kind in ('product', 'sku'):
        for row in s.all_rows(c, kind):
            for url in row.get('source_images') or []:
                try:
                    source = flow.local_path(url, source=True).resolve()
                except HTTPException:
                    continue
                if str(source) in selected:
                    raise HTTPException(409, '待清理图片仍是商品原图，已停止')
    prefix = f'/design-outputs/{pid}/'
    for project in c.execute('SELECT id,source_images,output_images FROM design_projects'):
        for field in ('source_images', 'output_images'):
            if project['id'] == pid and field == 'output_images':
                continue
            if any(prefix in url for url in _strings(json.loads(project[field] or '[]'))):
                raise HTTPException(409, '图片仍被其他项目或原图引用，已停止')
    for listing in s.all_rows(c, 'listing'):
        if any(prefix in text for text in _strings(listing.get('draft') or {})):
            raise HTTPException(409, '当前刊登草稿仍引用待清理图片，请先处理引用')


def _listing_guard(c, pid):
    product = product_archive.for_project(c, pid)
    listings = [row for row in s.all_rows(c, 'listing') if row.get('product_id') == (product or {}).get('id')]
    if listings and (s.by_key(c, 'settings', 'global') or {}).get('listing_automation_paused') is not True:
        raise HTTPException(409, '请先暂停自动刊登再清理设计')
    for row in listings:
        draft = row.get('draft') or {}
        if (row.get('status') != 'not_submitted' or row.get('published_version') is not None
                or row.get('published_skus') or row.get('sku_results') or row.get('paused') is not True):
            raise HTTPException(409, '仅允许清理已暂停、未上架且无提交结果的商品设计')
        if (draft.get('images') or draft.get('detail_page') or any(v.get('images') for v in draft.get('skus', []))
                or (draft.get('source') or {}).get('design_run_id')):
            raise HTTPException(409, '当前刊登草稿仍有设计图片引用，请先处理引用')
    if c.execute("SELECT 1 FROM commerce_jobs WHERE status IN ('queued','running','retry_wait','unknown') OR lease_until>? LIMIT 1", (time.time(),)).fetchone():
        raise HTTPException(409, '仍有活动或结果未知的业务请求，请等待请求结果后再清理')
    return listings


def _files(pid):
    m = s.main()
    paths = set()
    for root in (m.DESIGN_OUTPUT_ROOT, m.PRODUCTION_MATERIAL_ROOT):
        directory = _checked_path(root / str(pid))
        for path in directory.rglob('*') if directory.exists() else []:
            path = _checked_path(path)
            if path.is_file():
                paths.add(path)
    # Derive only this product's cached preview keys; never delete the shared cache.
    for source in list(paths):
        if source.suffix.lower() in media_preview.IMAGE_SUFFIXES:
            for width in media_preview.WIDTHS:
                for height in (None, *media_preview.WIDTHS):
                    path = _checked_path(m.DATA_ROOT / 'image_previews' / (media_preview._key(source, width, height) + '.webp'))
                    if path.is_file():
                        paths.add(path)
    return [{'path': path.relative_to(m.DATA_ROOT.resolve()).as_posix(), 'bytes': path.stat().st_size,
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(paths)]


def _returned_failure(task):
    """Only retire a stored complete rejection; never retry or clear its unknown flag."""
    return image_receipts.retirable_failure(s.main(), flow.obj(task))


def _plan(c, pid):
    project = flow.project(pid, c)
    listings = _listing_guard(c, pid)
    runs = [dict(r) for r in c.execute("SELECT * FROM design_simple_runs WHERE project_id=? AND status!='archived' ORDER BY id", (pid,))]
    if not runs:
        raise HTTPException(409, '当前没有需要清除的设计')
    if any(r['status'] in OPEN for r in runs):
        raise HTTPException(409, '设计仍在运行，请先暂停并等待请求返回')
    ids = [r['id'] for r in runs]
    marks = ','.join('?' for _ in ids)
    tasks = [dict(t) for t in c.execute(f'SELECT * FROM design_simple_tasks WHERE run_id IN ({marks}) ORDER BY id', ids)]
    if any(t['status'] == 'running' or t['lease'] or t['lease_until'] > time.time()
           or (json.loads(t['remote']).get('unknown') and not _returned_failure(t)) for t in tasks):
        raise HTTPException(409, '仍有执行中或结果未知的设计请求，已停止清理')
    files = _files(pid)
    _references(c, pid, files)
    state = {'project': project, 'runs': runs, 'tasks': tasks, 'listings': listings,
             'records': [dict(r) for r in c.execute('SELECT * FROM commerce_records ORDER BY id')]}
    plan = {'project_id': pid, 'expected_run_id': max(ids), 'run_ids': ids, 'run_count': len(ids),
            'task_count': len(tasks), 'listing_count': len(listings), 'file_manifest': files,
            'files': len(files), 'bytes': sum(p['bytes'] for p in files), 'state_sha256': _hash(state)}
    return plan | {'scope_sha256': _hash(plan)}


def preview(pid: int):
    with s.main().db() as c:
        record = next((r for r in s.all_rows(c, 'design_cleanup') if r['project_id'] == pid and r['status'] != 'completed'), None)
        return _public(record or (_plan(c, pid) | {'request_id': uuid.uuid4().hex}))


def _remove_files(record):
    for item in record['file_manifest']:
        path = _checked_path(s.main().DATA_ROOT / item['path'])
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size != item['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise HTTPException(409, '待清理文件已变化，已保留该文件并停止')
        path.unlink()
    # Empty owned project directories can be removed; the shared preview root stays.
    for root in (s.main().DESIGN_OUTPUT_ROOT, s.main().PRODUCTION_MATERIAL_ROOT):
        directory = _checked_path(root / str(record['project_id']))
        if directory.exists():
            for path in sorted(directory.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                path = _checked_path(path)
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            if not any(directory.iterdir()):
                directory.rmdir()


def cleanup(pid, payload, actor):
    # Let in-progress preview writers finish, then prevent a late cache write
    # from recreating a deleted product preview after physical cleanup.
    with ExitStack() as locks:
        for lock in media_preview._LOCKS:
            locks.enter_context(lock)
        return _cleanup_locked(pid, payload, actor)


def _cleanup_locked(pid, payload, actor):
    with s.transaction() as c:
        key = f'{pid}:{payload.request_id}'
        record = s.by_key(c, 'design_cleanup', key)
        if record:
            if record.get('scope_sha256') != payload.scope_sha256 or record['expected_run_id'] != payload.expected_run_id:
                raise HTTPException(409, '清理请求与原范围不一致')
            if record['status'] == 'completed':
                return _public(record)
        else:
            if pending(c, pid):
                raise HTTPException(409, '已有未完成清理，请重试原清理请求')
            plan = _plan(c, pid)
            if plan['expected_run_id'] != payload.expected_run_id or plan['scope_sha256'] != payload.scope_sha256:
                raise HTTPException(409, '商品资料、设计或文件已变化，请重新查看清理范围')
            record = s.put(c, 'design_cleanup', key, plan | {'status': 'deleting', 'actor': actor, 'request_id': payload.request_id})
            c.execute("UPDATE design_projects SET output_images='[]',simple_current_run=NULL,detail_current_run=NULL,simple_recognition='{}',simple_input_hash='',status='待设计',updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
            c.execute("UPDATE design_simple_history SET images='[]' WHERE project_id=?", (pid,))
            for rid in plan['run_ids']:
                # Keep IDs, prompts and encrypted request receipts: no ID reuse and no retry of old requests.
                c.execute("UPDATE design_simple_runs SET status='archived',updated_at=CURRENT_TIMESTAMP WHERE id=?", (rid,))
                c.execute("UPDATE design_simple_tasks SET status='archived',lease='',lease_until=0,next_retry_at=0,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (rid,))
            for workflow in s.all_rows(c, 'listing_workflow'):
                if workflow.get('project_id') == pid and workflow.get('status') in {'running', 'waiting', 'needs_action'}:
                    s.save(c, 'listing_workflow', workflow | {'status': 'cancelled', 'stage': 'cancelled', 'message': '旧设计已清理，请从原图重新开始'})
    error = None
    # Writer lock also prevents new source/listing references while unlinking the fixed manifest.
    with s.transaction() as c:
        record = s.by_key(c, 'design_cleanup', key)
        if record['status'] == 'completed':
            return _public(record)
        try:
            _listing_guard(c, pid)
            _references(c, pid, record['file_manifest'])
            _remove_files(record)
        except (OSError, HTTPException) as exc:
            error = exc
            result = s.save(c, 'design_cleanup', record | {'status': 'needs_retry', 'error': str(getattr(exc, 'detail', exc))})
        else:
            result = s.save(c, 'design_cleanup', record | {'status': 'completed', 'error': '', 'finished_at': time.time()})
            s.audit(c, 'design_permanently_cleaned', pid, s.dump({k: result[k] for k in ('run_count', 'task_count', 'files', 'bytes', 'actor')}))
    if error:
        raise HTTPException(409, '部分文件尚未删除，请重试原清理；已变化的文件不会被删除，也不会重新生成图片') from error
    return _public(result)


def pending(c, pid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='commerce_records'").fetchone():
        return False
    return bool(c.execute("SELECT 1 FROM commerce_records WHERE kind='design_cleanup' AND json_extract(data,'$.project_id')=? AND json_extract(data,'$.status')!='completed' LIMIT 1", (pid,)).fetchone())


def install_routes(app):
    def run(pid: int, payload: CleanupRequest, request: Request):
        if getattr(request.state, 'commerce_role', None) != 'admin':
            raise HTTPException(403, '仅管理员可清除设计')
        return cleanup(pid, payload, getattr(request.state, 'commerce_username', 'admin'))
    app.get('/api/design-projects/{pid}/cleanup')(preview)
    app.post('/api/design-projects/{pid}/cleanup')(run)
