"""Remove obsolete working drafts into a verified, read-only recovery archive."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from app.security_secrets import _backend
from . import store as s

KINDS = frozenset({'material_version', 'listing_checks'})
MAGIC = b'WEAVING-LISTING-HISTORY-V1\n'
PROTOCOL = 'listing-working-history-cleanup-v1'


def _hash(value):
    return hashlib.sha256(s.dump(value).encode('utf-8')).hexdigest()


def _directory():
    return Path(s.main().DATA_ROOT) / 'listing_history_archive'


def _context(name):
    return b'weaving-listing-history-v1/' + name.encode('ascii')


def _indexes(c):
    return [json.loads(row[0]) for row in c.execute(
        "SELECT data FROM commerce_records WHERE kind='listing_history_archive' ORDER BY id DESC")]


def summary(c, ident):
    for index in _indexes(c):
        baseline = index['baselines'].get(str(ident))
        if baseline:
            return {**baseline, 'archived_at': index['archived_at']}
    return None


@lru_cache(maxsize=2)
def _load(path_string, cipher_sha256, plain_sha256, stamp):
    path = Path(path_string)
    try:
        cipher = path.read_bytes()
        if hashlib.sha256(cipher).hexdigest() != cipher_sha256 or not cipher.startswith(MAGIC):
            raise ValueError('Archive fingerprint mismatch')
        plain = gzip.decompress(_backend(path).decrypt(cipher[len(MAGIC):], _context(path.name)))
        if hashlib.sha256(plain).hexdigest() != plain_sha256:
            raise ValueError('Archive content mismatch')
        bundle = json.loads(plain)
        if bundle.get('protocol') != PROTOCOL:
            raise ValueError('Archive protocol mismatch')
        return bundle
    except Exception:
        raise HTTPException(503, '历史资料归档无法校验，原回执保留；请核对恢复文件') from None


def _read(index):
    name = index['file']
    if Path(name).name != name or not name.endswith('.dpapi'):
        raise HTTPException(503, '历史资料归档路径无效')
    directory = _directory().resolve()
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory:
        raise HTTPException(503, '历史资料归档路径无效')
    try:
        stat = path.stat()
    except OSError:
        raise HTTPException(503, '历史资料归档不存在，原回执保留；请核对恢复文件') from None
    return _load(str(path), index['cipher_sha256'], index['plain_sha256'], (stat.st_mtime_ns, stat.st_size))


def lookup(c, kind, *, key=None, ident=None):
    """Historical readers keep their references; working lists use live rows only."""
    if kind not in KINDS:
        return None
    token = f'{kind}:{key}' if key is not None else None
    for index in _indexes(c):
        if token is not None:
            wanted = index['keys'].get(token)
        else:
            wanted = ident if ident in index['keys'].values() else None
        if wanted is None:
            continue
        for row in _read(index)['rows']:
            if row['id'] == wanted and row['kind'] == kind and (key is None or row['natural_key'] == str(key)):
                return {**s.decode(row), '_history_archived': True}
        raise HTTPException(503, '历史资料归档索引不一致，原记录未覆盖')
    return None


def _collect(c, listing_ids):
    from .listing_identity import target_scope

    if (s.by_key(c, 'settings', 'global') or {}).get('listing_automation_paused') is not True:
        raise HTTPException(409, '请先暂停自动刊登再清理旧草稿')
    if c.execute("SELECT 1 FROM commerce_jobs WHERE status IN ('queued','running','retry_wait','unknown') LIMIT 1").fetchone():
        raise HTTPException(409, '仍有活动或结果未知的任务，不能清理旧草稿')
    ids = sorted(set(listing_ids))
    if not ids or ids != sorted(listing_ids) or any(type(ident) is not int or ident <= 0 for ident in ids):
        raise HTTPException(422, '请明确选择不重复的商品组')
    baselines, scope, rows = {}, None, []
    for ident in ids:
        raw = c.execute("SELECT * FROM commerce_records WHERE kind='listing' AND id=?", (ident,)).fetchone()
        listing = s.decode(raw)
        if not listing:
            raise HTTPException(404, '所选商品组不存在')
        if listing.get('status') != 'not_submitted' or listing.get('published_version') is not None or listing.get('published_skus'):
            raise HTTPException(409, '只清理未上架商品的旧草稿，已发布版本不能清理')
        selected_scope = target_scope(s.get(c, 'target', listing['target_id']))
        if scope is not None and selected_scope != scope:
            raise HTTPException(409, '请选择同一店铺和站点的商品组')
        scope = selected_scope
        version = listing['draft_version']
        current = c.execute("SELECT * FROM commerce_records WHERE kind='material_version' AND natural_key=?", (f'{ident}:{version}',)).fetchone()
        if not current or s.decode(current).get('draft') != listing['draft']:
            raise HTTPException(409, '当前草稿与版本快照不一致，不能清理')
        baseline = {'version': version, 'listing_revision': listing['revision'], 'listing_sha256': _hash(dict(raw)),
                    'materials': 0, 'checks': 0}
        for row in c.execute("SELECT * FROM commerce_records WHERE kind IN ('material_version','listing_checks') "
                             "AND json_extract(data,'$.listing_id')=? ORDER BY id", (ident,)):
            data = json.loads(row['data'])
            old = data['version']
            if type(old) is not int or old < 1 or old > version or row['natural_key'] != f'{ident}:{old}':
                raise HTTPException(409, '版本顺序或归属异常，不能清理')
            if old < version:
                rows.append(dict(row))
                baseline['materials' if row['kind'] == 'material_version' else 'checks'] += 1
        baselines[str(ident)] = baseline
    return {'target_scope': scope, 'baselines': baselines, 'rows': sorted(rows, key=lambda row: row['id'])}


def _review(collected):
    return {key: value for key, value in collected.items() if key != 'rows'} | {
        'rows': [{'id': row['id'], 'kind': row['kind'], 'natural_key': row['natural_key'], 'sha256': _hash(row)}
                 for row in collected['rows']]}


def plan(listing_ids):
    with s.main().db() as c:
        return {'protocol': PROTOCOL, **_review(_collect(c, listing_ids))}


def apply(reviewed):
    """Archive durably before one atomic, exact-row cleanup; never call Amazon."""
    if reviewed.get('protocol') != PROTOCOL:
        raise HTTPException(422, '清理计划格式无效')
    with s.transaction() as c:
        current = _collect(c, sorted(int(ident) for ident in reviewed['baselines']))
        if reviewed != {'protocol': PROTOCOL, **_review(current)}:
            raise HTTPException(409, '商品或旧版本已变化，请重新生成清理计划')
        if not current['rows']:
            return {'status': 'nothing_to_clean', 'removed_materials': 0, 'removed_checks': 0}
        archive_id = uuid.uuid4().hex
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (archive_id + '.dpapi')
        archived_at = datetime.now(timezone.utc).isoformat()
        bundle = {'protocol': PROTOCOL, 'archive_id': archive_id, 'archived_at': archived_at, **current}
        plain = s.dump(bundle).encode('utf-8')
        cipher = MAGIC + _backend(path).encrypt(gzip.compress(plain, mtime=0), _context(path.name))
        with path.open('xb') as output:
            output.write(cipher)
            output.flush()
            os.fsync(output.fileno())
        index = {'file': path.name, 'archived_at': archived_at, 'protocol': PROTOCOL,
                 'plain_sha256': hashlib.sha256(plain).hexdigest(), 'cipher_sha256': hashlib.sha256(cipher).hexdigest(),
                 'baselines': current['baselines'], 'target_scope': current['target_scope'],
                 'keys': {f"{row['kind']}:{row['natural_key']}": row['id'] for row in current['rows']},
                 'plan_sha256': _hash(reviewed)}
        if _read(index) != bundle:
            raise HTTPException(503, '归档回读不一致，旧版本未清理')
        archive = s.put(c, 'listing_history_archive', archive_id, index)
        for row in current['rows']:
            removed = c.execute('DELETE FROM commerce_records WHERE id=? AND kind=? AND natural_key=? AND data=? AND revision=?',
                                (row['id'], row['kind'], row['natural_key'], row['data'], row['revision'])).rowcount
            if removed != 1:
                raise HTTPException(409, '清理时版本已变化，事务已撤销')
        counts = {kind: sum(row['kind'] == kind for row in current['rows']) for kind in KINDS}
        result = {'status': 'cleaned', 'archive_id': archive['id'], 'archive_file': str(path),
                  'removed_materials': counts['material_version'], 'removed_checks': counts['listing_checks'],
                  'groups': len(current['baselines']), 'baselines': current['baselines'], 'amazon_writes': 0}
        s.audit(c, 'listing_working_history_cleaned', archive['id'], s.dump({key: result[key] for key in
                ('groups', 'removed_materials', 'removed_checks', 'amazon_writes')}))
        return result
