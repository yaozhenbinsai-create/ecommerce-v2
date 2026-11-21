"""Delete completed program image conversations only after retaining business evidence."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)
WORKER = None
LOCK = threading.Lock()


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def compact_deleted(directory,state):
    """Keep a small deletion receipt, not a second base64 conversation forever."""
    if state.get('status')!='deleted' or state.get('trace_copy_removed'):return state
    ident=str(state.get('thread_id',''))
    if not re.fullmatch('[A-Za-z0-9_-]{8,80}',ident):return state
    retained=directory/(ident+'.jsonl')
    try:
        if retained.is_symlink() or retained.resolve().parent!=directory.resolve():
            raise ValueError('unexpected retained trace path')
        if retained.is_file():
            if _hash(retained)!=state.get('evidence_sha256'):raise ValueError('retained trace was changed')
            retained.unlink()
        state.pop('evidence_path',None);state.pop('original_trace_path',None)
        state.update(trace_copy_removed=True,evidence_retention='compact_receipt')
        state.pop('trace_cleanup_error',None)
    except (OSError,ValueError) as exc:
        state['trace_cleanup_error']=type(exc).__name__
    _write(directory/(ident+'.json'),state)
    return state


def compact_finished(directory):
    """Also remove completed program trace copies orphaned by an authorized reset."""
    removed=0
    if not directory.is_dir():return removed
    for record in directory.glob('*.json'):
        try:state=json.loads(record.read_text('utf-8'))
        except (OSError,ValueError):continue
        if (state.get('status')!='deleted' or state.get('thread_id')!=record.stem
                or not isinstance(state.get('task_id'),int)
                or not re.fullmatch('[a-f0-9]{64}',str(state.get('evidence_sha256','')))
                or not re.fullmatch('[a-f0-9]{64}',str(state.get('image_sha256','')))):continue
        trace=directory/(record.stem+'.jsonl')
        before=trace.stat().st_size if trace.is_file() else 0
        compact_deleted(directory,state)
        if not trace.exists():removed+=before
    return removed


def _has_children(bridge, ident):
    kinds = ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
             'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']
    for archived in (False, True):
        cursor = None
        seen = set()
        for _ in range(50):
            params = {'archived': archived, 'limit': 100, 'sourceKinds': kinds,
                      'modelProviders': [], 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            page = bridge.request('thread/list', params, timeout=15)
            for item in page['data']:
                source = item.get('source') or {}
                spawn = source.get('subAgent', {}).get('thread_spawn', {}) if isinstance(source, dict) and isinstance(source.get('subAgent'), dict) else {}
                if ident in (item.get('parentThreadId'), item.get('forkedFromId'), spawn.get('parent_thread_id')):
                    return True
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise ValueError('conversation inventory did not finish')
            seen.add(cursor)
        else:
            raise ValueError('conversation inventory exceeds verification limit')
    return False


def cleanup_one(bridge, task, directory, output_path):
    """No database changes and no model invocation. An RPC failure remains retryable."""
    remote = task['remote'].get('codex') or {}
    ident = remote.get('thread_id', '')
    if (task['status'] != 'completed' or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', ident)
            or not remote.get('turn_id') or not task['result'].get('sha256')):
        return {'status': 'ineligible'}
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / (ident + '.json')
    state = json.loads(record_path.read_text('utf-8')) if record_path.is_file() else {
        'thread_id': ident, 'task_id': task['id'], 'attempts': 0, 'status': 'pending'}
    if state['status']=='deleted':return compact_deleted(directory,state)
    if state.get('next_at', 0) > time.time() or state['attempts'] >= 6:
        return state
    try:
        if not output_path.is_file() or _hash(output_path) != task['result']['sha256']:
            raise ValueError('saved image is missing or changed')
        thread = bridge.request('thread/read', {'threadId': ident, 'includeTurns': True}, timeout=30)['thread']
        turns = thread.get('turns') or []
        if (thread.get('id') != ident or thread.get('isPinned') or thread.get('ephemeral')
                or (thread.get('status') or {}).get('type') == 'active'
                or bridge._local_path(thread.get('cwd') or '') != bridge.cwd
                or len(turns) != 1 or turns[0].get('id') != remote['turn_id']
                or turns[0].get('status') != 'completed'):
            raise ValueError('conversation ownership or completion no longer matches')
        trace = bridge._validated_trace_path(thread.get('path'), ident)
        if trace is None or not trace.is_file():
            raise ValueError('original conversation evidence is unavailable')
        with trace.open('r', encoding='utf-8') as source:
            first = json.loads(source.readline(1024 * 1024))
        meta = first.get('payload') or {}
        if (first.get('type') != 'session_meta' or meta.get('id') != ident
                or meta.get('originator') != 'weaving-console'
                or bridge._local_path(meta.get('cwd') or '') != bridge.cwd):
            raise ValueError('conversation was not created by this program')
        # thread/delete also deletes descendants. Never apply that cascading
        # operation to a conversation somebody has subsequently branched.
        if _has_children(bridge, ident):
            raise ValueError('conversation has descendants; deletion is deferred')
        retained = directory / (ident + '.jsonl')
        original_hash = _hash(trace)
        if not retained.exists():
            with trace.open('rb') as source, retained.open('xb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        if _hash(retained) != original_hash:
            raise ValueError('retained conversation evidence does not match')
        state.update(status='delete_requested', evidence_path=str(retained),
                     evidence_sha256=original_hash, image_sha256=task['result']['sha256'],
                     original_trace_path=str(trace), attempts=state['attempts'] + 1)
        _write(record_path, state)
        bridge.request('thread/delete', {'threadId': ident}, timeout=30)
        state.update(status='deleted', deleted_at=datetime.now(timezone.utc).isoformat(),
                     evidence_preserved=True, last_error=None, next_at=0)
        _write(record_path, state)
        try:return compact_deleted(directory,state)
        except OSError:
            LOG.warning('Codex deleted; local trace compaction will retry for %s',ident)
            return state
    except Exception as exc:
        # Preserve the actual original failure in private logs; public state
        # does not copy provider bodies or credentials.
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        LOG.warning('Codex task cleanup pending for %s: %s', ident, reason)
        if state.get('status') != 'delete_requested':
            state['attempts'] += 1
        state.update(status='pending', last_error=reason,
                     next_at=time.time() + min(3600, 60 * 2 ** state['attempts']))
        _write(record_path, state)
        return state


def tick(main):
    from app.codex_bridge import CodexAppServer
    from app import simple_design_flow as flow
    directory = main.DATA_ROOT / 'codex_task_cleanup'
    directory.mkdir(parents=True, exist_ok=True)
    compact_finished(directory)
    policy_path = directory / 'policy.json'
    if not policy_path.exists():
        _write(policy_path, {'effective_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                            'scope': 'program image calls completed after activation',
                            'owner_instruction': 'delete completed program Codex task after preserving result'})
    policy = json.loads(policy_path.read_text('utf-8'))
    with main.db() as c:
        rows = c.execute("""SELECT t.*,r.project_id FROM design_simple_tasks t
            JOIN design_simple_runs r ON r.id=t.run_id
            WHERE t.status='completed' AND t.updated_at>=?
            AND json_extract(t.remote,'$.codex.thread_id') IS NOT NULL
            ORDER BY t.id DESC""", (policy['effective_at'],)).fetchall()
    manager = CodexAppServer(main.ROOT)
    count = 0
    try:
        for row in rows:
            task = flow.obj(row)
            ident = str(task['remote']['codex']['thread_id'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', ident):
                continue
            record = directory / (ident + '.json')
            if record.is_file():
                state = json.loads(record.read_text('utf-8'))
                if state.get('status')=='deleted':
                    compact_deleted(directory,state)
                    continue
                if state.get('next_at', 0) > time.time() or state.get('attempts', 0) >= 6:
                    continue
            if not flow.usable_result(task['result']):
                continue
            output = flow.local_path(task['result']['url'], pid=task['project_id'])
            cleanup_one(manager, task, directory, output)
            count += 1
            if count >= 4:
                break
    finally:
        manager.close()


def start(main):
    global WORKER
    with LOCK:
        if WORKER and WORKER.is_alive():
            return
        def loop():
            while True:
                try:
                    tick(main)
                except Exception:
                    LOG.exception('Codex cleanup maintenance failed; generation is unaffected')
                time.sleep(60)
        WORKER = threading.Thread(target=loop, name='codex-task-cleanup', daemon=True)
        WORKER.start()
